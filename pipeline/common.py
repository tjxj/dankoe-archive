"""共享工具：路径、配置、原子写、内容哈希、阶段状态机、密钥脱敏。"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{12,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]{12,}"),
]

# ---------------------------------------------------------------- 日志


class Log:
    """带时间戳、带阶段标签的轻量日志，直接写 stderr，不污染 stdout（stdout 留给结果）。"""

    def __init__(self, stage: str = "run", quiet: bool = False) -> None:
        self.stage = stage
        self.quiet = quiet

    def _emit(self, level: str, msg: str) -> None:
        if self.quiet and level == "debug":
            return
        stamp = dt.datetime.now().strftime("%H:%M:%S")
        print(f"[{stamp}] {level:5s} {self.stage}: {msg}", file=sys.stderr, flush=True)

    def info(self, msg: str) -> None:
        self._emit("INFO", msg)

    def warn(self, msg: str) -> None:
        self._emit("WARN", msg)

    def error(self, msg: str) -> None:
        self._emit("ERROR", msg)

    def debug(self, msg: str) -> None:
        self._emit("debug", msg)

    def child(self, stage: str) -> "Log":
        return Log(stage, self.quiet)


def scrub(text: str) -> str:
    """把任何看起来像密钥的串打码，避免写进日志或产物文件。"""
    out = text or ""
    for pat in SECRET_PATTERNS:
        out = pat.sub("[REDACTED]", out)
    return out


# ---------------------------------------------------------------- 配置


def load_config(path: Path | None = None) -> dict[str, Any]:
    cfg_path = path or ROOT / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    for key in ("data_dir", "site_dir"):
        value = cfg.get(key, key)
        cfg[key + "_path"] = Path(value) if Path(value).is_absolute() else ROOT / value
    video_dir = Path(cfg["video_dir"]).expanduser()
    cfg["video_dir_path"] = video_dir
    return cfg


def data_dir(cfg: dict[str, Any]) -> Path:
    return cfg["data_dir_path"]


def site_dir(cfg: dict[str, Any]) -> Path:
    return cfg["site_dir_path"]


def video_data_dir(cfg: dict[str, Any], video_id: str) -> Path:
    return data_dir(cfg) / "videos" / video_id


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------- JSON / 原子写


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _atomic_write(path: Path, text: str, suffix: str) -> Path:
    """先写临时文件再 rename，避免中断留下半截内容。

    mkstemp 出于安全默认 0600，但这些产出是要被 http server / 别人读的网站文件，
    所以显式放宽到 0644（内容里本来就不含密钥，见 scrub / _check_no_secrets）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text if text.endswith("\n") else text + "\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return path


def write_json(path: Path, payload: Any, *, indent: int = 2) -> Path:
    return _atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=indent), ".json")


def write_text(path: Path, text: str) -> Path:
    return _atomic_write(path, text, ".txt")


# ---------------------------------------------------------------- 哈希


def hash_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def hash_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(chunk):
            h.update(block)
    return h.hexdigest()[:16]


def hash_obj(obj: Any) -> str:
    return hash_text(json.dumps(obj, ensure_ascii=False, sort_keys=True))


# ---------------------------------------------------------------- 阶段状态机


class StageState:
    """记录每个视频每个阶段的输入指纹。

    指纹 = 阶段参数 + 输入内容哈希。指纹不变则跳过，变了才重跑。
    这让整条流水线幂等：新增视频自动处理，已完成的不动，改了提示词只重跑受影响阶段。
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, Any] = read_json(path, {}) or {}
        self.data.setdefault("stages", {})

    def fingerprint(self, stage: str) -> str:
        return str(self.data["stages"].get(stage, {}).get("fingerprint", ""))

    def is_current(self, stage: str, fingerprint: str) -> bool:
        entry = self.data["stages"].get(stage)
        return bool(entry) and entry.get("fingerprint") == fingerprint and entry.get("status") == "ok"

    def mark(self, stage: str, fingerprint: str, *, status: str = "ok", **extra: Any) -> None:
        self.data["stages"][stage] = {
            "fingerprint": fingerprint,
            "status": status,
            "at": dt.datetime.now().isoformat(timespec="seconds"),
            **extra,
        }
        self.save()

    def save(self) -> None:
        write_json(self.path, self.data)


# ---------------------------------------------------------------- 外部命令


class CommandError(RuntimeError):
    def __init__(self, cmd: list[str], returncode: int, output: str) -> None:
        self.cmd = cmd
        self.returncode = returncode
        self.output = scrub(output)
        super().__init__(f"command-failed[{returncode}]: {' '.join(cmd[:4])}… → {self.output[:400]}")


def run(cmd: list[str], *, timeout: int = 600, cwd: Path | None = None) -> str:
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=False, cwd=str(cwd) if cwd else None
    )
    if proc.returncode != 0:
        raise CommandError(cmd, proc.returncode, (proc.stderr or "") + (proc.stdout or ""))
    return proc.stdout


def resolve_tool(preferred: str | Path, name: str) -> Path:
    p = Path(preferred)
    if p.exists() and os.access(p, os.X_OK):
        return p
    found = shutil.which(name)
    if found:
        return Path(found)
    raise RuntimeError(f"tool-not-found: {name}")


FFMPEG_CANDIDATES = ["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"]
FFPROBE_CANDIDATES = ["/opt/homebrew/bin/ffprobe", "/usr/local/bin/ffprobe"]


def ffmpeg() -> Path:
    for c in FFMPEG_CANDIDATES:
        if Path(c).exists():
            return Path(c)
    return resolve_tool("ffmpeg", "ffmpeg")


def ffprobe() -> Path:
    for c in FFPROBE_CANDIDATES:
        if Path(c).exists():
            return Path(c)
    return resolve_tool("ffprobe", "ffprobe")


def ytdlp() -> Path:
    """优先用 /tmp 下的新版二进制（conda 里那个旧版会被 YouTube 拒绝），其次 PATH。"""
    for c in ["/tmp/yt-dlp-new", str(ROOT / "bin" / "yt-dlp")]:
        p = Path(c)
        if p.exists() and os.access(p, os.X_OK):
            return p
    found = shutil.which("yt-dlp")
    if found:
        return Path(found)
    raise RuntimeError("yt-dlp-not-found")


# ---------------------------------------------------------------- 杂项


def seconds_to_label(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def parse_iso_date(raw: str) -> dt.date | None:
    try:
        return dt.datetime.strptime(str(raw), "%Y%m%d").date()
    except (ValueError, TypeError):
        return None


def format_date(raw: str, fmt: str = "%Y-%m-%d") -> str:
    d = parse_iso_date(raw)
    return d.strftime(fmt) if d else str(raw or "")


def human_count(n: Any) -> str:
    try:
        v = float(n)
    except (TypeError, ValueError):
        return "—"
    for unit, div in (("亿", 1e8), ("万", 1e4)):
        if v >= div:
            return f"{v / div:.1f}{unit}".replace(".0", "")
    return f"{int(v)}"


def chunked(seq: list[Any], size: int) -> list[list[Any]]:
    return [seq[i : i + size] for i in range(0, len(seq), size)] if size > 0 else [list(seq)]


def safe_name(text: str, max_len: int = 80) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", str(text or "")).strip()
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return cleaned[:max_len].strip() or "untitled"
