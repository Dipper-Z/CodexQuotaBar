# -*- coding: utf-8 -*-
"""
额度 -> 本地 JSON 取数服务

给同目录的 codex_quota_bar.py（任务栏挂件）消费。
零第三方依赖，只用标准库。
所有可变项都在同目录的 config.json 里，换机器只改那一个文件。

支持多数据源：config.json 的 sources 数组里每项一个，
挂件用 --item <id> 选择显示哪一个。

接口：
    GET /?item=<id>            取某个源的展示数据（缺省用第一个）
    GET /?item=<id>&force=1    跳过缓存，真正打一次上游
    GET /items                 列出所有可用的源 id
    GET /raw?item=<id>         上游原始响应（排查用）
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
    "bridge_port":   28317,
    "refresh_seconds": 30,
    "cache_seconds": 90,
    "log_file":      "quota_bridge.log",
    "sources":       [],
}


def load_config():
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            cfg.update(json.load(f))

    # 兼容旧版扁平配置：顶层有 proxy/management_key 就当成唯一的 codex 源
    if not cfg.get("sources") and cfg.get("proxy"):
        cfg["sources"] = [{
            "id": "codex", "label": "Codex", "kind": "codex",
            "proxy": cfg["proxy"],
            "management_key": cfg.get("management_key", ""),
            "auth_index": cfg.get("auth_index", ""),
        }]
    cfg["sources"] = [s for s in cfg.get("sources", []) if s.get("id")]
    return cfg


CFG    = load_config()
SOURCES = CFG["sources"]
PORT   = int(CFG["bridge_port"])
CACHE_TTL = int(CFG["cache_seconds"])
LOG_PATH  = os.path.join(HERE, CFG["log_file"])

CODEX_UA = "codex_cli_rs/0.76.0 (Debian 13.0.0; x86_64) WindowsTerminal"


def source_by_id(sid):
    for s in SOURCES:
        if s["id"] == sid:
            return s
    return None


def check_config():
    """返回配置缺失项列表，空列表表示配置完整。"""
    if not os.path.exists(CONFIG_PATH):
        return ["config.json 文件不存在"]
    if not SOURCES:
        return ["sources 为空"]
    missing = []
    for s in SOURCES:
        sid = s["id"]
        if s.get("kind", "codex") == "codex":
            if not s.get("management_key"):
                missing.append("%s.management_key" % sid)
            if not s.get("auth_index"):
                missing.append("%s.auth_index" % sid)
        elif not s.get("url"):
            missing.append("%s.url" % sid)
    return missing


_lock = threading.Lock()
_cache = {}          # sid -> {"at": float, "data": dict, "err": str|None}
_last_force = {}     # sid -> 上次强制刷新的时间戳


def _cache_of(sid):
    return _cache.setdefault(sid, {"at": 0.0, "data": None, "err": None})


def log(msg):
    try:
        # 日志轮转，避免长期运行把磁盘写满
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 256 * 1024:
            os.replace(LOG_PATH, LOG_PATH + ".1")
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def _unwrap(payload):
    """上游可能把真正的 JSON 塞在 body 字符串里，解一层。"""
    if isinstance(payload, dict) and isinstance(payload.get("body"), str):
        try:
            return json.loads(payload["body"])
        except Exception:
            return payload
    return payload


# ---------------- 上游拉取 ----------------
def fetch_codex(src):
    """走 Sub2API / CLIProxyAPI 一类的转发接口，查 ChatGPT 用量。"""
    body = {
        "auth_index": src["auth_index"],
        "method": "GET",
        "url": "https://chatgpt.com/backend-api/wham/usage",
        "header": {
            "Authorization": "Bearer $TOKEN$",
            "User-Agent": src.get("user_agent", CODEX_UA),
        },
    }
    req = urllib.request.Request(
        src["proxy"].rstrip("/") + "/v0/management/api-call",
        data=json.dumps(body).encode("utf-8"),
        headers={"X-Management-Key": src["management_key"],
                 "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = json.loads(resp.read().decode("utf-8"))
    return _unwrap(raw)


def fetch_gptquota(src):
    """直连一个已经算好百分比的 /quota 接口（Gemini / Claude 额度）。"""
    headers = dict(src.get("headers") or {"Content-Type": "application/json"})
    req = urllib.request.Request(src["url"], headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = json.loads(resp.read().decode("utf-8"))
    return _unwrap(raw)


FETCHERS = {"codex": fetch_codex, "gptquota": fetch_gptquota}


FORCE_DEBOUNCE = 8      # 强制刷新之间的最小间隔(秒)，防止连点打爆上游


def snapshot(src, force=False):
    """返回 (data, fetched_at)。缓存未过期时复用旧数据。

    force=True 会跳过缓存真正打上游，但连续强制刷新有 FORCE_DEBOUNCE 秒
    的防抖间隔，避免手抖把上游打得太狠。
    """
    sid = src["id"]
    c = _cache_of(sid)
    now = time.time()
    with _lock:
        fresh = c["data"] is not None and (now - c["at"]) < CACHE_TTL
    if fresh and not force:
        return c["data"], c["at"]

    if force:
        with _lock:
            last = _last_force.get(sid, 0.0)
            if last and (now - last) < FORCE_DEBOUNCE and c["data"] is not None:
                return c["data"], c["at"]
            _last_force[sid] = now

    fetcher = FETCHERS.get(src.get("kind", "codex"))
    if fetcher is None:
        return None, now
    try:
        data = fetcher(src)
    except Exception as e:
        with _lock:
            c["err"] = "%s: %s" % (type(e).__name__, e)
            # 上游失败时继续用旧数据，避免任务栏闪断
            if c["data"] is not None:
                return c["data"], c["at"]
        log("[%s] fetch failed %s" % (sid, c["err"]))
        return None, now

    with _lock:
        c["data"] = data
        c["at"] = now
        c["err"] = None
    log("[%s] fetch ok" % sid)
    return data, c["at"]


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


def render_codex(src, data, at):
    """ChatGPT / wham/usage 的结构：rate_limit.primary_window / secondary_window。"""
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

    rs = reset_seconds(pw, elapsed)
    p_left = fmt_dur(rs) if rs is not None else "5h"

    if s_used is None:
        week_txt, week_sub = "--", ""
    else:
        week_txt = "周%d%%" % round(s_used)
        rs2 = reset_seconds(sw, elapsed)
        week_sub = " · " + fmt_dur(rs2) if rs2 is not None else ""

    detail = "\n".join([
        "%s · 5 小时窗口：已用 %s%% / 剩余 %s%%" % (src.get("label", "Codex"), round(p_used), left_s),
        "重置倒计时：%s 后" % p_left,
        "周窗口：已用 %s%%" % ("?" if s_used is None else round(s_used)),
        "查询时间：%s" % time.strftime("%H:%M:%S", time.localtime(at)),
    ])
    return {"five": "5h %s%% · %s" % (left_s, p_left),
            "week": week_txt + week_sub, "detail": detail}


def render_gptquota(src, data, at):
    """/quota 接口的结构：直接给了剩余百分比和 reset 字符串。"""
    g = data.get("gemini") or {}
    c = data.get("claude_gpt") or {}

    def pct(v):
        n = _num(v)
        return None if n is None else round(n)

    g5, gw = pct(g.get("pct_remaining_5h")), pct(g.get("pct_remaining_week"))
    c5, cw = pct(c.get("pct_remaining_5h")), pct(c.get("pct_remaining_week"))

    g_reset = g.get("reset_5h") or "5h"
    if gw is None:
        week_txt = "--"
    else:
        week_txt = "周%d%%" % round(max(0.0, 100.0 - gw))

    # 挂件上只显示 Claude 的 5 小时剩余；周剩余放进悬停明细，避免行太宽
    parts = []
    if c5 is not None:
        parts.append("C%d%%" % c5)
    week_full = " · ".join([week_txt] + parts) if parts else week_txt

    detail = "\n".join([
        "%s · 5 小时剩余：%s%%" % (src.get("label", "Gemini"),
                                 "?" if g5 is None else g5),
        "重置倒计时：%s 后" % g_reset,
        "周剩余：%s%%" % ("?" if gw is None else gw),
        "Claude 5h 剩余：%s%%" % ("?" if c5 is None else c5),
        "Claude 周剩余：%s%%" % ("?" if cw is None else cw),
        "查询时间：%s" % time.strftime("%H:%M:%S", time.localtime(at)),
    ])

    return {"five": "%s%% · %s" % ("?" if g5 is None else g5, g_reset),
            "week": week_full, "detail": detail}


RENDERERS = {"codex": render_codex, "gptquota": render_gptquota}


def _first_pct(text):
    import re
    m = re.search(r"(-?\d+(?:\.\d+)?)\s*%", text or "")
    return float(m.group(1)) if m else None


def render(src, force=False):
    data, at = snapshot(src, force=force)
    label = src.get("label", src["id"])
    if not data:
        return {"id": src["id"], "label": label, "five": "-- 离线", "week": "--",
                "pct": None,
                "detail": "%s：上游查询失败\n%s" % (label, _cache_of(src["id"])["err"] or "")}

    renderer = RENDERERS.get(src.get("kind", "codex"))
    try:
        out = renderer(src, data, at) if renderer else {}
    except Exception:
        log(traceback.format_exc())
        out = {"five": "错误", "week": "错误",
               "detail": traceback.format_exc()[-400:]}
    out["id"] = src["id"]
    out["label"] = label
    out["pct"] = _first_pct(out.get("five"))
    return out


def render_all(force=False):
    """把所有数据源合成一次响应。挂件每 30 秒只发一个请求。"""
    return {"rows": [render(s, force=force) for s in SOURCES]}


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
        raw_path, _, query = self.path.partition("?")
        path = raw_path.rstrip("/") or "/"

        def q(name):
            for kv in query.split("&"):
                k, _, v = kv.partition("=")
                if k == name:
                    return v
            return ""

        sid = q("item") or (SOURCES[0]["id"] if SOURCES else "")
        force = q("force") == "1"
        log("HIT %s %s item=%s%s" % (self.command, path, sid, " force" if force else ""))

        try:
            if path == "/items":
                self._send(200, {"items": [
                    {"id": s["id"], "label": s.get("label", s["id"]),
                     "kind": s.get("kind", "codex")} for s in SOURCES]})
                return
            if path == "/all":
                self._send(200, render_all(force=force))
                return
            src = source_by_id(sid)
            if src is None:
                self._send(404, {"error": "unknown item: %s" % sid})
                return
            if path == "/raw":
                data, _ = snapshot(src, force=True)
                self._send(200, {"ok": data is not None, "data": data,
                                 "err": _cache_of(sid)["err"]})
            else:
                self._send(200, render(src, force=force))
        except Exception:
            log(traceback.format_exc())
            self._send(500, {"five": "错误", "week": "错误", "label": sid,
                             "detail": traceback.format_exc()[-500:]})

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
    log("=== bridge start, port %d, sources=%s ==="
        % (PORT, ",".join(s["id"] for s in SOURCES)))
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()