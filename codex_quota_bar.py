# -*- coding: utf-8 -*-
"""
Codex 额度挂件 —— 贴在 Windows 任务栏上的半透明小挂件。

数据来自同目录的 quota_bridge.py（本地取数服务），
取数服务再向上游代理查询 Codex 用量。本脚本只负责展示，不直接联网。

命令行用法：
    python codex_quota_bar.py                  启动挂件（用第一个数据源）
    python codex_quota_bar.py --item gemini    指定显示哪个数据源
    python codex_quota_bar.py --install        为所有数据源写入开机自启并启动
    python codex_quota_bar.py --uninstall      取消开机自启并停止
    python codex_quota_bar.py --check          自检：打印配置并逐个试拉
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import urllib.request

# ---------------- DPI 感知（必须在建窗口前设置） ----------------
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

# ---------------- 配置 ----------------
HERE        = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("CODEX_QUOTA_CONFIG",
                             os.path.join(HERE, "config.json"))
BRIDGE_PY   = os.path.join(HERE, "quota_bridge.py")
PYTHONW     = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
BRIDGE_PID_FILE = os.path.join(HERE, "bridge.pid")
WATCHDOG_MS = 15000
REFRESH_MS  = 30000


def write_pid_file(path):
    try:
        with open(path, "w") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass


WIDGET_LOG = os.path.join(HERE, "widget.log")


def wlog(msg, detail=""):
    """挂件自己的日志。pythonw 没有 stderr，异常必须落盘才查得到。"""
    try:
        if os.path.exists(WIDGET_LOG) and os.path.getsize(WIDGET_LOG) > 256 * 1024:
            os.replace(WIDGET_LOG, WIDGET_LOG + ".1")
        with open(WIDGET_LOG, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
            if detail:
                f.write(detail.rstrip() + "\n")
    except Exception:
        pass


def install_excepthooks():
    """把主线程和工作线程的未捕获异常都写进 widget.log。"""
    import traceback as _tb

    def hook(exc_type, exc, tb):
        wlog("未捕获异常: %s" % exc_type.__name__, "".join(_tb.format_exception(exc_type, exc, tb)))
    sys.excepthook = hook

    def thook(args):
        wlog("工作线程异常: %s" % args.exc_type.__name__,
             "".join(_tb.format_exception(args.exc_type, args.exc_value, args.exc_traceback)))
    if hasattr(threading, "excepthook"):
        threading.excepthook = thook


def load_config():
    cfg = {"bridge_port": 28317, "refresh_seconds": 30, "cache_seconds": 90,
           "sources": []}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
                cfg.update(json.load(f))
        except Exception as e:
            print("config.json 解析失败: %s" % e, file=sys.stderr)
    # 兼容旧版扁平配置
    if not cfg.get("sources") and cfg.get("proxy"):
        cfg["sources"] = [{"id": "codex", "label": "Codex", "kind": "codex"}]
    return cfg


CFG         = load_config()
BRIDGE_PORT = int(CFG.get("bridge_port", 28317))
REFRESH_MS  = int(CFG.get("refresh_seconds", 30)) * 1000
ITEM        = os.environ.get("CODEX_QUOTA_ITEM") or ""      # 由 --item 覆盖


def set_item(item):
    """切换当前显示的数据源，并同步互斥量名与 pid 文件名。"""
    global ITEM, MUTEX_SUFFIX, BAR_PID_FILE
    ITEM = item or ""
    MUTEX_SUFFIX = ("_" + re.sub(r"[^A-Za-z0-9]", "", ITEM)) if ITEM else ""
    BAR_PID_FILE = os.path.join(HERE, "bar%s.pid" % ("-" + ITEM if ITEM else ""))


set_item(ITEM)

# 图形界面按需加载：--check / --install 不依赖 tkinter
try:
    import tkinter as tk
    from tkinter import font as tkfont
except ImportError:                                    # pragma: no cover
    tk = None
    tkfont = None

KEY_COLOR   = "#0B0C0E"        # 与 -transparentcolor 相同的抠底色
BG          = "#191B1F"
BORDER      = "#2E3238"
FG          = "#E8EAED"
FG_DIM      = "#9AA0A6"
SEPARATOR   = "#3A3F46"
ACCENT      = "#7AA2F7"

GOOD, WARN, BAD, OFF = "#3DD68C", "#F5A524", "#F2555A", "#6B7280"

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VAL_PREFIX = "CodexQuotaBar"


def _run_name(item=""):
    return "%s:%s" % (RUN_VAL_PREFIX, item) if item else RUN_VAL_PREFIX


def _rect(hwnd):
    r = wt.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return None
    return r.left, r.top, r.right, r.bottom

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


def _class_of(hwnd):
    b = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, b, 256)
    return b.value


def _pid_of(hwnd):
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _collect(rect_of, pid_self, pid_shell, box, out):
    l, t, rr, b = box
    tw, th = rr - l, b - t

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = _pid_of(hwnd)
        if pid in (pid_self, pid_shell):
            return True
        r = wt.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
            return True
        w, h = r.right - r.left, r.bottom - r.top
        if not (0 < w <= tw and 0 < h <= th + 8):
            return True
        if r.left >= l and r.right <= rr and r.top >= t - 4 and r.bottom <= b + 4:
            out.append(r.left)
        return True

    rect_of(cb)


def _dock_obstacles(tb):
    """任务栏条带上其它挂件（TrafficMonitor 等）的左边界。"""
    lefts = []
    pid_self = kernel32.GetCurrentProcessId()
    tbh = user32.FindWindowW("Shell_TrayWnd", None)
    pid_shell = _pid_of(tbh) if tbh else 0

    # 任务栏的子窗口：TrafficMonitor 就是挂在 Shell_TrayWnd 下的
    if tbh:
        _collect(lambda cb: user32.EnumChildWindows(tbh, cb, 0),
                 pid_self, pid_shell, tb, lefts)
    # 以及少数自绘成顶层窗口的挂件
    _collect(lambda cb: user32.EnumWindows(cb, 0),
             pid_self, pid_shell, tb, lefts)
    return lefts


def taskbar_gap():
    """任务栏上可以自由占用的横向区间 (left, right)。

    左边界 = 任务按钮区的右沿（系统图标优先，挂件绝不去压它）
    右边界 = 托盘左沿 与 其它第三方挂件左沿 中更靠左的一个
    """
    tbh = user32.FindWindowW("Shell_TrayWnd", None)
    if not tbh:
        return None
    tb = _rect(tbh)
    if not tb:
        return None

    # 左边界：任务按钮 / 开始菜单那一段的右沿
    left = tb[0]
    for cls in ("MSTaskSwWClass", "MSTaskListWClass", "MSTaskTabWClass",
                "ReBarWindow32"):
        h = user32.FindWindowExW(tbh, 0, cls, None)
        if h:
            r = _rect(h)
            if r:
                left = max(left, r[2])

    # 右边界：托盘左沿
    right = tb[2]
    tray = user32.FindWindowExW(tbh, 0, "TrayNotifyWnd", None)
    if not tray:
        tray = user32.FindWindowExW(tbh, 0, "TrayButton", None)
    if not tray:
        tray = user32.FindWindowExW(tbh, 0,
                                    "Windows.UI.Composition.DesktopWindowContentBridge", None)
    if tray:
        t = _rect(tray)
        if t:
            right = min(right, t[0])

    # 其它第三方挂件也要让
    for l in _dock_obstacles(tb):
        right = min(right, l)

    return (left, right) if right > left else None


def taskbar_geometry():
    """返回 (任务栏下沿 y, 挂件右边界 x, 任务栏顶沿 y, 任务栏高度)。"""
    hwnd = user32.FindWindowW("Shell_TrayWnd", None)
    if not hwnd:
        sw = user32.GetSystemMetrics(0)
        sh = user32.GetSystemMetrics(1)
        return sh, sw - 340, sh - 60, 60

    tb = _rect(hwnd)
    height = tb[3] - tb[1]

    edges = []
    tray = user32.FindWindowExW(hwnd, 0, "TrayNotifyWnd", None)
    if not tray:
        tray = user32.FindWindowExW(hwnd, 0, "TrayButton", None)
    if not tray:
        tray = user32.FindWindowExW(hwnd, 0,
                                    "Windows.UI.Composition.DesktopWindowContentBridge", None)
    if tray:
        t = _rect(tray)
        if t:
            edges.append(t[0])

    edges.extend(_dock_obstacles(tb))
    right_x = (min(edges) if edges else tb[2]) - 12
    return tb[3], right_x, tb[1], height


# ---------------- 桥取服务 ----------------
def bridge_alive():
    import socket
    try:
        s = socket.create_connection(("127.0.0.1", BRIDGE_PORT), timeout=1.5)
        s.close()
        return True
    except Exception:
        return False


def fetch_quota(force=False, item=""):
    """向本地取数服务要数据。force=True 时带 force=1 真正打一次上游。"""
    if item:
        url = "http://127.0.0.1:%d/?item=%s" % (BRIDGE_PORT, item)
    else:
        url = "http://127.0.0.1:%d/all" % BRIDGE_PORT
    if force:
        url += ("&" if "?" in url else "?") + "force=1"
    with urllib.request.urlopen(url, timeout=25 if force else 8) as r:
        d = json.loads(r.read().decode("utf-8"))
    if item:
        return [d]
    return d.get("rows", [])


def bridge_items():
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/items" % BRIDGE_PORT, timeout=6) as r:
            return json.loads(r.read().decode("utf-8")).get("items", [])
    except Exception:
        return []


def ensure_bridge():
    if bridge_alive():
        return
    if not os.path.exists(BRIDGE_PY):
        return
    try:
        subprocess.Popen([PYTHONW, BRIDGE_PY],
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        pass
    for _ in range(20):
        if bridge_alive():
            return
        time.sleep(0.5)


def parse(text, default=None):
    """从 "5h 91% · 4h18m" 这类文本里取出第一个百分数。取不到就返回 default。"""
    if not text:
        return default
    m = re.search(r"(-?\d+(?:\.\d+)?)\s*%", text)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return default


# ---------------- 开机自启 ----------------
def autostart_enabled(item=""):
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            return winreg.QueryValueEx(k, _run_name(item))[0]
    except Exception:
        return None


def set_autostart(on, item=""):
    import winreg
    name = _run_name(item)
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            cmd = '"%s" "%s"' % (PYTHONW, os.path.abspath(__file__))
            if item:
                cmd += " --item %s" % item
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(k, name)
            except FileNotFoundError:
                pass


# ---------------- 挂件 ----------------
def acquire_single_instance():
    """同一个数据源已经在跑就退出，避免重复启动出现两个。每个数据源一个互斥量。"""
    k32 = ctypes.windll.kernel32
    k32.CreateMutexW(None, False, "Local\\QuotaBarSingleton" + MUTEX_SUFFIX)
    return k32.GetLastError() != 183      # 183 = ERROR_ALREADY_EXISTS


class QuotaBar:
    PAD_X = 14
    PAD_Y = 7
    RADIUS = 9
    ROW_H = 23
    BALL = 46                # 悬浮球直径
    GAP_PAD = 10             # 任务栏空隙两边各留的余量
    CLICK_SLOP = 4        # 指针移动超过这么多像素就算拖拽，不算点击
    CLICK_TIME = 0.6      # 按下超过这么久也不算点击

    def __init__(self):
        ensure_bridge()

        # 首次同步取数，保证第一次绘制就有内容、尺寸正确
        self.item = ITEM
        self.rows = self._first_fetch()

        self.pos = None
        self._drag = None
        self._tip = None
        self._busy = False
        self._flash = 0.0
        self._mode = None
        self._stale = False

        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        try:
            self.root.attributes("-transparentcolor", KEY_COLOR)
        except tk.TclError:
            pass
        self.root.configure(bg=KEY_COLOR)

        # 用像素字号，避免 150% 缩放下按磅换算出错
        self.f_bold = tkfont.Font(family="Microsoft YaHei UI", size=-17, weight="bold")
        self.f_reg = tkfont.Font(family="Microsoft YaHei UI", size=-16)
        self.f_lbl = tkfont.Font(family="Microsoft YaHei UI", size=-14)
        self.f_ball = tkfont.Font(family="Microsoft YaHei UI", size=-13, weight="bold")

        self.cv = None
        self._build_canvas()

        self.layout()
        self.root.after(REFRESH_MS, self.tick)
        self.root.after(WATCHDOG_MS, self.watchdog)

    # ---------- 自愈 ----------
    def _build_canvas(self):
        """（重新）创建画布并重新绑定事件。画布坏掉时用它整个换掉。"""
        cv = tk.Canvas(self.root, bg=KEY_COLOR, highlightthickness=0, bd=0)
        cv.pack()
        cv.bind("<ButtonPress-1>", self.on_press)
        cv.bind("<B1-Motion>", self.on_drag)
        cv.bind("<ButtonRelease-1>", self.on_release)
        cv.bind("<Button-3>", self.on_menu)
        cv.bind("<Enter>", lambda e: self.show_tip())
        cv.bind("<Leave>", lambda e: self.hide_tip())
        cv.bind("<Double-Button-1>", lambda e: self._click_feedback())
        self.cv = cv
        return cv

    def watchdog(self):
        """每 15 秒自检一次。

        捕获两类「进程还活着但看起来像消失了」的情况：
        1. 画布空了 —— Tk 在锁屏/休眠唤醒、DWM 重合成后可能丢绘制，
           或某次 layout 中途异常。这两种都会让画布一个元素都不剩。
        2. 被别的置顶窗口压住 —— 重新抢一次置顶。
        """
        try:
            blank = not self.cv.winfo_exists() or not self.cv.find_all()
        except Exception:
            blank = True

        if blank:
            wlog("检测到画布为空，自动重建")
            try:
                self.hide_tip()
                if self.cv is not None:
                    self.cv.destroy()
                self._build_canvas()
                self._busy = False          # 顺手解开可能卡住的刷新锁
                self.layout()
                self.root.attributes("-topmost", False)
                self.root.attributes("-topmost", True)
            except Exception:
                wlog("重建失败", traceback.format_exc())

        # 每 5 分钟抢一次置顶，防止被别的置顶窗口压住
        if int(time.time()) % 300 < WATCHDOG_MS // 1000:
            try:
                self.root.attributes("-topmost", False)
                self.root.attributes("-topmost", True)
            except Exception:
                pass

        self._check_mode()
        self._sweep_orphan_tips()
        self.root.after(WATCHDOG_MS, self.watchdog)

    def _sweep_orphan_tips(self):
        """清理变成孤儿的提示框窗口。

        提示框一旦因为漏掉 Leave 事件而残留，就是一个钉在屏幕上的不透明黑块。
        这里定期核对 root 下的顶层窗口：除了正在用的那个，其余一律销毁。
        """
        try:
            children = self.root.tk.call("winfo", "children", self.root._w)
        except Exception:
            return
        keep = self._valid_tip()
        keep_name = keep._w if keep is not None else None
        killed = 0
        for name in children:
            if name == self.root._w or name == keep_name:
                continue
            # winfo children 返回的是所有直接子控件，主画布也在里面。
            # 只清理 Toplevel，否则会把 .!canvas 一起删掉。
            try:
                if self.root.tk.call("winfo", "class", name) != "Toplevel":
                    continue
            except Exception:
                continue
            try:
                self.root.tk.call("destroy", name)
                killed += 1
            except Exception:
                pass
        if killed:
            wlog("清理残留提示框 %d 个" % killed)
            if keep_name is not None and keep_name in children:
                self._tip = None

    def _check_mode(self):
        """任务按钮区宽度会随开关的窗口变化，形态要跟着在药丸/悬浮球之间切换。

        只有用户没手动拖动过（self.pos 由系统算的）时才自动挪位置。
        """
        try:
            gap = taskbar_gap()
            plan = self._plan()
            want = plan["mode"]
            if self._mode == want:
                return
            self._mode = want
            wlog("形态切换 -> %s (空隙 %s)" % (
                want, ("%dpx" % (gap[1] - gap[0])) if gap else "n/a"))
            self._auto = True
            self.pos = self._auto_pos(plan)
            self.layout()
        except Exception:
            wlog("形态检查异常", traceback.format_exc())

    # ---------- 绘制 ----------
    def _round_rect(self, cv, x1, y1, x2, y2, r, **kw):
        cv.create_arc(x1, y1, x1 + 2 * r, y1 + 2 * r, start=90,  extent=90,  style="pieslice", **kw)
        cv.create_arc(x2 - 2 * r, y1, x2, y1 + 2 * r, start=0,   extent=90,  style="pieslice", **kw)
        cv.create_arc(x1, y2 - 2 * r, x1 + 2 * r, y2, start=180, extent=90,  style="pieslice", **kw)
        cv.create_arc(x2 - 2 * r, y2 - 2 * r, x2, y2, start=270, extent=90, style="pieslice", **kw)
        cv.create_rectangle(x1 + r, y1, x2 - r, y2, **kw)
        cv.create_rectangle(x1, y1 + r, x2, y2 - r, **kw)

    def _plan(self):
        """只算不画。

        任何异常都发生在这里，画布还留着上一帧的内容 ——
        这是画布不会被「清空后画不出来」的关键。

        多个数据源竖着排成若干行：宽度取最宽的一行，
        这样加数据源只增加高度，不再横向拉长。

        顺手算出该用哪种形态：任务栏塞得下就用药丸，
        塞不下（任务按钮太宽、托盘太挤）就降级成悬浮球。
        """
        rows = []
        for d in (self.rows if isinstance(self.rows, list) else []):
            if not isinstance(d, dict):
                continue
            five = str(d.get("five") or "--")
            week = str(d.get("week") or "--")
            label = str(d.get("label") or d.get("id") or "?")

            pct = parse(five, None)
            if pct is None:
                color = OFF
            elif pct > 30:
                color = GOOD
            elif pct > 10:
                color = WARN
            else:
                color = BAD

            parts_five = five.replace("5h", "").strip()
            rows.append({
                "label": label, "color": color, "pct": pct,
                "five": parts_five, "week": week,
                "tl": self.f_lbl.measure(label),
                "t1": self.f_bold.measure(parts_five),
                "t2": self.f_reg.measure(week),
            })

        if not rows:
            rows = [{"label": "…", "color": OFF, "pct": None, "five": "--",
                     "week": "--", "tl": 10, "t1": 20, "t2": 20}]

        # 每行：圆点 + 标签 + 5h 值 + 分隔线 + 周值
        row_w = max(r["tl"] + r["t1"] + r["t2"] for r in rows) + 56
        width = max(int(self.PAD_X * 2 + row_w), 150)
        height = self.PAD_Y * 2 + len(rows) * self.ROW_H

        gap = taskbar_gap()
        avail = (gap[1] - gap[0] - self.GAP_PAD) if gap else 0
        if avail >= width:
            mode, w, h = "pill", width, height
        else:
            mode, w, h = "ball", self.BALL, self.BALL

        # 悬浮球只显示最紧张的那一行（剩余最少），颜色也跟着它
        worst = None
        for r in rows:
            if r["pct"] is not None and (worst is None or r["pct"] < worst["pct"]):
                worst = r
        if worst is None:
            worst = rows[0]

        return dict(mode=mode, width=w, height=h, rows=rows, worst=worst,
                    border=self._flash_border())

    def _flash_border(self):
        """边框颜色：点击反馈 > 数据过期 > 常态。"""
        f = getattr(self, "_flash", 0.0)
        if getattr(self, "_stale", False):
            base = OFF
        else:
            base = BORDER
        if f <= 0:
            return base
        a = tuple(int(base[i:i + 2], 16) for i in (1, 3, 5))
        b = tuple(int(ACCENT[i:i + 2], 16) for i in (1, 3, 5))
        return "#%02X%02X%02X" % tuple(int(a[i] + (b[i] - a[i]) * f) for i in range(3))

    def layout(self):
        try:
            plan = self._plan()
        except Exception:
            wlog("layout 计算阶段异常，保留上一帧", traceback.format_exc())
            return

        cv = self.cv
        try:
            cv.configure(width=plan["width"], height=plan["height"])
            cv.delete("all")

            if plan["mode"] == "ball":
                self._draw_ball(cv, plan)
            else:
                self._round_rect(cv, 1, 1, plan["width"] - 1, plan["height"] - 1,
                                 self.RADIUS, fill=BG, outline=plan["border"], width=1)

                y = self.PAD_Y + self.ROW_H // 2
                for r in plan["rows"]:
                    x = self.PAD_X
                    cv.create_oval(x, y - 3, x + 7, y + 4, fill=r["color"], outline="")
                    x += 13
                    cv.create_text(x, y, text=r["label"], font=self.f_lbl,
                                   fill=FG_DIM, anchor="w")
                    x += r["tl"] + 7
                    cv.create_text(x, y, text=r["five"], font=self.f_bold,
                                   fill=FG, anchor="w")
                    x += r["t1"] + 8
                    cv.create_line(x, y - 6, x, y + 6, fill=SEPARATOR)
                    x += 8
                    cv.create_text(x, y, text=r["week"], font=self.f_reg,
                                   fill=FG_DIM, anchor="w")
                    y += self.ROW_H
        except Exception:
            wlog("layout 绘制阶段异常", traceback.format_exc())

        try:
            if self.pos is None:
                self.pos = self._auto_pos(plan)
            self.root.geometry("%dx%d+%d+%d" % (plan["width"], plan["height"],
                                                self.pos[0], self.pos[1]))
        except Exception:
            wlog("layout 定位阶段异常", traceback.format_exc())

    def _draw_ball(self, cv, plan):
        """悬浮球：只显示最紧张的那一行，悬停仍然能看到全部明细。"""
        d = plan["worst"]
        s = self.BALL
        c = s // 2
        cv.create_oval(1, 1, s - 1, s - 1, fill=BG,
                       outline=plan["border"], width=1)
        # 内圈用状态色，点缀一圈
        cv.create_oval(5, 5, s - 5, s - 5, outline=d["color"], width=2)
        txt = d["five"].split("·")[0].strip() or "--"
        cv.create_text(c, c - 1, text=txt, font=self.f_ball, fill=FG)

    # ---------- 位置 ----------
    @staticmethod
    def _taskbar_pos(w, h):
        bottom, right, top, _ = taskbar_geometry()
        return (right - w, top + (bottom - top - h) // 2)

    def _auto_pos(self, plan):
        """放不下就浮在任务栏上方右对齐 —— 系统图标优先。"""
        w, h = plan["width"], plan["height"]
        bottom, right, top, _ = taskbar_geometry()
        x = right - w
        if plan["mode"] == "ball":
            return (x, max(0, top - h - self.GAP_PAD))
        return (x, top + (bottom - top - h) // 2)

    def place_at_taskbar(self):
        w = self.root.winfo_reqwidth() or 220
        h = self.root.winfo_reqheight() or (self.PAD_Y * 2 + self.ROW_H)
        self.pos = self._taskbar_pos(w, h)
        self.layout()

    def on_press(self, e):
        self._drag = (e.x_root, e.y_root, self.root.winfo_x(),
                      self.root.winfo_y(), time.time())

    def on_drag(self, e):
        if not self._drag:
            return
        sx, sy, wx, wy, _ = self._drag
        self.pos = (wx + e.x_root - sx, wy + e.y_root - sy)
        w = self.root.winfo_width()
        h = self.root.winfo_height()
        self.root.geometry("%dx%d+%d+%d" % (w, h, self.pos[0], self.pos[1]))

    def on_release(self, e):
        """松手时区分「点击」和「拖拽」。

        没移动、按下不久 → 算点击，立即强制刷新一次（真正打上游）。
        移动过或按得久 → 算拖拽，只做位置处理。
        """
        if not self._drag:
            return
        sx, sy, _, _, t0 = self._drag
        self._drag = None

        moved = abs(e.x_root - sx) > self.CLICK_SLOP or abs(e.y_root - sy) > self.CLICK_SLOP
        if not moved and (time.time() - t0) < self.CLICK_TIME:
            self._click_feedback()
            self.refresh(force=True)
            return

        # 松手时离任务栏很近就自动吸附回去
        bottom, _, top, _ = taskbar_geometry()
        h = self.root.winfo_height() or (self.PAD_Y * 2 + self.ROW_H)
        x, y = self.pos
        if (top - 48) <= y <= (bottom + 48):
            self.place_at_taskbar()

    # ---------- 点击反馈 ----------
    def _click_feedback(self):
        """边框闪一下，让用户知道点到了。"""
        self._flash = 1.0
        self.layout()
        self.root.after(90, self._flash_step)

    def _flash_step(self):
        self._flash = round(self._flash - 0.18, 3)
        if self._flash <= 0:
            self._flash = 0.0
            self.layout()
            return
        self.layout()
        self.root.after(70, self._flash_step)

    # ---------- 数据 ----------
    def _first_fetch(self):
        try:
            return fetch_quota(item=self.item)
        except Exception:
            return [{"label": "离线", "five": "--", "week": "--",
                     "pct": None, "detail": "取数服务无响应"}]

    def refresh(self, force=False):
        if self._busy:
            return
        self._busy = True

        def work():
            try:
                rows = fetch_quota(force=force, item=self.item)
                if rows:
                    self.rows = rows
                    self._stale = False
            except Exception:
                # 取数失败时保留上一次的好数据，只标记为过期。
                # 不要让任务栏闪成「离线」—— 下一次轮询多半就好了。
                if not self.rows:
                    self.rows = [{"label": "离线", "five": "--", "week": "--",
                                  "pct": None, "detail": "取数服务无响应"}]
                else:
                    self._stale = True
                    wlog("取数失败，沿用上次数据: %s" % sys.exc_info()[1])
            self.root.after(0, self._done)

        threading.Thread(target=work, daemon=True).start()

    def _done(self):
        self._busy = False
        self.layout()
        if self._valid_tip():
            self.show_tip()

    def tick(self):
        self.refresh()
        self.root.after(REFRESH_MS, self.tick)

    # ---------- 悬停明细 ----------
    def _valid_tip(self):
        """self._tip 可能指向一个已被销毁的窗口，先确认它还在。"""
        if self._tip is None:
            return None
        try:
            if not self._tip.winfo_exists():
                self._tip = None
        except Exception:
            self._tip = None
        return self._tip

    def show_tip(self):
        self.hide_tip()
        chunks = []
        for d in (self.rows if isinstance(self.rows, list) else []):
            if not isinstance(d, dict):
                continue
            detail = d.get("detail") or ""
            chunks.append(detail if detail else
                          "%s\n%s %s" % (d.get("label"), d.get("five"), d.get("week")))
        text = "\n\n".join(c for c in chunks if c) or "正在获取…"
        lines = text.split("\n")
        f = tkfont.Font(family="Microsoft YaHei UI", size=-16)
        line_h = f.metrics("linespace") + 4
        w = int(max(f.measure(l) for l in lines)) + 28
        h = len(lines) * line_h + 16

        t = tk.Toplevel(self.root)
        # 先 withdraw 再定位，最后才 deiconify。
        # 否则窗口会先以默认尺寸映射在 (0,0)，左上角闪一个黑块。
        t.withdraw()
        t.overrideredirect(True)
        t.attributes("-topmost", True)
        # 少了这一行，底色 KEY_COLOR 会变成一个不透明的黑方块
        try:
            t.attributes("-transparentcolor", KEY_COLOR)
        except tk.TclError:
            pass
        t.configure(bg=KEY_COLOR)

        cv = tk.Canvas(t, width=w, height=h, bg=KEY_COLOR, highlightthickness=0, bd=0)
        cv.pack()
        helper = QuotaBar.__new__(QuotaBar)
        helper.cv = cv
        helper._round_rect(1, 1, w - 1, h - 1, self.RADIUS,
                           fill=BG, outline=BORDER, width=1)
        cv.create_text(14, h // 2, text=text, font=f, fill=FG,
                       anchor="w", justify="left")

        # 先让 Tk 把几何算出来，再读窗口坐标，否则 winfo_rootx 可能返回 0
        t.update_idletasks()
        sw = t.winfo_screenwidth()
        sh = t.winfo_screenheight()
        x = self.root.winfo_rootx() + 6
        y = self.root.winfo_rooty() - h - 8
        x = max(0, min(x, sw - w - 8))       # 不许跑出屏幕
        y = max(0, min(y, sh - h - 8))
        t.geometry("%dx%d+%d+%d" % (w, h, x, y))
        t.deiconify()
        self._tip = t

    def hide_tip(self):
        tip = self._valid_tip()
        if tip is not None:
            try:
                tip.destroy()
            except Exception:
                pass
        self._tip = None

    # ---------- 右键菜单 ----------
    def on_menu(self, e):
        m = tk.Menu(self.root, tearoff=0, bg=BG, fg=FG,
                    activebackground="#2A2E34", activeforeground=FG,
                    relief="flat", borderwidth=0)
        m.add_command(label="立即刷新", command=lambda: self.refresh(force=True))
        m.add_command(label="重新贴回任务栏", command=self.place_at_taskbar)
        m.add_separator()
        state = "已开启" if autostart_enabled(self.item) else "未开启"
        m.add_command(label="开机自启 (%s)" % state,
                      command=self.toggle_autostart)
        m.add_separator()
        m.add_command(label="退出", command=self.root.destroy)
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    def toggle_autostart(self):
        set_autostart(not autostart_enabled(self.item), item=self.item)

    def run(self):
        self.root.mainloop()


# ---------------- 命令行入口 ----------------
def source_ids():
    return [s.get("id") for s in CFG.get("sources", []) if s.get("id")]


def stop_running():
    """结束本目录下正在跑的挂件和取数服务（按 pid 文件精确定位，不误伤自身）。"""
    import signal

    killed = []
    pidfiles = [(BAR_PID_FILE, "挂件%s" % (ITEM or ""))]
    for sid in source_ids():
        p = os.path.join(HERE, "bar-%s.pid" % sid)
        if p != BAR_PID_FILE:
            pidfiles.append((p, "挂件-%s" % sid))
    pidfiles.append((BRIDGE_PID_FILE, "取数服务"))

    for pidfile, label in pidfiles:
        if not os.path.exists(pidfile):
            continue
        try:
            with open(pidfile) as f:
                pid = int(f.read().strip())
        except Exception:
            continue
        if pid == os.getpid():
            continue
        try:
            # 确认还是 python 进程，避免 PID 复用误杀
            h = kernel32.OpenProcess(0x1000, False, pid)      # QUERY_LIMITED_INFORMATION
            if h:
                buf = ctypes.create_unicode_buffer(512)
                n = wt.DWORD(512)
                ok = kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n))
                kernel32.CloseHandle(h)
                if not ok or "python" not in buf.value.lower():
                    continue
            os.kill(pid, signal.SIGTERM)
            killed.append("%s(pid=%d)" % (label, pid))
        except Exception:
            pass
        finally:
            try:
                os.remove(pidfile)
            except OSError:
                pass
    return killed


def do_check():
    print("安装目录 : %s" % HERE)
    print("配置文件 : %s%s" % (CONFIG_PATH,
                               "" if os.path.exists(CONFIG_PATH) else "  (不存在!)"))
    print("  bridge_port    = %s" % BRIDGE_PORT)
    print("  refresh_seconds= %s" % CFG.get("refresh_seconds", 30))
    print("  cache_seconds  = %s" % CFG.get("cache_seconds", 90))

    if not os.path.exists(CONFIG_PATH):
        print("\n✗ 找不到 config.json，请先复制 config.example.json 并填好 sources")
        return 2

    srcs = CFG.get("sources", [])
    if not srcs:
        print("\n✗ config.json 里没有 sources")
        return 2

    print("\n已配置数据源：")
    for s in srcs:
        kind = s.get("kind", "codex")
        where = s.get("url") or (s.get("proxy", "") + "  (auth_index=%s)" % s.get("auth_index", "?"))
        key = s.get("management_key") or ""
        if key:
            where += "  key=%s...%s" % (key[:6], key[-4:])
        print("  - %-8s kind=%-9s %s" % (s.get("id"), kind, where))

    try:
        ensure_bridge()
    except Exception:
        pass

    print("\n逐个试拉：")
    rc = 0
    for s in srcs:
        sid = s.get("id")
        print("  %-8s " % sid, end="", flush=True)
        try:
            rows = fetch_quota(force=True, item=sid)
            d = rows[0] if isinstance(rows, list) and rows else {}
        except Exception as e:
            print("✗ %s" % e)
            print("           排查：端口 %d 是否被占用？上游是否可达？" % BRIDGE_PORT)
            rc = 1
            continue
        if not isinstance(d, dict) or (d.get("five") or "").startswith("--") \
                or d.get("five") == "错误":
            print("✗ 上游查询失败")
            for line in (d.get("detail") or "").split("\n"):
                print("           " + line)
            rc = 1
        else:
            print("✓  %s │ %s" % (d.get("five"), d.get("week")))
    return rc


def main():
    # 命令行输出统一走 UTF-8，避免 GBK 控制台报 UnicodeEncodeError
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(add_help=True,
                                 description="额度任务栏挂件（Codex / Gemini 等）")
    ap.add_argument("--item", default="", help="只显示某个数据源，缺省显示第一个")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--install", action="store_true", help="写开机自启并立即启动")
    g.add_argument("--uninstall", action="store_true", help="取消开机自启并停止进程")
    g.add_argument("--check", action="store_true", help="自检配置并逐个试拉数据")
    args = ap.parse_args()

    global ITEM
    set_item(args.item)

    ids = source_ids()
    if args.check:
        return do_check()

    if args.uninstall:
        for sid in ids:
            set_autostart(False, item=sid)
        set_autostart(False)
        for name in stop_running():
            print("已停止 %s" % name)
        print("✓ 已取消开机自启并停止运行")
        return 0

    if args.install:
        set_autostart(False)          # 清掉旧版不带 --item 的遗留项
        for sid in ids:
            set_autostart(False, item=sid)
        set_autostart(True)           # 单个挂件，竖排显示全部数据源
        print("✓ 已写入开机自启（将显示 %d 个数据源：%s）" % (len(ids), ", ".join(ids)))
        stop_running()
        time.sleep(0.5)
        subprocess.Popen([PYTHONW, os.path.abspath(__file__)],
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        time.sleep(2.5)
        if bridge_alive():
            print("✓ 挂件已启动，取数服务端口 %d 正常" % BRIDGE_PORT)
        else:
            print("! 挂件已启动，但取数服务没起来，请看 quota_bridge.log")
        return 0

    if tk is None:
        print("✗ 未安装 tkinter，无法显示挂件", file=sys.stderr)
        return 2
    if not acquire_single_instance():
        return 0
    install_excepthooks()
    write_pid_file(BAR_PID_FILE)
    wlog("=== 挂件启动 pid=%d item=%s ===" % (os.getpid(), ITEM or "-"))
    QuotaBar().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())