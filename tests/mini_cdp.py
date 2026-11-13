# -*- coding: utf-8 -*-
"""极简 CDP 客户端（只用标准库）：自启 Edge 无头实例，用 DevTools 协议跑前端验收。

这台机器上没装 Chrome，也没装 playwright/selenium，但 Edge 是 Chromium，CDP 一样能用。
"""
import base64
import json
import os
import socket
import struct
import subprocess
import tempfile
import time
import urllib.request

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"


class WS:
    def __init__(self, url, timeout=30):
        assert url.startswith("ws://"), url
        rest = url[5:]
        hostport, _, path = rest.partition("/")
        host, _, port = hostport.partition(":")
        self.sock = socket.create_connection((host, int(port)), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = ("GET /%s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\n"
               "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n") % (path, hostport, key)
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("握手期间连接断了")
            buf += chunk
        if b"101" not in buf.split(b"\r\n", 1)[0]:
            raise RuntimeError("握手失败：%r" % buf[:120])
        self.buf = buf.split(b"\r\n\r\n", 1)[1]
        self.next_id = 0

    def send(self, obj):
        data = json.dumps(obj).encode()
        header = bytearray([0x81])
        n = len(data)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        self.sock.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _fill(self):
        chunk = self.sock.recv(65536)
        if not chunk:
            raise RuntimeError("连接断了")
        self.buf += chunk

    def recv(self):
        while True:
            if len(self.buf) < 2:
                self._fill()
                continue
            b1, b2 = self.buf[0], self.buf[1]
            opcode = b1 & 0x0F
            fin = b1 & 0x80
            ln = b2 & 0x7F
            off = 2
            if ln == 126:
                while len(self.buf) < 4:
                    self._fill()
                ln = struct.unpack(">H", self.buf[2:4])[0]
                off = 4
            elif ln == 127:
                while len(self.buf) < 10:
                    self._fill()
                ln = struct.unpack(">Q", self.buf[2:10])[0]
                off = 10
            while len(self.buf) < off + ln:
                self._fill()
            payload = self.buf[off:off + ln]
            self.buf = self.buf[off + ln:]
            if opcode == 0x9:                                  # ping
                mask = os.urandom(4)
                self.sock.sendall(bytes([0x8A, 0x80 | len(payload)]) + mask
                                  + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))
                continue
            if opcode == 0x8:
                raise RuntimeError("服务端关了连接")
            if opcode in (0x1, 0x2):
                if fin:
                    self.msg = payload
                else:
                    self.msg = getattr(self, "msg", b"") + payload
                if fin:
                    return json.loads(self.msg.decode())


class CDP:
    def __init__(self, ws):
        self.ws = ws

    def call(self, method, params=None, timeout=40):
        self.ws.next_id += 1
        mid = self.ws.next_id
        self.ws.send({"id": mid, "method": method, "params": params or {}})
        t0 = time.time()
        while True:
            msg = self.ws.recv()
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError("%s 失败：%s" % (method, msg["error"]))
                return msg.get("result") or {}
            if time.time() - t0 > timeout:
                raise RuntimeError("%s 超时" % method)

    def js(self, expr, await_promise=False):
        res = self.call("Runtime.evaluate", {
            "expression": expr, "returnByValue": True, "awaitPromise": await_promise,
            "userGesture": True,
        })
        if res.get("exceptionDetails"):
            det = res["exceptionDetails"]
            raise RuntimeError("JS 报错：%s" % (det.get("exception", {}).get("description") or det.get("text")))
        return (res.get("result") or {}).get("value")

    def wait_js(self, expr, timeout=25, label=""):
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                if self.js(expr):
                    return True
            except RuntimeError:
                pass
            time.sleep(0.25)
        raise RuntimeError("等不到：%s（%s）" % (label or expr, expr))


def launch(port=9223, profile=None):
    profile = profile or tempfile.mkdtemp(prefix="sredge-")
    proc = subprocess.Popen([
        EDGE, "--headless=new", "--remote-debugging-port=%d" % port,
        "--user-data-dir=" + profile, "--no-first-run", "--no-default-browser-check",
        "--disable-gpu", "--window-size=1100,900", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    t0 = time.time()
    while time.time() - t0 < 25:
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/json/list" % port, timeout=2) as r:
                tabs = json.loads(r.read().decode())
            pages = [t for t in tabs if t.get("type") == "page"]
            if pages:
                return proc, CDP(WS(pages[0]["webSocketDebuggerUrl"]))
        except Exception:
            time.sleep(0.3)
    proc.kill()
    raise RuntimeError("Edge 起不来")
