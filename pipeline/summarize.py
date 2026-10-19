"""图文总结：Qwen-VL 逐段分析（关键帧 + 转录）→ 深度模型合成全局学习结构。

沿用 z-video-study-webpage-qwen 的核心思想：**每个知识点必须绑定到真实存在的画面**，
模型给出的 frame_id 若不存在，自动降级到同段首帧并记录修正，绝不留下坏图。

反 AI-slop 约束写死在提示词里：要求引用原话、给出具体机制、禁止空泛套话，
并对无法从内容中支撑的结论标注低置信度而不是编造。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import frames as framesmod
from . import srt as srtmod
from .common import Log, hash_obj
from .llm import ChatClient, LLMError, extract_json

PROMPT_VERSION = "v2"


def _b64(path: Path) -> str:
    import base64
    import mimetypes

    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


@dataclass
class Segment:
    index: int
    start: float
    end: float
    text: str
    frames: list[framesmod.Frame]


def split_segments(
    sentences: list[srtmod.Sentence],
    frames: list[framesmod.Frame],
    *,
    count: int = 6,
    frames_per_segment: int = 3,
) -> list[Segment]:
    """按时间等分成 count 段，每段带上落在其区间内的关键帧。"""
    if not sentences:
        return []
    total_start = sentences[0].start
    total_end = max(s.end for s in sentences)
    span = max(total_end - total_start, 1.0)
    count = max(1, min(count, 12))
    segs: list[Segment] = []
    for i in range(count):
        lo = total_start + span * i / count
        hi = total_start + span * (i + 1) / count if i < count - 1 else total_end + 1
        text = " ".join(s.text for s in sentences if lo <= s.start < hi)
        if not text.strip():
            continue
        segs.append(
            Segment(
                index=i,
                start=lo,
                end=min(hi, total_end),
                text=text,
                frames=framesmod.frames_for_segment(frames, lo, hi, frames_per_segment),
            )
        )
    return segs


SEG_SYSTEM = """你是严谨的视频内容研究员。你在分析一段英文演讲视频的一个片段，
产出要供中文读者复习用。你只能依据给你的转录和画面，不能凭常识补充视频里没说过的内容。

输出严格 JSON，不要代码围栏，不要解释。"""


def seg_prompt(seg: Segment, video_title: str, frame_ids: list[str]) -> str:
    return f"""视频标题：{video_title}
片段编号：{seg.index + 1}
时间范围：{seg.start / 60:.1f} – {seg.end / 60:.1f} 分钟
可用画面 frame_id：{", ".join(frame_ids) or "（本段无画面）"}

该片段的英文转录：
\"\"\"
{seg.text[:9000]}
\"\"\"

请输出 JSON：
{{
  "segment_title": "这一段在讲什么（中文，12字以内，具体到内容，不要写"核心观点"这种空话）",
  "segment_summary": "2-3 句中文摘要，必须包含这一段真正提出的机制、步骤或论据",
  "key_points": [
    {{
      "title": "知识点标题（中文，具体）",
      "body": "展开说明：他到底主张什么、为什么、怎么做。要能让没看视频的人复述出实质内容。",
      "quote_en": "支撑这个点的英文原话（从转录里原样摘一句，不超过 30 词）",
      "quote_zh": "上面那句的中文翻译",
      "frame_id": "从可用画面里选最能代表这一点的一个，没有合适的就填 \\"\\"",
      "confidence": "high|medium|low"
    }}
  ],
  "uncertain": ["转录或画面里含糊、需要核验的地方；没有就给空数组"]
}}

硬性要求：
- key_points 给 2-4 个，宁少勿滥。
- body 至少 40 个中文字，写实质内容。禁止这类空话："这非常重要""值得深思""帮助我们更好地理解""在当今时代"。
- quote_en 必须是转录里真实出现的原句，逐字摘录，不许改写或拼接。
- frame_id 只能从"可用画面"里选，或留空。
- 如果这一段是口播广告、闲聊或片尾求订阅，就如实说明，key_points 给空数组。"""


FINAL_SYSTEM = """你是学习网页的主编。你要把一个长视频的分段研究结果合成为一份结构化的中文学习指南。
你只能使用分段结果里已有的信息，不能编造视频里没有的内容。输出严格 JSON，不要代码围栏。"""


def final_prompt(video: dict[str, Any], seg_results: list[dict[str, Any]], frame_ids: list[str]) -> str:
    from .common import seconds_to_label

    return f"""视频标题（英文）：{video.get('title')}
时长：{seconds_to_label(float(video.get('duration') or 0))}
发布日期：{video.get('date')}
播放量：{video.get('view_count')}
频道简介里的相关背景：{(video.get('description') or '')[:600]}

各分段研究结果：
{json.dumps(seg_results, ensure_ascii=False)[:26000]}

全部可用 frame_id：{", ".join(frame_ids)}

请输出 JSON：
{{
  "title_zh": "中文标题（传达视频真正的主题，可以比英文标题更准确，12-22字）",
  "one_liner": "一句话说清这个视频的核心主张（不超过 45 字，要具体，不要写成"教你如何通过XX实现XX"这种模板）",
  "thesis": "核心论点：2-3 句，说清他的论证链条是什么",
  "overview_cards": [
    {{"title": "小标题", "body": "60-120字，讲清一个具体的观点或方法", "accent": "ink|ochre|blue|green|red"}}
  ],
  "knowledge_points": [
    {{
      "title": "知识点（具体）",
      "body": "80-160字：主张 + 理由 + 怎么做",
      "why": "为什么值得记住，一句话",
      "quote_en": "英文原话",
      "quote_zh": "中文翻译",
      "frame_id": "绑定的画面 frame_id",
      "confidence": "high|medium|low"
    }}
  ],
  "timeline": [
    {{"time": "MM:SS", "title": "节点标题", "body": "一句话说明这里讲了什么", "frame_id": "画面"}}
  ],
  "frameworks": [
    {{"title": "框架/张力名称", "body": "这个框架怎么用，两端分别是什么", "accent": "ink|ochre|blue|green|red"}}
  ],
  "quotes": [
    {{"en": "最有力的英文原话（逐字摘自分段结果里的 quote_en）", "zh": "中文翻译", "context": "这句话出现在什么语境"}}
  ],
  "actions": ["看完可以立刻做的事，具体到动作，4-6 条"],
  "questions": [{{"q": "复盘问题", "why": "为什么问这个"}}],
  "caveats": ["这份总结的局限：哪些是低置信度、哪些是广告口播、哪些转录可能有误"],
  "topics": ["3-6 个主题标签，中文，用于站点筛选"]
}}

硬性要求：
- knowledge_points 给 5-8 个，按视频里的出现顺序排。
- timeline 给 6-12 个节点，时间必须落在视频时长内，且按顺序递增。
- quotes 给 3-5 条，en 字段必须逐字来自分段结果的 quote_en，不许自己造。
- overview_cards 给 3-4 张。
- frame_id 只能从"全部可用 frame_id"里选；选不到就填 ""。
- 禁止空话套话："在当今快速变化的时代""赋能""闭环""底层逻辑""干货满满""让我们一起"。
  写不出实质内容就少写，不要用套话凑数。
- caveats 必须诚实：如果视频里有明显的产品广告口播，要指出来。"""


def _valid_frame(value: Any, frame_ids: list[str]) -> tuple[str, bool]:
    v = str(value or "").strip()
    if v in frame_ids:
        return v, False
    import re

    m = re.search(r"(?:f|frame)[-_ ]?(\d+)", v, re.I)
    if m:
        cand = f"f{int(m.group(1)):02d}"
        if cand in frame_ids:
            return cand, True
    return "", True


def normalize(analysis: dict[str, Any], frame_ids: list[str]) -> dict[str, Any]:
    """把模型给的 frame_id 校正到真实存在的帧；无效的降级为空，并记录修正。"""
    fixes: list[dict[str, str]] = []
    fallback = frame_ids[0] if frame_ids else ""
    for coll in ("knowledge_points", "timeline"):
        for i, item in enumerate(analysis.get(coll) or []):
            if not isinstance(item, dict):
                continue
            orig = str(item.get("frame_id") or "")
            fixed, changed = _valid_frame(orig, frame_ids)
            if not fixed and orig and fallback:
                fixed = fallback
                changed = True
            if changed:
                fixes.append({"where": f"{coll}[{i}]", "from": orig, "to": fixed})
            item["frame_id"] = fixed
    if fixes:
        analysis.setdefault("_frame_fixes", []).extend(fixes)
    # 列表字段绝不能是字符串：模型把整段返回成一串时，渲染端会逐字迭代
    #（真踩过：一个视频的 caveats 是 str，页面上排成一列竖字）
    for coll in ("actions", "caveats"):
        v = analysis.get(coll)
        if isinstance(v, str):
            analysis[coll] = [ln.strip() for ln in v.splitlines() if ln.strip()] or [v.strip()]
    qs = analysis.get("questions")
    if isinstance(qs, str):
        qs = [ln.strip() for ln in qs.splitlines() if ln.strip()]
    if isinstance(qs, list):
        analysis["questions"] = [
            q if isinstance(q, dict) else {"q": str(q).strip(), "why": ""}
            for q in qs if str(q or "").strip()
        ]
    return analysis


def summarize_video(
    *,
    video: dict[str, Any],
    sentences: list[srtmod.Sentence],
    frames: list[framesmod.Frame],
    frames_dir: Path,
    vision: ChatClient,
    synth: ChatClient,
    cfg: dict[str, Any],
    log: Log,
) -> dict[str, Any]:
    scfg = cfg["summarize"]
    segs = split_segments(
        sentences,
        frames,
        count=int(scfg.get("segments", 6)),
        frames_per_segment=int(scfg.get("frames_per_segment", 3)),
    )
    frame_ids = [f.frame_id for f in frames]
    log.info(f"{video['id']}: {len(segs)} 段 / {len(frames)} 帧")

    def analyze(seg: Segment, c: ChatClient) -> dict[str, Any]:
        ids = [f.frame_id for f in seg.frames]
        content: list[dict[str, Any]] = [{"type": "text", "text": seg_prompt(seg, video.get("title", ""), ids)}]
        for f in seg.frames:
            p = frames_dir / f.filename
            if p.exists():
                content.append({"type": "image_url", "image_url": {"url": _b64(p)}})
        res = c.complete([{"role": "system", "content": SEG_SYSTEM}, {"role": "user", "content": content}],
                         temperature=float(scfg.get("temperature", 0.35)), thinking=False, max_tokens=6000,
                         timeout=int(scfg.get("vision_timeout", 600)))
        data = extract_json(res.text)
        if not isinstance(data, dict):
            raise LLMError(f"segment-{seg.index}-not-object")
        data["_range"] = [round(seg.start, 1), round(seg.end, 1)]
        data["_frames"] = ids
        return data

    seg_results = vision.map(
        segs,
        lambda seg, c: analyze(seg, c),
        concurrency=int(scfg.get("vision_concurrency", 6)),
        on_error="keep",
    )
    good = [r for r in seg_results if isinstance(r, dict)]
    if not good:
        raise LLMError("all-segments-failed")
    if len(good) < len(segs):
        log.warn(f"{video['id']}: {len(segs) - len(good)} 段分析失败，用剩余 {len(good)} 段继续")

    log.info(f"  {video['id']} 分段完成，开始合成")
    final_text = synth.complete(
        [
            {"role": "system", "content": FINAL_SYSTEM},
            {"role": "user", "content": final_prompt(video, good, frame_ids)},
        ],
        temperature=0.4,
        thinking=bool(scfg.get("thinking", True)),
        max_tokens=16000,
        # 开 thinking 又要吐 16k token 的大 JSON，默认 300s 不够（已实测超时一次）
        timeout=int(scfg.get("synth_timeout", 900)),
    )
    analysis = extract_json(final_text.text)
    if not isinstance(analysis, dict):
        raise LLMError("final-analysis-not-object")

    analysis = normalize(analysis, frame_ids)
    analysis["_meta"] = {
        "video_id": video["id"],
        "prompt_version": PROMPT_VERSION,
        "vision_model": vision.model,
        "synth_model": synth.model,
        "segments_total": len(segs),
        "segments_ok": len(good),
        "frames": len(frames),
        "frame_fixes": len(analysis.get("_frame_fixes") or []),
    }
    analysis["_segments"] = good
    return analysis


def fingerprint(video: dict[str, Any], cfg: dict[str, Any], frames: list[framesmod.Frame], sentences_count: int) -> str:
    return hash_obj(
        {
            "prompt": PROMPT_VERSION,
            "vision": cfg["api"]["vision_model"],
            "synth": cfg["api"]["synthesize_model"],
            "params": cfg["summarize"],
            "frames": [f.frame_id for f in frames],
            "sentences": sentences_count,
            "video_fp": video.get("meta_fingerprint") or video.get("id"),
        }
    )
