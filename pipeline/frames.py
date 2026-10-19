"""封面与关键帧。

两类图片用途不同，来源也不同：
  cover.jpg   —— YouTube 官方封面（创作者设计的标题图），下载到本地缓存，离线可用。
                 用于站点首页卡片和详情页头图。
  frames/*.jpg —— 从本地 mp4 等间隔抽取的画面，用于"图文总结"里把知识点绑定到具体画面。

两者都是幂等的：文件存在且视频指纹未变就跳过。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from .common import Log, ensure_dir, ffmpeg, run, seconds_to_label


@dataclass
class Frame:
    frame_id: str
    timestamp: float
    time_label: str
    filename: str  # 相对 frames 目录
    index: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "frame_id": self.frame_id,
            "timestamp": round(self.timestamp, 2),
            "time_label": self.time_label,
            "filename": self.filename,
            "index": self.index,
        }


# ---------------------------------------------------------------- 封面

THUMB_SIZES = ("maxresdefault", "sddefault", "hqdefault", "mqdefault")


def fetch_cover(video_id: str, dest: Path, log: Log, *, preferred_url: str = "") -> bool:
    """下载 YouTube 官方封面。maxres → sd → hq → mq 逐级回退。

    dest 已存在则直接返回 True（幂等）。
    """
    if dest.exists() and dest.stat().st_size > 2048:
        return True
    dest.parent.mkdir(parents=True, exist_ok=True)
    candidates: list[str] = []
    if preferred_url:
        candidates.append(preferred_url)
    candidates += [f"https://i.ytimg.com/vi/{video_id}/{size}.jpg" for size in THUMB_SIZES]
    candidates += [f"https://i.ytimg.com/vi_webp/{video_id}/{size}.webp" for size in ("maxresdefault", "hqdefault")]

    for url in candidates:
        try:
            r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200 or len(r.content) < 2048:
                continue
            # maxresdefault 对部分视频返回 120x90 占位图，按尺寸过滤
            tmp = dest.with_suffix(".part")
            tmp.write_bytes(r.content)
            if _image_width(tmp) < 320:
                tmp.unlink(missing_ok=True)
                continue
            tmp.replace(dest)
            log.debug(f"封面 {video_id} ← {url.split('/')[-1]} ({len(r.content)//1024}KB)")
            return True
        except Exception as exc:  # noqa: BLE001
            log.debug(f"封面候选失败 {url}: {exc}")
            continue
    log.warn(f"封面下载失败: {video_id}")
    return False


def _image_width(path: Path) -> int:
    """用 ffprobe 读图片宽度，避免引入 PIL 依赖。"""
    try:
        out = run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width", "-of", "csv=p=0", str(path)],
            timeout=60,
        )
        return int(out.strip() or 0)
    except Exception:  # noqa: BLE001
        return 0


# ---------------------------------------------------------------- 关键帧


def _pick_timestamps(duration: float, count: int) -> list[float]:
    """等间隔取点，避开首尾（片头片尾常是黑场或 logo 定格）。"""
    count = max(3, min(count, 40))
    if duration <= 0:
        duration = 600.0
    lo, hi = duration * 0.02, duration * 0.985
    span = max(hi - lo, 1.0)
    return [lo + span * (i + 0.5) / count for i in range(count)]


def extract_keyframes(
    mp4: Path,
    out_dir: Path,
    *,
    duration: float,
    count: int = 15,
    width: int = 720,
    quality: int = 4,
    log: Log | None = None,
    force: bool = False,
) -> list[Frame]:
    """从 mp4 等间隔抽帧。已存在的帧文件会被复用（除非 force）。"""
    log = log or Log("frames")
    ensure_dir(out_dir)
    ff = ffmpeg()
    timestamps = _pick_timestamps(duration, count)
    frames: list[Frame] = []
    for i, ts in enumerate(timestamps, start=1):
        frame_id = f"f{i:02d}"
        filename = f"{frame_id}.jpg"
        dest = out_dir / filename
        if force or not dest.exists() or dest.stat().st_size < 1024:
            cmd = [
                str(ff), "-y", "-v", "error",
                "-ss", f"{ts:.2f}", "-i", str(mp4),
                "-frames:v", "1",
                "-vf", f"scale={width}:-2",
                "-q:v", str(quality),
                str(dest),
            ]
            try:
                run(cmd, timeout=180)
            except Exception as exc:  # noqa: BLE001
                log.warn(f"抽帧失败 {mp4.name} @ {seconds_to_label(ts)}: {exc}")
                continue
        if not dest.exists():
            continue
        frames.append(
            Frame(
                frame_id=frame_id,
                timestamp=ts,
                time_label=seconds_to_label(ts),
                filename=filename,
                index=i,
            )
        )
    return frames


def frames_for_segment(frames: list[Frame], start: float, end: float, limit: int = 3) -> list[Frame]:
    """取落在 [start, end) 内的帧；不足则取最接近的，保证每段都有画面可绑定。"""
    inside = [f for f in frames if start <= f.timestamp < end]
    if len(inside) >= limit:
        step = len(inside) / limit
        return [inside[int(step * i)] for i in range(limit)]
    if inside:
        return inside
    mid = (start + end) / 2
    return sorted(frames, key=lambda f: abs(f.timestamp - mid))[:limit]


def write_storyboard(frames: list[Frame], dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        json.dumps({"frames": [f.to_dict() for f in frames]}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return dest
