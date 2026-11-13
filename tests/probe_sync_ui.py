# -*- coding: utf-8 -*-
"""同步对话框的点击路径（不联网）。

验的是"接线"：按钮在不在、点了会不会真去拉、拉完状态行有没有说人话。
网络用替身：`sync_engine.pull_books` 和 `SyncClient` 都被换掉，一个字节都不出本机。
真 app 在本机跑着，config.json 会被 `_save_conf()` 碰一下——开跑前存一份，收尾写回。
"""

import io
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SR = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(SR, "libs"))
sys.path.insert(0, SR)
os.chdir(SR)
os.environ["QT_QPA_PLATFORM"] = "offscreen"

_TMP = tempfile.mkdtemp(prefix="sr-syncui-")
os.environ["STEALTHREADER_STATS"] = os.path.join(_TMP, "reading_stats.json")
os.environ["STEALTHREADER_SYNC"] = "0"          # 真 app 的同步回路关掉，别让它自己去连网

_CFG = os.path.join(SR, "config.json")
try:
    with open(_CFG, "rb") as _f:
        _CFG_BAK = _f.read()
except Exception:
    _CFG_BAK = None

import atexit                                                            # noqa: E402


def _restore_cfg():
    if _CFG_BAK is None:
        return
    try:
        with open(_CFG, "rb") as _f:
            if _f.read() == _CFG_BAK:
                return
        with open(_CFG, "wb") as _f:
            _f.write(_CFG_BAK)
        print("\n（探针动过 config.json，已按开跑前那份原样写回）")
    except Exception:
        pass


atexit.register(_restore_cfg)

import sync_engine as se                                                # noqa: E402
import main as app_mod                                                  # noqa: E402
from PyQt5.QtCore import QTimer                                          # noqa: E402
from PyQt5.QtWidgets import QApplication, QDialog, QLabel, QPushButton   # noqa: E402

RESULTS = []


def check(name, cond, extra=""):
    RESULTS.append((name, bool(cond)))
    print(("  ok  " if cond else "  FAIL") + " " + name + (("   << " + str(extra)) if extra else ""))


# ---------------------------------------------------------------- 替身：不联网
CALLS = {"pull": [], "clients": []}


class FakeClient:
    def __init__(self, base, token, *a, **k):
        CALLS["clients"].append((base, token))

    def blob_list(self):
        return []

    def close(self):
        pass


def fake_pull(client, dest_dir, local_shas=None, log=None):
    CALLS["pull"].append({"dest": dest_dir, "local": len(local_shas or ())})
    return {"remote": 2, "downloaded": 1, "skipped": 1, "failed": [], "names": ["甲.epub"]}


se.SyncClient = FakeClient
se.pull_books = fake_pull


def main():
    qapp = QApplication.instance() or QApplication([])
    w = app_mod.StealthReader()
    try:
        # 让"已配置"成立：地址 + token + 开关都在内存里给上（不存盘前先看对话框）
        conf = w.sync_conf()
        conf.update({"enabled": True, "base_url": "https://sync.noteklip.org",
                     "token": "deadbeef" * 6, "books": True, "auto": True})
        w.config["sync"] = conf
        _hold = os.environ.pop("STEALTHREADER_SYNC", None)   # 只为这一条：开关就是让它返回 False
        try:
            check("同步已算配置好", w.sync_configured() is True)
        finally:
            if _hold is not None:
                os.environ["STEALTHREADER_SYNC"] = _hold

        seen = {}

        def fake_exec(self):
            buttons = [b for b in self.findChildren(QPushButton)]
            texts = [b.text() for b in buttons]
            seen["texts"] = texts
            pull = [b for b in buttons if "补齐" in b.text()]
            seen["found"] = bool(pull)
            labels = self.findChildren(QLabel)
            before = [l.text() for l in labels]
            if pull:
                pull[0].click()
                deadline = time.time() + 15
                while time.time() < deadline:
                    qapp.processEvents()
                    time.sleep(0.02)
                    now = [l.text() for l in self.findChildren(QLabel)]
                    done = [t for t in now if ("云端" in t and "补下" in t) or "失败" in t]
                    if done:
                        seen["status"] = done
                        break
            qapp.processEvents()
            return 0

        _old_exec = QDialog.exec_
        QDialog.exec_ = fake_exec
        try:
            w.open_sync_dialog()
        finally:
            QDialog.exec_ = _old_exec

        check("对话框里有「从云端补齐书」按钮", seen.get("found") is True, seen.get("texts"))
        check("点了之后真去拉了（替身被调用一次）", len(CALLS["pull"]) == 1, CALLS["pull"])
        check("拉的时候用的是应用自己的书架目录",
              bool(CALLS["pull"]) and CALLS["pull"][0]["dest"] == w.shelf_dir(),
              (CALLS["pull"][0]["dest"] if CALLS["pull"] else None, w.shelf_dir()))
        status = " ".join(seen.get("status") or [])
        check("状态行说了云端几本、补了几本", ("云端" in status and "补下 1 本" in status), status)
        check("没有真的连网（SyncClient 替身只被建了一次）", len(CALLS["clients"]) == 1, CALLS["clients"])
    finally:
        try:
            w.stop_sync()
        except Exception:
            pass
        try:
            w.close()
        except Exception:
            pass

    bad = [r for r in RESULTS if not r[1]]
    print("\n%d 通过 / %d 失败" % (len(RESULTS) - len(bad), len(bad)))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
