// Cloudflare Worker for ContentOps: lets a server in Russia reach Telegram, YouTube upload and LinkedIn.
// Deploy: dash.cloudflare.com -> Workers & Pages -> Create -> Worker -> paste this file -> Deploy,
// or: cd cloudflare && npx wrangler deploy. Then set TELEGRAM_API_URL=https://<worker>.<account>.workers.dev in .env.
//
// Routes:
//   /bot<token>/<method>, /file/bot<token>/<path>  -> https://api.telegram.org   (only bots from ALLOWED_BOT_ID)
//   /g/oauth2/token, /g/oauth2/tokeninfo           -> https://oauth2.googleapis.com   (needs X-Proxy-Secret)
//   /g/api/upload/youtube/v3/videos                -> https://www.googleapis.com      (needs X-Proxy-Secret)
//   /g/api/youtube/v3/videos (GET), /g/ya/v2/reports (GET) -> статистика YouTube    (needs X-Proxy-Secret)
//   /li?url=<linkedin url>                        -> public LinkedIn page            (needs X-Proxy-Secret)
//   /px/api/videos, /px/cdn/video/….mp4            -> Pixabay API and video files     (needs X-Proxy-Secret)
// Everything else: 404. Variables: ALLOWED_BOT_ID (digits before ":" in bot tokens, comma-separated),
// PROXY_SECRET (secret, same value as CF_PROXY_SECRET in the bot's .env).

const PROXY_ROUTES = [
  { re: /^\/g\/oauth2\/(token|tokeninfo)$/, prefix: "/g/oauth2/", host: "https://oauth2.googleapis.com/" },
  { re: /^\/g\/api\/upload\/youtube\/v3\/videos$/, prefix: "/g/api/", host: "https://www.googleapis.com/" },
  // статистика YouTube: только чтение (GET), чтобы через эти маршруты нельзя было менять или удалять видео
  { re: /^\/g\/api\/youtube\/v3\/videos$/, prefix: "/g/api/", host: "https://www.googleapis.com/", methods: ["GET"] },
  { re: /^\/g\/ya\/v2\/reports$/, prefix: "/g/ya/", host: "https://youtubeanalytics.googleapis.com/", methods: ["GET"] },
  // стоковые видео для фона Shorts: поиск и скачивание файлов
  { re: /^\/px\/api\/videos\/?$/, prefix: "/px/api/", host: "https://pixabay.com/api/" },
  { re: /^\/px\/cdn\/video\/[\w\-./]+\.mp4$/, prefix: "/px/cdn/", host: "https://cdn.pixabay.com/" },
];
const LINKEDIN_HOST = /^(?:[\w-]+\.)*linkedin\.com$|^lnkd\.in$/;
const UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36";

function forward(request, target) {
  const headers = new Headers(request.headers);
  headers.delete("host");
  headers.delete("x-proxy-secret");
  return fetch(target, {
    method: request.method,
    headers,
    body: ["GET", "HEAD"].includes(request.method) ? undefined : request.body,
    redirect: "manual", // Location of a YouTube resumable session must reach the bot as is
  });
}

function sameSecret(a, b) {
  if (!a || !b || a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname;

    const bot = path.match(/^\/(?:file\/)?bot(\d+):[\w-]+\//);
    if (bot) {
      const allowed = (env.ALLOWED_BOT_ID || "").split(",").map((id) => id.trim()).filter(Boolean);
      if (!allowed.includes(bot[1])) return new Response("Forbidden", { status: 403 });
      return forward(request, "https://api.telegram.org" + path + url.search);
    }

    const google = PROXY_ROUTES.find((r) => r.re.test(path) && (!r.methods || r.methods.includes(request.method)));
    if (!google && path !== "/li") return new Response("Not found", { status: 404 });
    if (!sameSecret(request.headers.get("x-proxy-secret") || "", env.PROXY_SECRET || "")) {
      return new Response("Forbidden", { status: 403 });
    }

    if (google) return forward(request, google.host + path.slice(google.prefix.length) + url.search);

    // /li?url=...
    let target;
    try {
      target = new URL(url.searchParams.get("url") || "");
    } catch {
      return new Response("bad url", { status: 400 });
    }
    if (target.protocol !== "https:" || !LINKEDIN_HOST.test(target.hostname)) {
      return new Response("only linkedin.com is allowed", { status: 400 });
    }
    const resp = await fetch(target.toString(), { headers: { "User-Agent": UA, "Accept-Language": "en-US,en;q=0.9" } });
    return new Response(resp.body, {
      status: resp.status,
      headers: { "Content-Type": resp.headers.get("Content-Type") || "text/html", "X-Final-Url": resp.url },
    });
  },
};
