#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""假同步后端：用标准库在 127.0.0.1 上跑一个内存版 R2 + Worker。

它的作用只有两个：
  * 让 `tests/probe_sync.py` 不装 node、不联外网也能把整条链路跑一遍；
  * 能按命令"断网 / 401 / 强制 412"，用来验客户端的退避与冲突重试。

协议跟 `cloudflare-sync/src/worker.js` 一致（那边有 27 条自检），两边不一致时以设计文档为准。
"""
import json
import threading
import urllib.parse
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KINDS = {"progress", "annotations", "stats", "settings"}


class FakeSyncServer:
    def __init__(self, token="tok", host="127.0.0.1", port=0):
        self.token = token
        self.host = host
        self.port = port
        self.objects = {}          # key -> {"bytes": b, "etag": str, "at": float, "content_type": str}
        self.blob_names = {}       # sha -> 原始文件名（已百分号编码，和 Worker 一致）
        self.requests = []         # (method, path, status)
        self.offline = False       # True：所有请求直接断（模拟没网）
        self.force_401 = False
        self.fail_times = 0        # >0：接下来 N 个请求回 500
        self._httpd = None
        self._thread = None
        self._hist_seq = 0

    # ---------------- 生命周期 ----------------
    def start(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, code, body=b"", extra=None):
                outer.requests.append((self.command, self.path, code))   # 每个请求都记一笔
                self.send_response(code)
                headers = {"Content-Length": str(len(body)), "Connection": "close"}
                for k, v in (extra or {}).items():
                    headers[k] = v
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def handle_one_request(self):
                try:
                    super().handle_one_request()
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def _json(self, code, obj, extra=None):
                self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                           {"Content-Type": "application/json; charset=utf-8", **(extra or {})})

            def _guard(self):
                """返回 True 表示已经回过话了，别再往下走。"""
                if outer.offline:
                    self.close_connection = True
                    try:
                        self.connection.close()
                    except OSError:
                        pass
                    outer.requests.append((self.command, self.path, 0))
                    return True
                if outer.fail_times > 0:
                    outer.fail_times -= 1
                    self._json(500, {"ok": False, "err": "boom"})        # _send 里已经记过
                    return True
                return False

            def _body(self):
                length = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(length) if length else b""

            def _auth(self):
                if outer.force_401 or (self.headers.get("Authorization") or "") != "Bearer " + outer.token:
                    self._json(401, {"ok": False, "err": "unauthorized"})
                    outer.requests.append((self.command, self.path, 401))
                    return False
                return True

            # ---- 路由 ----
            def do_GET(self):
                if self._guard():
                    return
                path = self.path.split("?")[0].rstrip("/") or "/"
                if path == "/":
                    self._json(200, {"ok": True, "name": "fake-sync", "version": 1})
                    return
                if not self._auth():
                    return
                if path == "/v1/ping":
                    self._json(200, {"ok": True, "server_time": time.time(), "bucket": True})
                elif path == "/v1/manifest":
                    kinds = {}
                    for key, row in outer.objects.items():
                        if key.startswith("v1/state/") and "/history/" not in key:
                            kinds[key.split("/")[2].split(".")[0]] = {
                                "rev": row["etag"], "size": len(row["bytes"]), "updated_at": row["at"]}
                    self._json(200, {"ok": True, "kinds": kinds})
                elif path.startswith("/v1/state/"):
                    kind = path.split("/")[3]
                    row = outer.objects.get("v1/state/%s.json" % kind)
                    if not row:
                        self._json(404, {"ok": False, "err": "not found"})
                        return
                    self._send(200, row["bytes"], {"Content-Type": "application/json; charset=utf-8",
                                                   "ETag": row["etag"]})
                elif path == "/v1/blobs":
                    blobs = []
                    for key, row in outer.objects.items():
                        if key.startswith("v1/blob/"):
                            sha = key[len("v1/blob/"):]
                            raw_name = outer.blob_names.get(sha, "")
                            blobs.append({"sha": sha, "size": len(row["bytes"]),
                                          "name": urllib.parse.unquote(raw_name)})
                    self._json(200, {"ok": True, "blobs": blobs})
                elif path.startswith("/v1/blob/"):
                    sha = path.split("/")[3]
                    row = outer.objects.get("v1/blob/" + sha)
                    if not row:
                        self._json(404, {"ok": False, "err": "not found"})
                        return
                    self._send(200, row["bytes"], {"Content-Type": "application/octet-stream",
                                                   "ETag": row["etag"],
                                                   "X-File-Name": outer.blob_names.get(sha, "")})
                elif path.startswith("/v1/lite/"):
                    row = outer.objects.get(path.lstrip("/"))
                    if not row:
                        self._json(404, {"ok": False, "err": "not found"})
                        return
                    self._send(200, row["bytes"], {"Content-Type": "application/json; charset=utf-8",
                                                   "ETag": row["etag"]})
                else:
                    self._json(404, {"ok": False, "err": "not found"})

            def do_PUT(self):
                if self._guard():
                    return
                if not self._auth():
                    return
                path = self.path.split("?")[0]
                body = self._body()
                if path.startswith("/v1/state/"):
                    kind = path.split("/")[3]
                    if kind not in KINDS:
                        self._json(404, {"ok": False, "err": "unknown kind"})
                        return
                    key = "v1/state/%s.json" % kind
                    cur = outer.objects.get(key)
                    if (self.headers.get("X-Create") or "") == "1":
                        if cur:
                            self._json(412, {"ok": False, "err": "exists", "rev": cur["etag"]})
                            return
                        rev = outer._put(key, body, "application/json")
                        self._json(200, {"ok": True, "rev": rev})
                        return
                    if_match = (self.headers.get("If-Match") or "").strip()
                    if not if_match:
                        self._json(428, {"ok": False, "err": "If-Match required"})
                        return
                    if not cur:
                        self._json(412, {"ok": False, "err": "gone", "rev": ""})
                        return
                    if if_match != cur["etag"]:
                        self._json(412, {"ok": False, "err": "conflict", "rev": cur["etag"]})
                        return
                    outer._archive(kind, cur)
                    rev = outer._put(key, body, "application/json")
                    self._json(200, {"ok": True, "rev": rev})
                elif path.startswith("/v1/lite/"):
                    outer._put(path.lstrip("/"), body, "application/json")
                    self._json(200, {"ok": True, "size": len(body)})
                elif path.startswith("/v1/blob/"):
                    sha = path.split("/")[3]
                    if len(sha) != 64:
                        self._json(404, {"ok": False, "err": "not found"})
                        return
                    key = "v1/blob/" + sha
                    if key in outer.objects:
                        self._json(200, {"ok": True, "existed": True, "size": len(outer.objects[key]["bytes"])})
                        return
                    rev = outer._put(key, body, self.headers.get("Content-Type") or "application/octet-stream")
                    outer.blob_names[sha] = self.headers.get("X-File-Name") or ""
                    self._json(200, {"ok": True, "size": len(body), "rev": rev})
                else:
                    self._json(404, {"ok": False, "err": "not found"})

            def do_POST(self):
                if self._guard():
                    return
                if not self._auth():
                    return
                path = self.path.split("?")[0]
                body = self._body()
                if path == "/v1/blob/check":
                    try:
                        want = json.loads(body.decode("utf-8")).get("sha256") or []
                    except Exception:
                        self._json(400, {"ok": False, "err": "bad json"})
                        return
                    missing = [s for s in want if "v1/blob/" + str(s) not in outer.objects]
                    self._json(200, {"ok": True, "missing": missing})
                else:
                    self._json(404, {"ok": False, "err": "not found"})

        self._httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None

    @property
    def base_url(self):
        return "http://%s:%d" % (self.host, self.port)

    # ---------------- 存储 ----------------
    def _put(self, key, body, content_type):
        etag = uuid.uuid4().hex[:16]
        self.objects[key] = {"bytes": bytes(body), "etag": etag, "at": time.time(), "content_type": content_type}
        return etag

    def _archive(self, kind, row):
        self._hist_seq += 1
        key = "v1/state/history/%s/%04d.json" % (kind, self._hist_seq)
        self.objects[key] = dict(row)
        keys = sorted(k for k in self.objects if k.startswith("v1/state/history/%s/" % kind))
        for old in keys[:-5]:
            self.objects.pop(old, None)

    # ---------------- 给测试用的手脚 ----------------
    def state(self, kind):
        row = self.objects.get("v1/state/%s.json" % kind)
        return json.loads(row["bytes"].decode("utf-8")) if row else None

    def put_state_directly(self, kind, payload):
        """绕过客户端直接写远端，用来造"另一台机器改了"的局面。"""
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        key = "v1/state/%s.json" % kind
        cur = self.objects.get(key)
        if cur:
            self._archive(kind, cur)
        return self._put(key, body, "application/json")

    def blob_keys(self):
        return [k for k in self.objects if k.startswith("v1/blob/")]

    def counts(self):
        out = {}
        for method, path, status in self.requests:
            out[(method, status)] = out.get((method, status), 0) + 1
        return out


if __name__ == "__main__":
    srv = FakeSyncServer(token="tok").start()
    print("假后端起来了：%s   token=tok" % srv.base_url)
    print("按 Ctrl+C 停。")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        srv.stop()
