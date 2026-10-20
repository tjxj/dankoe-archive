"""把 data/ 渲染成 site/ 静态站。

设计约束：
  - 无构建步骤、无 npm、无 JS 框架。产出就是可以直接双击打开或 `run.py serve` 的纯静态文件。
  - 增量友好：每次 build 全量重渲染（便宜，毫秒级），但只重新拷贝发生变化的图片。
  - 缺数据不崩：没翻译的视频仍然出页面（提示缺哪一层），没总结的隐藏总结区。
  - build 结束做一次自检：所有本地 src/href 指向的文件必须真实存在，否则报错退出。
"""
from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from . import catalog as catalogmod
from .common import (
    Log,
    ensure_dir,
    hash_file,
    hash_text,
    human_count,
    read_json,
    seconds_to_label,
    site_dir,
    video_data_dir,
    write_json,
    write_text,
)

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent.parent / "site-static"
DOCS_DIR = Path(__file__).resolve().parent.parent / "docs"
ASSET_NAMES = ("app.css", "app.js", "favicon.svg")

CONF_LABEL = {"high": "高置信", "medium": "中置信", "low": "待核验"}
ACCENTS = {"accent", "ochre", "blue", "green", "red", "ink"}

_PUNCT = re.compile(r"[^a-z0-9 ]+")
_SPACE = re.compile(r"\s+")


def _norm_for_match(text: str) -> str:
    """匹配用归一化：小写、去标点、压空白。字幕里 'don't' 和 'don t' 应当等价。"""
    return _SPACE.sub(" ", _PUNCT.sub(" ", str(text or "").lower())).strip()


def find_quote_time(quote_en: str, index: list[tuple[float, str]]) -> float | None:
    """把摘录的原话定位回字幕时间轴。

    提示词已要求 quote_en 逐字摘自字幕，所以先试整句互相包含，
    再用开头几个词做锚（模型偶尔会截断或改写引号内的一两个词）。
    找不到就返回 None —— 宁可不显示时间，也不要指到错误的位置。
    """
    q = _norm_for_match(quote_en)
    words = q.split()
    if len(words) < 3 or not index:
        return None
    for start, norm in index:
        if q in norm or (len(norm) >= 12 and norm in q):
            return start
    head = " ".join(words[:5])
    for start, norm in index:
        if head in norm:
            return start
    tail = " ".join(words[-5:])
    for start, norm in index:
        if tail in norm:
            return start
    return None


# ------------------------------------------------------------------ 视图模型


@dataclass
class VideoView:
    """模板直接消费的形状。所有字段都保证存在，模板里不用做防御性判断。"""

    raw: dict[str, Any]
    id: str
    title: str
    title_zh: str = ""
    date: str = ""
    month: str = ""
    duration: float = 0.0
    duration_label: str = ""
    url: str = ""
    views: str = ""
    description: str = ""
    chapters: list[dict[str, Any]] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    one_liner: str = ""
    sentence_count: int = 0
    index_label: str = ""
    # 派生
    search_hay: str = ""
    has_transcript: bool = False
    has_summary: bool = False

    def as_dict(self) -> dict[str, Any]:
        """给模板用：VideoView 的属性访问在 Jinja 里本来就能用，这个只为 manifest 输出。"""
        return {
            "id": self.id, "title": self.title, "title_zh": self.title_zh,
            "date": self.date, "month": self.month, "duration": self.duration,
            "duration_label": self.duration_label, "url": self.url,
            "views": self.views, "sentence_count": self.sentence_count,
            "topics": self.topics, "one_liner": self.one_liner,
            "has_transcript": self.has_transcript, "has_summary": self.has_summary,
        }


def snap_to_sentence(seconds: float, starts: list[float]) -> float:
    """把秒数对齐到最近的句子起点。

    模型给的时间线秒数是从分段范围估算的，可能落在一个词中间；
    对齐到真实句首后，点「跳到 12:03」会正好停在一句话的开头，观感完全不同。
    """
    if not starts:
        return float(seconds)
    best = min(starts, key=lambda s: (abs(s - seconds), s))
    return best


def _clean_tag(tag: str) -> str:
    t = re.sub(r"\s+", " ", str(tag or "")).strip()
    return t[:28]


def _topics_for(summary: dict[str, Any], raw: dict[str, Any], limit: int = 5) -> list[str]:
    """优先用总结产出的 topics（中文、贴合内容），退回视频 tags。"""
    out: list[str] = []
    for t in (summary.get("topics") or []):
        name = t if isinstance(t, str) else str((t or {}).get("name") or "")
        name = _clean_tag(name)
        if name and name not in out:
            out.append(name)
    if not out:
        for t in (raw.get("tags") or []):
            name = _clean_tag(t)
            if name and name not in out:
                out.append(name)
    return out[:limit]


def _as_str_list(val) -> list:
    """模型偶尔把本该是列表的字段返回成一整串字符串（换行分隔）。

    直接迭代字符串会逐字拆成单字符条目（真踩过：某个视频的 caveats
    在页面上排成了一列竖字），所以先按行拆开。
    """
    if isinstance(val, str):
        val = [ln for ln in val.splitlines() if ln.strip()] or [val]
    if not isinstance(val, list):
        return []
    return [str(x).strip() for x in val if str(x or "").strip()]


def build_video_view(
    raw: dict[str, Any],
    sentences: dict[str, Any] | None,
    summary: dict[str, Any] | None,
    frames: dict[str, dict[str, Any]],
    *,
    index_label: str = "",
) -> tuple[VideoView, dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """返回 (视图, 总结视图, 对照行, 英文段落, 中文段落)。"""
    s = summary or {}
    v = VideoView(
        raw=raw,
        id=str(raw.get("id") or ""),
        title=str(raw.get("title") or raw.get("id") or ""),
        title_zh=str(raw.get("title_zh") or s.get("title_zh") or "").strip(),
        date=str(raw.get("date") or ""),
        month=str(raw.get("month") or (raw.get("date") or "")[:7]),
        duration=float(raw.get("duration") or 0.0),
        duration_label=str(raw.get("duration_label") or seconds_to_label(float(raw.get("duration") or 0))),
        url=str(raw.get("url") or f"https://www.youtube.com/watch?v={raw.get('id')}"),
        views=human_count(raw.get("view_count")),
        description=str(raw.get("description") or ""),
        chapters=[
            {
                "start": float(c.get("start") or 0),
                "time_label": seconds_to_label(float(c.get("start") or 0)),
                "title": str(c.get("title") or "").strip(),
            }
            for c in (raw.get("chapters") or [])
        ],
        topics=_topics_for(s, raw),
        one_liner=str(s.get("one_liner") or "").strip(),
        index_label=index_label,
        has_transcript=bool(sentences),
        has_summary=bool(s.get("knowledge_points") or s.get("timeline") or s.get("overview_cards")),
    )

    # ---- 句子 / 段落 ----
    paras = ((sentences or {}).get("paragraphs") or [])
    lines: list[dict[str, Any]] = []
    paras_en: list[dict[str, Any]] = []
    paras_zh: list[dict[str, Any]] = []
    for p in paras:
        sents = p.get("sentences") or []
        en_parts, zh_parts = [], []
        for sent in sents:
            en = str(sent.get("en") or "").strip()
            zh = str(sent.get("zh") or "").strip()
            start = float(sent.get("s") or 0)
            if en or zh:
                lines.append({"s": round(start, 3), "ts": seconds_to_label(start), "en": en, "zh": zh})
            if en:
                en_parts.append(en)
            if zh:
                zh_parts.append(zh)
        head = sents[0] if sents else {}
        ps = float(head.get("s") or p.get("s") or 0)
        if en_parts:
            paras_en.append({"s": round(ps, 3), "ts": seconds_to_label(ps), "text": " ".join(en_parts)})
        if zh_parts:
            paras_zh.append({"s": round(ps, 3), "ts": seconds_to_label(ps), "text": "".join(zh_parts)})

    v.sentence_count = len(lines)
    line_index = [(ln["s"], _norm_for_match(ln["en"])) for ln in lines if ln["en"]]
    v.search_hay = " ".join(
        [v.title, v.title_zh, v.one_liner, " ".join(v.topics),
         " ".join(_clean_tag(t) for t in (raw.get("tags") or [])),
         " ".join(c["title"] for c in v.chapters)]
    ).lower()

    # ---- 总结：把 frame_id 落到真实文件名 ----
    def frame_ref(fid: str) -> dict[str, Any]:
        f = frames.get(str(fid or ""))
        if not f:
            return {"frame_file": "", "frame_time": 0.0, "frame_label": ""}
        return {"frame_file": f["filename"], "frame_time": float(f["timestamp"]),
                "frame_label": f["time_label"]}

    def accent(val: Any, default: str) -> str:
        a = str(val or "").strip().lower()
        return a if a in ACCENTS else default

    cards = [
        {"title": str(c.get("title") or "").strip(), "body": str(c.get("body") or "").strip(),
         "accent": accent(c.get("accent"), "accent")}
        for c in (s.get("overview_cards") or []) if isinstance(c, dict) and (c.get("title") or c.get("body"))
    ]
    frameworks = [
        {"title": str(f.get("title") or "").strip(), "body": str(f.get("body") or "").strip(),
         "accent": accent(f.get("accent"), "blue")}
        for f in (s.get("frameworks") or []) if isinstance(f, dict) and (f.get("title") or f.get("body"))
    ]

    kps: list[dict[str, Any]] = []
    for k in (s.get("knowledge_points") or []):
        if not isinstance(k, dict) or not (k.get("title") or k.get("body")):
            continue
        conf = str(k.get("confidence") or "").strip().lower()
        item = {
            "title": str(k.get("title") or "").strip(),
            "body": str(k.get("body") or "").strip(),
            "why": str(k.get("why") or "").strip(),
            "quote_en": str(k.get("quote_en") or "").strip().strip('"'),
            "quote_zh": str(k.get("quote_zh") or "").strip(),
            "frame_id": str(k.get("frame_id") or "").strip(),
            "confidence": conf if conf in CONF_LABEL else "",
            "conf_label": CONF_LABEL.get(conf, ""),
        }
        item.update(frame_ref(item["frame_id"]))
        kps.append(item)

    timeline: list[dict[str, Any]] = []
    starts = [ln["s"] for ln in lines]
    for t in (s.get("timeline") or []):
        if not isinstance(t, dict) or not (t.get("title") or t.get("body")):
            continue
        secs = t.get("seconds")
        if secs is None:
            secs = t.get("start") or 0
        try:
            secs = float(secs)
        except (TypeError, ValueError):
            secs = 0.0
        secs = snap_to_sentence(secs, starts)
        item = {
            "title": str(t.get("title") or "").strip(),
            "body": str(t.get("body") or "").strip(),
            "seconds": round(secs, 1),
            # 标签一律由对齐后的秒数生成，保证「显示的时间」就是「跳过去的时间」
            "time": seconds_to_label(secs),
            "frame_id": str(t.get("frame_id") or "").strip(),
        }
        item.update(frame_ref(item["frame_id"]))
        timeline.append(item)
    timeline.sort(key=lambda x: x["seconds"])

    quotes = []
    for q in (s.get("quotes") or []):
        if not isinstance(q, dict):
            continue
        en = str(q.get("en") or "").strip().strip('"')
        if not en:
            continue
        secs = find_quote_time(en, line_index)
        quotes.append({
            "en": en,
            "zh": str(q.get("zh") or "").strip(),
            "context": str(q.get("context") or "").strip(),
            "seconds": secs,
            "time_label": seconds_to_label(secs) if secs is not None else "",
        })
    actions = _as_str_list(s.get("actions"))
    questions = []
    raw_qs = s.get("questions")
    if isinstance(raw_qs, str):
        raw_qs = [ln for ln in raw_qs.splitlines() if ln.strip()]
    for q in raw_qs or []:
        if isinstance(q, dict):
            qq = str(q.get("q") or q.get("question") or "").strip()
            why = str(q.get("why") or "").strip()
        else:
            qq, why = str(q).strip(), ""
        if qq:
            questions.append({"q": qq, "why": why})
    caveats = _as_str_list(s.get("caveats"))

    summary_view = {
        "title_zh": str(s.get("title_zh") or "").strip(),
        "one_liner": str(s.get("one_liner") or "").strip(),
        "thesis": str(s.get("thesis") or "").strip(),
        "overview_cards": cards,
        "knowledge_points": kps,
        "timeline": timeline,
        "frameworks": frameworks,
        "quotes": quotes,
        "actions": actions,
        "questions": questions,
        "caveats": caveats,
        "meta": s.get("_meta") or {},
        "frame_fixes": len(s.get("_frame_fixes") or []),
    }
    if not any([cards, kps, timeline, frameworks, quotes, summary_view["thesis"]]):
        summary_view = {}

    return v, summary_view, lines, paras_en, paras_zh


# ------------------------------------------------------------------ 资源拷贝


def _copy_if_changed(src: Path, dst: Path) -> bool:
    if not src.exists():
        return False
    if dst.exists():
        ss, ds = src.stat(), dst.stat()
        if ss.st_size == ds.st_size and int(ss.st_mtime) == int(ds.st_mtime):
            return False
    ensure_dir(dst.parent)
    shutil.copy2(src, dst)
    return True


def copy_assets(cfg: dict[str, Any], views: list[VideoView], log: Log) -> dict[str, int]:
    root = site_dir(cfg)
    n_new = n_skip = n_missing = 0

    for name in ASSET_NAMES:
        src = STATIC_DIR / name
        if not src.exists():
            log.error(f"缺少静态资源 {src}")
            n_missing += 1
            continue
        if _copy_if_changed(src, root / "assets" / name):
            n_new += 1
        else:
            n_skip += 1

    for v in views:
        vdir = video_data_dir(cfg, v.id)
        cover = vdir / "cover.jpg"
        if cover.exists():
            n_new += _copy_if_changed(cover, root / "assets" / "cover" / f"{v.id}.jpg")
        else:
            log.warn(f"{v.id}: 没有封面 {cover}")
            n_missing += 1
        fdir = vdir / "frames"
        if fdir.is_dir():
            for p in sorted(fdir.glob("*.jpg")):
                if _copy_if_changed(p, root / "assets" / "frames" / v.id / p.name):
                    n_new += 1
                else:
                    n_skip += 1

    # 配套文章是手写 HTML（不进模板体系），随站一起发布到 /write-tech.html
    article = DOCS_DIR / "write-tech.html"
    if article.exists():
        n_new += _copy_if_changed(article, root / "write-tech.html")
    return {"copied": n_new, "unchanged": n_skip, "missing": n_missing}


# ------------------------------------------------------------------ 自检

_SRC_HREF = re.compile(r'(?:src|href)\s*=\s*"([^"]+)"', re.I)


def validate_site(root: Path, log: Log) -> list[str]:
    """扫所有生成的 HTML，确认每个本地资源与内部链接都真实存在。"""
    problems: list[str] = []
    htmls = sorted(root.rglob("*.html"))
    checked = 0
    for page in htmls:
        text = page.read_text(encoding="utf-8", errors="replace")
        for raw_url in _SRC_HREF.findall(text):
            url = raw_url.strip()
            if not url or url.startswith(("http://", "https://", "//", "data:", "mailto:", "javascript:")):
                continue
            path_part = unquote(urlparse(url).path)
            if not path_part or path_part == "/":
                continue
            target = (page.parent / path_part.lstrip("/")).resolve()
            checked += 1
            if not target.exists():
                problems.append(f"{page.relative_to(root)}: 缺失 {url}")
    log.info(f"自检: {len(htmls)} 个页面 / {checked} 处本地引用 / {len(problems)} 个问题")
    for p in problems[:20]:
        log.error("  " + p)
    return problems


def _check_no_secrets(root: Path) -> list[str]:
    """API key 只从环境变量读；确认它没被写进任何产出文件。"""
    pats = [re.compile(r"sk-[A-Za-z0-9]{16,}"), re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]{16,}")]
    hits: list[str] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4"}:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for pat in pats:
            if pat.search(text):
                hits.append(f"{p.name}: 命中 {pat.pattern}")
                break
    return hits


# ------------------------------------------------------------------ 渲染


def _make_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "xml", "j2"], default_for_string=True, default=True),
        undefined=StrictUndefined,
        trim_blocks=False,
        lstrip_blocks=False,
    )


def _month_label(key: str) -> str:
    y, _, m = key.partition("-")
    return f"{y} 年 {int(m)} 月" if m else y


def build_site(cfg: dict[str, Any], log: Log, only: str = "") -> int:
    t0 = datetime.now()
    cat = catalogmod.load_catalog(cfg)
    raw_videos = list(cat.get("videos") or [])
    if not raw_videos:
        log.error("catalog.json 里没有视频，先跑 `run.py catalog`")
        return 1
    if only:
        wanted = {x.strip() for x in only.split(",") if x.strip()}
        raw_videos = [v for v in raw_videos if v.get("id") in wanted]
        log.warn(f"--only 只渲染 {len(raw_videos)} 个视频，首页统计与筛选也会按这个子集算；"
                 f"要完整站点请去掉 --only 重跑 build")

    # 时间正序（老 → 新），用于「上一个 / 下一个」和序号
    chrono = sorted(raw_videos, key=lambda v: (str(v.get("date") or ""), str(v.get("id") or "")))
    total = len(chrono)
    labels = {v["id"]: f"第 {i + 1} / {total} 个" for i, v in enumerate(chrono)}

    root = ensure_dir(site_dir(cfg))

    views: list[VideoView] = []
    pages: dict[str, dict[str, Any]] = {}
    stats = {"videos": 0, "with_transcript": 0, "with_summary": 0,
             "sentences": 0, "words": 0, "quotes": 0, "quotes_timed": 0, "knowledge_points": 0}

    for raw in chrono:
        vid = str(raw.get("id") or "")
        vdir = video_data_dir(cfg, vid)
        sentences = read_json(vdir / "sentences.json", None)
        summary = read_json(vdir / "summary.json", None)
        sb = read_json(vdir / "storyboard.json", {"frames": []})
        frames = {str(f.get("frame_id")): f for f in (sb.get("frames") or []) if f.get("frame_id")}
        # summary 里的 frame_id 可能带前缀差异，做一次宽松索引
        for f in (sb.get("frames") or []):
            frames.setdefault(str(f.get("filename", "")).rsplit(".", 1)[0], f)

        view, sview, lines, paras_en, paras_zh = build_video_view(
            raw, sentences, summary, frames,
            index_label=labels.get(vid, ""),
        )
        views.append(view)
        pages[vid] = {"v": view, "s": sview, "lines": lines,
                      "paragraphs_en": paras_en, "paragraphs_zh": paras_zh}

        stats["videos"] += 1
        stats["sentences"] += len(lines)
        stats["words"] += sum(len(l["en"].split()) for l in lines)
        stats["with_transcript"] += int(view.has_transcript)
        stats["with_summary"] += int(bool(sview))
        qs = sview.get("quotes") or []
        stats["quotes"] += len(qs)
        stats["quotes_timed"] += sum(1 for q in qs if q.get("seconds") is not None)
        stats["knowledge_points"] += len(sview.get("knowledge_points") or [])
        if not view.has_transcript:
            log.warn(f"{vid}: 没有 sentences.json，页面只出总结/元信息")
        elif not sview:
            log.warn(f"{vid}: 没有 summary.json，「图文总结」标签页会显示提示")

    # 首页按新 → 旧
    newest_first = sorted(views, key=lambda v: (v.date, v.id), reverse=True)
    featured = newest_first[0]
    rest = newest_first[1:]

    topic_counts: dict[str, int] = {}
    for v in views:
        for t in v.topics:
            topic_counts[t] = topic_counts.get(t, 0) + 1
    topic_chips = [{"name": n, "n": c} for n, c in
                   sorted(topic_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:10]]

    month_counts: dict[str, int] = {}
    for v in views:
        if v.month:
            month_counts[v.month] = month_counts.get(v.month, 0) + 1
    month_chips = [{"key": k, "label": _month_label(k), "n": c}
                   for k, c in sorted(month_counts.items(), reverse=True)]

    dates = [v.date for v in views if v.date]
    date_range = (f"{min(dates)[:7].replace('-', '.')} – {max(dates)[:7].replace('-', '.')}"
                  if dates else "—")

    build_id = hash_text(
        "|".join([hash_file(STATIC_DIR / n) if (STATIC_DIR / n).exists() else n for n in ASSET_NAMES])
        + f"|{stats['videos']}|{stats['sentences']}|{stats['with_summary']}"
    )[:10]

    common = {
        "channel": cfg.get("channel") or {},
        "site_title": f"{(cfg.get('channel') or {}).get('name', 'Videos')} · 视频学习档案",
        "site_description": (cfg.get("channel") or {}).get("description", ""),
        "video_count": stats["videos"],
        "total_hours": round(sum(v.duration for v in views) / 3600.0, 1),
        "total_words_k": round(stats["words"] / 1000.0),
        "total_sentences": f"{stats['sentences']:,}",
        "date_range": date_range,
        "topic_chips": topic_chips,
        "month_chips": month_chips,
        "built_at": t0.strftime("%Y-%m-%d %H:%M"),
        "build_id": build_id,
        "translate_model": (cfg.get("api") or {}).get("translate_model", "LLM").split("/")[-1],
        "vision_model": (cfg.get("api") or {}).get("vision_model", "LLM").split("/")[-1],
        "year": t0.year,
    }

    env = _make_env()
    ensure_dir(root / "v")

    # ---- 首页 ----
    idx_ctx = dict(common)
    idx_ctx.update({"asset_prefix": "assets/", "home_href": "index.html",
                    "featured": featured, "videos": rest})
    write_text(root / "index.html", env.get_template("index.html.j2").render(**idx_ctx))
    log.info("渲染 index.html")

    # ---- 详情页 ----
    by_id = {v.id: v for v in views}
    for i, raw in enumerate(chrono):
        vid = str(raw["id"])
        page = pages[vid]
        ctx = dict(common)
        ctx.update(page)
        ctx.update({
            "asset_prefix": "../assets/",
            "home_href": "../index.html",
            "prev_v": by_id.get(str(chrono[i - 1]["id"])) if i > 0 else None,
            "next_v": by_id.get(str(chrono[i + 1]["id"])) if i + 1 < total else None,
        })
        write_text(root / "v" / f"{vid}.html", env.get_template("video.html.j2").render(**ctx))
    log.info(f"渲染 {len(pages)} 个详情页 → v/")

    # ---- 资源 ----
    a = copy_assets(cfg, views, log)
    log.info(f"资源: 新拷 {a['copied']} / 未变 {a['unchanged']} / 缺失 {a['missing']}")

    # ---- 清单 ----
    write_json(root / "manifest.json", {
        "built_at": t0.isoformat(timespec="seconds"),
        "build_id": build_id,
        "channel": cfg.get("channel"),
        "stats": stats,
        "videos": [v.as_dict() for v in newest_first],
    })

    # ---- 自检 ----
    problems = validate_site(root, log)
    secrets = _check_no_secrets(root)
    for s in secrets:
        log.error(f"产出里疑似含密钥：{s}")
    problems += secrets

    if stats["quotes"]:
        miss = stats["quotes"] - stats["quotes_timed"]
        log.info(f"  摘录定位: {stats['quotes_timed']}/{stats['quotes']} 条对上了字幕时间轴"
                 + (f"（{miss} 条对不上，页面不显示跳转按钮）" if miss else ""))
    log.info(
        f"build 完成: {stats['videos']} 视频 / 对照句 {stats['sentences']:,} / "
        f"英文词 {stats['words']:,} / 有总结 {stats['with_summary']} / "
        f"知识点 {stats['knowledge_points']} 条 / 耗时 {(datetime.now() - t0).total_seconds():.1f}s"
    )
    log.info(f"  → {root}/index.html")
    return 1 if problems else 0
