#!/usr/bin/env python3
"""用无头 Chrome 真实打开站点，检查渲染、报错和布局。

跑法：
    python3 tests/browser_check.py                 # 默认测 3 个页面
    python3 tests/browser_check.py --all           # 测全部详情页
    python3 tests/browser_check.py --keep          # 保留截图别删

产出截图放在 /tmp/dk-shots/，**必须自己打开看一眼**——脚本只能证明没崩，
证明不了好看。所以除了断言，它还会打印每个页面的体积、DOM 关键节点数，
并把 console 里的报错原样列出来。

注意：无头环境里 YouTube iframe 基本加载不出来，播放器会走 8 秒超时降级路径，
console 里出现 youtube.com 相关的网络错误是**预期的**，不算失败。
"""
from __future__ import annotations

import argparse
import http.server
import json
import os
import re
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
SHOTS = Path("/tmp/dk-shots")
CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")

# 每次运行换一个 profile 目录：localStorage（主题偏好等）会跨运行残留，
# 曾让「暗色截图」读到上一次存下的浅色偏好，拍出浅色图却报通过
RUN_NONCE = f"{time.time():.0f}-{os.getpid()}"

# 这些错误在无头 + 无外网环境里是预期的，不作为失败
EXPECTED_NOISE = (
    "youtube.com", "ytimg.com", "google.com", "googlevideo.com",
    "iframe_api", "www-widgetapi", "ERR_NAME_NOT_RESOLVED", "ERR_INTERNET_DISCONNECTED",
    "ERR_CONNECTION", "ERR_BLOCKED_BY_CLIENT", "Failed to load resource",
    "net::ERR_", "Refused to", "CORS",
)

VIEWPORTS = [
    ("desktop", 1440, 900),
    ("laptop", 1280, 800),
    ("tablet", 820, 1180),
    ("mobile", 390, 844),
]


# ------------------------------------------------------------------ 本地服务


def start_server(root: Path) -> tuple[Any, int]:
    # 必须是多线程的：浏览器会并行拉 CSS/JS/图片，单线程会互相等死
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a: Any, **kw: Any) -> None:
            super().__init__(*a, directory=str(root), **kw)

        def log_message(self, *a: Any) -> None:
            pass

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

        def handle_error(self, request, client_address):
            # 浏览器截图完就把连接掐了，BrokenPipe/ConnectionReset 是正常现象，别刷屏
            exc = sys.exc_info()[1]
            if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
                return
            super().handle_error(request, client_address)

    httpd = Server(("127.0.0.1", 0), Quiet)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


# ------------------------------------------------------------------ Chrome


def run_chrome(args: list[str], timeout: int = 120) -> tuple[int, str, str]:
    """返回 (returncode, stdout, stderr)。stdout/stderr 必须分开：
    --dump-dom 把 DOM 写到 stdout，而 Chrome 自己的噪音全在 stderr。"""
    cmd = [str(CHROME), "--headless=new", "--disable-gpu", "--no-sandbox",
           "--no-first-run", "--no-default-browser-check",
           "--disable-extensions", "--mute-audio", *args]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or ""), (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "", "TIMEOUT"


def screenshot(url: str, out: Path, w: int, h: int, *, wait_ms: int = 12000) -> bool:
    out.parent.mkdir(parents=True, exist_ok=True)
    profile = out.parent / f".profile-{RUN_NONCE}-{out.stem}"
    rc, _, _ = run_chrome([
        f"--user-data-dir={profile}",
        f"--window-size={w},{h}",
        f"--virtual-time-budget={wait_ms}",
        "--hide-scrollbars",
        "--force-device-scale-factor=1",
        f"--screenshot={out}",
        url,
    ])
    return rc == 0 and out.exists() and out.stat().st_size > 2000


def dump_dom(url: str, *, wait_ms: int = 12000) -> tuple[str, str]:
    """返回 (dom, console日志)。dom 只取 stdout，日志取 stderr。"""
    profile = SHOTS / f".profile-{RUN_NONCE}-dom"
    rc, out, err = run_chrome([
        f"--user-data-dir={profile}",
        "--window-size=1440,900",
        f"--virtual-time-budget={wait_ms}",
        "--enable-logging=stderr", "--v=0",
        "--dump-dom",
        url,
    ], timeout=180)
    return (out if rc != 124 else ""), err


def capture_slices(port: int, rel: str, prefix: str, *, w: int = 1440, h: int = 1100,
                   max_slices: int = 14) -> list[Path]:
    """整页视觉复查：把目标页装进同源 iframe，滚 iframe 而不是滚窗口。

    直接用 --screenshot 拍超高窗口会画出没上色的黑块（headless 的已知毛病），
    用 hash 滚动也不可靠。iframe 方案里外层文档永远只有一个视口高，不会踩到。
    文档总高度由 wrapper 写进 <title>，先 dump 一次读出来，决定切几片。
    """
    wrapper = SITE / "_capture.html"
    wrapper.write_text(
        "<!doctype html><meta charset=utf-8><style>html,body{margin:0;overflow:hidden}"
        f"iframe{{width:{w}px;height:{h}px;border:0;display:block}}</style>"
        f"<iframe id=f src='{rel}'></iframe>"
        "<script>var off=parseInt(new URLSearchParams(location.search).get('off')||'0',10);"
        "var f=document.getElementById('f');"
        "f.addEventListener('load',function(){var d=f.contentDocument;"
        "f.contentWindow.scrollTo(0,off);"
        "document.title=String(d.documentElement.scrollHeight);});</script>",
        encoding="utf-8",
    )
    outs: list[Path] = []
    try:
        probe, _ = dump_dom(f"http://127.0.0.1:{port}/_capture.html?off=0", wait_ms=10000)
        m = re.search(r"<title>(\d+)</title>", probe)
        total = int(m.group(1)) if m else h * 3
        step = h - 140
        for off in range(0, min(total, max_slices * step) + 1, step):
            out = SHOTS / f"{prefix}-slice{off:05d}.png"
            if screenshot(f"http://127.0.0.1:{port}/_capture.html?off={off}", out, w, h, wait_ms=10000):
                outs.append(out)
    finally:
        wrapper.unlink(missing_ok=True)
    return outs


def console_errors(log_text: str) -> list[str]:
    """只看真正的页面 console 输出。

    Chrome 在 macOS 无头模式下自己会吐一大堆 ERROR（CVDisplayLink、task_policy_set、
    GCM 注册、WebGPU、sqlite flush…），那些跟页面无关。页面自己的 console 消息
    一律带 `:CONSOLE(行号)` 前缀，只认这个。
    """
    errs: list[str] = []
    for line in log_text.splitlines():
        if ":CONSOLE(" not in line:
            continue
        if any(n.lower() in line.lower() for n in EXPECTED_NOISE):
            continue
        errs.append(line.strip()[:240])
    return errs


def chrome_noise_count(log_text: str) -> int:
    """统计被过滤掉的 Chrome 自身噪音条数，方便确认过滤器没把真问题也吞掉。"""
    return sum(1 for ln in log_text.splitlines()
               if ":CONSOLE(" not in ln and re.search(r":(ERROR|WARNING):", ln))


def png_pixel(path: Path, x: int, y: int) -> tuple[int, int, int] | None:
    """纯 Python 解 8-bit PNG（color type 2/6）取一个像素。

    暗色截图光看文件存在没用：主题被系统事件抢回去时页面会整套变浅，
    而 DOM/截图断言全绿。取 (18,17) 这种导航栏底色点，暗色应接近 (18,17,16)。
    """
    import zlib
    data = path.read_bytes()
    pos, idat = 8, b""
    while pos < len(data):
        (ln,) = struct.unpack(">I", data[pos:pos + 4])
        typ, chunk = data[pos + 4:pos + 8], data[pos + 8:pos + 8 + ln]
        if typ == b"IHDR":
            w, h, _bd, ct = struct.unpack(">IIBB", chunk[:10])
        elif typ == b"IDAT":
            idat += chunk
        pos += 12 + ln
    ch = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ct]
    stride = w * ch
    raw = zlib.decompress(idat)
    prev, i = bytearray(stride), 0
    for _y in range(h):
        f = raw[i]; i += 1
        line = bytearray(raw[i:i + stride]); i += stride
        if f == 1:
            for xx in range(ch, stride):
                line[xx] = (line[xx] + line[xx - ch]) & 255
        elif f == 2:
            for xx in range(stride):
                line[xx] = (line[xx] + prev[xx]) & 255
        elif f == 3:
            for xx in range(stride):
                line[xx] = (line[xx] + ((line[xx - ch] if xx >= ch else 0) + prev[xx]) // 2) & 255
        elif f == 4:
            for xx in range(stride):
                a = line[xx - ch] if xx >= ch else 0
                b = prev[xx]
                c = prev[xx - ch] if xx >= ch else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[xx] = (line[xx] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        if _y == y:
            o = x * ch
            return tuple(line[o:o + 3])
        prev = line
    return None


# ------------------------------------------------------------------ DOM 断言


def count(dom: str, pattern: str) -> int:
    return len(re.findall(pattern, dom))


def check_js_ran(dom: str) -> list[str]:
    """证明 app.js 真的执行了，而不只是 HTML 静态正确。

    注意不能用 data-theme 判断：默认偏好是「跟随系统」，applyTheme("system") 会
    **移除**这个属性，所以没有它才是对的。真正的判据是 selectTab() 会给所有面板
    写 data-active —— 模板里只有第一个面板带这个属性，其余的是 JS 补上去的。
    """
    bad: list[str] = []
    if 'data-active="false"' not in dom:
        bad.append('没有任何 data-active="false" 面板，说明 selectTab() 没跑（app.js 未加载或报错）')
    return bad


def check_theme_override(dom: str) -> list[str]:
    """?theme=dark 必须真的把 data-theme="dark" 写到 <html> 上。"""
    if 'data-theme="dark"' not in dom[:800]:
        return ["?theme=dark 没生效，<html> 上没有 data-theme=\"dark\""]
    return []


def check_index(dom: str) -> list[str]:
    # 首页没有面板，用不了 data-active 判据；JS 是否执行由 check_theme_override 单独验
    bad: list[str] = []
    if count(dom, r'class="vcard"') < 1 and count(dom, r'class="feature"') < 1:
        bad.append("首页没有任何视频卡片")
    if 'id="videoGrid"' not in dom:
        bad.append("缺 #videoGrid，筛选 JS 不会启动")
    if 'id="searchInput"' not in dom:
        bad.append("缺搜索框")
    if count(dom, r'<img[^>]+assets/cover/') < 1:
        bad.append("首页没有封面图")
    if " Undefined" in dom or ">{{" in dom or "{%" in dom:
        bad.append("模板没渲染干净（出现 Jinja 残留或 Undefined）")
    return bad


def check_video(dom: str) -> list[str]:
    bad: list[str] = check_js_ran(dom)
    for need, label in [
        (r'id="ytPlayer"', "播放器挂载点"),
        (r'data-tab="summary"', "图文总结标签"),
        (r'data-tab="transcript"', "中英对照标签"),
        (r'data-tab="en"', "英文原稿标签"),
        (r'data-tab="zh"', "中文稿标签"),
        (r'id="transcriptList"', "字幕列表"),
        (r'id="modeSelect"', "显示模式选择"),
    ]:
        if not re.search(need, dom):
            bad.append(f"缺 {label} ({need})")
    lines = count(dom, r'class="line"')
    if lines < 50:
        bad.append(f"字幕行只有 {lines} 条，疑似没渲染出来")
    if count(dom, r'class="zh"') < 10:
        bad.append("几乎没有中文译文")
    if not re.search(r'class="kp"', dom) and "总结尚未生成" not in dom:
        bad.append("图文总结区既没有知识点、也没有缺数据的提示")
    zh = re.findall(r'class="zh">([^<]{4,})<', dom)
    if zh and not any(re.search(r"[一-鿿]", z) for z in zh):
        bad.append("class=zh 里没有中文字符")
    if "Undefined" in dom or "{%" in dom or ">{{" in dom:
        bad.append("模板没渲染干净（出现 Jinja 残留或 Undefined）")
    if re.search(r'src="\.\./assets/frames/[^"]+/\.jpg"', dom):
        bad.append("有空的 frame 文件名")
    return bad


def check_fallback_overlay(port: int, rel: str) -> list[str]:
    """降级罩初始态的 computed display 必须是 none。

    作者 CSS 的 .player-fallback{display:grid} 会赢过 UA 的 [hidden]{display:none}——
    少写一条 [hidden] 兜底，「播放器未能加载」的罩子就会从首字节起盖在播放器上，
    而且 DOM 断言看不出来（hidden 属性一直都在），只能读 computed style。
    办法：同源 iframe  wrapper 里 getComputedStyle。
    """
    wrapper = SITE / "_fbprobe.html"
    wrapper.write_text(
        "<!doctype html><meta charset=utf-8><title>pending</title>"
        f"<iframe id=\"f\" src=\"/{rel}\" style=\"width:100%;height:800px;border:0\"></iframe>"
        "<script>var f=document.getElementById('f');"
        "function probe(){try{var d=f.contentDocument;if(!d)return;"
        "var el=d.getElementById('playerFallback');if(!el)return;"
        "document.title=JSON.stringify({hidden:el.hasAttribute('hidden'),"
        "display:f.contentWindow.getComputedStyle(el).display});}catch(e){}}"
        "f.onload=function(){probe();setTimeout(probe,300);};</script>",
        encoding="utf-8")
    try:
        _, out, _ = run_chrome([
            f"--user-data-dir={SHOTS / f'.profile-{RUN_NONCE}-fb'}",
            "--window-size=1200,900",
            "--virtual-time-budget=4000",
            "--dump-dom",
            f"http://127.0.0.1:{port}/_fbprobe.html",
        ])
    finally:
        wrapper.unlink(missing_ok=True)
    m = re.search(r"<title>(\{[^<]*\})</title>", out)
    if not m:
        return ["降级罩探针没回传状态（wrapper title 仍是 pending）"]
    state = json.loads(m.group(1))
    if state.get("hidden") is not True or state.get("display") != "none":
        return [f"降级罩初始态就是可见的（会盖住播放器）：{state}"]
    return []


# ------------------------------------------------------------------ 主流程


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="测全部详情页")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--slices", action="store_true",
                    help="额外拍整页切片，供人工视觉复查")
    ap.add_argument("--limit", type=int, default=3)
    args = ap.parse_args()

    if not CHROME.exists():
        print(f"找不到 Chrome: {CHROME}")
        return 2
    if not (SITE / "index.html").exists():
        print("站点还没构建，先跑 python3 run.py build")
        return 2

    SHOTS.mkdir(parents=True, exist_ok=True)
    httpd, port = start_server(SITE)
    base = f"http://127.0.0.1:{port}"
    failures: list[str] = []
    t0 = time.time()

    try:
        # ---- 1. 所有页面和资源都返回 200 ----
        pages = ["index.html"] + sorted(
            f"v/{p.name}" for p in (SITE / "v").glob("*.html"))
        assets = sorted(str(p.relative_to(SITE)) for p in SITE.rglob("*")
                        if p.is_file() and p.suffix in {".css", ".js", ".svg", ".json"})
        targets = pages if args.all else pages[:1 + args.limit]
        targets = targets + assets

        print(f"── HTTP 检查 {len(targets)} 个 URL ──")
        bad_http = 0
        for rel in targets:
            try:
                with urllib.request.urlopen(f"{base}/{rel}", timeout=10) as r:
                    if r.status != 200:
                        failures.append(f"HTTP {r.status} {rel}")
                        bad_http += 1
            except urllib.error.URLError as exc:
                failures.append(f"打不开 {rel}: {exc}")
                bad_http += 1
        print(f"   {len(targets) - bad_http}/{len(targets)} 个 200")

        # ---- 2. 首页 DOM + 截图 ----
        print("\n── 首页 ──")
        dom, log = dump_dom(f"{base}/index.html")
        if not dom:
            failures.append("首页 --dump-dom 返回空")
        else:
            problems = check_index(dom)
            failures += [f"index.html: {p}" for p in problems]
            print(f"   DOM {len(dom):,} 字节 / 头条 {count(dom, r'class=\"feature\"')} 张 + "
                  f"卡片 {count(dom, r'class=\"vcard\"')} 张 / "
                  f"封面 {count(dom, r'assets/cover/')} 个 / 筛选芯片 {count(dom, r'class=\"chip\"')} 个")
            for p in problems:
                print(f"   ✗ {p}")
            if not problems:
                print("   ✓ DOM 断言全过")
        for err in console_errors(log):
            failures.append(f"index.html console: {err}")
            print(f"   ✗ console: {err}")
        if not console_errors(log):
            print(f"   ✓ 页面 console 干净（已过滤 {chrome_noise_count(log)} 条 Chrome 自身噪音）")

        # JS 真的跑起来了吗：?theme=dark 必须落到 <html data-theme="dark">
        tdom, _ = dump_dom(f"{base}/index.html?theme=dark", wait_ms=6000)
        tproblems = check_theme_override(tdom) if tdom else ["?theme=dark 页面 dump 为空"]
        failures += [f"index.html: {p}" for p in tproblems]
        print(f"   {'✗ ' + tproblems[0] if tproblems else '✓ app.js 已执行（?theme=dark 生效）'}")

        # 降级罩初始态必须不可见（CSS 级联问题，DOM 断言看不出，得读 computed style）
        fbproblems = check_fallback_overlay(port, pages[1])
        failures += [f"{pages[1]}: {p}" for p in fbproblems]
        print(f"   {'✗ ' + fbproblems[0] if fbproblems else '✓ 降级罩初始态隐藏（computed display: none）'}")

        for name, w, h in VIEWPORTS:
            ok = screenshot(f"{base}/index.html", SHOTS / f"index-{name}.png", w, h)
            print(f"   {'✓' if ok else '✗'} 截图 index-{name}.png {w}×{h}")
            if not ok:
                failures.append(f"index-{name} 截图失败")

        # ---- 3. 详情页 ----
        detail_pages = pages[1:] if args.all else pages[1:1 + args.limit]
        for rel in detail_pages:
            vid = Path(rel).stem
            print(f"\n── 详情页 {vid} ──")
            dom, log = dump_dom(f"{base}/{rel}")
            if not dom:
                failures.append(f"{rel}: --dump-dom 返回空")
                continue
            problems = check_video(dom)
            failures += [f"{rel}: {p}" for p in problems]
            print(f"   DOM {len(dom):,} 字节 / 字幕 {count(dom, r'class=\"line\"')} 行 / "
                  f"知识点 {count(dom, r'class=\"kp\"')} 条 / 时间线 {count(dom, r'class=\"tl-item\"')} 项 / "
                  f"章节 {count(dom, r'data-chap=')} 个")
            for p in problems:
                print(f"   ✗ {p}")
            if not problems:
                print("   ✓ DOM 断言全过")
            page_errs = console_errors(log)
            for err in page_errs:
                failures.append(f"{rel} console: {err}")
                print(f"   ✗ console: {err}")
            if not page_errs:
                print(f"   ✓ 页面 console 干净（已过滤 {chrome_noise_count(log)} 条 Chrome 自身噪音）")

            for name, w, h in [("desktop", 1440, 900), ("mobile", 390, 844)]:
                ok = screenshot(f"{base}/{rel}", SHOTS / f"{vid}-{name}.png", w, h)
                print(f"   {'✓' if ok else '✗'} 截图 {vid}-{name}.png")
                if not ok:
                    failures.append(f"{vid}-{name} 截图失败")

            # 标签页深链：#transcript 应当被 JS 激活
            dom2, _ = dump_dom(f"{base}/{rel}#transcript", wait_ms=9000)
            if dom2:
                m = re.search(r'id="panel-transcript"[^>]*data-active="(\w+)"', dom2)
                if not m:
                    m = re.search(r'data-active="(\w+)"[^>]*id="panel-transcript"', dom2)
                if m and m.group(1) != "true":
                    failures.append(f"{rel}: #transcript 深链没有激活对应面板")
                    print("   ✗ 深链 #transcript 未激活")
                else:
                    print("   ✓ 深链 #transcript 激活正常")

        # ---- 4. 整页切片（视觉复查用，--slices 开启） ----
        if args.slices and detail_pages:
            print("\n── 整页切片 ──")
            vid = Path(detail_pages[0]).stem
            slices = capture_slices(port, detail_pages[0], vid)
            print(f"   {vid}: {len(slices)} 片 → {[p.name for p in slices][:3]}…")
            idx_slices = capture_slices(port, "index.html", "index", max_slices=4)
            print(f"   index: {len(idx_slices)} 片")
            if not slices:
                failures.append("整页切片一个都没拍出来")

        # ---- 5. 暗色主题（app.js 支持 ?theme=dark 强制指定） ----
        print("\n── 暗色主题 ──")
        for label, rel in [("index", "index.html")] + [
                (Path(p).stem, p) for p in detail_pages[:1]]:
            ok = screenshot(f"{base}/{rel}?theme=dark", SHOTS / f"{label}-dark.png", 1440, 1400)
            print(f"   {'✓' if ok else '✗'} {label}-dark.png")
            if not ok:
                failures.append(f"{label}-dark 截图失败")
                continue
            # 文件存在不等于暗色生效：主题被系统事件抢回去时整页变浅而断言全绿
            px = png_pixel(SHOTS / f"{label}-dark.png", 18, 17)
            if px is None or len(px) != 3 or sum(px) > 300:
                failures.append(f"{label}-dark.png 导航栏像素 {px} 偏浅，暗色没真生效")
                print(f"   ✗ 像素 {px} 偏浅，暗色未生效")
            else:
                print(f"   ✓ 像素 {px} 确认暗色")

    finally:
        httpd.shutdown()

    print(f"\n{'=' * 60}")
    if failures:
        print(f"✗ {len(failures)} 个问题（用时 {time.time() - t0:.0f}s）")
        for f in failures:
            print("  - " + f)
    else:
        print(f"✓ 全部通过（用时 {time.time() - t0:.0f}s）")
    print(f"截图在 {SHOTS}/")
    if not args.keep:
        print("（--keep 可以保留 profile 目录；截图本身不会删）")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
