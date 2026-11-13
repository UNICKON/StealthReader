# -*- coding: utf-8 -*-
"""阅读页的本地假后端：静态文件 + /v1（登录/书架/进度/设置/章节表/带 Range 的书）。

只为在浏览器里验前端逻辑，不连线上、不碰真 token：
    python reader_mock_server.py [端口]
"""
import hashlib
import io
import json
import os
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(SR, "cloudflare-sync", "web")

COOKIE = "srtoken=demo-token"
STATE = {}
BOOK = {"sha": "", "name": "测试书.epub", "bytes": b"", "uid": "", "total": 60000}
REQUESTS = []


def load_book():
    """从书架里挑最小的一本真 epub 当素材。"""
    cfg = json.load(io.open(os.path.join(SR, "config.json"), encoding="utf-8"))
    shelf = cfg.get("shelf_dir")
    best = None
    for base in [shelf] + list((cfg.get("local_book_positions") or {}).keys()):
        if not base:
            continue
        cands = [base] if os.path.isfile(base) else []
        if os.path.isdir(base):
            cands = [os.path.join(base, n) for n in os.listdir(base) if n.lower().endswith(".epub")]
        for p in cands:
            try:
                size = os.path.getsize(p)
            except OSError:
                continue
            if size > 300 * 1024 and (best is None or size < best[0]):
                best = (size, p)
    if not best:
        raise SystemExit("找不到可用的 epub 素材")
    with open(best[1], "rb") as f:
        data = f.read()
    BOOK["bytes"] = data
    BOOK["sha"] = hashlib.sha256(data).hexdigest()
    BOOK["uid"] = BOOK["sha"][:16]
    BOOK["name"] = os.path.basename(best[1])
    print("素材：%s（%.1f MB, uid=%s）" % (BOOK["name"], len(data) / 1048576.0, BOOK["uid"]))


def progress_doc():
    return STATE.get("progress") or {"v": 1, "schema": 1, "updated_at": 0, "device": "mock", "books": {}}


def settings_doc():
    return STATE.get("settings") or {"v": 1, "updated_at": 0, "device": "mock",
                                     "values": {"font_size": 19, "line_spacing": 100,
                                                "paragraph_spacing": 0, "page_margin_chars": 1.0,
                                                "first_line_indent": 2.0, "font_family": "SimSun"},
                                     "clock": {}}


def stats_doc():
    """账本：今天已经有一场（桌面端记的），网页这边只往里加自己的会话。"""
    return STATE.get("stats") or {"v": 1, "updated_at": 0, "device": "mock",
                                  "sessions": [],
                                  "days": {time.strftime("%Y-%m-%d"):
                                           {"n": 3, "sec": 2385.0, "chars": 15724.0}},
                                  "books": {}}


class QuietServer(ThreadingHTTPServer):
    """浏览器被 kill 掉时不要往 stderr 吐 ConnectionResetError。"""
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        pass


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    # ---- 小工具 ----
    def authed(self):
        return COOKIE in (self.headers.get("Cookie") or "")

    def send(self, code, body=b"", ctype="application/json; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def js(self, code, obj, extra=None):
        self.send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), extra=extra)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    # ---- GET ----
    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        path = url.path
        REQUESTS.append(("GET", path))
        if path.startswith("/r/"):
            return self.static(path[3:] or "index.html")
        if path == "/v1/ping":
            return self.js(200, {"ok": True}) if self.authed() else self.js(401, {"ok": False})
        if not self.authed():
            return self.js(401, {"ok": False})
        if path == "/v1/shelf":
            row = {k: BOOK[k] for k in ("sha", "name", "uid", "total")}
            pos = ((progress_doc().get("books") or {}).get(BOOK["uid"]) or {})
            row.update({"size": len(BOOK["bytes"]), "title": pos.get("title") or BOOK["name"],
                        "pos": pos.get("pos") or 0, "total": pos.get("total") or BOOK["total"],
                        "at": pos.get("at") or 0, "dev": pos.get("dev") or "", "hidden": False})
            return self.js(200, {"ok": True, "books": [row]})
        if path.startswith("/v1/state/"):
            kind = path.rsplit("/", 1)[-1]
            if kind not in ("progress", "settings", "annotations", "stats"):
                return self.js(404, {"ok": False})
            doc = (progress_doc() if kind == "progress"
                   else stats_doc() if kind == "stats" else settings_doc())
            raw = json.dumps(doc, ensure_ascii=False).encode("utf-8")
            return self.send(200, raw, extra={"ETag": '"%d"' % len(raw)})
        if path.startswith("/v1/lite/"):
            return self.js(200, {"schema": 1, "total": BOOK["total"], "uid": BOOK["uid"],
                                 "sections": [{"title": "第一章", "start": 0, "level": 1},
                                              {"title": "第二章", "start": BOOK["total"] // 2, "level": 1}]})
        if path.startswith("/v1/blob/"):
            return self.blob()
        return self.js(404, {"ok": False, "err": "not found"})

    def static(self, rel):
        rel = rel.replace("/", os.sep)
        full = os.path.normpath(os.path.join(WEB, rel))
        if not full.startswith(os.path.normpath(WEB)) or not os.path.isfile(full):
            return self.send(404, b"nf", "text/plain")
        ctype = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".md": "text/plain; charset=utf-8",
                 ".json": "application/json"}.get(os.path.splitext(full)[1].lower(), "application/octet-stream")
        with open(full, "rb") as f:
            data = f.read()
        extra = {"Cache-Control": "no-cache"}
        if ctype.startswith("text/html"):
            extra["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data: blob:; font-src 'self' data: blob:; "
                "media-src 'self' data: blob:; connect-src 'self'; frame-src 'self' blob:; "
                "base-uri 'none'; form-action 'self'; object-src 'none'")
        return self.send(200, data, ctype, extra)

    def blob(self):
        data = BOOK["bytes"]
        rng = (self.headers.get("Range") or "").strip()
        if not rng:
            return self.send(200, data, "application/epub+zip", {"Accept-Ranges": "bytes"})
        try:
            spec = rng.split("=", 1)[1]
            a, _, b = spec.partition("-")
            start = int(a) if a else 0
            end = int(b) if b else len(data) - 1
        except Exception:
            return self.send(200, data, "application/epub+zip", {"Accept-Ranges": "bytes"})
        end = min(end, len(data) - 1)
        chunk = data[start:end + 1]
        return self.send(206, chunk, "application/epub+zip",
                         {"Accept-Ranges": "bytes",
                          "Content-Range": "bytes %d-%d/%d" % (start, end, len(data))})

    # ---- POST / PUT ----
    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        raw = self.body()
        REQUESTS.append(("POST", path))
        if path == "/v1/login":
            ctype = (self.headers.get("Content-Type") or "").lower()
            token = ""
            if "json" in ctype:
                try:
                    token = (json.loads(raw.decode("utf-8")) or {}).get("token") or ""
                except Exception:
                    token = ""
            else:
                token = (urllib.parse.parse_qs(raw.decode("utf-8")).get("token") or [""])[0]
            if token != "demo-token":
                return self.js(401, {"ok": False, "err": "bad token"})
            return self.js(200, {"ok": True}, {"Set-Cookie": COOKIE + "; Path=/; HttpOnly; SameSite=Lax"})
        if path == "/v1/logout":
            return self.js(200, {"ok": True}, {"Set-Cookie": "srtoken=; Path=/; Max-Age=0"})
        return self.js(404, {"ok": False})

    def do_PUT(self):
        path = urllib.parse.urlsplit(self.path).path
        raw = self.body()
        REQUESTS.append(("PUT", path))
        if not self.authed():
            return self.js(401, {"ok": False})
        if path.startswith("/v1/state/"):
            kind = path.rsplit("/", 1)[-1]
            try:
                doc = json.loads(raw.decode("utf-8"))
            except Exception:
                return self.js(400, {"ok": False})
            STATE[kind] = doc
            raw2 = json.dumps(doc, ensure_ascii=False).encode("utf-8")
            return self.js(200, {"ok": True, "rev": "r%d" % len(raw2)}, {"ETag": '"%d"' % len(raw2)})
        if path.startswith("/v1/lite/"):
            return self.js(200, {"ok": True, "size": len(raw)})
        return self.js(404, {"ok": False})

    def do_HEAD(self):
        self.do_GET()


def start(port=0):
    """起一个假后端，返回 (server, 实际端口)。port=0 时由系统挑空端口。"""
    if not BOOK["sha"]:
        load_book()
    srv = QuietServer(("127.0.0.1", port), H)
    import threading
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def main(argv):
    port = int(argv[0]) if argv else 8899
    srv, real = start(port)
    print("阅读页：http://127.0.0.1:%d/r/   （token: demo-token）" % real)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main(sys.argv[1:])
