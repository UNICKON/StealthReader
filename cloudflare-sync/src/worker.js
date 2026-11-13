/**
 * StealthReader 同步后端（Cloudflare Worker + R2）
 *
 * 这个文件只做四件事，故意保持"笨"：
 *   1. 认 token（Authorization: Bearer …），常量时间比较；
 *   2. 只放行四个 kind（progress / annotations / stats / settings），别的一律 404；
 *   3. 条件写：更新必须带 If-Match: <etag>，新建必须带 X-Create: 1；
 *      什么都没带的 PUT 一律 428 —— 服务端不提供"无条件覆盖"这条路；
 *   4. 书文件按 sha256 内容寻址，PUT/GET 直接转 R2。
 *
 * 合并逻辑一个字都不在这里：客户端拿到两边数据自己并（见 docs/cloudflare-sync-design.md §7）。
 * 所以这个 Worker 出问题最多是"同步不动"，不会把谁的数据并坏。
 */

const KINDS = new Set(["progress", "annotations", "stats", "settings"]);
const STATE_PREFIX = "v1/state/";
const HISTORY_PREFIX = "v1/state/history/";
const BLOB_PREFIX = "v1/blob/";
const LITE_PREFIX = "v1/lite/";
const COOKIE_NAME = "srtoken";
const COOKIE_MAX_AGE = 60 * 60 * 24 * 365;         // 一年，和桌面端 token 同寿命
const MAX_LITE_INDEX = 1024 * 1024;               // 章节偏移表，几百 KB 顶天
const KEEP_VERSIONS = 5;          // 每个 kind 留最近 5 个旧版本，给"回滚"用
const MAX_BLOB = 50 * 1024 * 1024; // 50 MB（epub 最大 8 MB，留足余量）

const json = (obj, status = 200, extra = {}) =>
  new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store", ...extra },
  });

/** 常量时间比较：长度不同直接 false，长度相同逐字节异或累加。 */
function safeEqual(a, b) {
  const x = new TextEncoder().encode(String(a || ""));
  const y = new TextEncoder().encode(String(b || ""));
  if (x.length !== y.length) return false;
  let diff = 0;
  for (let i = 0; i < x.length; i++) diff |= x[i] ^ y[i];
  return diff === 0;
}

function tokenOf(request) {
  const bearer = (request.headers.get("authorization") || "").replace(/^Bearer\s+/i, "").trim();
  if (bearer) return bearer;
  // 浏览器（阅读页）走 Cookie：网页里没法安全地放 Authorization，登录一次就给个 HttpOnly 的
  for (const part of String(request.headers.get("cookie") || "").split(";")) {
    const eq = part.indexOf("=");
    if (eq < 0) continue;
    if (part.slice(0, eq).trim() !== COOKIE_NAME) continue;
    const raw = part.slice(eq + 1).trim();
    try {
      return decodeURIComponent(raw);
    } catch {
      return raw;
    }
  }
  return "";
}

function authed(request, env) {
  const want = (env.SYNC_TOKEN || "").trim();
  if (!want) return false;                       // 没配 token = 谁也别想进来
  return safeEqual(tokenOf(request), want);
}

/** 只接受 16 进制 sha256（64 位），顺带把 key 限制在我们自己的前缀下。 */
function blobKey(sha) {
  if (!/^[0-9a-f]{64}$/.test(sha)) return null;
  return BLOB_PREFIX + sha;
}

/** 书文件原名：只留文件名、去掉路径分隔符与控制字符。
 *  中文在 HTTP 头里是非法字节，所以客户端发之前 encodeURIComponent、这里解开。 */
function safeName(v) {
  let name = String(v || "");
  try {
    name = decodeURIComponent(name);
  } catch (e) {
    /* 不是合法百分号编码：原样用 */
  }
  name = name.split(/[\\/]/).pop() || "";
  name = name.replace(/[\u0000-\u001f\u007f]/g, "").replace(/[\\/:*?"<>|]/g, "_").trim();
  if (!name) return "";
  return name.slice(-160);          // 太长只留尾部，别把扩展名截掉
}

/* ── 阅读页：登录（发 Cookie）、静态资源 ───────────────────────────── */

async function handleLogin(request, env) {
  const want = (env.SYNC_TOKEN || "").trim();
  if (!want) return json({ ok: false, err: "服务端没配 token" }, 500);
  let got = "";
  const ct = (request.headers.get("content-type") || "").toLowerCase();
  try {
    if (ct.includes("application/json")) got = String((await request.json())?.token || "");
    else if (ct.includes("form")) got = String((await request.formData()).get("token") || "");
  } catch (e) {
    got = "";
  }
  got = got.trim();
  if (!safeEqual(got, want)) return json({ ok: false, err: "token 不对" }, 401);
  return json({ ok: true }, 200, {
    "set-cookie": `${COOKIE_NAME}=${encodeURIComponent(got)}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=${COOKIE_MAX_AGE}`,
  });
}

function handleLogout() {
  return json({ ok: true }, 200, {
    "set-cookie": `${COOKIE_NAME}=; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=0`,
  });
}

/** 阅读页的静态资源。HTML 里不含任何数据；数据全在要鉴权的 /v1/* 上。
 *  CSP 是硬要求：EPUB 里可以带 JS，内容是 blob: 同源加载的，不锁 script-src 等于让书里的脚本在我们域名下跑。 */
const SECURITY_HEADERS = {
  "content-security-policy": [
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "font-src 'self' data: blob:",
    "media-src 'self' data: blob:",
    "connect-src 'self'",
    "frame-src 'self' blob:",
    "base-uri 'none'",
    "form-action 'self'",
    "object-src 'none'",
  ].join("; "),
  "x-content-type-options": "nosniff",
  "referrer-policy": "same-origin",
  "x-frame-options": "SAMEORIGIN",
};

async function serveStatic(request, env) {
  if (!env.ASSETS) return json({ ok: false, err: "没有 ASSETS 绑定" }, 500);
  const url = new URL(request.url);
  const path = url.pathname;
  // 阅读页挂在 /r/ 下，但静态文件就在 assets 根目录：转发前把前缀去掉。
  // 首页也显式指到 /index.html——否则 assets 那边会把 /index.html 307 到 /，
  // 用户敲 /r/ 会被弹成 /（能看，但 URL 莫名其妙）。
  const mapped = (path === "/" || path === "/r" || path === "/r/") ? "/index.html"
    : path.startsWith("/r/") ? path.slice(2) : path;
  const inner = new Request(new URL(mapped, url).toString(), request);
  const res = await env.ASSETS.fetch(inner);
  const headers = new Headers(res.headers);
  const type = (headers.get("content-type") || "").toLowerCase();
  if (type.includes("text/html")) {
    for (const [k, v] of Object.entries(SECURITY_HEADERS)) headers.set(k, v);
    headers.set("cache-control", "no-cache");
  }
  return new Response(res.body, { status: res.status, headers });
}

/* ── 书架 / 章节偏移表 ───────────────────────────────────────────── */

/** 书架：R2 里的书 × 进度表。只回元信息，正文要靠 /v1/blob/<sha> 单本取。 */
async function handleShelf(env) {
  const blobs = await listBlobs(env);
  let progress = {};
  const obj = await env.BUCKET.get(STATE_PREFIX + "progress.json");
  if (obj) {
    try {
      progress = JSON.parse(await obj.text()) || {};
    } catch (e) {
      progress = {};
    }
  }
  const rows = progress.books && typeof progress.books === "object" ? progress.books : {};
  const books = blobs.map((b) => {
    const uid = b.sha.slice(0, 16);
    const row = rows[uid] && typeof rows[uid] === "object" ? rows[uid] : {};
    const size = Number(b.size) || 0;
    return {
      uid,
      sha: b.sha,
      name: b.name || "",
      title: String(row.title || b.name || uid),
      size,
      pos: Math.max(0, Math.round(Number(row.pos) || 0)),
      total: Math.max(0, Math.round(Number(row.total) || 0)),
      at: Number(row.at) || 0,
      dev: String(row.dev || ""),
      hidden: !!row.hidden,
    };
  });
  books.sort((a, b) => (b.at - a.at) || String(a.title).localeCompare(String(b.title), "zh"));
  return json({ ok: true, books, server_time: Date.now() / 1000, schema: 1 });
}

/** 章节偏移表：桌面端抽全文时顺手算出（章节标题 + 起始字符偏移 + 总字数）。
 *  阅读页拿它把"第几个字"换算成"跳到哪一章"，比按字节比例猜准得多。 */
async function handleLiteIndexGet(uid, env) {
  const obj = await env.BUCKET.get(LITE_PREFIX + uid + "/index.json");
  if (!obj) return json({ ok: false, err: "not found" }, 404);
  return new Response(obj.body, {
    status: 200,
    headers: {
      "content-type": "application/json; charset=utf-8",
      etag: obj.etag,
      "cache-control": "private, max-age=600",
    },
  });
}

async function handleLiteIndexPut(request, uid, env) {
  const body = await request.arrayBuffer();
  if (!body.byteLength) return json({ ok: false, err: "empty body" }, 400);
  if (body.byteLength > MAX_LITE_INDEX) return json({ ok: false, err: "index too large" }, 413);
  // 派生数据、随手可重建：不做条件写，谁后写谁算
  await env.BUCKET.put(LITE_PREFIX + uid + "/index.json", body, {
    httpMetadata: { contentType: "application/json; charset=utf-8" },
  });
  return json({ ok: true, size: body.byteLength });
}

/** 老版本归档 + 只留 KEEP_VERSIONS 个。归档失败不影响这次写入成功。
 *  注意：R2 的 head() 只回元数据、没有 body，所以这里要再 get() 一次拿内容。 */
async function archive(env, kind, oldKey) {
  try {
    const old = await env.BUCKET.get(oldKey);
    if (!old) return;
    const stamp = new Date(old.uploaded || Date.now()).toISOString().replace(/[:.]/g, "-");
    const key = `${HISTORY_PREFIX}${kind}/${stamp}.json`;
    await env.BUCKET.put(key, await old.arrayBuffer(), {
      httpMetadata: { contentType: "application/json; charset=utf-8" },
    });
    const list = await env.BUCKET.list({ prefix: `${HISTORY_PREFIX}${kind}/` });
    const rows = (list.objects || []).sort((a, b) => (a.key < b.key ? 1 : -1));
    for (const row of rows.slice(KEEP_VERSIONS)) await env.BUCKET.delete(row.key);
  } catch (e) {
    // 归档是"锦上添花"，不能因为它让这次同步失败
  }
}

async function handleStateGet(kind, env) {
  const obj = await env.BUCKET.get(STATE_PREFIX + kind + ".json");
  if (!obj) return json({ ok: false, err: "not found", kind }, 404);
  return new Response(obj.body, {
    status: 200,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
      etag: obj.etag,
      "x-updated-at": String(obj.uploaded ? new Date(obj.uploaded).getTime() / 1000 : 0),
    },
  });
}

async function handleStatePut(request, kind, env) {
  const body = await request.arrayBuffer();
  if (body.byteLength === 0) return json({ ok: false, err: "empty body" }, 400);
  if (body.byteLength > 8 * 1024 * 1024) return json({ ok: false, err: "state too large" }, 413);

  const key = STATE_PREFIX + kind + ".json";
  const cur = await env.BUCKET.head(key);
  const ifMatch = (request.headers.get("if-match") || "").trim();
  const createOnly = (request.headers.get("x-create") || "").trim() === "1";

  if (createOnly) {
    if (cur) return json({ ok: false, err: "already exists", rev: cur.etag }, 412);
    const put = await env.BUCKET.put(key, body, {
      httpMetadata: { contentType: "application/json; charset=utf-8" },
      onlyIf: { etagDoesNotMatch: "*" },
    });
    if (!put) return json({ ok: false, err: "race: created by someone else" }, 412);
    return json({ ok: true, rev: put.etag, created: true });
  }

  // 更新必须带 If-Match：服务端不提供无条件覆盖（设计里那条反目标）
  if (!ifMatch) return json({ ok: false, err: "If-Match required" }, 428);
  if (!cur) return json({ ok: false, err: "gone: remote has no such object", rev: "" }, 412);
  if (ifMatch !== cur.etag) return json({ ok: false, err: "conflict", rev: cur.etag }, 412);

  const put = await env.BUCKET.put(key, body, {
    httpMetadata: { contentType: "application/json; charset=utf-8" },
    onlyIf: { etagMatches: ifMatch },
  });
  if (!put) return json({ ok: false, err: "conflict (conditional write failed)", rev: (await env.BUCKET.head(key))?.etag || "" }, 412);

  await archive(env, kind, key);
  return json({ ok: true, rev: put.etag });
}

async function handleManifest(env) {
  const out = {};
  for (const kind of KINDS) {
    const head = await env.BUCKET.head(STATE_PREFIX + kind + ".json");
    if (head) {
      out[kind] = {
        rev: head.etag,
        size: head.size,
        updated_at: head.uploaded ? new Date(head.uploaded).getTime() / 1000 : 0,
      };
    }
  }
  return json({ ok: true, kinds: out, server_time: Date.now() / 1000 });
}

async function handleBlobCheck(request, env) {
  let payload;
  try {
    payload = await request.json();
  } catch {
    return json({ ok: false, err: "bad json" }, 400);
  }
  const want = Array.isArray(payload?.sha256) ? payload.sha256.slice(0, 200) : [];
  const missing = [];
  const sizes = {};
  for (const sha of want) {
    const key = blobKey(String(sha).toLowerCase());
    if (!key) continue;
    const head = await env.BUCKET.head(key);
    if (!head) missing.push(sha);
    else sizes[sha] = head.size;
  }
  return json({ ok: true, missing, sizes });
}

async function handleBlobPut(request, sha, env) {
  const key = blobKey(sha);
  if (!key) return json({ ok: false, err: "bad sha256" }, 400);

  const len = Number(request.headers.get("content-length") || 0);
  if (!len) return json({ ok: false, err: "Content-Length required" }, 411);
  if (len > MAX_BLOB) return json({ ok: false, err: "blob too large" }, 413);

  const existed = await env.BUCKET.head(key);
  if (existed) {
    // 幂等：同一份内容，传过就算了。唯一的例外是"补名字"——老对象没有原名，
    // 客户端带 X-Fix-Name 再传一次同样的字节，就地把原名补上（内容不变，etag 换来换去无害）。
    const wantName = safeName(request.headers.get("x-file-name"));
    const haveName = safeName(existed.customMetadata && existed.customMetadata.name);
    if (!wantName || haveName || request.headers.get("x-fix-name") !== "1") {
      return json({ ok: true, existed: true, size: existed.size, name: haveName });
    }
  }

  // 一次读进内存再写：书最大 8 MB，比"流长度未知导致 R2 抛错"确定得多
  const buf = await request.arrayBuffer();
  if (buf.byteLength > MAX_BLOB) return json({ ok: false, err: "blob too large" }, 413);
  const put = await env.BUCKET.put(key, buf, {
    httpMetadata: { contentType: request.headers.get("content-type") || "application/octet-stream" },
    // 原始文件名：另一台机器要靠它把书还原成原名（只存名字，不带路径）
    customMetadata: { name: safeName(request.headers.get("x-file-name")) },
    onlyIf: existed ? { etagMatches: existed.etag } : { etagDoesNotMatch: "*" },
  });
  if (!put) return json({ ok: true, existed: true });   // 并发下别人先传了，同样算成功
  return json({ ok: true, size: put.size, name: safeName(request.headers.get("x-file-name")) });
}

async function handleBlobGet(request, sha, env) {
  const key = blobKey(sha);
  if (!key) return json({ ok: false, err: "bad sha256" }, 400);
  // Range：阅读页（zip.js/foliate-js）和"大书边下边看"都要它；不带 Range 时行为跟以前一样。
  // 注意：真 R2 整份取的时候也会填 obj.range（offset=0, length=size），所以"是不是部分响应"
  // 必须看**请求**有没有带 Range，不能看 obj.range 有没有值——否则整份下载会被标成 206，
  // 桌面端的拉书逻辑只认 200。
  const range = (request.headers.get("range") || "").trim();
  let obj;
  try {
    obj = await env.BUCKET.get(key, range ? { range: request.headers } : undefined);
  } catch (e) {
    obj = await env.BUCKET.get(key);          // Range 不合法就整份给，别因此读不了书
  }
  if (!obj) return json({ ok: false, err: "not found" }, 404);
  const headers = {
    "content-type": (obj.httpMetadata && obj.httpMetadata.contentType) || "application/octet-stream",
    "accept-ranges": "bytes",
    etag: obj.etag,
    "cache-control": "private, max-age=86400",
    "x-file-name": encodeURIComponent(safeName(obj.customMetadata && obj.customMetadata.name)),
  };
  let status = 200;
  const partial = range && obj.range && typeof obj.range === "object";
  if (partial) {
    const off = Number(obj.range.offset) || 0;
    const len = obj.range.length === undefined ? obj.size - off : Number(obj.range.length);
    headers["content-range"] = `bytes ${off}-${off + len - 1}/${obj.size}`;
    headers["content-length"] = String(len);
    status = 206;
  } else {
    headers["content-length"] = String(obj.size);
  }
  return new Response(obj.body, { status, headers });
}

// 远端有哪些书（另一台机器据此补齐本地没有的）。只列 key / 大小 / 原名。
async function listBlobs(env) {
  const items = [];
  let cursor;
  for (let page = 0; page < 10; page += 1) {           // 最多 10 页 × 1000 = 一万本，够用
    const listed = await env.BUCKET.list({ prefix: BLOB_PREFIX, limit: 1000, cursor,
                                           include: ["customMetadata"] });
    for (const obj of listed.objects || []) {
      items.push({
        sha: String(obj.key || "").slice(BLOB_PREFIX.length),
        size: obj.size || 0,
        name: safeName(obj.customMetadata && obj.customMetadata.name),
      });
    }
    if (!listed.truncated) break;
    cursor = listed.cursor;
    if (!cursor) break;
  }
  return items;
}

async function handleBlobList(env) {
  return json({ ok: true, blobs: await listBlobs(env) });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    // 只走 https：登录下发的 Cookie 带 Secure，浏览器在 http 页面上会直接丢掉它——
    // 表现就是"输了 token 又弹回登录框"。手机上很多浏览器/内置 WebView 不会自动升级，
    // 所以在这里兜一层（308 保留方法和 body，表单/PUT 都能穿过去）。
    if (url.protocol === "http:" && url.hostname !== "localhost" && url.hostname !== "127.0.0.1") {
      url.protocol = "https:";
      return new Response(null, { status: 308, headers: { location: url.toString() } });
    }

    const path = url.pathname.replace(/\/+$/, "") || "/";

    // 阅读页（静态）：公开——页面里没有任何数据，/v1/* 仍然逐个鉴权
    if (path === "/" || path === "/r" || path.startsWith("/r/")) return serveStatic(request, env);

    // 登录：唯一的免鉴权写操作，只认 body 里的 token
    if (path === "/v1/login") {
      if (request.method !== "POST") return json({ ok: false, err: "method not allowed" }, 405);
      return handleLogin(request, env);
    }
    if (path === "/v1/logout" && request.method === "POST") return handleLogout();

    if (path === "/v1" || path === "/v1/") {
      return json({ ok: true, name: "stealthreader-sync", version: 1 });
    }
    if (path === "/v1/ping") {
      // 自检端点也给鉴权，免得被人当存活探针
      if (!authed(request, env)) return json({ ok: false, err: "unauthorized" }, 401);
      return json({ ok: true, server_time: Date.now() / 1000, version: 1, bucket: !!env.BUCKET });
    }
    if (!authed(request, env)) return json({ ok: false, err: "unauthorized" }, 401);

    if (path === "/v1/manifest" && request.method === "GET") return handleManifest(env);
    if (path === "/v1/shelf" && request.method === "GET") return handleShelf(env);

    const lite = path.match(/^\/v1\/lite\/([0-9a-f]{16})\/index\.json$/);
    if (lite) {
      if (request.method === "GET") return handleLiteIndexGet(lite[1], env);
      if (request.method === "PUT") return handleLiteIndexPut(request, lite[1], env);
      return json({ ok: false, err: "method not allowed" }, 405);
    }

    const state = path.match(/^\/v1\/state\/([a-z]+)$/);
    if (state) {
      const kind = state[1];
      if (!KINDS.has(kind)) return json({ ok: false, err: "unknown kind" }, 404);
      if (request.method === "GET") return handleStateGet(kind, env);
      if (request.method === "PUT") return handleStatePut(request, kind, env);
      return json({ ok: false, err: "method not allowed" }, 405);
    }

    if (path === "/v1/blob/check" && request.method === "POST") return handleBlobCheck(request, env);
    if (path === "/v1/blobs" && request.method === "GET") return handleBlobList(env);

    const blob = path.match(/^\/v1\/blob\/([0-9a-fA-F]{64})$/);
    if (blob) {
      if (request.method === "PUT") return handleBlobPut(request, blob[1].toLowerCase(), env);
      if (request.method === "GET") return handleBlobGet(request, blob[1].toLowerCase(), env);
      return json({ ok: false, err: "method not allowed" }, 405);
    }

    return json({ ok: false, err: "not found" }, 404);
  },
};
