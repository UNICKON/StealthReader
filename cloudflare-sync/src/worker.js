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

function authed(request, env) {
  const want = (env.SYNC_TOKEN || "").trim();
  if (!want) return false;                       // 没配 token = 谁也别想进来
  const got = (request.headers.get("authorization") || "").replace(/^Bearer\s+/i, "").trim();
  return safeEqual(got, want);
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

async function handleBlobGet(sha, env) {
  const key = blobKey(sha);
  if (!key) return json({ ok: false, err: "bad sha256" }, 400);
  const obj = await env.BUCKET.get(key);
  if (!obj) return json({ ok: false, err: "not found" }, 404);
  return new Response(obj.body, {
    status: 200,
    headers: {
      "content-type": (obj.httpMetadata && obj.httpMetadata.contentType) || "application/octet-stream",
      "content-length": String(obj.size),
      etag: obj.etag,
      "x-file-name": encodeURIComponent(safeName(obj.customMetadata && obj.customMetadata.name)),
    },
  });
}

// 远端有哪些书（另一台机器据此补齐本地没有的）。只列 key / 大小 / 原名。
async function handleBlobList(env) {
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
  return json({ ok: true, blobs: items });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, "") || "/";

    if (path === "/" || path === "/v1" || path === "/v1/") {
      return json({ ok: true, name: "stealthreader-sync", version: 1 });
    }
    if (path === "/v1/ping") {
      // 自检端点也给鉴权，免得被人当存活探针
      if (!authed(request, env)) return json({ ok: false, err: "unauthorized" }, 401);
      return json({ ok: true, server_time: Date.now() / 1000, version: 1, bucket: !!env.BUCKET });
    }
    if (!authed(request, env)) return json({ ok: false, err: "unauthorized" }, 401);

    if (path === "/v1/manifest" && request.method === "GET") return handleManifest(env);

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
      if (request.method === "GET") return handleBlobGet(blob[1].toLowerCase(), env);
      return json({ ok: false, err: "method not allowed" }, 405);
    }

    return json({ ok: false, err: "not found" }, 404);
  },
};
