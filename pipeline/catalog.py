"""视频目录发现与元数据。

职责：
  1. 扫描 video_dir 下的 *.mp4，从文件名解析日期/标题/视频 ID
  2. 缺元数据时调用 yt-dlp 抓取并瘦身缓存到 data/raw/<id>.json
  3. 合成 data/catalog.json —— 全站唯一数据源

幂等：已缓存的元数据不重复抓；新丢进目录的视频会被自动发现。
"""
from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .common import (
    Log,
    ensure_dir,
    format_date,
    hash_file,
    read_json,
    run,
    seconds_to_label,
    write_json,
    ytdlp,
)

# 文件名形如： 2026-09-27 - How To Become ... [oMtAfbNHfnw].mp4
NAME_RE = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})\s*-\s*(?P<title>.+?)\s*\[(?P<id>[\w\-]{6,})\]$")

# 只保留站点真正用得到的字段，其余（formats/thumbnails 全量等）会让 catalog 膨胀到几十 MB
SLIM_FIELDS = (
    "id title description upload_date timestamp duration duration_string view_count like_count "
    "comment_count channel channel_id channel_url uploader webpage_url categories tags chapters "
    "language availability is_live was_live"
).split()


@dataclass
class Video:
    id: str
    title: str
    date: str  # YYYY-MM-DD
    duration: float
    url: str
    local_path: Path
    srt_path: Path | None
    meta: dict[str, Any]

    @property
    def duration_label(self) -> str:
        return seconds_to_label(self.duration)

    @property
    def year(self) -> int:
        return int(self.date[:4]) if re.match(r"^\d{4}", self.date) else 0

    @property
    def month_key(self) -> str:
        return self.date[:7]  # YYYY-MM

    def to_dict(self) -> dict[str, Any]:
        m = self.meta
        chapters = [
            {
                "start": float(c.get("start_time") or 0),
                "title": str(c.get("title") or "").strip(),
            }
            for c in (m.get("chapters") or [])
            if str(c.get("title") or "").strip()
        ]
        return {
            "id": self.id,
            "title": self.title,
            "title_zh": str(m.get("title_zh") or ""),
            "date": self.date,
            "year": self.year,
            "month": self.month_key,
            "duration": round(self.duration, 1),
            "duration_label": self.duration_label,
            "url": self.url,
            "channel": m.get("channel") or m.get("uploader") or "",
            "channel_url": m.get("channel_url") or "",
            "view_count": m.get("view_count"),
            "like_count": m.get("like_count"),
            "description": (m.get("description") or "").strip(),
            "tags": m.get("tags") or [],
            "chapters": chapters,
            "local_file": self.local_path.name,
            "srt_file": self.srt_path.name if self.srt_path else "",
            "has_subtitle": self.srt_path is not None,
            "meta_fingerprint": m.get("_fingerprint", ""),
        }


# ---------------------------------------------------------------- 发现


def parse_filename(stem: str) -> tuple[str, str, str] | None:
    m = NAME_RE.match(stem)
    if not m:
        return None
    return m.group("date"), m.group("title").strip(), m.group("id")


def discover(cfg: dict[str, Any], log: Log) -> list[dict[str, Any]]:
    """扫描 video_dir，返回原始条目（date/title/id/mp4/srt）。"""
    root: Path = cfg["video_dir_path"]
    if not root.exists():
        log.error(f"视频目录不存在: {root}")
        return []
    found: list[dict[str, Any]] = []
    orphans: list[str] = []
    for mp4 in sorted(root.glob("*.mp4")):
        parsed = parse_filename(mp4.stem)
        if not parsed:
            # 文件名不符合约定时，仍尝试从 .info.json 或 archive 里认 ID
            orphans.append(mp4.name)
            continue
        date, title, vid = parsed
        srt = mp4.with_suffix("").with_name(mp4.stem + ".en.srt")
        if not srt.exists():
            for cand in (mp4.stem + ".zh.srt", mp4.stem + ".srt"):
                alt = mp4.parent / cand
                if alt.exists():
                    srt = alt
                    break
        found.append(
            {
                "id": vid,
                "title": title,
                "date": date,
                "mp4": mp4,
                "srt": srt if srt.exists() else None,
            }
        )
    if orphans:
        log.warn(f"{len(orphans)} 个 mp4 文件名不符合 'YYYY-MM-DD - 标题 [ID].mp4' 约定，已跳过: {orphans[:3]}")
    return found


# ---------------------------------------------------------------- 元数据


def _slim(raw: dict[str, Any]) -> dict[str, Any]:
    out = {k: raw.get(k) for k in SLIM_FIELDS if k in raw}
    # chapters 只留 start_time/title/end_time，原始结构里塞了大量冗余
    if out.get("chapters"):
        out["chapters"] = [
            {
                "start_time": c.get("start_time"),
                "end_time": c.get("end_time"),
                "title": c.get("title"),
            }
            for c in out["chapters"]
        ]
    thumbs = raw.get("thumbnails") or []
    if thumbs:
        best = max(thumbs, key=lambda t: (t.get("height") or 0) * (t.get("width") or 0))
        out["thumbnail_best"] = best.get("url")
    out["thumbnail"] = raw.get("thumbnail")
    return out


def fetch_metadata(video_id: str, cfg: dict[str, Any], log: Log, *, force: bool = False) -> dict[str, Any]:
    """抓取并瘦身单条视频元数据，缓存到 data/raw/<id>.json。"""
    raw_dir = ensure_dir(cfg["data_dir_path"] / "raw")
    cache = raw_dir / f"{video_id}.json"
    if cache.exists() and not force:
        cached = read_json(cache, None)
        if isinstance(cached, dict) and cached.get("id"):
            return cached

    tool = ytdlp()
    cmd = [
        str(tool), "--no-update", "--js-runtimes", "node",
        "--skip-download", "-J",
        f"https://www.youtube.com/watch?v={video_id}",
    ]
    log.info(f"抓取元数据 {video_id}")
    try:
        out = run(cmd, timeout=240)
        data = json.loads(out)
    except Exception as exc:  # noqa: BLE001
        log.warn(f"元数据抓取失败 {video_id}: {exc}")
        if cache.exists():
            return read_json(cache, {}) or {}
        return {}
    slim = _slim(data)
    write_json(cache, slim)
    return slim


# ---------------------------------------------------------------- 合成 catalog


def build_catalog(cfg: dict[str, Any], log: Log, *, fetch: bool = True) -> dict[str, Any]:
    entries = discover(cfg, log)
    log.info(f"发现 {len(entries)} 个视频")
    videos: list[Video] = []
    for e in entries:
        meta: dict[str, Any] = {}
        if fetch:
            meta = fetch_metadata(e["id"], cfg, log) or {}
        duration = float(meta.get("duration") or 0)
        if duration <= 0 and e["mp4"].exists():
            duration = probe_duration(e["mp4"], log)
        date = format_date(meta.get("upload_date"), "%Y-%m-%d") if meta.get("upload_date") else e["date"]
        title = (meta.get("title") or e["title"]).strip()
        fp = hash_file(e["mp4"]) if e["mp4"].exists() else ""
        meta["_fingerprint"] = fp
        if e["srt"]:
            meta["_srt_fingerprint"] = hash_file(e["srt"])
        videos.append(
            Video(
                id=e["id"],
                title=title,
                date=date,
                duration=duration,
                url=f"https://www.youtube.com/watch?v={e['id']}",
                local_path=e["mp4"],
                srt_path=e["srt"],
                meta=meta,
            )
        )
    videos.sort(key=lambda v: v.date, reverse=True)
    catalog = {
        "channel": cfg["channel"],
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "count": len(videos),
        "total_duration": round(sum(v.duration for v in videos), 1),
        "videos": [v.to_dict() for v in videos],
    }
    path = write_json(cfg["data_dir_path"] / "catalog.json", catalog)
    log.info(f"catalog 写入 {path}（{len(videos)} 条，总时长 {seconds_to_label(catalog['total_duration'])}）")
    return catalog


def probe_duration(mp4: Path, log: Log) -> float:
    from .common import ffprobe

    try:
        out = run(
            [str(ffprobe()), "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(mp4)],
            timeout=120,
        )
        return float(out.strip() or 0)
    except Exception as exc:  # noqa: BLE001
        log.warn(f"ffprobe 失败 {mp4.name}: {exc}")
        return 0.0


def load_catalog(cfg: dict[str, Any]) -> dict[str, Any]:
    return read_json(cfg["data_dir_path"] / "catalog.json", {"videos": []}) or {"videos": []}
