# -*- coding: utf-8 -*-
"""
Codex 额度 -> 本地 JSON 取数服务

给同目录的 codex_quota_bar.py（任务栏挂件）消费。
零第三方依赖，只用标准库。
所有可变项都在同目录的 config.json 里，改机器时只改那一个文件。
"""
import json
import os
import sys
import time
import threading
import traceback
import urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("CODEX_QUOTA_CONFIG",
                             os.path.join(HERE, "config.json"))

DEFAULTS = {
    "proxy":          "http://127.0.0.1:8317",
    "management_key": "",
    "auth_index":     "",
    "bridge_port":    28317,
    "cache_seconds":  90,
    "log_file":       "quota_bridge.log",
}


def load_config():
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            cfg.update(json.load(f))
    return cfg


CFG = load_config()

PROXY      = CFG["proxy"].rstrip("/")
MKEY       = CFG["management_key"]
AUTH_INDEX = CFG["auth_index"]
PORT       = int(CFG["bridge_port"])
CACHE_TTL  = int(CFG["cache_seconds"])
LOG_PATH   = os.path.join(HERE, CFG["log_file"])

REQ_BODY = {
    "auth_index": AUTH_INDEX,
    "method": "GET",
    "url": "https://chatgpt.com/backend-api/wham/usage",
    "header": {
        "Authorization": "Bearer $TOKEN$",
        "User-Agent": "codex_cli_rs/0.76.0 (Debian 13.0.0; x86_64) WindowsTerminal",
    },
}

_lock = threading.Lock()
_cache = {"at": 0.0, "data": None, "err": None}


def check_config():
    """返回配置缺失项列表，空列表表示配置完整。"""
    missing = []
    if not MKEY:
        missing.append("management_key")
    if not AUTH_INDEX:
        missing.append("auth_index")
    if not os.path.exists(CONFIG_PATH):
        missing.append("config.json 文件不存在")
    return missing


def log(msg):
    try:
        # 日志轮转，避免长期运行把磁盘写满
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 256 * 1024:
            os.replace(LOG_PATH, LOG_PATH + ".1")
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def fetch():
    req = urllib.request.Request(
        PROXY + "/v0/management/api-call",
        data=json.dumps(REQ_BODY).encode("utf-8"),
        headers={"X-Management-Key": MKEY, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = json.loads(resp.read().decode("utf-8"))
    body = raw.get("body", raw)
    if isinstance(body, str):
        body = json.loads(body)
    return body


def snapshot(force=False):
    """返回 (data, fetched_at)。缓存未过期时复用旧数据。"""
    now = time.time()
    with _lock:
        fresh = _cache["data"] is not None and (now - _cache["at"]) < CACHE_TTL
    if fresh and not force:
        return _cache["data"], _cache["at"]

    try:
        data = fetch()
    except Exception as e:
        with _lock:
            _cache["err"] = "%s: %s" % (type(e).__name__, e)
            # 上游失败时继续用旧数据，避免任务栏闪断
            if _cache["data"] is not None:
                return _cache["data"], _cache["at"]
        log("fetch failed %s" % _cache["err"])
        return None, now

    with _lock:
        _cache["data"] = data
        _cache["at"] = now
        _cache["err"] = None
    log("fetch ok")
    return data, _cache["at"]


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fmt_dur(sec):
    sec = int(max(0, sec))
    d, rem = divmod(sec, 86400)
    h, m = divmod(rem, 3600)
    m //= 60
    if d:
        return "%dd%dh" % (d, h) if h else "%dd" % d
    if h:
        return "%dh%dm" % (h, m) if m else "%dh" % h
    if m:
        return "%dm" % m
    return "<1m"


def reset_seconds(window, elapsed):
    rs = _num(window.get("reset_after_seconds"))
    if rs is None:
        rs = _num(window.get("resets_in_seconds"))
    if rs is None:
        at = _num(window.get("reset_at"))
        if at:
            rs = at - time.time()
    if rs is None:
        return None
    return max(0.0, rs - elapsed)   # 扣掉缓存已过去的时间，倒计时才准


def render():
    data, at = snapshot()
    if not data:
        return {
            "five": "-- 离线",
            "week": "--",
            "detail": "上游查询失败\n%s" % (_cache["err"] or ""),
        }

    elapsed = max(0.0, time.time() - at)
    rl = data.get("rate_limit") or {}
    pw = rl.get("primary_window") or {}
    sw = rl.get("secondary_window") or {}

    p_used = _num(pw.get("used_percent"))
    s_used = _num(sw.get("used_percent"))
    if p_used is None:
        p_used = 100.0
    left = max(0.0, 100.0 - p_used)
    left_s = ("%.1f" % left).rstrip("0").rstrip(".")

    p_left = fmt_dur(reset_seconds(pw, elapsed)) if reset_seconds(pw, elapsed) is not None else "5h"

    if s_used is None:
        week_txt = "--"
        week_sub = ""
    else:
        week_txt = "周%d%%" % round(s_used)
        rs = reset_seconds(sw, elapsed)
        week_sub = " · " + fmt_dur(rs) if rs is not None else ""

    five_txt = "5h %s%% · %s" % (left_s, p_left)

    detail = "\n".join([
        "5 小时窗口：已用 %s%% / 剩余 %s%%" % (round(p_used), left_s),
        "重置倒计时：%s 后" % p_left,
        "周窗口：已用 %s%%" % ("?" if s_used is None else round(s_used)),
        "查询时间：%s" % time.strftime("%H:%M:%S", time.localtime(at)),
    ])

    return {"five": five_txt, "week": week_txt + week_sub, "detail": detail}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/") or "/"
        log("HIT %s %s" % (self.command, path))
        try:
            if path == "/raw":
                data, at = snapshot(force=True)
                self._send(200, {"ok": data is not None, "data": data, "err": _cache["err"]})
            else:
                self._send(200, render())
        except Exception:
            log(traceback.format_exc())
            self._send(500, {"five": "错误", "week": "错误", "detail": traceback.format_exc()[-500:]})

    def log_message(self, fmt, *args):
        pass    # 静默，避免刷屏


if __name__ == "__main__":
    missing = check_config()
    if missing:
        msg = "config.json 配置不完整: %s (文件: %s)" % ("、".join(missing), CONFIG_PATH)
        log("FATAL " + msg)
        print(msg, file=sys.stderr)
        sys.exit(2)
    try:
        with open(os.path.join(HERE, "bridge.pid"), "w") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass
    log("=== bridge start, port %d, proxy %s ===" % (PORT, PROXY))
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()