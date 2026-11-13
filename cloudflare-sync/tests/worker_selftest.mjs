/**
 * worker.js 的自检：不用 workerd（这台机器上 workerd 起不来），
 * 用一个内存版 R2 把 Worker 的每条路由都打一遍。
 *
 *   node tests/worker_selftest.mjs
 *
 * 覆盖：鉴权 / ping / manifest / 状态无条件写被拒 / 新建 / 重复新建 / 条件写 /
 *      错 rev / 未知 kind / 书文件 check-put-get / 坏 sha / 旧版本归档。
 */

import worker from "../src/worker.js";

const TOKEN = "t0k3n-for-selftest";

// ------------------------------------------------------------ 内存 R2
function makeBucket() {
  const store = new Map(); // key -> {bytes, uploaded}
  let seq = 0;
  const etagOf = (bytes) => "-" + (++seq) + "-" + bytes.length; // 每次写都换 etag，够用了
  const mk = (key) => {
    const row = store.get(key);
    if (!row) return null;
    return {
      key,
      size: row.bytes.length,
      etag: row.etag,
      uploaded: row.uploaded,
      httpMetadata: row.httpMetadata || {},
      customMetadata: row.customMetadata || {},
      body: new Blob([row.bytes]).stream(),
      async arrayBuffer() { return row.bytes.buffer.slice(row.bytes.byteOffset, row.bytes.byteOffset + row.bytes.byteLength); },
      async text() { return Buffer.from(row.bytes).toString("utf8"); },
    };
  };
  return {
    _store: store,
    async head(key) {
      const row = store.get(key);
      return row ? { key, size: row.bytes.length, etag: row.etag, uploaded: row.uploaded,
                     customMetadata: row.customMetadata || {} } : null;
    },
    // 支持 Range（阅读页的 zip.js 要靠它）：只回被请求的那一段，并带上 range 元信息
    async get(key, opts = {}) {
      const obj = mk(key);
      if (!obj) return null;
      const hdr = opts && opts.range;
      const raw = hdr && typeof hdr.get === "function" ? hdr.get("range") : "";
      const row = store.get(key);
      // 真 R2 整份取的时候也会填 range（offset=0, length=size），这里照样模拟——
      // 否则"不带 Range 却回 206"这类 bug 在本地测不出来
      if (!raw || !row) return { ...obj, range: { offset: 0, length: obj.size } };
      const m = /^bytes=(\d*)-(\d*)$/.exec(String(raw).trim());
      if (!m) return obj;
      const size = row.bytes.length;
      let start = m[1] === "" ? null : Number(m[1]);
      let end = m[2] === "" ? null : Number(m[2]);
      if (start === null) { start = Math.max(0, size - (end ?? 0)); end = size - 1; }
      else if (end === null || end >= size) end = size - 1;
      const length = Math.max(0, end - start + 1);
      const slice = row.bytes.subarray(start, start + length);
      return {
        key, size, etag: row.etag, uploaded: row.uploaded,
        httpMetadata: row.httpMetadata || {}, customMetadata: row.customMetadata || {},
        range: { offset: start, length },
        body: new Blob([slice]).stream(),
        async arrayBuffer() { return slice.buffer.slice(slice.byteOffset, slice.byteOffset + slice.byteLength); },
      };
    },
    async put(key, value, opts = {}) {
      let bytes;
      if (value instanceof ArrayBuffer) bytes = new Uint8Array(value);
      else if (ArrayBuffer.isView(value)) bytes = new Uint8Array(value.buffer, value.byteOffset, value.byteLength);
      else if (value && typeof value.getReader === "function") bytes = new Uint8Array(await new Response(value).arrayBuffer());
      else bytes = new TextEncoder().encode(String(value));
      const cur = store.get(key);
      const only = opts.onlyIf || {};
      if (only.etagDoesNotMatch === "*" && cur) return null;
      if (only.etagMatches && (!cur || cur.etag !== only.etagMatches)) return null;
      const etag = etagOf(bytes);
      store.set(key, { bytes: Uint8Array.from(bytes), etag, uploaded: new Date(),
                       httpMetadata: opts.httpMetadata, customMetadata: opts.customMetadata || {} });
      return { key, size: bytes.length, etag, uploaded: new Date() };
    },
    async list({ prefix = "" } = {}) {
      const objects = [...store.entries()].filter(([k]) => k.startsWith(prefix)).map(([key, row]) => ({ key, size: row.bytes.length, etag: row.etag, customMetadata: row.customMetadata || {} }));
      return { objects, truncated: false };
    },
    async delete(key) { store.delete(key); },
  };
}

// ------------------------------------------------------------ 小工具
let pass = 0, fail = 0;
const bad = [];
function check(name, cond, extra = "") {
  if (cond) { pass++; console.log("  ok   " + name); }
  else { fail++; bad.push(name + (extra ? "  << " + extra : "")); console.log("  FAIL " + name + (extra ? "  << " + extra : "")); }
}

/** 假的静态资源：只回一页 HTML，够验"HTML 带上 CSP"这一条。 */
function makeAssets() {
  return {
    async fetch() {
      return new Response("<!doctype html><title>reader</title><p>shelf</p>", {
        status: 200,
        headers: { "content-type": "text/html; charset=utf-8" },
      });
    },
  };
}

const env = () => ({ SYNC_TOKEN: TOKEN, BUCKET: makeBucket(), ASSETS: makeAssets() });
const H = (extra = {}) => ({ Authorization: "Bearer " + TOKEN, ...extra });
const call = (envObj, path, init = {}) => worker.fetch(new Request("https://sync.test" + path, init), envObj);
const jcall = async (envObj, path, init = {}) => {
  const res = await call(envObj, path, init);
  let body = null;
  try { body = await res.json(); } catch { body = null; }
  return { res, body };
};

// ------------------------------------------------------------ 1. 鉴权
{
  const e = env();
  const home = await call(e, "/");
  check("根路径不需要 token（回的是阅读页，不是 JSON）",
        home.status === 200 && (home.headers.get("content-type") || "").includes("text/html"),
        home.headers.get("content-type"));
  check("阅读页 HTML 带 CSP 且锁住 script-src",
        (home.headers.get("content-security-policy") || "").includes("script-src 'self'"),
        home.headers.get("content-security-policy"));
  check("没带 token → 401", (await call(e, "/v1/manifest")).status === 401);
  check("token 错了 → 401", (await call(e, "/v1/manifest", { headers: { Authorization: "Bearer nope" } })).status === 401);
  check("token 对了 → 200", (await call(e, "/v1/manifest", { headers: H() })).status === 200);
  const pin = await jcall(e, "/v1/ping", { headers: H() });
  check("ping 通", pin.res.status === 200 && pin.body.ok === true && pin.body.bucket === true, JSON.stringify(pin.body));
}

// ------------------------------------------------------------ 2. 状态对象
const e = env();
const payload = (n) => JSON.stringify({ v: 1, schema: 1, n, books: {} });

{
  const plain = await jcall(e, "/v1/state/progress", { method: "PUT", headers: H({ "Content-Type": "application/json" }), body: payload(1) });
  check("无条件写被拒（428）", plain.res.status === 428, plain.res.status);

  const created = await jcall(e, "/v1/state/progress", { method: "PUT", headers: H({ "Content-Type": "application/json", "X-Create": "1" }), body: payload(1) });
  check("新建成功并给 rev", created.res.status === 200 && !!created.body.rev, JSON.stringify(created.body));
  const rev1 = created.body.rev;

  const again = await jcall(e, "/v1/state/progress", { method: "PUT", headers: H({ "Content-Type": "application/json", "X-Create": "1" }), body: payload(2) });
  check("重复新建 → 412 且带回当前 rev", again.res.status === 412 && again.body.rev === rev1, JSON.stringify(again.body));

  const got = await call(e, "/v1/state/progress", { headers: H() });
  const text = await got.text();
  check("GET 拿回同样的内容", JSON.parse(text).n === 1, text);
  check("GET 带 ETag", (got.headers.get("etag") || "") === rev1, got.headers.get("etag") + " vs " + rev1);

  const wrong = await jcall(e, "/v1/state/progress", { method: "PUT", headers: H({ "Content-Type": "application/json", "If-Match": rev1 + "x" }), body: payload(3) });
  check("错 rev → 412", wrong.res.status === 412, wrong.res.status);

  const upd = await jcall(e, "/v1/state/progress", { method: "PUT", headers: H({ "Content-Type": "application/json", "If-Match": rev1 }), body: payload(3) });
  check("对 rev → 200 且换新 rev", upd.res.status === 200 && upd.body.rev && upd.body.rev !== rev1, JSON.stringify(upd.body));

  const after = JSON.parse(await (await call(e, "/v1/state/progress", { headers: H() })).text());
  check("内容真的换了", after.n === 3, JSON.stringify(after));

  const hist = await e.BUCKET.list({ prefix: "v1/state/history/progress/" });
  check("旧版本归档了一份", hist.objects.length === 1, hist.objects.length);

  check("未知 kind → 404", (await call(e, "/v1/state/whatever", { headers: H() })).status === 404);
  check("未知路由 → 404", (await call(e, "/v1/nope", { headers: H() })).status === 404);
  const man = await jcall(e, "/v1/manifest", { headers: H() });
  check("manifest 里有 progress", !!man.body.kinds.progress && !!man.body.kinds.progress.rev, JSON.stringify(man.body));
  check("manifest 里没有没写过的 kind", man.body.kinds.stats === undefined, JSON.stringify(Object.keys(man.body.kinds)));
}

// ------------------------------------------------------------ 3. 书文件
{
  const data = Buffer.from("epub-bytes-1234");
  const crypto = await import("node:crypto");
  const sha = crypto.createHash("sha256").update(data).digest("hex");

  const chk0 = await jcall(e, "/v1/blob/check", { method: "POST", headers: H({ "Content-Type": "application/json" }), body: JSON.stringify({ sha256: [sha] }) });
  check("check 说缺", chk0.body.missing.length === 1, JSON.stringify(chk0.body));

  const noLen = await jcall(e, "/v1/blob/" + sha, { method: "PUT", headers: H({ "Content-Type": "application/octet-stream" }), body: data });
  check("没 Content-Length → 411", noLen.res.status === 411, noLen.res.status);

  const put = await jcall(e, "/v1/blob/" + sha, { method: "PUT", headers: H({ "Content-Type": "application/octet-stream", "Content-Length": String(data.length) }), body: data });
  check("书文件上传成功", put.res.status === 200 && put.body.size === data.length, JSON.stringify(put.body));

  const put2 = await jcall(e, "/v1/blob/" + sha, { method: "PUT", headers: H({ "Content-Type": "application/octet-stream", "Content-Length": String(data.length) }), body: data });
  check("同一份内容再传：幂等（existed）", put2.res.status === 200 && put2.body.existed === true, JSON.stringify(put2.body));

  const chk1 = await jcall(e, "/v1/blob/check", { method: "POST", headers: H({ "Content-Type": "application/json" }), body: JSON.stringify({ sha256: [sha, "0".repeat(64)] }) });
  check("check 说只有 0 缺", chk1.body.missing.length === 1 && chk1.body.missing[0] === "0".repeat(64), JSON.stringify(chk1.body));

  const back = await call(e, "/v1/blob/" + sha, { headers: H() });
  const bytes = Buffer.from(await back.arrayBuffer());
  check("取回来的字节一致", bytes.equals(data), bytes.toString());
  check("坏 sha 的路径直接 404（不匹配路由，生不出 key）", (await call(e, "/v1/blob/zzz", { headers: H() })).status === 404);
  check("没传过的 sha → 404", (await call(e, "/v1/blob/" + "1".repeat(64), { headers: H() })).status === 404);

  // 缓存：24 小时私有缓存 + 过期后靠 etag 回 304（不然每次重开一本 1 MB 的书都要重下）
  const cc = back.headers.get("cache-control") || "";
  const et = back.headers.get("etag") || "";
  check("书的 GET 带 etag 和 24 小时私有缓存", !!et && cc.includes("private") && cc.includes("max-age=86400"), cc + " / " + et);
  const re = await call(e, "/v1/blob/" + sha, { headers: H({ "If-None-Match": et }) });
  check("同一个 If-None-Match → 304，一个字节都不给",
    re.status === 304 && (await re.arrayBuffer()).byteLength === 0, re.status);
  const reQuote = await call(e, "/v1/blob/" + sha, { headers: H({ "If-None-Match": '"' + et + '"' }) });
  check("etag 被浏览器加了引号也认（304）", reQuote.status === 304, reQuote.status);
  const reBad = await call(e, "/v1/blob/" + sha, { headers: H({ "If-None-Match": "nope" }) });
  check("etag 对不上 → 还是 200 整本", reBad.status === 200, reBad.status);
  const reRange = await call(e, "/v1/blob/" + sha, { headers: H({ Range: "bytes=0-9", "If-None-Match": et }) });
  const rangeBytes = Buffer.from(await reRange.arrayBuffer());
  check("带 Range 时不走 304（要什么给什么）",
    reRange.status === 206 && rangeBytes.length === 10, reRange.status + "/" + rangeBytes.length);
}

// ------------------------------------------------------------ 4. 别的桶里不该有垃圾
{
  const keys = [...e.BUCKET._store.keys()].sort();
  check("桶里只有我们自己的前缀", keys.every((k) => k.startsWith("v1/")), keys.join(","));
}


// ------------------------------------------------------------ 12. 书文件：原名 + 列清单
{
  const e = env();
  const sha = "a".repeat(64);
  const bytes = new Uint8Array([1, 2, 3, 4, 5]);
  const putRes = await jcall(e, "/v1/blob/" + sha, {
    method: "PUT",
    headers: H({ "Content-Length": String(bytes.length), "X-File-Name": encodeURIComponent("跨越边界的社区 (项飙).epub") }),
    body: bytes,
  });
  check("传书带原名 → 200", putRes.res.status === 200 && putRes.body.ok === true, JSON.stringify(putRes.body));

  const listed = await jcall(e, "/v1/blobs", { headers: H() });
  check("列书清单 → 200", listed.res.status === 200 && Array.isArray(listed.body.blobs), JSON.stringify(listed.body).slice(0, 120));
  const row = (listed.body.blobs || []).find((b) => b.sha === sha);
  check("清单里有这本书且带原名", !!row && row.name === "跨越边界的社区 (项飙).epub", JSON.stringify(row));
  check("清单里带大小", !!row && row.size === bytes.length, JSON.stringify(row));

  const got = await call(e, "/v1/blob/" + sha, { headers: H() });
  check("下书回原名响应头", decodeURIComponent(got.headers.get("x-file-name") || "") === "跨越边界的社区 (项飙).epub",
        got.headers.get("x-file-name"));
  check("下书字节一致", (await got.arrayBuffer()).byteLength === bytes.length);

  const evil = await jcall(e, "/v1/blob/" + "b".repeat(64), {
    method: "PUT",
    headers: H({ "Content-Length": "3", "X-File-Name": encodeURIComponent("../../etc/passwd") }),
    body: new Uint8Array([9, 9, 9]),
  });
  check("恶意原名照样能传", evil.res.status === 200);
  const listed2 = await jcall(e, "/v1/blobs", { headers: H() });
  const evilRow = (listed2.body.blobs || []).find((b) => b.sha === "b".repeat(64));
  check("原名里没有路径分隔符", !!evilRow && !evilRow.name.includes("/") && !evilRow.name.includes("\\"),
        JSON.stringify(evilRow));

  check("没带 token 列不出来 → 401", (await call(e, "/v1/blobs")).status === 401);
}

// ------------------------------------------------------------ 5. 阅读页：登录 Cookie / 书架 / 章节表 / Range
{
  const e2 = env();
  const sha = "c".repeat(64);
  const uid = sha.slice(0, 16);
  const bytes = new Uint8Array([10, 20, 30, 40, 50, 60]);
  await call(e2, "/v1/blob/" + sha, {
    method: "PUT",
    headers: H({ "Content-Length": String(bytes.length), "X-File-Name": encodeURIComponent("测试书.epub") }),
    body: bytes,
  });

  const bad = await jcall(e2, "/v1/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token: "nope" }) });
  check("登录：token 不对 → 401", bad.res.status === 401, bad.res.status);
  check("登录：错的不会发 Cookie", !bad.res.headers.get("set-cookie"));

  const ok = await jcall(e2, "/v1/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token: TOKEN }) });
  const cookie = ok.res.headers.get("set-cookie") || "";
  check("登录：对了 → 200 且发 Cookie", ok.res.status === 200 && cookie.includes("srtoken="), cookie);
  check("Cookie 是 HttpOnly + Secure + SameSite=Lax",
        /HttpOnly/i.test(cookie) && /Secure/i.test(cookie) && /SameSite=Lax/i.test(cookie), cookie);

  const withCookie = { Cookie: cookie.split(";")[0] };
  check("只带 Cookie 也能读 manifest", (await call(e2, "/v1/manifest", { headers: withCookie })).status === 200);
  check("只带 Cookie 也能读 ping", (await call(e2, "/v1/ping", { headers: withCookie })).status === 200);
  check("错 Cookie → 401", (await call(e2, "/v1/manifest", { headers: { Cookie: "srtoken=wrong" } })).status === 401);
  check("表单登录也认（Kindle 那条路要用）", (await jcall(e2, "/v1/login", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: "token=" + encodeURIComponent(TOKEN),
  })).res.status === 200);

  const shelf0 = await jcall(e2, "/v1/shelf", { headers: withCookie });
  check("书架：200 且只有一本书", shelf0.res.status === 200 && shelf0.body.books.length === 1,
        JSON.stringify(shelf0.body).slice(0, 160));
  const row0 = shelf0.body.books[0];
  check("书架：uid 是 sha 前 16 位", row0.uid === uid, row0.uid);
  check("书架：带原名和大小", row0.name === "测试书.epub" && row0.size === bytes.length, JSON.stringify(row0));
  check("书架：没读过时 pos=0、at=0", row0.pos === 0 && row0.at === 0, JSON.stringify(row0));

  const putProg = await jcall(e2, "/v1/state/progress", {
    method: "PUT",
    headers: { ...withCookie, "Content-Type": "application/json", "X-Create": "1" },
    body: JSON.stringify({ v: 1, schema: 1, updated_at: 1, device: "d", books: { [uid]: { title: "测试书", pos: 120, total: 480, at: 1700000000, dev: "d", hidden: false } } }),
  });
  check("写一条进度（书架的输入）", putProg.res.status === 200, JSON.stringify(putProg.body));
  const shelf1 = await jcall(e2, "/v1/shelf", { headers: withCookie });
  const row1 = shelf1.body.books[0];
  check("书架：把进度并进来了", row1.pos === 120 && row1.total === 480 && row1.title === "测试书", JSON.stringify(row1));

  const idx = { total: 480, generated_at: 1, schema: 1, sections: [{ title: "一", start: 0, level: 1 }] };
  const putIdx = await jcall(e2, `/v1/lite/${uid}/index.json`, {
    method: "PUT", headers: { ...withCookie, "Content-Type": "application/json" }, body: JSON.stringify(idx),
  });
  check("章节表：能存", putIdx.res.status === 200 && putIdx.body.size > 0, JSON.stringify(putIdx.body));
  const getIdx = await call(e2, `/v1/lite/${uid}/index.json`, { headers: withCookie });
  check("章节表：能取回同样的内容", (await getIdx.json()).total === 480);
  check("章节表：uid 不合法 → 404", (await call(e2, "/v1/lite/zzz/index.json", { headers: withCookie })).status === 404);
  check("章节表：没登录 → 401", (await call(e2, `/v1/lite/${uid}/index.json`)).status === 401);

  const part = await call(e2, "/v1/blob/" + sha, { headers: { ...withCookie, Range: "bytes=1-3" } });
  check("Range：206", part.status === 206, part.status);
  check("Range：Content-Range 对", part.headers.get("content-range") === `bytes 1-3/${bytes.length}`,
        part.headers.get("content-range"));
  const partBytes = Buffer.from(await part.arrayBuffer());
  check("Range：只回那三个字节", partBytes.length === 3 && partBytes[0] === 20 && partBytes[2] === 40, partBytes.toString());
  check("Range：响应带 Accept-Ranges", part.headers.get("accept-ranges") === "bytes");
  const whole = await call(e2, "/v1/blob/" + sha, { headers: withCookie });
  check("不带 Range 还是 200 + 整份", whole.status === 200 && (await whole.arrayBuffer()).byteLength === bytes.length);
}

console.log("\n" + pass + " 通过 / " + fail + " 失败");
if (fail) { console.log(bad.map((b) => "  - " + b).join("\n")); process.exit(1); }
