"""SiliconFlow / OpenAI 兼容 API 客户端：重试退避、并发、磁盘缓存、用量统计、密钥脱敏。

缓存按 (model, temperature, thinking, messages 内容哈希) 落盘。
翻译和总结都是确定性输入 → 重跑同一份数据不花一分钱，也不重复等网络。
"""
from __future__ import annotations

import json
import os
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import requests

from .common import Log, hash_text, scrub

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}
# 只影响传输、不影响生成结果的选项，不参与缓存 key
_NON_SEMANTIC_OPTS = {"timeout"}


class LLMError(RuntimeError):
    pass


@dataclass
class Usage:
    calls: int = 0
    cached: int = 0
    failed: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    seconds: float = 0.0

    def merge(self, other: "Usage") -> None:
        self.calls += other.calls
        self.cached += other.cached
        self.failed += other.failed
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.seconds += other.seconds

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def as_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "cached": self.cached,
            "failed": self.failed,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "seconds": round(self.seconds, 1),
        }


@dataclass
class Result:
    text: str
    usage: Usage = field(default_factory=Usage)
    from_cache: bool = False


class ResponseCache:
    """文件级缓存：每个 key 一个 .json，写入原子。"""

    def __init__(self, root: Path, enabled: bool = True) -> None:
        self.root = root
        self.enabled = enabled
        self._lock = threading.Lock()
        if enabled:
            root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        # 两级目录，避免单目录上万文件
        return self.root / key[:2] / f"{key}.json"

    @staticmethod
    def make_key(model: str, messages: Sequence[dict[str, Any]], **opts: Any) -> str:
        # timeout 只是传输参数，不影响语义；放进 key 会让调超时白白作废缓存
        keep = {k: v for k, v in sorted(opts.items()) if k not in _NON_SEMANTIC_OPTS}
        payload = {"model": model, "messages": messages, "opts": keep}
        return hash_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))

    def get(self, key: str) -> str | None:
        if not self.enabled:
            return None
        p = self._path(key)
        try:
            with self._lock:
                data = json.loads(p.read_text(encoding="utf-8"))
            return data.get("text")
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None

    def put(self, key: str, text: str, meta: dict[str, Any] | None = None) -> None:
        if not self.enabled:
            return
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        try:
            tmp.write_text(
                json.dumps({"text": text, "meta": meta or {}}, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(tmp, p)
        except OSError:
            pass


class ChatClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        timeout: int = 300,
        max_retries: int = 6,
        cache: ResponseCache | None = None,
        log: Log | None = None,
        rate_limit_per_min: int = 0,
    ) -> None:
        if not api_key:
            raise LLMError("missing-api-key")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self.cache = cache or ResponseCache(Path("/tmp/llm-cache"), enabled=False)
        self.log = log or Log("llm")
        self.session = requests.Session()
        self.usage = Usage()
        self._usage_lock = threading.Lock()
        # 简易令牌桶，避免并发把 API 打到 429
        self._min_interval = 60.0 / rate_limit_per_min if rate_limit_per_min > 0 else 0.0
        self._last_call = 0.0
        self._rate_lock = threading.Lock()

    # ------------------------------------------------------------------

    def _throttle(self) -> None:
        if self._min_interval <= 0:
            return
        with self._rate_lock:
            now = time.monotonic()
            wait = self._last_call + self._min_interval - now
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()

    def _post(self, messages: Sequence[dict[str, Any]], **opts: Any) -> tuple[str, dict[str, Any]]:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": list(messages),
            "temperature": opts.get("temperature", 0.3),
        }
        if opts.get("max_tokens"):
            body["max_tokens"] = opts["max_tokens"]
        if "thinking" in opts:
            body["enable_thinking"] = bool(opts["thinking"])
        if opts.get("response_format"):
            body["response_format"] = opts["response_format"]

        self._throttle()
        try:
            r = self.session.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json=body,
                timeout=int(opts.get("timeout") or self.timeout),
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            # 读超时/连接断开是可重试的瞬时故障，绝不能让它冒出去废掉整个视频
            raise RetryableLLMError(f"network: {type(exc).__name__}: {scrub(str(exc))[:200]}") from exc
        except requests.RequestException as exc:
            raise LLMError(f"request-failed: {type(exc).__name__}: {scrub(str(exc))[:200]}") from exc
        if r.status_code >= 400:
            raise _http_error(r)
        data = r.json()
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"malformed-response: {scrub(json.dumps(data)[:300])}") from exc
        return text, data.get("usage") or {}

    def complete(self, messages: Sequence[dict[str, Any]], **opts: Any) -> Result:
        key = self.cache.make_key(self.model, messages, **opts)
        cached = self.cache.get(key)
        if cached is not None:
            with self._usage_lock:
                self.usage.cached += 1
            return Result(text=cached, from_cache=True, usage=Usage(cached=1))

        last: Exception | None = None
        started = time.monotonic()
        for attempt in range(1, self.max_retries + 1):
            try:
                text, usage = self._post(messages, **opts)
                elapsed = time.monotonic() - started
                u = Usage(
                    calls=1,
                    prompt_tokens=int(usage.get("prompt_tokens") or 0),
                    completion_tokens=int(usage.get("completion_tokens") or 0),
                    seconds=elapsed,
                )
                with self._usage_lock:
                    self.usage.merge(u)
                self.cache.put(
                    key,
                    text,
                    {"model": self.model, "tokens": u.total_tokens, "at": round(time.time())},
                )
                return Result(text=text, usage=u)
            except RetryableLLMError as exc:
                last = exc
                backoff = min(60.0, (2 ** attempt) + random.uniform(0, 1.5))
                self.log.warn(f"retry {attempt}/{self.max_retries} in {backoff:.0f}s: {exc}")
                time.sleep(backoff)
            except LLMError as exc:
                with self._usage_lock:
                    self.usage.failed += 1
                raise exc
        with self._usage_lock:
            self.usage.failed += 1
        raise LLMError(f"exhausted-retries: {scrub(str(last))}")

    def complete_json(self, messages: Sequence[dict[str, Any]], **opts: Any) -> tuple[Any, Result]:
        """要求 JSON 输出并稳健解析：剥 ```json 围栏、截取首个完整括号块。"""
        res = self.complete(messages, **opts)
        return extract_json(res.text), res

    # ------------------------------------------------------------------

    def map(
        self,
        items: Iterable[Any],
        fn: Callable[[Any, "ChatClient"], Any],
        *,
        concurrency: int = 4,
        on_error: str = "raise",
        progress: Callable[[int, int, Any], None] | None = None,
    ) -> list[Any]:
        """并发执行 fn(item, client)，保持输入顺序。

        on_error='raise' 任一失败即抛；'keep' 失败位置放 None，由调用方决定补救。
        """
        items = list(items)
        out: list[Any] = [None] * len(items)
        if not items:
            return out
        total = len(items)
        done = 0
        lock = threading.Lock()
        errors: list[tuple[int, Exception]] = []
        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
            futures = {pool.submit(fn, item, self): i for i, item in enumerate(items)}
            for fut in as_completed(futures):
                i = futures[fut]
                try:
                    out[i] = fut.result()
                except Exception as exc:  # noqa: BLE001
                    errors.append((i, exc))
                    if on_error == "raise":
                        for f in futures:
                            f.cancel()
                        raise
                with lock:
                    done += 1
                    if progress:
                        progress(done, total, out[i])
        if errors and on_error == "raise":
            raise errors[0][1]
        return out


class RetryableLLMError(LLMError):
    pass


def _http_error(r: requests.Response) -> LLMError:
    detail = scrub((r.text or "")[:400])
    msg = f"HTTP {r.status_code}: {detail}"
    if r.status_code in RETRYABLE_STATUS:
        return RetryableLLMError(msg)
    return LLMError(msg)


# ---------------------------------------------------------------- JSON 解析


def extract_json(text: str) -> Any:
    """稳健解析模型返回的 JSON。

    依次尝试：直接 parse → 剥 ```json 围栏 → 括号配平扫描截取首个完整块。
    括号扫描会跳过字符串内的括号，避免正文里的 { } 干扰配平。
    """
    raw = (text or "").strip()
    if not raw:
        raise LLMError("empty-response")

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    fenced = re.findall(r"```(?:json)?\s*(.*?)```", raw, re.S | re.I)
    if fenced:
        raw = fenced[-1].strip()
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass

    starts = [p for p in (raw.find("{"), raw.find("[")) if p >= 0]
    if not starts:
        raise LLMError(f"no-json-in-response: {scrub(raw[:200])}")
    start = min(starts)
    open_ch = raw[start]
    close_ch = "}" if open_ch == "{" else "]"
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(raw)):
        ch = raw[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(raw[start : i + 1])
                except json.JSONDecodeError:
                    break
    raise LLMError(f"unbalanced-json: {scrub(raw[:200])}")


# ---------------------------------------------------------------- 工厂


def build_client(cfg: dict[str, Any], role: str, *, log: Log | None = None, cache_dir: Path | None = None) -> ChatClient:
    """role: translate | vision | synthesize。从 config.json 的 api 段取模型。"""
    api = cfg["api"]
    key = os.environ.get(api["api_key_env"], "").strip()
    if not key:
        raise LLMError(f"missing-env:{api['api_key_env']}")
    base = os.environ.get(api.get("base_url_env", ""), "").strip() or api["default_base_url"]
    model = api[f"{role}_model"]
    cache = ResponseCache(cache_dir or (cfg["data_dir_path"] / "cache" / role))
    return ChatClient(
        api_key=key,
        base_url=base,
        model=model,
        timeout=int(api.get("timeout", 300)),
        max_retries=int(api.get("max_retries", 6)),
        cache=cache,
        log=log or Log(role),
    )
