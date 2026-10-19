// 唯一的职责：把根路径送到 index.html。
// 其余请求全部直达 assets（html_handling=none，站内链接的 .html 原样 200，不产生 307）。
export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/") {
      return env.ASSETS.fetch(new Request(new URL("/index.html", url), request));
    }
    return env.ASSETS.fetch(request);
  },
};
