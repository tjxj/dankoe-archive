"""英文字幕 → 中文翻译，句子级时间轴对齐。

为什么按"句子"而不是按"字幕条"翻译：
YouTube 自动字幕的 cue 常在句中切断，逐条翻译会得到破碎的中文。
srt.build_sentences 已把 cue 合并成完整句子并保留时间戳，这里按句翻译，
于是每句中文都带一个精确的起始时间 → 网页上点任意一句就能跳转到视频对应位置。

可靠性设计：
  * 行号协议：输入 `1. 原文`，要求输出 `1. 译文`，按号回填，不靠顺序猜。
  * 对齐校验：解析后检查号数是否齐、是否错位，缺号单独重试。
  * 术语表：Dan Koe 的高频概念固定译法，避免同一术语在 19 个视频里翻成 5 种。
  * 幂等缓存：llm.ResponseCache 按内容哈希落盘，重跑不重复花钱。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import srt as srtmod
from .common import Log, StageState, ensure_dir, hash_obj, write_json, write_text
from .llm import ChatClient, LLMError

PROMPT_VERSION = "v3"

# Dan Koe 高频概念固定译法。术语一致性是长视频合集翻译质量的关键。
GLOSSARY = """
one-person business → 一人企业
one-person business model → 一人企业模式
productize yourself → 把自己产品化
high-income skill(s) → 高收入技能
deep work → 深度工作
monk mode → 闭关模式
dopamine detox → 多巴胺戒断
dopamine → 多巴胺
the Matrix → 矩阵（指被设计好的默认人生脚本，保留隐喻，不要意译成"体制"）
escape the Matrix → 逃出矩阵
strategic thinking → 战略思维
5-dimensional thinking → 五维思考
leverage → 杠杆
attention → 注意力
creative work → 创造性工作
digital writing / writing online → 线上写作
build in public → 公开构建
audience → 受众（不要译成"观众"）
personal brand → 个人品牌
future-proof → 面向未来的 / 不会被淘汰的
AI-first → AI 优先
second brain → 第二大脑
mental model → 思维模型
self-discipline → 自律
reinvent yourself → 重塑自己
"""

SYSTEM_PROMPT = f"""你是专业的英中译者，为一位中文读者翻译 Dan Koe 的英文视频字幕。
他的内容关于一人企业、专注力、战略思维、自我重塑，语气是直接、口语化、带号召力的演讲体。

翻译要求：
1. 流畅自然的简体中文，像中文母语者自己写出来的，不要翻译腔。
2. 保留口语节奏和演讲的推进感。短句就译成短句，不要为了"通顺"把几句揉成一句。
3. 不要增译、不要解释、不要加"他说""我们可以看到"之类的话。只输出译文。
4. 原文是 YouTube 自动生成的字幕，会有语音识别错误（例如把 Claude Code 识别成 "clod code"、
   把 DM you 识别成 "DMU"、把 "cold hard" 识别成 "clod"）。遇到明显不通的词，按上下文判断真实含义
   并译出正确意思，不要直译错词，也不要标注存疑。
5. 专有名词（人名、产品名、公司名、Instagram / YouTube / AI 等）保留英文原文。
6. 数字、金额、百分比按中文习惯（如 $1M → 100 万美元，99.99% → 99.99%）。
7. 术语必须统一：
{GLOSSARY}

输出格式（严格遵守）：
输入是若干行，每行形如 `数字. 英文句子`。
你必须为每一行输出一行，形如 `数字. 中文译文`，数字与输入完全一致、顺序一致、一行不多一行不少。
不要输出任何解释、前言、总结或代码围栏。"""


@dataclass
class Chunk:
    sentences: list[srtmod.Sentence]

    @property
    def words(self) -> int:
        return sum(s.words for s in self.sentences)


def make_chunks(sentences: list[srtmod.Sentence], *, word_budget: int = 500, max_sentences: int = 34) -> list[Chunk]:
    """按词数预算切块，绝不在句子中间切断。"""
    chunks: list[Chunk] = []
    cur: list[srtmod.Sentence] = []
    cur_words = 0
    for s in sentences:
        if cur and (cur_words + s.words > word_budget or len(cur) >= max_sentences):
            chunks.append(Chunk(cur))
            cur, cur_words = [], 0
        cur.append(s)
        cur_words += s.words
    if cur:
        chunks.append(Chunk(cur))
    return chunks


LINE_RE = re.compile(r"^\s*(\d{1,4})\s*[.、)）:：\|]\s*(.*)$")
# 模型偶尔漏掉点号（输出 "3 第三句"）。但裸数字也可能是译文本身的开头（"3 个理由…"），
# 所以不能直接采信：两种解析都跑一遍，取覆盖行号更多的那个。严格优先，宽松只在更好时替换。
BARE_LINE_RE = re.compile(r"^\s*(\d{1,4})\s+(\S.*)$")


def _parse_impl(text: str, expected: int, *, bare_ok: bool) -> dict[int, str]:
    out: dict[int, str] = {}
    current: int | None = None
    buf: list[str] = []

    def flush() -> None:
        nonlocal buf
        if current is not None and buf:
            body = " ".join(x.strip() for x in buf if x.strip())
            body = re.sub(r"\s{2,}", " ", body).strip()
            if body:
                # 同一号出现多次时拼接（模型偶尔会把长句换行输出）
                out[current] = (out[current] + " " + body).strip() if current in out else body
        buf = []

    for line in (text or "").splitlines():
        line = line.rstrip()
        if not line.strip():
            continue
        if line.strip().startswith("```"):
            continue
        m = LINE_RE.match(line)
        if not m and bare_ok:
            m2 = BARE_LINE_RE.match(line)
            # 只接受"正好是下一个号"的裸数字，避免把译文里的数字当成行号
            if m2 and current is not None and int(m2.group(1)) == current + 1:
                m = m2
        if m:
            flush()
            current = int(m.group(1))
            buf = [m.group(2)]
        elif current is not None:
            buf.append(line)
    flush()
    return {k: v for k, v in out.items() if 1 <= k <= expected}


def parse_numbered(text: str, expected: int) -> dict[int, str]:
    """解析 `N. 译文` 行。容忍模型漏掉点号、用全角标点、或把一句拆成多行。"""
    strict = _parse_impl(text, expected, bare_ok=False)
    if len(strict) >= expected:
        return strict
    loose = _parse_impl(text, expected, bare_ok=True)
    return loose if len(loose) > len(strict) else strict


def build_user_prompt(chunk: Chunk, *, video_title: str, prev_tail: str) -> str:
    lines = [f"{i}. {s.text}" for i, s in enumerate(chunk.sentences, start=1)]
    ctx = f"视频标题：{video_title}\n" if video_title else ""
    if prev_tail:
        ctx += f"上一段结尾（仅供衔接语气，不要翻译它）：…{prev_tail[-120:]}\n"
    return (
        f"{ctx}"
        f"共 {len(lines)} 行，请逐行翻译，输出 {len(lines)} 行，行号保持不变：\n\n"
        + "\n".join(lines)
    )


def translate_chunk(
    chunk: Chunk,
    client: ChatClient,
    *,
    video_title: str,
    prev_tail: str,
    temperature: float = 0.3,
    thinking: bool = False,
    max_tokens: int = 8000,
) -> list[str]:
    """翻译一块，返回与 chunk.sentences 等长的中文列表。缺行会重试补齐。"""
    n = len(chunk.sentences)
    prompt = build_user_prompt(chunk, video_title=video_title, prev_tail=prev_tail)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    opts: dict[str, Any] = {"temperature": temperature, "thinking": thinking, "max_tokens": max_tokens}

    merged: dict[int, str] = {}
    pending = list(range(1, n + 1))
    for attempt in range(3):
        sub = Chunk([chunk.sentences[i - 1] for i in pending])
        if attempt > 0:
            # 重试用紧凑格式，并在 prompt 里明确缺了哪些行
            prompt = build_user_prompt(sub, video_title=video_title, prev_tail=prev_tail)
            messages[1]["content"] = (
                prompt + f"\n\n（注意：必须输出 {len(pending)} 行，一行都不能少。）"
            )
        res = client.complete(messages, **opts)
        got = parse_numbered(res.text, len(pending))
        for local_idx, value in got.items():
            merged[pending[local_idx - 1]] = value
        missing = [i for i in range(1, n + 1) if i not in merged]
        if not missing:
            break
        pending = missing
    # 仍然缺的行：留空并在上层记录，绝不拿相邻句子冒充（那会造成时间轴错位）
    return [merged.get(i, "") for i in range(1, n + 1)]


def translate_video(
    *,
    video: dict[str, Any],
    sentences: list[srtmod.Sentence],
    client: ChatClient,
    cfg: dict[str, Any],
    log: Log,
) -> dict[str, Any]:
    """翻译一个视频的全部句子，返回统计信息。sentences 会被就地填入 .zh。"""
    tcfg = cfg["translate"]
    chunks = make_chunks(sentences, word_budget=int(tcfg.get("chunk_words_budget", 500)))
    title = video.get("title", "")
    total_words = sum(c.words for c in chunks)
    log.info(f"{video['id']}: {len(sentences)} 句 / {len(chunks)} 块 / {total_words} 词")

    # 上一块最后一句英文作为衔接上下文，不依赖翻译结果，可以先算好
    tails = [""] + [c.sentences[-1].text for c in chunks[:-1]]

    def work(idx: int, c: ChatClient) -> list[str]:
        return translate_chunk(
            chunks[idx],
            c,
            video_title=title,
            prev_tail=tails[idx],
            temperature=float(tcfg.get("temperature", 0.3)),
            thinking=bool(tcfg.get("thinking", False)),
        )

    def progress(d: int, t: int, _v: Any) -> None:
        if d % 5 == 0 or d == t:
            log.info(f"  {video['id']} 翻译进度 {d}/{t} 块")

    # map 保证返回值与输入同序，所以 results[i] 对应 chunks[i]
    results: list[list[str]] = client.map(
        list(range(len(chunks))),
        work,
        concurrency=int(cfg["api"].get("concurrency", 4)),
        on_error="raise",
        progress=progress,
    )

    filled = 0
    empty: list[int] = []
    cursor = 0
    for chunk, zh in zip(chunks, results):
        for s, z in zip(chunk.sentences, zh or []):
            s.zh = (z or "").strip()
            cursor += 1
            if s.zh:
                filled += 1
            else:
                empty.append(cursor)
    ratio = filled / max(len(sentences), 1)
    log.info(f"  {video['id']} 完成：{filled}/{len(sentences)} 句有译文（{ratio:.1%}）" + (f"，缺 {len(empty)} 句" if empty else ""))
    return {
        "sentences": len(sentences),
        "chunks": len(chunks),
        "words": total_words,
        "translated": filled,
        "missing_indices": empty[:50],
        "coverage": round(ratio, 4),
    }


def write_outputs(
    video: dict[str, Any],
    sentences: list[srtmod.Sentence],
    paragraphs: list[srtmod.Paragraph],
    out_dir: Path,
    stats: dict[str, Any],
    log: Log,
) -> dict[str, Path]:
    ensure_dir(out_dir)
    paths = {
        "sentences": write_json(
            out_dir / "sentences.json",
            {"id": video["id"], "title": video.get("title"), "count": len(sentences),
             "paragraphs": [p.to_dict() for p in paragraphs]},
        ),
        "zh_srt": write_text(out_dir / "zh.srt", srtmod.sentences_to_srt(sentences, use_zh=True)),
        "en_srt": write_text(out_dir / "en.srt", srtmod.sentences_to_srt(sentences, use_zh=False)),
        "en_txt": write_text(out_dir / "en.txt", srtmod.sentences_to_text(sentences, paragraphs, use_zh=False)),
        "zh_txt": write_text(out_dir / "zh.txt", srtmod.sentences_to_text(sentences, paragraphs, use_zh=True)),
        "stats": write_json(out_dir / "translate-stats.json", stats),
    }
    return paths


def fingerprint(video: dict[str, Any], cfg: dict[str, Any]) -> str:
    return hash_obj(
        {
            "srt": video.get("_srt_fingerprint") or video.get("srt_file"),
            "model": cfg["api"]["translate_model"],
            "prompt": PROMPT_VERSION,
            "params": cfg["translate"],
        }
    )
