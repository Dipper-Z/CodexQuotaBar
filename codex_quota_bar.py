# -*- coding: utf-8 -*-
"""
Codex 额度挂件 —— 贴在 Windows 任务栏上的半透明小挂件。

数据来自同目录的 quota_bridge.py（本地取数服务），
取数服务再向上游代理查询 Codex 用量。本脚本只负责展示，不直接联网。

命令行用法：
    python codex_quota_bar.py              启动挂件（正常情况用不到手敲）
    python codex_quota_bar.py --install    写开机自启并立即启动
    python codex_quota_bar.py --uninstall  取消开机自启并停止
    python codex_quota_bar.py --check      自检：打印配置并试拉一次数据
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
BAR_PID_FILE = os.path.join(HERE, "bar.pid")
BRIDGE_PID_FILE = os.path.join(HERE, "bridge.pid")
REFRESH_MS  = 30000


def write_pid_file(path):
    try:
        with open(path, "w") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass


def load_config():
    cfg = {"bridge_port": 28317, "refresh_seconds": 30}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
                cfg.update(json.load(f))
        except Exception as e:
            print("config.json 解析失败: %s" % e, file=sys.stderr)
    return cfg


CFG         = load_config()
BRIDGE_PORT = int(CFG.get("bridge_port", 28317))
BRIDGE      = "http://127.0.0.1:%d/" % BRIDGE_PORT
REFRESH_MS  = int(CFG.get("refresh_seconds", 30)) * 1000

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

GOOD, WARN, BAD, OFF = "#3DD68C", "#F5A524", "#F2555A", "#6B7280"

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VAL = "CodexQuotaBar"


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


def taskbar_geometry():
    """返回 (任务栏下沿 y, 挂件右边界 x, 任务栏顶沿 y, 任务栏高度)。

    右边界取「系统托盘左沿」与「其它挂件左沿」中更靠左的一个，
    这样挂件会紧挨在 TrafficMonitor 之类显示项的左边，不会互相遮挡。
    """
    hwnd = user32.FindWindowW("Shell_TrayWnd", None)
    if not hwnd:
        sw = user32.GetSystemMetrics(0)
        sh = user32.GetSystemMetrics(1)
        return sh, sw - 340, sh - 60, 60

    tb = _rect(hwnd)
    height = tb[3] - tb[1]

    tray = user32.FindWindowExW(hwnd, 0, "TrayNotifyWnd", None)
    if not tray:
        tray = user32.FindWindowExW(hwnd, 0, "TrayButton", None)
    if not tray:
        tray = user32.FindWindowExW(hwnd, 0,
                                    "Windows.UI.Composition.DesktopWindowContentBridge", None)
    edges = []
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


def fetch_quota():
    with urllib.request.urlopen(BRIDGE, timeout=6) as r:
        return json.loads(r.read().decode("utf-8"))


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
def autostart_enabled():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            return winreg.QueryValueEx(k, RUN_VAL)[0]
    except Exception:
        return None


def set_autostart(on):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            cmd = '"%s" "%s"' % (PYTHONW, os.path.abspath(__file__))
            winreg.SetValueEx(k, RUN_VAL, 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(k, RUN_VAL)
            except FileNotFoundError:
                pass


# ---------------- 挂件 ----------------
def acquire_single_instance():
    """已有一个挂件在跑就退出，避免重复启动出现两个。"""
    k32 = ctypes.windll.kernel32
    k32.CreateMutexW(None, False, "Local\\CodexQuotaBarSingleton")
    return k32.GetLastError() != 183      # 183 = ERROR_ALREADY_EXISTS


class QuotaBar:
    PAD_X = 14
    RADIUS = 9
    HEIGHT = 30

    def __init__(self):
        ensure_bridge()

        # 首次同步取数，保证第一次绘制就有内容、尺寸正确
        try:
            self.data = fetch_quota()
        except Exception:
            self.data = {"five": "-- 离线", "week": "--", "detail": "取数服务无响应"}

        self.pos = None
        self._drag = None
        self._tip = None
        self._busy = False

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
        self.f_reg = tkfont.Font(family="Microsoft YaHei UI", size=-17)

        self.cv = tk.Canvas(self.root, bg=KEY_COLOR, highlightthickness=0, bd=0)
        self.cv.pack()

        self.cv.bind("<ButtonPress-1>", self.on_press)
        self.cv.bind("<B1-Motion>", self.on_drag)
        self.cv.bind("<ButtonRelease-1>", self.on_release)
        self.cv.bind("<Button-3>", self.on_menu)
        self.cv.bind("<Enter>", lambda e: self.show_tip())
        self.cv.bind("<Leave>", lambda e: self.hide_tip())
        self.cv.bind("<Double-Button-1>", lambda e: self.refresh(force=True))

        self.layout()
        self.root.after(REFRESH_MS, self.tick)

    # ---------- 绘制 ----------
    def _round_rect(self, x1, y1, x2, y2, r, **kw):
        c = self.cv
        c.create_arc(x1, y1, x1 + 2 * r, y1 + 2 * r, start=90,  extent=90,  style="pieslice", **kw)
        c.create_arc(x2 - 2 * r, y1, x2, y1 + 2 * r, start=0,   extent=90,  style="pieslice", **kw)
        c.create_arc(x1, y2 - 2 * r, x1 + 2 * r, y2, start=180, extent=90,  style="pieslice", **kw)
        c.create_arc(x2 - 2 * r, y2 - 2 * r, x2, y2, start=270, extent=90, style="pieslice", **kw)
        c.create_rectangle(x1 + r, y1, x2 - r, y2, **kw)
        c.create_rectangle(x1, y1 + r, x2, y2 - r, **kw)

    def layout(self):
        """重算尺寸、绘制、并一次性把「尺寸+位置」写进窗口。"""
        d = self.data
        five = d.get("five", "--")
        week = d.get("week", "--")

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
        parts_week = week

        t1 = self.f_bold.measure(parts_five)
        t2 = self.f_reg.measure(parts_week)
        dot, gap, sep = 10, 10, 17
        width = int(self.PAD_X * 2 + dot + gap + t1 + sep + t2)
        height = self.HEIGHT

        self.cv.configure(width=width, height=height)

        self.cv.delete("all")
        self._round_rect(1, 1, width - 1, height - 1, self.RADIUS,
                         fill=BG, outline=BORDER, width=1)

        x, y = self.PAD_X, height // 2
        self.cv.create_oval(x, y - 4, x + 8, y + 4, fill=color, outline="")
        x += dot + gap
        self.cv.create_text(x, y, text=parts_five, font=self.f_bold, fill=FG, anchor="w")
        x += t1 + 8
        self.cv.create_line(x, y - 7, x, y + 7, fill=SEPARATOR)
        x += 9
        self.cv.create_text(x, y, text=parts_week, font=self.f_reg, fill=FG_DIM, anchor="w")

        if self.pos is None:
            self.pos = self._taskbar_pos(width, height)
        self.root.geometry("%dx%d+%d+%d" % (width, height, self.pos[0], self.pos[1]))

    # ---------- 位置 ----------
    @staticmethod
    def _taskbar_pos(w, h):
        bottom, right, top, _ = taskbar_geometry()
        return (right - w, top + (bottom - top - h) // 2)

    def place_at_taskbar(self):
        w = self.root.winfo_reqwidth() or 220
        h = self.root.winfo_reqheight() or self.HEIGHT
        self.pos = self._taskbar_pos(w, h)
        self.layout()

    def on_press(self, e):
        self._drag = (e.x_root, e.y_root, self.root.winfo_x(), self.root.winfo_y())

    def on_drag(self, e):
        if not self._drag:
            return
        sx, sy, wx, wy = self._drag
        self.pos = (wx + e.x_root - sx, wy + e.y_root - sy)
        w = self.root.winfo_width()
        h = self.root.winfo_height()
        self.root.geometry("%dx%d+%d+%d" % (w, h, self.pos[0], self.pos[1]))

    def on_release(self, e):
        self._drag = None
        # 松手时离任务栏很近就自动吸附回去
        bottom, _, top, _ = taskbar_geometry()
        h = self.root.winfo_height() or self.HEIGHT
        x, y = self.pos
        if (top - 48) <= y <= (bottom + 48):
            self.place_at_taskbar()

    # ---------- 数据 ----------
    def refresh(self, force=False):
        if self._busy:
            return
        self._busy = True

        def work():
            try:
                self.data = fetch_quota()
            except Exception:
                self.data = {"five": "-- 离线", "week": "--",
                             "detail": "取数服务无响应"}
            self.root.after(0, self._done)

        threading.Thread(target=work, daemon=True).start()

    def _done(self):
        self._busy = False
        self.layout()
        if self._tip:
            self.show_tip()

    def tick(self):
        self.refresh()
        self.root.after(REFRESH_MS, self.tick)

    # ---------- 悬停明细 ----------
    def show_tip(self):
        self.hide_tip()
        text = self.data.get("detail") or "正在获取…"
        lines = text.split("\n")
        f = tkfont.Font(family="Microsoft YaHei UI", size=-16)
        line_h = f.metrics("linespace") + 4

        t = tk.Toplevel(self.root)
        t.overrideredirect(True)
        t.attributes("-topmost", True)
        t.configure(bg=KEY_COLOR)

        w = int(max(f.measure(l) for l in lines)) + 28
        h = len(lines) * line_h + 16
        cv = tk.Canvas(t, width=w, height=h, bg=KEY_COLOR, highlightthickness=0, bd=0)
        cv.pack()

        # 复用圆角绘制
        helper = QuotaBar.__new__(QuotaBar)
        helper.cv = cv
        helper._round_rect(1, 1, w - 1, h - 1, self.RADIUS,
                           fill=BG, outline=BORDER, width=1)
        cv.create_text(14, h // 2, text=text, font=f, fill=FG,
                       anchor="w", justify="left")

        x = self.root.winfo_x() + 6
        y = self.root.winfo_y() - h - 6
        t.geometry("+%d+%d" % (max(0, x), max(0, y)))
        self._tip = t

    def hide_tip(self):
        if self._tip is not None:
            try:
                self._tip.destroy()
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
        state = "已开启" if autostart_enabled() else "未开启"
        m.add_command(label="开机自启 (%s)" % state,
                      command=self.toggle_autostart)
        m.add_separator()
        m.add_command(label="退出", command=self.root.destroy)
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    def toggle_autostart(self):
        set_autostart(not autostart_enabled())

    def run(self):
        self.root.mainloop()


# ---------------- 命令行入口 ----------------
DEFAULTS_HINT = {"proxy": "http://127.0.0.1:8317", "management_key": "",
                 "auth_index": "", "bridge_port": 28317, "refresh_seconds": 30}


def stop_running():
    """结束本目录下正在跑的挂件和取数服务（按 pid 文件精确定位，不误伤自身）。"""
    import signal

    killed = []
    for pidfile, label in ((BAR_PID_FILE, "挂件"), (BRIDGE_PID_FILE, "取数服务")):
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
    for k in ("proxy", "management_key", "auth_index", "bridge_port",
              "refresh_seconds"):
        v = CFG.get(k, DEFAULTS_HINT.get(k, ""))
        if k == "management_key" and v:
            v = v[:6] + "..." + v[-4:] + "  (已设置)"
        print("  %-16s = %s" % (k, v))

    if not os.path.exists(CONFIG_PATH):
        print("\n✗ 找不到 config.json，请先复制一份并填好 proxy / management_key / auth_index")
        return 2

    print("\n正在拉取上游数据 ...", end=" ", flush=True)
    try:
        ensure_bridge()
    except Exception:
        pass
    try:
        d = fetch_quota()
    except Exception as e:
        print("✗ 失败: %s" % e)
        print("  排查：取数服务是否起来？端口 %d 是否被占用？上游代理是否可达？" % BRIDGE_PORT)
        return 1
    if d.get("five", "").startswith("--"):
        print("✗ 上游查询失败")
        for line in (d.get("detail") or "").split("\n"):
            print("    " + line)
        return 1
    print("✓ 正常")
    print("  5h  : %s" % d.get("five"))
    print("  周  : %s" % d.get("week"))
    return 0


def main():
    # 命令行输出统一走 UTF-8，避免 GBK 控制台报 UnicodeEncodeError
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(add_help=True,
                                 description="Codex 额度任务栏挂件")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--install", action="store_true", help="写开机自启并立即启动")
    g.add_argument("--uninstall", action="store_true", help="取消开机自启并停止进程")
    g.add_argument("--check", action="store_true", help="自检配置并试拉一次数据")
    args = ap.parse_args()

    if args.check:
        return do_check()

    if args.uninstall:
        set_autostart(False)
        for name in stop_running():
            print("已停止 %s" % name)
        print("✓ 已取消开机自启并停止运行")
        return 0

    if args.install:
        set_autostart(True)
        print("✓ 已写入开机自启")
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
    write_pid_file(BAR_PID_FILE)
    QuotaBar().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())