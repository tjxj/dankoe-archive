"""SRT 解析、完整性校验、句子级重组。

YouTube 自动字幕的 cue 常在句中切断（"…will not, in" / "fact, become successful"），
逐条翻译会得到破碎的中文。这里先把 cue 合并成完整句子并保留时间戳，
翻译按句进行，网页上点任意一句都能跳转到视频对应位置。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TS = r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})"
CUE_RE = re.compile(rf"^{TS}\s*-->\s*{TS}")

# 句末标点后又跟小写开头 → 说明还在同一句（自动字幕常在缩写处误断）
ABBREV = {
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc", "inc", "ltd",
    "co", "corp", "no", "nos", "approx", "e.g", "i.e", "u.s", "a.m", "p.m",
}
SENTENCE_END = ".?!"


@dataclass
class Cue:
    index: int
    start: float
    end: float
    text: str


@dataclass
class Sentence:
    start: float
    end: float
    text: str
    cue_start: int
    cue_end: int
    zh: str = ""

    @property
    def words(self) -> int:
        return len(self.text.split())

    def to_dict(self) -> dict[str, Any]:
        return {
            "s": round(self.start, 3),
            "e": round(self.end, 3),
            "en": self.text,
            "zh": self.zh,
        }


@dataclass
class Paragraph:
    start: float
    end: float
    sentences: list[Sentence] = field(default_factory=list)

    @property
    def en(self) -> str:
        return " ".join(s.text for s in self.sentences)

    @property
    def zh(self) -> str:
        return "".join(s.zh for s in self.sentences if s.zh)

    def to_dict(self) -> dict[str, Any]:
        return {"s": round(self.start, 3), "e": round(self.end, 3), "sentences": [s.to_dict() for s in self.sentences]}


# ---------------------------------------------------------------- 解析


def ts_to_seconds(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")[:3]) / 1000.0


def seconds_to_ts(seconds: float) -> str:
    total = max(0.0, seconds)
    ms = int(round((total - int(total)) * 1000))
    if ms == 1000:
        total += 1
        ms = 0
    secs = int(total)
    return f"{secs // 3600:02d}:{(secs % 3600) // 60:02d}:{secs % 60:02d},{ms:03d}"


def parse_srt(text: str) -> list[Cue]:
    """解析 SRT。容忍缺序号、CRLF、BOM、cue 内多行文本。"""
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    cues: list[Cue] = []
    pending: list[str] = []
    pending_ts: tuple[float, float] | None = None

    def flush() -> None:
        nonlocal pending, pending_ts
        if pending_ts is None:
            pending = []
            return
        body = "\n".join(pending).strip()
        body = re.sub(r"<[^>]+>", "", body)  # 去掉 <c> </c> 之类的 VTT 残留标签
        body = re.sub(r"\{\\[^}]*\}", "", body)
        body = " ".join(part.strip() for part in body.split("\n") if part.strip())
        if body:
            start, end = pending_ts
            cues.append(Cue(index=len(cues) + 1, start=start, end=end, text=body))
        pending = []
        pending_ts = None

    for line in text.split("\n"):
        m = CUE_RE.match(line.strip())
        if m:
            flush()
            g = m.groups()
            pending_ts = (ts_to_seconds(*g[0:4]), ts_to_seconds(*g[4:8]))
            continue
        if pending_ts is None:
            continue  # 序号行或文件头
        if not line.strip():
            flush()
            continue
        if re.fullmatch(r"\d{1,4}", line.strip()) and not pending:
            continue  # 序号
        pending.append(line)
    flush()
    cues.sort(key=lambda c: c.start)
    for i, c in enumerate(cues, 1):
        c.index = i
    return cues


def parse_srt_file(path: Path) -> list[Cue]:
    return parse_srt(path.read_text(encoding="utf-8", errors="replace"))


# ---------------------------------------------------------------- 完整性校验


def completeness(cues: list[Cue], duration: float, thresholds: dict[str, float]) -> dict[str, Any]:
    """判断字幕是否覆盖全片、有无中间大段空洞、语速是否正常。

    返回 {ok, reasons[], metrics{}}；ok=False 时 reasons 说明为什么，
    上层据此决定是否回退到 Whisper 转录。
    """
    reasons: list[str] = []
    if not cues:
        return {"ok": False, "reasons": ["字幕为空"], "metrics": {"cues": 0}}

    first = cues[0].start
    last = max(c.end for c in cues)
    words = sum(len(c.text.split()) for c in cues)
    chars = sum(len(c.text) for c in cues)

    tail_ratio = (last / duration) if duration > 0 else 0.0
    if tail_ratio < thresholds["min_tail_coverage_ratio"]:
        reasons.append(f"字幕只覆盖到 {last:.0f}s / 全片 {duration:.0f}s（{tail_ratio:.1%}），尾部缺失")

    # 中间空洞：相邻 cue 间隔
    gaps: list[tuple[float, float, float]] = []
    for a, b in zip(cues, cues[1:]):
        gap = b.start - a.end
        if gap > thresholds["max_interior_gap_seconds"]:
            gaps.append((a.end, b.start, gap))
    if gaps:
        worst = max(gaps, key=lambda g: g[2])
        reasons.append(
            f"存在 {len(gaps)} 处超过 {thresholds['max_interior_gap_seconds']:.0f}s 的空洞，"
            f"最大一处 {worst[0]:.0f}s→{worst[1]:.0f}s（{worst[2]:.0f}s）"
        )

    spoken_minutes = max((last - first) / 60.0, 1e-6)
    wpm = words / spoken_minutes
    if wpm < thresholds["min_words_per_minute"]:
        reasons.append(f"语速异常偏低：{wpm:.0f} 词/分（正常英文演讲 120–180），疑似字幕不完整")

    # 重复率：自动字幕偶尔整段复读
    texts = [c.text.lower() for c in cues]
    dup_ratio = 1 - (len(set(texts)) / len(texts)) if texts else 0
    if dup_ratio > 0.35:
        reasons.append(f"重复 cue 占比 {dup_ratio:.0%}，疑似滚动字幕未去重")

    return {
        "ok": not reasons,
        "reasons": reasons,
        "metrics": {
            "cues": len(cues),
            "words": words,
            "chars": chars,
            "first_start": round(first, 2),
            "last_end": round(last, 2),
            "duration": round(duration, 2),
            "tail_coverage_ratio": round(tail_ratio, 4),
            "words_per_minute": round(wpm, 1),
            "interior_gaps": len(gaps),
            "duplicate_cue_ratio": round(dup_ratio, 4),
        },
    }


# ---------------------------------------------------------------- 句子重组


def _ends_sentence(text: str) -> bool:
    t = text.rstrip()
    if not t:
        return False
    if t[-1] not in SENTENCE_END:
        return False
    if t[-1] == ".":
        # 排除缩写： "Dr." / "U.S." / "e.g."
        tail = re.search(r"([A-Za-z][A-Za-z.\-]*)\.$", t)
        if tail:
            token = tail.group(1).lower().rstrip(".")
            if token in ABBREV or (len(token) == 1 and token.isalpha()):
                return False
        if re.search(r"\b([A-Z]\.){2,}$", t):  # U.S. 形式
            return False
    return True


def _continues(prev_text: str, next_text: str) -> bool:
    """判断下一条 cue 是否是当前句的延续。

    唯一可靠信号：上一条**没有**句末标点，且下一条以**小写字母**开头
    （YouTube 自动字幕在句中换行时保持小写，新句子则首字母大写）。

    不要对连词做大小写不敏感匹配 —— 那样 "Tactics is execution." + "If you…"
    会被误判成同一句，导致多句粘连。
    """
    nxt = next_text.lstrip()
    if not nxt:
        return False
    if not nxt[0].isalpha() or not nxt[0].islower():
        return False
    return not _ends_sentence(prev_text)


CLAUSE_START = re.compile(
    r"^(and|but|or|so|because|yet|which|that|though|although|while|when|if|then|now|look|see)\b",
    re.I,
)


def _clause_boundary(prev_text: str, next_text: str) -> bool:
    """长句内部的可切分点：上一条以逗号结尾，或下一条以从句连接词开头。

    自动字幕有 23%–71% 的 cue 不带句末标点，纯靠标点断句会产生 100+ 词的长句。
    这类切点切出来的片段以 and/but/or 开头，口语转录里是自然的，翻成中文也顺。
    """
    if prev_text.rstrip().endswith((",", ";", ":")):
        return True
    return bool(CLAUSE_START.match(next_text.lstrip()))


def build_sentences(
    cues: list[Cue],
    *,
    max_chars: int = 380,
    hard_chars: int = 560,
    clause_chars: int = 170,
    runon_chars: int = 420,
    runon_hard: int = 430,
    runon_cues: int = 8,
    max_cues: int = 14,
    pause_split: float = 1.2,
) -> list[Sentence]:
    """把 cue 合并成完整句子。

    切分条件（按优先级）：
      1. hard_chars 硬上限（560 字符）
      2. runon_chars + 从句边界 —— 即使下一条是延续也切
      3. runon_hard + cue 数 ≥ runon_cues —— 无条件切，run-on 兜底
      4. 句末标点，且下一条不是延续
      5. 停顿 > pause_split，且下一条不是延续
      6. max_chars / max_cues 软上限，且下一条不是延续
      7. clause_chars 从句边界，且下一条不是延续

    第 4–7 条受"延续"保护，避免把 "…help you in the slightest" / "if you don't have
    context" 切成两半（该例 331 字符 / 7 cue）。但有的字幕几乎全程不带标点
    （2026-09-27 那条无尾标点率 71%、只有 3% 的 cue 以逗号结尾），每个小写 cue
    都被判为延续，句子会一路连到硬上限、产出 121 词的怪物。第 2、3 条为此兜底：
    阈值都高于那个要保护的案例，所以正常长句不受影响，只有真正的 run-on 会被切断。
    """
    sentences: list[Sentence] = []
    buf: list[Cue] = []

    def flush() -> None:
        if not buf:
            return
        text = " ".join(c.text.strip() for c in buf if c.text.strip())
        text = re.sub(r"\s{2,}", " ", text).strip()
        if not text:
            buf.clear()
            return
        sentences.append(
            Sentence(
                start=buf[0].start,
                end=buf[-1].end,
                text=text,
                cue_start=buf[0].index,
                cue_end=buf[-1].index,
            )
        )
        buf.clear()

    for i, cue in enumerate(cues):
        if buf:
            prev = buf[-1]
            gap = cue.start - prev.end
            joined_len = sum(len(c.text) for c in buf) + len(cue.text) + 1
            continues = _continues(prev.text, cue.text)
            boundary = _clause_boundary(prev.text, cue.text)
            if joined_len > hard_chars:
                flush()
            elif len(buf) >= 2 and joined_len > runon_chars and boundary:
                flush()
            elif joined_len > runon_hard and len(buf) >= runon_cues:
                flush()
            elif continues:
                pass  # 句中延续：只有上面三条能打断
            elif gap > pause_split:
                flush()
            elif joined_len > max_chars or len(buf) >= max_cues:
                flush()
            elif len(buf) >= 2 and joined_len > clause_chars and boundary:
                flush()
        buf.append(cue)
        if _ends_sentence(cue.text):
            nxt = cues[i + 1] if i + 1 < len(cues) else None
            if nxt is None or not _continues(cue.text, nxt.text):
                flush()
    flush()

    # 收尾：过滤掉过短的碎片（如单字 cue），并入前一句
    merged: list[Sentence] = []
    for s in sentences:
        if merged and len(s.text.split()) <= 2 and not _ends_sentence(merged[-1].text):
            prev = merged[-1]
            prev.text = f"{prev.text} {s.text}".strip()
            prev.end = s.end
            prev.cue_end = s.cue_end
        else:
            merged.append(s)
    return merged


def build_paragraphs(sentences: list[Sentence], *, target_sentences: int = 5, gap_split: float = 2.0) -> list[Paragraph]:
    """把句子按停顿和长度聚成自然段，供阅读视图使用。"""
    paragraphs: list[Paragraph] = []
    cur: list[Sentence] = []
    for s in sentences:
        if cur:
            gap = s.start - cur[-1].end
            if len(cur) >= target_sentences or gap > gap_split:
                paragraphs.append(Paragraph(start=cur[0].start, end=cur[-1].end, sentences=cur))
                cur = []
        cur.append(s)
    if cur:
        paragraphs.append(Paragraph(start=cur[0].start, end=cur[-1].end, sentences=cur))
    return paragraphs


# ---------------------------------------------------------------- 输出


def sentences_to_srt(sentences: list[Sentence], *, use_zh: bool = True) -> str:
    out: list[str] = []
    for i, s in enumerate(sentences, 1):
        body = (s.zh if use_zh else s.text).strip()
        if not body:
            continue
        out.append(f"{i}\n{seconds_to_ts(s.start)} --> {seconds_to_ts(s.end)}\n{body}\n")
    return "\n".join(out)


def sentences_to_text(sentences: list[Sentence], paragraphs: list[Paragraph], *, use_zh: bool = True) -> str:
    """生成可读的连续文稿：段落开头标时间戳，便于回查。"""
    from .common import seconds_to_label

    lines: list[str] = []
    for p in paragraphs:
        body = p.zh if use_zh else p.en
        body = body.strip()
        if not body:
            continue
        lines.append(f"[{seconds_to_label(p.start)}]  {body}")
    if not lines:  # 兜底
        for s in sentences:
            body = (s.zh if use_zh else s.text).strip()
            if body:
                lines.append(body)
    return "\n\n".join(lines)


def load_and_segment(path: Path, duration: float, thresholds: dict[str, float]) -> dict[str, Any]:
    cues = parse_srt_file(path)
    report = completeness(cues, duration, thresholds)
    sentences = build_sentences(cues)
    paragraphs = build_paragraphs(sentences)
    return {"cues": cues, "report": report, "sentences": sentences, "paragraphs": paragraphs}
