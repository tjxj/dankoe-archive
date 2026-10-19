#!/usr/bin/env python3
"""Dan Koe 视频学习站 —— 流水线入口。

用法：
    python3 run.py catalog      扫描视频目录 + 抓元数据 → data/catalog.json
    python3 run.py frames       抽封面和关键帧
    python3 run.py translate    英文字幕 → 中文（句子级时间轴对齐）
    python3 run.py summarize    Qwen-VL 分段分析 + 合成图文总结
    python3 run.py build        渲染静态站到 site/
    python3 run.py all          以上全部
    python3 run.py serve        本地起服务预览

    --only <id,id>   只处理指定视频
    --limit N        只处理前 N 个（调试用）
    --force          忽略缓存指纹，强制重跑该阶段
    --stage-xxx      见各阶段参数

所有阶段都是幂等增量的：新视频丢进 video_dir 后重跑 `all`，只会处理新增的那几个。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pipeline import catalog as catalogmod
from pipeline import frames as framesmod
from pipeline import srt as srtmod
from pipeline import summarize as summarizemod
from pipeline import translate as translatemod
from pipeline.common import (
    Log,
    StageState,
    ensure_dir,
    hash_file,
    hash_obj,
    load_config,
    read_json,
    seconds_to_label,
    video_data_dir,
    write_json,
)


def select_videos(cfg: dict[str, Any], args: argparse.Namespace) -> list[dict[str, Any]]:
    cat = catalogmod.load_catalog(cfg)
    videos = list(cat.get("videos") or [])
    if args.only:
        wanted = {x.strip() for x in args.only.split(",") if x.strip()}
        videos = [v for v in videos if v["id"] in wanted]
    if args.limit:
        videos = videos[: args.limit]
    return videos


# ---------------------------------------------------------------- stages


def stage_catalog(cfg: dict[str, Any], args: argparse.Namespace, log: Log) -> int:
    catalogmod.build_catalog(cfg, log, fetch=not args.no_fetch)
    return 0


def stage_frames(cfg: dict[str, Any], args: argparse.Namespace, log: Log) -> int:
    fcfg = cfg["frames"]
    videos = select_videos(cfg, args)
    root = Path(cfg["video_dir"])
    ok = 0
    for v in videos:
        vdir = ensure_dir(video_data_dir(cfg, v["id"]))
        frames_dir = ensure_dir(vdir / "frames")
        state = StageState(vdir / "state.json")
        fp = hash_obj_frames(v, fcfg)
        cover = vdir / "cover.jpg"
        if not args.force and state.is_current("frames", fp) and cover.exists():
            log.info(f"frames 跳过（未变）: {v['id']}")
            ok += 1
            continue
        got_cover = framesmod.fetch_cover(v["id"], cover, log, preferred_url=v.get("thumbnail_best") or v.get("thumbnail") or "")
        mp4 = root / v["local_file"]
        frames = (
            framesmod.extract_keyframes(
                mp4, frames_dir,
                duration=float(v["duration"]),
                count=int(fcfg["keyframe_count"]),
                width=int(fcfg["keyframe_width"]),
                quality=int(fcfg["jpeg_quality"]),
                log=log, force=args.force,
            )
            if mp4.exists()
            else []
        )
        framesmod.write_storyboard(frames, vdir / "storyboard.json")
        state.mark(
            "frames", fp,
            cover=got_cover, frames=len(frames),
            video_present=mp4.exists(),
        )
        log.info(f"frames 完成: {v['id']} cover={'✓' if got_cover else '✗'} frames={len(frames)}")
        ok += 1
    log.info(f"frames: {ok}/{len(videos)}")
    return 0 if ok == len(videos) else 1


def hash_obj_frames(v: dict[str, Any], fcfg: dict[str, Any]) -> str:
    return hash_obj({"fp": v.get("meta_fingerprint"), "params": fcfg, "v": "frames-1"})


def _load_sentences(cfg: dict[str, Any], v: dict[str, Any], log: Log):
    """读 srt → 校验完整性 → 断句 → 分段。"""
    root = Path(cfg["video_dir"])
    srt_path = root / v["srt_file"] if v.get("srt_file") else None
    if not srt_path or not srt_path.exists():
        return None, None, {"ok": False, "reasons": ["无字幕文件"], "metrics": {}}
    cues = srtmod.parse_srt_file(srt_path)
    report = srtmod.completeness(cues, float(v["duration"]), cfg["srt_completeness"])
    sentences = srtmod.build_sentences(cues)
    paragraphs = srtmod.build_paragraphs(sentences)
    return sentences, paragraphs, report


def stage_translate(cfg: dict[str, Any], args: argparse.Namespace, log: Log) -> int:
    from pipeline.llm import build_client

    videos = select_videos(cfg, args)
    client = build_client(cfg, "translate", log=log)
    done = failed = skipped = 0
    for v in videos:
        vdir = ensure_dir(video_data_dir(cfg, v["id"]))
        state = StageState(vdir / "state.json")
        sentences, paragraphs, report = _load_sentences(cfg, v, log)
        if sentences is None:
            log.warn(f"跳过（无字幕）: {v['id']}")
            state.mark("subtitle_check", "none", status="missing")
            skipped += 1
            continue

        write_json(vdir / "subtitle-check.json", report)
        if not report["ok"]:
            log.warn(f"{v['id']} 字幕完整性未通过：{report['reasons']}")
            state.mark("subtitle_check", hash_file(Path(cfg['video_dir']) / v['srt_file']), status="incomplete",
                       reasons=report["reasons"])
        else:
            state.mark("subtitle_check", hash_file(Path(cfg['video_dir']) / v['srt_file']), status="ok",
                       **report["metrics"])

        fp = translatemod.fingerprint(
            {**v, "_srt_fingerprint": hash_file(Path(cfg["video_dir"]) / v["srt_file"])}, cfg
        )
        if not args.force and state.is_current("translate", fp):
            log.info(f"translate 跳过（未变）: {v['id']}")
            skipped += 1
            continue

        t0 = time.time()
        try:
            stats = translatemod.translate_video(
                video=v, sentences=sentences, client=client, cfg=cfg, log=log,
            )
        except Exception as exc:  # noqa: BLE001
            log.error(f"translate 失败 {v['id']}: {exc}")
            state.mark("translate", fp, status="error", error=str(exc)[:300])
            failed += 1
            continue
        translatemod.write_outputs(v, sentences, paragraphs, vdir, stats, log)
        state.mark("translate", fp, **{k: val for k, val in stats.items() if k != "missing_indices"},
                   missing=len(stats.get("missing_indices") or []), seconds=round(time.time() - t0, 1))
        done += 1

    log.info(f"translate: 新完成 {done} / 跳过 {skipped} / 失败 {failed}  —— API 用量 {client.usage.as_dict()}")
    return 1 if failed else 0


def stage_summarize(cfg: dict[str, Any], args: argparse.Namespace, log: Log) -> int:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from pipeline.llm import build_client

    videos = select_videos(cfg, args)
    vision = build_client(cfg, "vision", log=log)
    synth = build_client(cfg, "synthesize", log=log)

    # 先串行决定要做什么（只读本地文件 + 比指纹，很便宜），再把 API 活并行发出去。
    # 每个视频有独立的 state.json，write_json 又是原子的，所以线程之间不会互相踩。
    todo: list[dict[str, Any]] = []
    skipped = 0
    for v in videos:
        vdir = video_data_dir(cfg, v["id"])
        state = StageState(vdir / "state.json")
        sb = read_json(vdir / "storyboard.json", {"frames": []})
        frames = [
            framesmod.Frame(
                frame_id=f["frame_id"], timestamp=f["timestamp"], time_label=f["time_label"],
                filename=f["filename"], index=f["index"],
            )
            for f in sb.get("frames", [])
        ]
        sentences_data = read_json(vdir / "sentences.json", None)
        if not sentences_data:
            log.warn(f"summarize 跳过（还没翻译）: {v['id']}")
            skipped += 1
            continue
        sentences = [
            srtmod.Sentence(
                start=s["s"], end=s["e"], text=s["en"], cue_start=0, cue_end=0, zh=s.get("zh", "")
            )
            for p in sentences_data.get("paragraphs", [])
            for s in p["sentences"]
        ]
        fp = summarizemod.fingerprint(v, cfg, frames, len(sentences))
        if not args.force and state.is_current("summarize", fp):
            log.info(f"summarize 跳过（未变）: {v['id']}")
            skipped += 1
            continue
        todo.append({"v": v, "vdir": vdir, "state": state, "frames": frames,
                     "sentences": sentences, "fp": fp})

    log.info(f"summarize: 待处理 {len(todo)} / 跳过 {skipped}")
    done = failed = 0
    if todo:
        workers = max(1, min(int((cfg.get("summarize") or {}).get("video_concurrency", 3)), len(todo)))
        log.info(f"  视频级并发 {workers}（每个视频内部还会并行跑分段视觉分析）")

        def work(item: dict[str, Any]) -> bool:
            vid = item["v"]["id"]
            t0 = time.time()
            try:
                analysis = summarizemod.summarize_video(
                    video=item["v"], sentences=item["sentences"], frames=item["frames"],
                    frames_dir=item["vdir"] / "frames", vision=vision, synth=synth, cfg=cfg, log=log,
                )
            except Exception as exc:  # noqa: BLE001
                log.error(f"summarize 失败 {vid}: {exc}")
                item["state"].mark("summarize", item["fp"], status="error", error=str(exc)[:300])
                return False
            write_json(item["vdir"] / "summary.json", analysis)
            item["state"].mark(
                "summarize", item["fp"],
                knowledge_points=len(analysis.get("knowledge_points") or []),
                timeline=len(analysis.get("timeline") or []),
                segments_ok=(analysis.get("_meta") or {}).get("segments_ok"),
                seconds=round(time.time() - t0, 1),
            )
            log.info(f"summarize 完成: {vid} ({time.time() - t0:.0f}s)")
            return True

        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [ex.submit(work, it) for it in todo]
            for fut in as_completed(futures):
                try:
                    if fut.result():
                        done += 1
                    else:
                        failed += 1
                except Exception as exc:  # noqa: BLE001
                    log.error(f"summarize 线程异常: {exc}")
                    failed += 1

    log.info(f"summarize: 新完成 {done} / 跳过 {skipped} / 失败 {failed}")
    log.info(f"  vision 用量 {vision.usage.as_dict()}")
    log.info(f"  synth  用量 {synth.usage.as_dict()}")
    return 1 if failed else 0


def stage_build(cfg: dict[str, Any], args: argparse.Namespace, log: Log) -> int:
    from pipeline import render

    return render.build_site(cfg, log, only=args.only)


def stage_serve(cfg: dict[str, Any], args: argparse.Namespace, log: Log) -> int:
    import http.server
    import functools
    import socketserver

    root = cfg["site_dir_path"]
    if not (root / "index.html").exists():
        log.error(f"站点未构建：{root}/index.html 不存在，先跑 build")
        return 1
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    socketserver.TCPServer.allow_reuse_address = True
    port = args.port
    with socketserver.TCPServer(("127.0.0.1", port), handler) as httpd:
        log.info(f"预览: http://127.0.0.1:{port}/  (root={root})")
        print(f"http://127.0.0.1:{port}/")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            log.info("已停止")
    return 0


STAGES = {
    "catalog": stage_catalog,
    "frames": stage_frames,
    "translate": stage_translate,
    "summarize": stage_summarize,
    "build": stage_build,
    "serve": stage_serve,
}
ALL_STAGES = ["catalog", "frames", "translate", "summarize", "build"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stage", nargs="?", default="all", choices=[*STAGES, "all"])
    ap.add_argument("--config", default="", help="config.json 路径")
    ap.add_argument("--only", default="", help="只处理这些视频 ID，逗号分隔")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 个")
    ap.add_argument("--force", action="store_true", help="忽略指纹，强制重跑")
    ap.add_argument("--no-fetch", action="store_true", help="catalog 阶段不联网抓元数据")
    ap.add_argument("--port", type=int, default=8765, help="serve 端口")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    cfg = load_config(Path(args.config) if args.config else None)
    log = Log(args.stage, quiet=args.quiet)
    stages = ALL_STAGES if args.stage == "all" else [args.stage]
    t0 = time.time()
    rc = 0
    for name in stages:
        log.stage = name
        log.info(f"── 开始 {name} ──")
        try:
            code = STAGES[name](cfg, args, log)
        except Exception as exc:  # noqa: BLE001
            log.error(f"{name} 异常: {exc}")
            import traceback

            traceback.print_exc()
            return 2
        if code:
            rc = code
            log.warn(f"{name} 返回非零 ({code})，继续后续阶段")
    log.stage = "run"
    log.info(f"── 全部完成，用时 {seconds_to_label(time.time() - t0)} ──")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
