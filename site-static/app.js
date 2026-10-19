/* ============================================================
   Dan Koe 学习站 —— 交互层（原生 JS，无框架、无构建）

   三块职责：
     1. 主题（浅色/深色，跟随系统 + 手动覆盖，存 localStorage）
     2. 首页：搜索 + 主题/月份筛选
     3. 详情页：标签页、YouTube 播放器联动、字幕同步高亮与点击跳转

   降级原则：YouTube API 加载失败（离线/被墙）时页面仍完全可读，
   字幕点击退化为"复制带时间戳的 YouTube 链接"，不抛错、不留白屏。
   ============================================================ */
(function () {
  "use strict";

  var STORE_THEME = "dk-theme";
  var STORE_AUTOSCROLL = "dk-autoscroll";
  var STORE_MODE = "dk-tr-mode";

  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  function store(key, val) {
    try {
      if (val === undefined) return localStorage.getItem(key);
      if (val === null) localStorage.removeItem(key);
      else localStorage.setItem(key, val);
    } catch (e) { /* 隐私模式下 localStorage 会抛，静默降级 */ }
    return null;
  }

  /* ───────────────────────── 主题 ───────────────────────── */

  function applyTheme(mode) {
    var root = document.documentElement;
    if (mode === "system") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", mode);
    var btn = $("#themeBtn");
    if (btn) {
      var eff = mode === "system"
        ? (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light")
        : mode;
      btn.textContent = eff === "dark" ? "深色" : "浅色";
      btn.setAttribute("aria-pressed", String(mode !== "system"));
      btn.title = "当前：" + (mode === "system" ? "跟随系统" : eff) + "，点击切换";
    }
  }

  function initTheme() {
    // ?theme=dark / ?theme=light 可以强制指定，方便分享和无头测试；否则用存下来的偏好
    var forced = null;
    try {
      var q = new URLSearchParams(location.search).get("theme");
      if (q === "dark" || q === "light") forced = q;
    } catch (e) { /* 老浏览器没有 URLSearchParams，忽略 */ }
    var saved = forced || store(STORE_THEME) || "system";
    applyTheme(saved);
    var btn = $("#themeBtn");
    if (!btn) return;
    btn.addEventListener("click", function () {
      // 从 DOM 读当前生效值，而不是从 localStorage —— ?theme= 强制指定时两者会不一致
      var eff = document.documentElement.getAttribute("data-theme")
        || (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
      // 循环：浅色 → 深色 → 跟随系统
      var next = eff === "light" ? "dark" : (eff === "dark" ? "system" : "light");
      store(STORE_THEME, next);
      applyTheme(next);
      forced = null; // 用户手动点过之后，系统事件恢复话语权
    });
    if (window.matchMedia) {
      window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", function () {
        // ?theme= 强制指定时不能被系统事件抢回去（无头 Chrome 会在加载后期补发一次 change）
        if (forced) return;
        if ((store(STORE_THEME) || "system") === "system") applyTheme("system");
      });
    }
  }

  /* ───────────────────── 阅读进度条 ───────────────────── */

  function initProgress() {
    var bar = $("#progress");
    if (!bar) return;
    var raf = null;
    function update() {
      raf = null;
      var h = document.documentElement.scrollHeight - window.innerHeight;
      var p = h > 0 ? Math.min(1, Math.max(0, window.scrollY / h)) : 0;
      bar.style.width = (p * 100).toFixed(2) + "%";
    }
    window.addEventListener("scroll", function () {
      if (raf === null) raf = requestAnimationFrame(update);
    }, { passive: true });
    window.addEventListener("resize", update);
    update();
  }

  /* ───────────────────── 首页：搜索与筛选 ───────────────────── */

  function initIndex() {
    var grid = $("#videoGrid");
    if (!grid) return;
    var cards = $$(".vcard, .feature", grid);
    var input = $("#searchInput");
    var countEl = $("#resultCount");
    var activeTopic = null;
    var activeMonth = null;

    function norm(s) { return (s || "").toLowerCase().trim(); }

    function apply() {
      var q = norm(input ? input.value : "");
      var shown = 0;
      cards.forEach(function (card) {
        var hay = norm(card.getAttribute("data-search"));
        var topics = (card.getAttribute("data-topics") || "").split("|").filter(Boolean);
        var month = card.getAttribute("data-month") || "";
        var okQ = !q || q.split(/\s+/).every(function (w) { return hay.indexOf(w) >= 0; });
        var okT = !activeTopic || topics.indexOf(activeTopic) >= 0;
        var okM = !activeMonth || month === activeMonth;
        var show = okQ && okT && okM;
        card.style.display = show ? "" : "none";
        if (show) shown++;
      });
      if (countEl) {
        countEl.textContent = shown === cards.length
          ? shown + " 个视频"
          : shown + " / " + cards.length + " 个";
      }
      var empty = $("#emptyState");
      if (empty) empty.style.display = shown ? "none" : "";
    }

    if (input) {
      input.addEventListener("input", apply);
      input.addEventListener("keydown", function (e) {
        if (e.key === "Escape") { input.value = ""; apply(); }
      });
    }

    $$(".chip[data-topic]").forEach(function (chip) {
      chip.addEventListener("click", function () {
        var t = chip.getAttribute("data-topic");
        activeTopic = activeTopic === t ? null : t;
        $$(".chip[data-topic]").forEach(function (c) {
          c.setAttribute("aria-pressed", String(c.getAttribute("data-topic") === activeTopic));
        });
        apply();
      });
    });

    $$(".chip[data-month]").forEach(function (chip) {
      chip.addEventListener("click", function () {
        var m = chip.getAttribute("data-month");
        activeMonth = activeMonth === m ? null : m;
        $$(".chip[data-month]").forEach(function (c) {
          c.setAttribute("aria-pressed", String(c.getAttribute("data-month") === activeMonth));
        });
        apply();
      });
    });

    var reset = $("#resetFilters");
    if (reset) reset.addEventListener("click", function () {
      activeTopic = activeMonth = null;
      if (input) input.value = "";
      $$(".chip").forEach(function (c) { c.setAttribute("aria-pressed", "false"); });
      apply();
    });

    apply();
  }

  /* ───────────────────── 详情页 ───────────────────── */

  var Player = {
    yt: null,          // YouTube.Player 实例
    ready: false,
    failed: false,
    videoId: "",
    duration: 0,
    listeners: [],
    _timer: null,
    _last: -1,

    init: function (videoId, duration, mountEl) {
      this.videoId = videoId;
      this.duration = duration || 0;
      this.mount = mountEl;
      var self = this;

      // 已有全局回调则排队
      var prev = window.onYouTubeIframeAPIReady;
      window.onYouTubeIframeAPIReady = function () {
        if (typeof prev === "function") { try { prev(); } catch (e) {} }
        self._create();
      };

      if (window.YT && window.YT.Player) { this._create(); return; }

      var tag = document.createElement("script");
      tag.src = "https://www.youtube.com/iframe_api";
      tag.async = true;
      tag.onerror = function () { self._fail("脚本加载失败"); };
      document.head.appendChild(tag);

      // 超时兜底：8 秒没就绪就当离线，走降级路径
      setTimeout(function () {
        if (!self.ready && !self.failed) self._fail("加载超时");
      }, 8000);
    },

    _create: function () {
      var self = this;
      var host = this.mount;
      if (!host || !window.YT || !window.YT.Player) { this._fail("API 不可用"); return; }
      try {
        this.yt = new window.YT.Player(host, {
          videoId: this.videoId,
          playerVars: { rel: 0, modestbranding: 1, playsinline: 1, origin: location.origin || undefined },
          events: {
            onReady: function (e) {
              self.ready = true;
              self.failed = false;
              // 慢网络下 8s 兜底可能先触发过降级；就绪后要把罩子收回去
              var fb = $("#playerFallback");
              if (fb) fb.hidden = true;
              var d = e.target.getDuration ? e.target.getDuration() : 0;
              if (d && d > 0) self.duration = d;
              document.documentElement.classList.add("player-ready");
              document.documentElement.classList.remove("player-failed");
              self._startPolling();
              // 支持 #t=123 直接定位
              var m = /[#&]t=(\d+)/.exec(location.hash);
              if (m) self.seek(parseInt(m[1], 10));
            },
            onStateChange: function (e) {
              if (e.data === window.YT.PlayerState.PLAYING) self._startPolling();
            },
            onError: function () { self._fail("播放器错误"); }
          }
        });
      } catch (e) { this._fail(String(e)); }
    },

    _fail: function (reason) {
      if (this.ready) return;
      this.failed = true;
      document.documentElement.classList.add("player-failed");
      var box = $("#playerFallback");
      if (box) {
        box.hidden = false;
        var r = $("#fallbackReason");
        if (r) r.textContent = reason;
      }
      console.warn("[dk] YouTube 播放器不可用，已降级：" + reason);
    },

    _startPolling: function () {
      var self = this;
      if (this._timer) return;
      this._timer = setInterval(function () {
        if (!self.yt || !self.yt.getCurrentTime) return;
        var t;
        try { t = self.yt.getCurrentTime(); } catch (e) { return; }
        if (typeof t !== "number" || isNaN(t)) return;
        if (Math.abs(t - self._last) > 0.01) {
          self._last = t;
          self.listeners.forEach(function (fn) { try { fn(t, self.duration); } catch (e) {} });
        }
      }, 250);
    },

    onTime: function (fn) { this.listeners.push(fn); },

    seek: function (sec) {
      sec = Math.max(0, sec | 0);
      if (this.ready && this.yt && this.yt.seekTo) {
        try { this.yt.seekTo(sec, true); this.yt.playVideo && this.yt.playVideo(); return true; } catch (e) {}
      }
      // 降级：打开带时间戳的 YouTube 链接
      var url = "https://www.youtube.com/watch?v=" + this.videoId + "&t=" + sec + "s";
      window.open(url, "_blank", "noopener");
      return false;
    },

    isReady: function () { return this.ready && !this.failed; }
  };

  function fmt(sec) {
    sec = Math.max(0, Math.floor(sec || 0));
    var h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    function p(n) { return n < 10 ? "0" + n : "" + n; }
    return h ? h + ":" + p(m) + ":" + p(s) : p(m) + ":" + p(s);
  }

  function initVideoPage() {
    var host = $("#ytPlayer");
    var videoId = document.body.getAttribute("data-video-id") || "";
    var duration = parseFloat(document.body.getAttribute("data-duration") || "0");
    if (host && videoId) Player.init(videoId, duration, host);

    // 当前时间显示
    var nowEl = $("#nowTime");
    Player.onTime(function (t, d) {
      if (nowEl) nowEl.innerHTML = "<b>" + fmt(t) + "</b> / " + fmt(d);
    });

    /* ── 标签页 ── */
    var tabs = $$(".tab");
    var panels = $$(".panel-out");
    function selectTab(id, push) {
      tabs.forEach(function (t) { t.setAttribute("aria-selected", String(t.getAttribute("data-tab") === id)); });
      panels.forEach(function (p) { p.setAttribute("data-active", String(p.id === "panel-" + id)); });
      if (push && history.replaceState) history.replaceState(null, "", "#" + id);
      window.dispatchEvent(new CustomEvent("dk:tab", { detail: id }));
    }
    tabs.forEach(function (t) {
      t.addEventListener("click", function () { selectTab(t.getAttribute("data-tab"), true); });
    });
    var initial = (location.hash || "").replace("#", "");
    var valid = tabs.map(function (t) { return t.getAttribute("data-tab"); });
    selectTab(valid.indexOf(initial) >= 0 ? initial : (valid[0] || "summary"), false);

    /* ── 所有跳转按钮（data-copy 的元素归复制逻辑处理，不在这里跳转） ── */
    document.addEventListener("click", function (e) {
      var el = e.target.closest ? e.target.closest("[data-seek]") : null;
      if (!el || el.hasAttribute("data-copy")) return;
      e.preventDefault();
      var sec = parseFloat(el.getAttribute("data-seek"));
      if (!isNaN(sec)) Player.seek(sec);
      var col = $(".playercol");
      if (col && window.innerWidth <= 1020) col.scrollIntoView({ behavior: "smooth", block: "start" });
    });

    /* ── 章节 ── */
    var chapWrap = $("#chapters");
    if (chapWrap) {
      Player.onTime(function (t) {
        var items = $$("[data-chap]", chapWrap);
        var cur = null;
        items.forEach(function (b) {
          var st = parseFloat(b.getAttribute("data-chap"));
          if (t >= st) cur = b;
        });
        items.forEach(function (b) { b.style.color = ""; });
        if (cur) cur.style.color = "var(--accent)";
      });
    }

    /* ── 字幕同步 ── */
    var lines = $$(".line[data-start]");
    var autoscroll = store(STORE_AUTOSCROLL) !== "0";
    var asBox = $("#autoscrollBox");
    if (asBox) {
      asBox.checked = autoscroll;
      asBox.addEventListener("change", function () {
        autoscroll = asBox.checked;
        store(STORE_AUTOSCROLL, autoscroll ? "1" : "0");
      });
    }
    var userScrollLock = 0;
    var trList = $("#transcriptList");
    if (trList) {
      trList.addEventListener("wheel", function () { userScrollLock = Date.now() + 2500; }, { passive: true });
      trList.addEventListener("touchmove", function () { userScrollLock = Date.now() + 2500; }, { passive: true });
    }

    // 二分查找当前句
    var starts = lines.map(function (l) { return parseFloat(l.getAttribute("data-start")) || 0; });
    function findActive(t) {
      var lo = 0, hi = starts.length - 1, ans = -1;
      while (lo <= hi) {
        var mid = (lo + hi) >> 1;
        if (starts[mid] <= t) { ans = mid; lo = mid + 1; } else { hi = mid - 1; }
      }
      return ans;
    }

    var lastIdx = -1;
    var trPanel = $("#panel-transcript");
    Player.onTime(function (t) {
      var i = findActive(t);
      if (i === lastIdx || i < 0) return;
      if (lines[lastIdx]) lines[lastIdx].classList.remove("active");
      lastIdx = i;
      var el = lines[i];
      if (!el) return;
      el.classList.add("active");
      if (!autoscroll || !trPanel || trPanel.getAttribute("data-active") !== "true") return;
      if (Date.now() < userScrollLock) return;
      var r = el.getBoundingClientRect();
      var vh = window.innerHeight;
      if (r.top < 140 || r.bottom > vh - 100) {
        el.scrollIntoView({ behavior: "smooth", block: "center" });
      }
    });

    /* ── 字幕显示模式：双语 / 仅中 / 仅英 ── */
    var modeSel = $("#modeSelect");
    function applyMode(m) {
      lines.forEach(function (l) { l.setAttribute("data-mode", m); });
      store(STORE_MODE, m);
    }
    if (modeSel) {
      modeSel.value = store(STORE_MODE) || "both";
      applyMode(modeSel.value);
      modeSel.addEventListener("change", function () { applyMode(modeSel.value); });
    }

    /* ── 跳转到当前播放位置 ── */
    function jumpToNow() {
      var el = lines[lastIdx];
      if (!el) return;
      userScrollLock = 0;
      el.scrollIntoView({ behavior: "smooth", block: "center" });
      el.classList.add("mark");
      setTimeout(function () { el.classList.remove("mark"); }, 1600);
    }
    $$("[data-jump-now]").forEach(function (b) { b.addEventListener("click", jumpToNow); });

    /* ── 原稿视图：段落高亮跟随 ── */
    var paras = $$(".prose-para[data-start]");
    if (paras.length) {
      var pStarts = paras.map(function (p) { return parseFloat(p.getAttribute("data-start")) || 0; });
      var lastP = -1;
      Player.onTime(function (t) {
        var lo = 0, hi = pStarts.length - 1, ans = -1;
        while (lo <= hi) { var mid = (lo + hi) >> 1; if (pStarts[mid] <= t) { ans = mid; lo = mid + 1; } else { hi = mid - 1; } }
        if (ans === lastP) return;
        if (paras[lastP]) paras[lastP].classList.remove("active");
        lastP = ans;
        if (paras[ans]) paras[ans].classList.add("active");
      });
    }

    /* ── 复制引用 ── */
    var copyBtns = $$("[data-copy]");
    copyBtns.forEach(function (b) {
      b.addEventListener("click", function (e) {
        e.preventDefault();
        var text = b.getAttribute("data-copy") || "";
        var url = "https://www.youtube.com/watch?v=" + videoId + "&t=" + (b.getAttribute("data-seek") || 0) + "s";
        var payload = text ? text + "\n" + url : url;
        var done = function () {
          var old = b.textContent;
          b.textContent = "已复制";
          setTimeout(function () { b.textContent = old; }, 1400);
        };
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(payload).then(done, function () { window.open(url, "_blank", "noopener"); });
        } else { window.open(url, "_blank", "noopener"); }
      });
    });
    /* ── 键盘可达：li/span 上的 role="button" 也要能用 Enter / 空格触发 ── */
    document.addEventListener("keydown", function (e) {
      if (e.key !== "Enter" && e.key !== " " && e.key !== "Spacebar") return;
      var el = e.target.closest ? e.target.closest("[data-seek]") : null;
      if (!el || el.tagName === "BUTTON" || el.tagName === "A" || el.tagName === "INPUT") return;
      e.preventDefault();
      el.click();
    });
  }

  /* ───────────────────────── 启动 ───────────────────────── */

  function boot() {
    initTheme();
    initProgress();
    initIndex();
    if (document.body.hasAttribute("data-video-id")) initVideoPage();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
