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

        # ---- 同步节奏：不做实时，改动后等 15 分钟；另外每 15 分钟问一次远端 ----
        check("自动同步最小间隔 = 15 分钟", app_mod.SYNC_MIN_GAP_SEC == 900, app_mod.SYNC_MIN_GAP_SEC)
        check("周期闹钟 = 15 分钟", app_mod.SYNC_PULL_EVERY_SEC == 900, app_mod.SYNC_PULL_EVERY_SEC)
        pull_timer = getattr(w, "_sync_pull", None)
        check("周期闹钟按 15 分钟挂着（%s 毫秒）" % (pull_timer.interval() if pull_timer else None),
              pull_timer is not None and pull_timer.interval() == 900000,
              pull_timer.interval() if pull_timer else None)
        check("周期闹钟连到 _sync_periodic", callable(getattr(w, "_sync_periodic", None)))

        w._sync_ready = True
        w._sync_last_fire = time.time() - 30       # 上一轮是 30 秒前跑的
        _hold2 = os.environ.pop("STEALTHREADER_SYNC", None)
        try:
            w.sync_kick("config")
        finally:
            if _hold2 is not None:
                os.environ["STEALTHREADER_SYNC"] = _hold2
        left = w._sync_timer.remainingTime()
        check("改动后要等约 15 分钟才推（还差 %s 秒）" % (round(left / 1000.0) if left > 0 else left),
              840_000 <= left <= 900_000, left)
        w._sync_timer.stop()

        # 周期那一轮不该被「改了就自动同步」挡住：真关掉它，网页上读的进度就永远收不进来
        w.sync_conf()["auto"] = False
        fired = {"n": 0}
        real_fire = w._sync_fire
        w._sync_fire = lambda: fired.__setitem__("n", fired["n"] + 1)
        _hold3 = os.environ.pop("STEALTHREADER_SYNC", None)
        try:
            w._sync_timer.stop()
            w._sync_periodic()
        finally:
            w._sync_fire = real_fire
            if _hold3 is not None:
                os.environ["STEALTHREADER_SYNC"] = _hold3
            w.sync_conf()["auto"] = True
        check("关了「改了就自动同步」，每 15 分钟那一轮照样跑", fired["n"] == 1, fired["n"])

        # ---- 同步回来的东西怎么落到本机：设置不落地，Cookie 要落地 ----
        # 这一轮的真要求是「电脑端的设置不用同步，cookie 要多端同步」，所以这两条必须在
        # 真实的窗口对象上验：_on_sync_applied 拿到 kinds 之后到底动了什么。
        font_before = w.config.get("font_size")
        color_before = w.config.get("text_color")
        w._on_sync_applied({"kinds": {"settings": {"font_size": 99,
                                                   "text_color": "rgba(1,2,3,255)"}},
                            "notes": []})
        check("设置从云端回来也不再落到本机（字号/颜色都没变）",
              w.config.get("font_size") == font_before and w.config.get("text_color") == color_before,
              (font_before, w.config.get("font_size"), color_before, w.config.get("text_color")))

        import feed_sources as _fs
        _tmp_auth = os.path.join(tempfile.mkdtemp(prefix="sr-auth-"), "feed_auth.json")
        _real_path = _fs.auth_path
        try:
            _fs.auth_path = lambda root=None: _tmp_auth
            with open(_tmp_auth, "w", encoding="utf-8") as f:
                json.dump({"v2ex": {"cookie": "keep=1"}}, f)
            w._on_sync_applied({"kinds": {"cookies": {"set": {"zhihu": {"cookie": "z_c0=9",
                                                                      "user_agent": "UA-z"}},
                                                      "drop": ["nga"]}},
                                "notes": []})
            with open(_tmp_auth, encoding="utf-8") as f:
                got = json.load(f)
        finally:
            _fs.auth_path = _real_path
            try:
                import shutil as _sh
                _sh.rmtree(os.path.dirname(_tmp_auth), ignore_errors=True)
            except Exception:
                pass
        check("Cookie 从云端落地到 feed_auth.json", (got.get("zhihu") or {}).get("cookie") == "z_c0=9", got)
        check("本地原有的那条没被写坏（只并、不整份覆盖）",
              (got.get("v2ex") or {}).get("cookie") == "keep=1", got)

        # 设置页贴完 Cookie 点保存：要真写盘，而且**要真排一轮同步**。
        # 这条以前接在了对话框自己身上（self.sync_kick），一按就是 AttributeError 被
        # 外层 except 吞掉——"贴了 Cookie 云端没动"这种哑巴问题就是这么来的。
        import shutil as _sh2
        _tmp2 = os.path.join(tempfile.mkdtemp(prefix="sr-auth2-"), "feed_auth.json")
        kicked = []
        _real_kick = w.sync_kick
        try:
            _fs.auth_path = lambda root=None: _tmp2
            w.sync_kick = lambda reason="change", delay_ms=None: kicked.append(reason)
            dlg = app_mod.SettingsDialog(w.config, w)
            dlg.cookie_edits = {"zhihu": "z_c0=from_dialog"}
            dlg.save_cookie_edits()
            with open(_tmp2, encoding="utf-8") as f:
                got2 = json.load(f)
        finally:
            w.sync_kick = _real_kick
            _fs.auth_path = _real_path
            _sh2.rmtree(os.path.dirname(_tmp2), ignore_errors=True)
        check("设置页贴的 Cookie 真写进了 feed_auth.json",
              (got2.get("zhihu") or {}).get("cookie") == "z_c0=from_dialog", got2)
        check("贴完就排了一轮 cookies 同步（不是等 15 分钟）", kicked == ["cookies"], kicked)
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
