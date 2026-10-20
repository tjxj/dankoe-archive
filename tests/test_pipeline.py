#!/usr/bin/env python3
"""流水线单元测试。

两种跑法都行：
    python3 tests/test_pipeline.py     # 自带 runner，不需要装 pytest
    pytest tests/test_pipeline.py -q   # 如果环境里有 pytest

重点覆盖调试过程中真踩过的坑，不是凑覆盖率：
  - 句子重组的三种失败模式（过度切分 / 多句粘连 / 无标点长句失控）
  - 编号翻译协议的容错
  - JSON 抽取
  - 密钥脱敏
  - 渲染层的降级路径与产出自检
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import catalog as catalogmod  # noqa: E402
from pipeline import render as rendermod  # noqa: E402
from pipeline import srt as srtmod  # noqa: E402
from pipeline import translate as translatemod  # noqa: E402
from pipeline.common import scrub  # noqa: E402
from pipeline.llm import extract_json  # noqa: E402


# ---------------------------------------------------------------- SRT 解析

SRT_SAMPLE = """﻿1
00:00:00,125 --> 00:00:01,877
You probably want to change your life.

2
00:00:02,085 --> 00:00:04,213
That's probably why you clicked

3
00:00:04,254 --> 00:00:07,216
on this video.
"""


def test_parse_srt_tolerates_bom_and_missing_blank_line():
    cues = srtmod.parse_srt(SRT_SAMPLE)
    assert len(cues) == 3, cues
    assert cues[0].text == "You probably want to change your life."
    assert abs(cues[0].start - 0.125) < 1e-6
    assert abs(cues[2].end - 7.216) < 1e-6


def test_parse_srt_handles_crlf_and_vtt_tags():
    raw = "1\r\n00:00:01,000 --> 00:00:03,000\r\n<i>hello</i> world\r\n\r\n"
    cues = srtmod.parse_srt(raw)
    assert len(cues) == 1
    assert cues[0].text == "hello world", cues[0].text


THRESHOLDS = {
    "min_tail_coverage_ratio": 0.97,
    "max_interior_gap_seconds": 120,
    "min_words_per_minute": 60,
}


def test_completeness_flags_short_tail():
    """字幕只覆盖前 20 秒、片子有 1000 秒 —— 必须判为不完整，好让上层回退去转录。"""
    cues = [srtmod.Cue(1, 0.0, 10.0, "opening remarks about the first idea"),
            srtmod.Cue(2, 10.0, 20.0, "and then a second completely different point")]
    ok = srtmod.completeness(cues, 1000.0, THRESHOLDS)
    assert ok["ok"] is False
    assert ok["metrics"]["tail_coverage_ratio"] < 0.05, ok
    assert any("尾部" in r or "覆盖" in r for r in ok["reasons"]), ok["reasons"]


def _dense_cues(n: int, *, step: float = 10.0, span: float = 9.5) -> list[srtmod.Cue]:
    """铺满时间轴的 cue，语速贴近真实英文演讲（≈150 词/分），每条文本不同。"""
    return [
        srtmod.Cue(i + 1, i * step, i * step + span,
                   f"talking about topic number {i} " + "and explaining it in more detail " * 3)
        for i in range(n)
    ]


def test_dense_fixture_is_realistic_speech():
    """护栏用例本身得先站得住：语速要在正常区间，否则后面的断言毫无意义。"""
    ok = srtmod.completeness(_dense_cues(100), 1000.0, THRESHOLDS)
    assert 120 <= ok["metrics"]["words_per_minute"] <= 190, ok["metrics"]["words_per_minute"]
    assert ok["metrics"]["duplicate_cue_ratio"] == 0.0


def test_completeness_passes_when_covered():
    cues = _dense_cues(100)
    ok = srtmod.completeness(cues, 1000.0, THRESHOLDS)
    assert ok["ok"] is True, ok
    assert ok["metrics"]["interior_gaps"] == 0


def test_completeness_flags_interior_gap():
    cues = _dense_cues(20) + [srtmod.Cue(99, 990.0, 999.0, "finally back after a long silence")]
    ok = srtmod.completeness(cues, 1000.0, THRESHOLDS)
    assert ok["ok"] is False, ok
    assert any("空洞" in r or "gap" in r.lower() for r in ok["reasons"]), ok["reasons"]


def test_completeness_flags_duplicate_rolling_captions():
    cues = [srtmod.Cue(i + 1, i * 2.0, i * 2.0 + 2.0, "the exact same line repeated") for i in range(60)]
    ok = srtmod.completeness(cues, 120.0, THRESHOLDS)
    assert ok["ok"] is False, ok
    assert ok["metrics"]["duplicate_cue_ratio"] > 0.5


# ---------------------------------------------------------------- 句子重组
# 这三个用例分别对应三次真实返工，改回归逻辑前先看它们。


def _cues(pairs):
    """[(start, text), ...] → Cue 列表，每条 2 秒。"""
    return [srtmod.Cue(i + 1, float(i) * 2, float(i) * 2 + 2, t) for i, (_, t) in enumerate(pairs)]


def test_sentences_do_not_split_mid_phrase():
    """v1 的坑：max_chars 太小，把 "…help you in the slightest" 从中间切开。"""
    cues = _cues([
        (0, "And the truth is that no amount of tactical advice is going to"),
        (2, "help you in the slightest"),
        (4, "if you don't have understanding or context or experience."),
    ])
    out = srtmod.build_sentences(cues)
    texts = [s.text for s in out]
    assert any(t.endswith("help you in the slightest") or "help you in the slightest" in t for t in texts), texts
    # 后半句以 if 小写开头，说明前半句没被误判为句子结束
    joined = " ".join(texts)
    assert "slightest if you don't" in joined or len(out) == 2, texts


def test_sentences_do_not_glue_two_sentences():
    """v2 的坑：大小写不敏感的连词匹配把 "Tactics is execution." 和 "If you…" 粘成一句。"""
    cues = _cues([
        (0, "Tactics is execution."),
        (2, "If you don't have a strategy"),
        (4, "then your tactics are meaningless."),
    ])
    out = srtmod.build_sentences(cues)
    assert out[0].text.strip() == "Tactics is execution.", out[0].text
    assert len(out) == 2, [s.text for s in out]


def test_runon_guard_fires_without_punctuation():
    """v3 的坑：整段没有句末标点时 continues 恒真，拼出 130 词的巨句。"""
    long_tail = "and then you keep going because that is what the work looks like every single day"
    cues = _cues([(i * 2, long_tail) for i in range(14)])
    out = srtmod.build_sentences(cues)
    worst = max(len(s.text.split()) for s in out)
    assert worst <= 90, f"最长句 {worst} 词，防粘连护栏没生效"
    assert len(out) > 1, "14 条 cue 不该被压成 1 句"


def test_sentences_preserve_order_and_coverage():
    cues = _cues([(i * 2, f"Sentence number {i} is here.") for i in range(50)])
    out = srtmod.build_sentences(cues)
    assert "".join(s.text for s in out) == "".join(c.text for c in cues)
    assert all(out[i].start <= out[i + 1].start for i in range(len(out) - 1))


def test_paragraphs_have_times():
    cues = _cues([(i * 2, f"Sentence number {i} is here.") for i in range(30)])
    paras = srtmod.build_paragraphs(srtmod.build_sentences(cues))
    assert paras and all(p.start <= p.end for p in paras)
    assert sum(len(p.sentences) for p in paras) == 30


# ---------------------------------------------------------------- 编号协议


def test_parse_numbered_plain():
    got = translatemod.parse_numbered("1. 甲\n2. 乙\n3. 丙", 3)
    assert got == {1: "甲", 2: "乙", 3: "丙"}, got


def test_parse_numbered_tolerates_mess():
    raw = (
        "1、第一句\n"
        "2) 第二句\n"
        "3 第三句\n"          # 缺点号
        "4. 第四句的开头\n"
        "   第四句的续写\n"    # 多行续写
        "4. 第四句又补了一次\n"  # 同号重复 → 拼接
    )
    got = translatemod.parse_numbered(raw, 4)
    assert got[1] == "第一句"
    assert got[2] == "第二句", got[2]
    assert got[3] == "第三句", "缺点号的裸行号也要认出来"
    assert "第四句的开头" in got[4] and "第四句的续写" in got[4] and "又补了一次" in got[4], got[4]


def test_parse_numbered_does_not_steal_numbers_from_translation():
    """严格解析已经全中时绝不启用宽松解析，否则译文开头的 "3 个理由…" 会被当成行号。"""
    raw = "1. 第一句\n2. 第二句\n3. 3 个理由让你必须现在开始\n"
    got = translatemod.parse_numbered(raw, 3)
    assert got[3] == "3 个理由让你必须现在开始", got


def test_parse_numbered_ignores_prose_preamble():
    got = translatemod.parse_numbered("好的，以下是翻译：\n1. 甲\n2. 乙", 2)
    assert got == {1: "甲", 2: "乙"}, got


def test_make_chunks_covers_everything_in_order():
    sents = [
        srtmod.Sentence(i * 2.0, i * 2.0 + 2, f"word " * 30 + f"{i}.", 0, 0)
        for i in range(100)
    ]
    chunks = translatemod.make_chunks(sents, word_budget=200, max_sentences=10)
    flat = [s for c in chunks for s in c.sentences]
    assert flat == sents, "分块必须无损、有序地覆盖全部句子"
    assert all(len(c.sentences) <= 10 for c in chunks)
    assert all(c.words <= 400 for c in chunks), [c.words for c in chunks]


# ---------------------------------------------------------------- JSON 抽取


def test_extract_json_plain():
    assert extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_fenced():
    assert extract_json('```json\n{"a": [1, 2]}\n```') == {"a": [1, 2]}


def test_extract_json_embedded_in_prose():
    got = extract_json('这是结果：{"a": {"b": 2}} 希望有帮助')
    assert got == {"a": {"b": 2}}, got


def test_extract_json_braces_inside_strings():
    got = extract_json('{"code": "if (x) { y }", "n": 1}')
    assert got == {"code": "if (x) { y }", "n": 1}, got


def test_extract_json_raises_on_garbage():
    """抽不出 JSON 就抛 LLMError（调用方靠它触发重试），不静默返回 None。"""
    from pipeline.llm import LLMError

    for bad in ("完全不是 JSON", "", "   "):
        try:
            extract_json(bad)
        except LLMError:
            continue
        raise AssertionError(f"应当抛 LLMError: {bad!r}")


# ---------------------------------------------------------------- 脱敏


def test_scrub_redacts_secrets():
    out = scrub("key=sk-abcdefghijklmnop1234567890 and Authorization: Bearer abc123def456ghi789")
    assert "sk-abcdefghijklmnop" not in out
    assert "abc123def456ghi789" not in out
    assert out != ""


def test_scrub_keeps_normal_text():
    assert scrub("video id oMtAfbNHfnw, 251 sentences") == "video id oMtAfbNHfnw, 251 sentences"


# ---------------------------------------------------------------- 文件名约定


def test_name_re_parses_convention():
    m = catalogmod.NAME_RE.match("2026-09-27 - How To Become Self-Disciplined [oMtAfbNHfnw]")
    assert m, "文件名不符合约定"
    assert m.group("date") == "2026-09-27"
    assert m.group("id") == "oMtAfbNHfnw"
    assert m.group("title") == "How To Become Self-Disciplined"


def test_name_re_rejects_junk():
    assert catalogmod.NAME_RE.match("random file.mp4") is None
    assert catalogmod.NAME_RE.match("2026-09-27 no id here") is None


# ---------------------------------------------------------------- 引用定位


INDEX = [
    (0.0, "you probably want to change your life"),
    (2.0, "that s probably why you clicked on this video"),
    (5.0, "but for some reason you just can t do it"),
    (8.0, "discipline is not about punishment it is about design"),
]


def test_find_quote_time_exact():
    assert rendermod.find_quote_time("You probably want to change your life.", INDEX) == 0.0


def test_find_quote_time_ignores_punctuation_case():
    assert rendermod.find_quote_time("but FOR some reason, you just can't do it", INDEX) == 5.0


def test_find_quote_time_partial_head():
    # 模型截断了后半句，靠前 5 词锚定
    assert rendermod.find_quote_time("Discipline is not about punishment", INDEX) == 8.0


def test_find_quote_time_returns_none_when_absent():
    assert rendermod.find_quote_time("this line never appears anywhere", INDEX) is None
    assert rendermod.find_quote_time("too short", INDEX) is None


def test_snap_to_sentence():
    starts = [0.0, 5.2, 10.8, 30.0]
    assert rendermod.snap_to_sentence(6.0, starts) == 5.2
    assert rendermod.snap_to_sentence(9.0, starts) == 10.8
    assert rendermod.snap_to_sentence(100.0, starts) == 30.0
    assert rendermod.snap_to_sentence(7.0, []) == 7.0, "没有时间轴时原样返回，不能吞掉"


def test_timeline_seconds_snap_to_real_sentence():
    """时间线的秒数是模型估算的；必须对齐到真实句首，否则跳转会停在词中间。"""
    sentences = {"paragraphs": [{"s": 0.0, "e": 30.0, "sentences": [
        {"s": 0.0, "e": 10.0, "en": "Alpha one.", "zh": "甲"},
        {"s": 10.0, "e": 20.0, "en": "Beta two.", "zh": "乙"},
        {"s": 20.0, "e": 30.0, "en": "Gamma three.", "zh": "丙"},
    ]}]}
    summary = {"thesis": "x", "timeline": [{"title": "late", "seconds": 21.4},
                                           {"title": "early", "seconds": 9.9}]}
    _, s, *_ = rendermod.build_video_view(
        _fake_raw(), sentences, summary, {})
    assert [t["seconds"] for t in s["timeline"]] == [10.0, 20.0], s["timeline"]
    assert [t["time"] for t in s["timeline"]] == ["00:10", "00:20"]


# ---------------------------------------------------------------- 渲染降级


def _fake_raw(vid="TEST123"):
    return {
        "id": vid, "title": "A Test Video", "title_zh": "", "date": "2026-05-01",
        "month": "2026-05", "duration": 600.0, "duration_label": "10:00",
        "url": f"https://www.youtube.com/watch?v={vid}", "view_count": 1234,
        "description": "hello", "tags": ["focus", "one person business"],
        "chapters": [{"start": 0.0, "title": "Intro"}, {"start": 300.0, "title": "Main"}],
        "local_file": f"{vid}.mp4",
    }


def test_build_video_view_without_any_data():
    v, s, lines, pe, pz = rendermod.build_video_view(
        _fake_raw(), None, None, {})
    assert v.id == "TEST123"
    assert v.has_transcript is False and v.has_summary is False
    assert s == {} and lines == [] and pe == [] and pz == []
    assert v.topics, "没有总结时应退回 tags"
    assert v.search_hay and "a test video" in v.search_hay
    assert [c["time_label"] for c in v.chapters] == ["00:00", "05:00"]


def test_build_video_view_with_transcript():
    sentences = {"paragraphs": [
        {"s": 0.0, "e": 5.0, "sentences": [
            {"s": 0.0, "e": 2.0, "en": "First sentence here.", "zh": "第一句。"},
            {"s": 2.0, "e": 5.0, "en": "Second sentence here.", "zh": "第二句。"},
        ]},
    ]}
    v, s, lines, pe, pz = rendermod.build_video_view(
        _fake_raw(), sentences, None, {})
    assert v.has_transcript and v.sentence_count == 2
    assert lines[0] == {"s": 0.0, "ts": "00:00", "en": "First sentence here.", "zh": "第一句。"}
    assert pe[0]["text"] == "First sentence here. Second sentence here."
    assert pz[0]["text"] == "第一句。第二句。"
    assert s == {}


def test_string_list_fields_are_split_by_line_not_char_iterated():
    # 模型把 caveats/actions/questions 返回成一整串时，必须按行拆，
    # 绝不能迭代字符串本身（否则页面上会排成一列竖字）
    summary = {
        "thesis": "论点",
        "caveats": "局限一。\n局限二。",
        "actions": "只做一件事",
        "questions": "留给自己问的",
    }
    _, s, *_ = rendermod.build_video_view(
        _fake_raw(), None, summary, {})
    assert s["caveats"] == ["局限一。", "局限二。"], s["caveats"]
    assert s["actions"] == ["只做一件事"]
    assert s["questions"] == [{"q": "留给自己问的", "why": ""}]


def test_build_video_view_maps_frame_ids():
    frames = {"f03": {"frame_id": "f03", "timestamp": 121.0, "time_label": "02:01", "filename": "f03.jpg"}}
    summary = {
        "one_liner": "一句话", "thesis": "论点",
        "knowledge_points": [
            {"title": "kp1", "body": "内容", "frame_id": "f03", "confidence": "HIGH"},
            {"title": "kp2", "body": "内容2", "frame_id": "f99"},   # 不存在的帧 → 降级
        ],
        "timeline": [
            {"title": "t2", "seconds": 300, "frame_id": "f03"},
            {"title": "t1", "seconds": 10, "frame_id": ""},
        ],
        "overview_cards": [{"title": "c", "body": "b", "accent": "purple"}],  # 非法色 → 回落
        "quotes": [{"en": "First sentence here.", "zh": "第一句。"}],
    }
    sentences = {"paragraphs": [
        {"s": 0.0, "e": 2.0, "sentences": [
            {"s": 0.0, "e": 2.0, "en": "First sentence here.", "zh": "第一句。"}]},
        {"s": 10.0, "e": 12.0, "sentences": [
            {"s": 10.0, "e": 12.0, "en": "Something at ten seconds.", "zh": "十秒处。"}]},
        {"s": 300.0, "e": 302.0, "sentences": [
            {"s": 300.0, "e": 302.0, "en": "Something at five minutes.", "zh": "五分钟处。"}]},
    ]}

    v, s, lines, _, _ = rendermod.build_video_view(
        _fake_raw(), sentences, summary, frames)

    assert v.sentence_count == 3
    assert s["knowledge_points"][0]["frame_file"] == "f03.jpg"
    assert s["knowledge_points"][0]["frame_time"] == 121.0
    assert s["knowledge_points"][0]["conf_label"] == "高置信"
    assert s["knowledge_points"][1]["frame_file"] == "", "无效 frame_id 不该渲染出坏图链接"
    assert [t["title"] for t in s["timeline"]] == ["t1", "t2"], "时间线必须按秒排序"
    assert s["overview_cards"][0]["accent"] == "accent", "非法 accent 要回落到默认"
    assert s["quotes"][0]["seconds"] == 0.0, "摘录应定位回时间轴"
    assert v.has_summary is True
    assert v.one_liner == "一句话"


def test_build_video_view_drops_empty_summary():
    v, s, *_ = rendermod.build_video_view(
        _fake_raw(), None, {"knowledge_points": [], "timeline": []}, {})
    assert s == {} and v.has_summary is False


# ---------------------------------------------------------------- runner


def main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    passed, failed = 0, []
    for name, fn in tests:
        try:
            fn()
            passed += 1
            print(f"  ok   {name}")
        except Exception as exc:  # noqa: BLE001
            failed.append((name, exc, traceback.format_exc()))
            print(f"  FAIL {name}: {exc}")
    print(f"\n{passed}/{len(tests)} passed")
    for name, _, tb in failed:
        print(f"\n=== {name} ===\n{tb}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
