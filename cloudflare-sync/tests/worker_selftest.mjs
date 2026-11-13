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
    async get(key) { return mk(key); },
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

const env = () => ({ SYNC_TOKEN: TOKEN, BUCKET: makeBucket() });
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
  check("根路径不需要 token", (await call(e, "/")).status === 200);
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

console.log("\n" + pass + " 通过 / " + fail + " 失败");
if (fail) { console.log(bad.map((b) => "  - " + b).join("\n")); process.exit(1); }
