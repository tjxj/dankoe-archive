# Dan Koe 视频学习档案

把 YouTube 频道的长视频变成可检索、可对照阅读、可跳转的学习材料。

每个视频产出四层内容：

| 层 | 内容 | 来源 |
| --- | --- | --- |
| 封面 + 入口 | 官方封面、YouTube 链接、章节 | YouTube |
| 中英对照 | 逐句时间轴对齐，点任意一句跳到视频那一刻 | 自动字幕 + 模型翻译 |
| 英文原稿 / 中文稿 | 按段落连续阅读，时间标记可点击 | 同上 |
| 图文总结 | 核心主张、知识点×画面、时间线、框架、摘录、行动清单 | 视觉模型看关键帧 + 语言模型合成 |

站点是**纯静态**的：无构建步骤、无 npm、无 JS 框架。产出 `site/` 可以直接双击打开，
也可以用 `python3 run.py serve` 起本地服务。

---

## 快速开始

```bash
export OPENAI_API_KEY=...                        # 必填
export OPENAI_API_BASE=https://api.siliconflow.cn/v1   # 可选，默认就是这个

python3 run.py all          # catalog → frames → translate → summarize → build
python3 run.py serve        # http://127.0.0.1:8765/
python3 tests/test_pipeline.py
python3 tests/browser_check.py --all   # 无头 Chrome 真机验收：DOM 断言、console、
                                       # 降级罩 computed style、暗色像素级断言、四档视口截图
python3 tests/browser_check.py            # 无头 Chrome 真开站点：DOM 断言 / console / 多分辨率截图 / 暗色像素校验
python3 tests/browser_check.py --all      # 逐个测全部详情页（约 8 分钟）
```

依赖：Python 3.11+、`jinja2`、`requests`，以及命令行有 `ffmpeg` / `ffprobe` / `yt-dlp`。

`browser_check.py` 的截图落在 `/tmp/dk-shots/`，**要真的打开看**：断言只能证明没崩，证明不了好看。
除常规断言外它还有两条防静默失败的探针：同源 iframe 读降级罩的 computed style（作者 `display` 会赢过 `[hidden]`），
以及把暗色截图解码取像素比颜色（主题被系统事件抢回去时整页变浅而 DOM 断言全绿）。

---

## 加新视频（最常用的一条）

**1. 下载视频和字幕到 `video_dir`**（默认 `/Volumes/T7/download/dankoe`）

文件名必须符合约定，`catalog` 阶段靠它解析日期和 ID：

```
2026-10-15 - The Video Title Here [dQw4w9WgXcQ].mp4
└─日期─────┘ └─标题──────────────┘ └─YouTube ID─┘
```

用 yt-dlp 一条命令就能下成这个格式：

```bash
yt-dlp \
  --download-archive /Volumes/T7/download/dankoe/.archive.txt \
  -o "/Volumes/T7/download/dankoe/%(upload_date>%Y-%m-%d)s - %(title)s [%(id)s].%(ext)s" \
  --dateafter 20261001 --break-on-reject \
  "https://www.youtube.com/@DanKoeTalks/videos"
```

字幕**单独跑一遍**，别和视频混在一条命令里（字幕请求容易 429，一旦 429 整个 item 会被跳过，
连 .mp4 都不写）：

```bash
yt-dlp --skip-download --write-subs --write-auto-subs --sub-langs "en.*,en" \
  --convert-subs srt --sleep-requests 2 --sleep-interval 6 \
  -o "/Volumes/T7/download/dankoe/%(upload_date>%Y-%m-%d)s - %(title)s [%(id)s].%(ext)s" \
  --dateafter 20261001 --break-on-reject \
  "https://www.youtube.com/@DanKoeTalks/videos"
```

> **yt-dlp 版本很关键。** 老版本用 `android_vr` client，YouTube 现在会对它返回
> `403 Forbidden`。遇到 403 先 `yt-dlp -U`。本项目会优先使用 `/tmp/yt-dlp-new`
> （如果存在），避免动到你的 conda 环境。

**2. 重跑流水线**

```bash
python3 run.py all
```

就这样。每个阶段都是**指纹幂等**的：`catalog` 自动发现新文件，
已经处理完的 19 个视频会被跳过（日志里显示 `跳过（未变）`），只有新视频会真的调 API。

想只处理某几个：

```bash
python3 run.py translate --only dQw4w9WgXcQ,AbCdEfGhIjK
python3 run.py all --limit 2          # 只跑前 2 个，调试用
python3 run.py summarize --force      # 忽略指纹强制重跑（改了提示词才需要）
```

**3. 重新 build**

`all` 已经包含 build。单独跑：`python3 run.py build`。
build 会做自检——所有本地 `src` / `href` 指向的文件必须真实存在，否则返回非零。

---

## 部署（Cloudflare）

站点是纯静态产物，部署到 Cloudflare Workers 静态资产并绑了自定义域名：

- 线上地址：**https://dankoe.zhanglearning.com/**（备用：https://dankoe-archive.venn5708.workers.dev）
- 配套文章《把 19 个长视频做成一个"点得动"的学习网站》：**https://dankoe.zhanglearning.com/write-tech.html**
  （源文件 `docs/write-tech.md` + 排版版 `docs/write-tech.html`，build 时随站拷贝，不经模板体系）
- 配置见 `wrangler.jsonc`：assets 目录 `site/`；`html_handling: "none"` 保证站内显式 `.html` 链接原样 200（默认的 auto-trailing-slash 会 307 掉一跳）
- `worker.js` 只有一个职责：把根路径 `/` 送到 `index.html`（`html_handling: "none"` 下根路径不会自动映射）

加完新视频、跑完 `run.py all` 之后重新部署：

```bash
python3 run.py build
wrangler deploy          # 首次需 wrangler login（浏览器 OAuth）
```

GitHub 仓库：`tjxj/dankoe-archive`（私有；不含 `site/` 构建产物与 `data/cache/`，克隆后 `run.py build` 即可重建）。

## 目录结构

```
config.json              唯一配置入口
run.py                   CLI：catalog | frames | translate | summarize | build | serve | all
pipeline/
  common.py              日志、脱敏、原子写、指纹状态机、外部命令解析
  catalog.py             扫目录 + 抓 YouTube 元数据 → data/catalog.json
  srt.py                 SRT 解析、完整性校验、句子/段落重组
  frames.py              官方封面 + 关键帧抽取
  llm.py                 OpenAI 兼容客户端：限流、重试、磁盘缓存、并发 map
  translate.py           编号行协议的逐句翻译（带术语表）
  summarize.py           视觉模型分段分析 + 合成图文总结
  render.py              Jinja2 渲染 → site/，含产出自检
templates/               base / index / video 三个模板
site-static/             app.css app.js favicon.svg（原样拷进 site/assets/）
tests/test_pipeline.py   36 个用例，自带 runner，也能用 pytest 跑
data/
  catalog.json           全站唯一数据源
  raw/<id>.json          yt-dlp 原始元数据（缓存）
  videos/<id>/           cover.jpg  frames/  *.srt  *.txt
                         sentences.json  summary.json  storyboard.json
                         state.json（阶段指纹）  *-stats.json
  cache/                 LLM 响应缓存，按内容哈希命名
site/                    最终产出，可以整个目录扔到任意静态服务器
```

---

## 几个设计决定

**为什么用字幕而不是转录。** 这个频道只有 YouTube 自动生成字幕、没有人工字幕。
在决定「翻译而不是转录」之前，`srt.py` 的 `completeness()` 会先量化校验：
尾部覆盖率、中间空洞、语速（词/分）、重复 cue 占比。19 个视频全部通过：
`ok=True` 19/19、**中间空洞 0 个**、最差的一条片尾只缺 0.8 秒（全片 1862 秒）、
合计 135,592 词。任何一项不达标都会打 warn，那时才需要回退到 Whisper。

自动字幕必然有识别错误，所以翻译提示词里明确要求**按上下文纠正错词**
（例如把 `clod code` 还原成 `Claude Code`），而不是直译错词或标注存疑。
页面底部和「英文原稿」标签页都如实说明了这一点。

**为什么要重切句子。** YouTube 自动字幕的 cue 是按语音停顿切的，经常从一句话中间断开。
直接逐 cue 翻译会得到读不通的碎片。`build_sentences()` 按优先级规则重新聚合，
并且有一个 `continues` 保护：上一条没有句末标点、且下一条以小写字母开头时，不许切开。
18,188 条 cue → 8,125 句。`tests/test_pipeline.py` 里锁了三个真实回归用例
（过度切分 / 多句粘连 / 无标点长句失控），改这段逻辑前先看它们。

**为什么翻译用编号行协议。** 逐句单独调用会让时间轴对齐变成猜谜，而且贵 10 倍。
批量发 `1. …\n2. …`，模型按同样编号回，`parse_numbered()` 容错解析
（缺点号、全角标点、多行续写、同号拼接），缺号最多重试 3 次，
**仍缺的留空，绝不拿相邻句冒充**——那会造成整段时间轴错位。19 个视频 8125 句，覆盖率 100%。

**为什么知识点必须绑真实画面。** 视觉模型很爱编造 `frame_id`。`normalize()` 会把每个
`frame_id` 校验到真实存在的帧，无效的降级为空、并记录 `_frame_fixes`。
宁可少一张图，也不要出现「图在这里、文字在说别的」。实测 12 条摘录全部逐字命中原稿，
0 次 frame 修正。

**为什么时间线要对齐句首。** 模型给的秒数是从分段范围估算的，可能落在一个词中间。
`snap_to_sentence()` 把它对齐到最近的真实句子起点，显示的时间标签也由对齐后的值生成——
保证「看到的 12:03」就是「跳过去的 12:03」。

**播放器降级。** YouTube IFrame API 加载失败（离线、被墙）时不会白屏：
8 秒超时后显示降级提示，字幕点击退化为打开带时间戳的 YouTube 链接。
页面其余部分完全可用。

**缓存与幂等。** LLM 响应按内容哈希缓存在 `data/cache/`，重跑免费。
每个视频有独立的 `state.json` 记录各阶段指纹（提示词版本 + 模型名 + 参数 + 输入哈希），
指纹不变就跳过。改提示词只会重跑受影响的阶段。

---

## 配置

`config.json` 里几个值得知道的键：

```jsonc
"api": {
  "api_key_env": "OPENAI_API_KEY",       // 只从环境变量读，绝不写进任何文件
  "concurrency": 4,                       // 令牌桶全局限流
  "max_retries": 6                        // 429/5xx/网络超时都会指数退避重试
},
"translate": { "chunk_words_budget": 900, "thinking": false },
"summarize": {
  "segments": 6,                          // 每个视频切几段给视觉模型看
  "vision_concurrency": 6,                // 段内并发
  "video_concurrency": 3,                 // 视频间并发（两者相乘 = 峰值请求数）
  "synth_timeout": 900,                   // 合成开 thinking 要吐 1.6 万 token，300s 不够
  "thinking": true
},
"srt_completeness": {                     // 字幕完整性阈值
  "min_tail_coverage_ratio": 0.97,
  "max_interior_gap_seconds": 120,
  "min_words_per_minute": 60
}
```

术语表在 `pipeline/translate.py` 的 `GLOSSARY`，保证 19 个视频之间译名一致
（one-person business → 一人企业，the Matrix → 矩阵，monk mode → 闭关模式…）。
加新术语改那里，然后 `run.py translate --force`。

---

## 安全

API key 只从环境变量读取，不会写入 `config.json`、脚本、日志、HTML 或任何产出文件。
`common.scrub()` 对所有日志和错误信息脱敏（`sk-...`、`Bearer ...`），
`render._check_no_secrets()` 在每次 build 后扫描整个 `site/`，命中即报错退出。

视频版权归原作者 Dan Koe 所有。本站仅作个人学习用途，
播放一律走 YouTube 官方嵌入，不分发任何视频文件。
