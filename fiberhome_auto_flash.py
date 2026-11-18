import os
import sys
import re
import time
import socket
import hashlib
import subprocess
import urllib.request
import urllib.error
import base64
import json
import webbrowser
import unicodedata
import ssl
import ctypes
import http.client
import http.server
import threading
import uuid
from ctypes import wintypes
from datetime import datetime
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

# ================= 路径智能识别 =================
GATEWAY_IP = "192.168.1.1"
UBOOT_IP = "192.168.0.1"
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
    INTERNAL_DIR = getattr(sys, '_MEIPASS', BASE_DIR)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    INTERNAL_DIR = BASE_DIR

BACKUP_DIR = os.path.join(BASE_DIR, "backup_mtd")
UBI_TOOL_NAME = "an758x-stock2ubi"

# ================= 全局变量 =================
CURRENT_FH_PORT = None
CURRENT_MODEL = None
_STATUS_CACHE = {"time": 0, "data": None}
CURRENT_VIEW = "FLASH"
CURRENT_FACTORY_BIN = None
CURRENT_FW_ITB = None          # 菜单 9：预检通过后锁定的系统固件（一键流程全程复用，不再询问）
HASH_CACHE_FILE = os.path.join(BASE_DIR, "files_sha256.json")

FLASH_CHOICES = {"P", "1", "2", "3", "4", "5", "6", "7", "8", "9", "G", "F", "M", "R", "0", "00"}
TOOLS_CHOICES = {"1", "01", "2", "02", "3", "03", "4", "04", "5", "05",
                 "6", "06", "7", "07", "8", "08", "9", "09", "10",
                 "F", "G", "M", "R", "0", "00"}

SPI_NAND_VENDORS = {
    0xef: "Winbond (华邦)",
    0x98: "Kioxia (铠侠)",
    0x2c: "Micron (美光)",
    0xc2: "Macronix (旺宏)",
    0x1c: "ESMT (晶豪)",
    0xad: "Hynix (海力士)",
    0xec: "Samsung (三星)",
    0x01: "SkyHigh (天海存储)",
}

# ================= 闪存型号适配表（按具体型号，不再按厂商一刀切） =================
# 需要临时过渡引导（8bit-read-4bit-write）的闪存型号
FIX_UBOOT_CHIPS = [
    "W29N02KVSIAF",        # Winbond 华邦
    "S34ML02H200TFI000",   # SkyHigh
    "S34ML02G300TFI00",    # SkyHigh
]
def _chip_matched(chip, models):
    """芯片型号与适配表双向子串匹配（/proc 输出的 chip 字段可能是完整订货号或缩写）"""
    c = re.sub(r'[^A-Z0-9]', '', (chip or "").upper())
    if not c:
        return False
    for m in models:
        k = re.sub(r'[^A-Z0-9]', '', m.upper())
        if k and (k in c or c in k):
            return True
    return False

def chip_needs_fixboot(info):
    """该闪存型号是否需要临时过渡引导（第一阶段刷 fix_uboot）"""
    return _chip_matched(info.get("chip"), FIX_UBOOT_CHIPS)

# ================= 刷机文件配置 =================
TOOLS_FILES = [
    {"name": "an758x-stock2ubi",
     "url": "https://github.com/pbs05/an758x-stock2ubi/releases/latest/download/an758x-stock2ubi"},
    {"name": "fiberhome-factory.exe",
     "url": "https://github.com/inonzi/fiberhome-factory/releases/download/v0.1.1.cli/fiberhome-factory.exe"},
    {"name": "fiberhome-factory-cli.exe",
     "url": "https://github.com/inonzi/fiberhome-factory/releases/download/v0.1.1.cli/fiberhome-factory-cli.exe"},
]

UBOOT_BASE = "https://github.com/pbs05/uboot-an758x/releases/latest/download/"
UBOOT_SUFFIXES = ["preloader.bin", "bl31-u-boot.fip", "firstblock.bin"]

FIX_UBOOT_BASE = "https://github.com/inonzi/uboot-an758x-skyhigh-winbond/releases/download/v2026.07-4-skyhigh-winbond/"
FIX_UBOOT_SUFFIXES = [
    "preloader-tmp-8bit-read-4bit-write.bin",
    "bl31-u-boot-tmp-8bit-read-4bit-write.fip",
    "firstblock-tmp-8bit-read-4bit-write.bin",
]

FIRMWARE_BASE = "https://github.com/inonzi/Actions-OpenWrt-AN758X/releases/download/ponwrt-an7581-all-master/"

MODEL_DATA = [
    {
        "name": "5585F-CT (电信)",
        "dir": "5585F-CT",
        "prefix": "an7581-fiberhome-hg5585f-ct",
        "firmware": "ponwrt-airoha-an7581-fiberhome_hg5585f-ct-squashfs-sysupgrade.itb",
    },
    {
        "name": "5585F-CU (联通)",
        "dir": "5585F-CU",
        "prefix": "an7581-fiberhome-hg5585f-cu",
        "firmware": "ponwrt-airoha-an7581-fiberhome_hg5585f-cu-squashfs-sysupgrade.itb",
    },
    {
        "name": "5382A",
        "dir": "5382A",
        "prefix": "an7581-fiberhome-hg5382a",
        "firmware": "ponwrt-airoha-an7581-fiberhome_hg5382a-squashfs-sysupgrade.itb",
    },
]

PROXIES = ["", "https://gh-proxy.com/", "https://ghfast.top/", "https://ghproxy.net/"]
PREFERRED_PROXY = None

def _ordered_proxies():
    if PREFERRED_PROXY is None:
        return PROXIES
    return [PREFERRED_PROXY] + [p for p in PROXIES if p != PREFERRED_PROXY]

def _mark_proxy_ok(proxy):
    global PREFERRED_PROXY
    if PREFERRED_PROXY != proxy:
        PREFERRED_PROXY = proxy
        node_name = "官方直连" if proxy == "" else proxy.replace("https://", "").strip("/")
        Logger.info(f"已锁定可用节点: {paint(node_name, 'green')}，后续请求优先使用。")

HASH_REPOS = [
    ("pbs05/uboot-an758x", None),
    ("pbs05/an758x-stock2ubi", None),
    ("inonzi/fiberhome-factory", "v0.1.1.cli"),
    ("inonzi/uboot-an758x-skyhigh-winbond", "v2026.07-4-skyhigh-winbond"),
    ("inonzi/Actions-OpenWrt-AN758X", "ponwrt-an7581-all-master"),
]

# ================= 颜色与排版系统 =================
C = {
    "reset": "\033[0m", "bold": "\033[1m", "dim": "\033[2m",
    "cyan": "\033[96m", "blue": "\033[94m", "green": "\033[92m",
    "yellow": "\033[93m", "red": "\033[91m", "white": "\033[97m", "gray": "\033[90m",
}

def paint(text, *styles):
    return "".join(C[s] for s in styles) + str(text) + C["reset"]

def line(char="─", width=76, color="gray"):
    _clear_progress_line()
    print(paint(char * width, color))

def clear():
    os.system("cls" if os.name == "nt" else "clear")

def pad_text(text, target_width):
    width = sum(2 if unicodedata.east_asian_width(c) in ('F', 'W', 'A') else 1 for c in text)
    padding = target_width - width
    return text + (" " * padding if padding > 0 else "")

# ================= 进度条状态（防止日志被 \r 覆盖成半行） =================
_PROGRESS_ACTIVE = False

def _clear_progress_line():
    """如果上一行还停在进度条上，先擦掉它，避免后续日志拼接错行"""
    global _PROGRESS_ACTIVE
    if _PROGRESS_ACTIVE:
        sys.stdout.write("\r\033[K")
        sys.stdout.flush()
        _PROGRESS_ACTIVE = False

class Logger:
    @staticmethod
    def _log(level, message):
        _clear_progress_line()
        now = datetime.now().strftime("%H:%M:%S")
        colors = {"INFO": "cyan", "OK": "green", "SUCCESS": "green", "WARN": "yellow", "ERROR": "red"}
        label = f"{now}  {level:<5}"
        print(f"  {paint(label, colors.get(level, 'white'))}  {message}")
    @classmethod
    def info(cls, msg): cls._log("INFO", msg)
    @classmethod
    def success(cls, msg): cls._log("OK", msg)
    @classmethod
    def warning(cls, msg): cls._log("WARN", msg)
    @classmethod
    def error(cls, msg): cls._log("ERROR", msg)

# ================= 进度条绘制函数 =================
def draw_progress(prefix, current, total, speed=0, width=28):
    global _PROGRESS_ACTIVE
    if total <= 0: total = 1
    percent = min(1.0, current / total)
    filled = int(percent * width)
    bar = paint("█" * filled, "cyan") + paint("░" * (width - filled), "gray")
    percent_str = f"{int(percent * 100):3d}%"
    speed_str = f"{speed / 1024 / 1024:.2f} MB/s" if speed > 0 else ""
    sys.stdout.write(f"\r  {prefix:<16} {bar} {percent_str}  {speed_str}\033[K")
    sys.stdout.flush()
    _PROGRESS_ACTIVE = True

# ================= 刷机全流程日志落盘（菜单 7/8/9 成功时写入 info/） =================
_FLASH_LOG = {"active": False, "tee": None, "real": None,
              "menu": None, "title": "", "start": None}
_LAST_FLASH_FACTORY = None   # 本次刷机实际选定的 factory-1m.bin 路径（用于日志反推 MAC）

class _FlashLogTee:
    """代理 sys.stdout：终端照常输出，同时把文本存进内存。
    所有方法都吞异常——日志只是旁路，绝不能影响刷机主流程。"""
    def __init__(self, real):
        self._real = real
        self._lines = []
        self._buf = _FlashLogTeeBuffer(getattr(real, "buffer", real), self._lines)

    def write(self, s):
        try: self._real.write(s)
        except Exception: pass
        try:
            self._lines.append(s if isinstance(s, str) else s.decode("utf-8", "replace"))
        except Exception: pass
        try: return len(s)
        except Exception: return 0

    def flush(self):
        try: self._real.flush()
        except Exception: pass

    def isatty(self):
        try: return self._real.isatty()
        except Exception: return False

    @property
    def buffer(self):
        return self._buf

    @property
    def encoding(self):
        return getattr(self._real, "encoding", "utf-8") or "utf-8"

    def __getattr__(self, name):
        return getattr(self._real, name)

class _FlashLogTeeBuffer:
    """代理 sys.stdout.buffer，兼容 convert_factory_to_1m 的字节流转发"""
    def __init__(self, real, lines):
        self._real = real
        self._lines = lines

    def write(self, b):
        try: self._real.write(b)
        except Exception: pass
        try:
            self._lines.append(b.decode("utf-8", "replace") if isinstance(b, (bytes, bytearray)) else str(b))
        except Exception: pass
        try: return len(b)
        except Exception: return 0

    def flush(self):
        try: self._real.flush()
        except Exception: pass

    def __getattr__(self, name):
        return getattr(self._real, name)

def _flash_log_begin(menu, title):
    """开始记录一次刷机全流程（重复调用不会嵌套）"""
    global _FLASH_LOG, _LAST_FLASH_FACTORY
    if _FLASH_LOG["active"]:
        return
    _LAST_FLASH_FACTORY = None
    try:
        real = sys.stdout
        tee = _FlashLogTee(real)
        _FLASH_LOG = {"active": True, "tee": tee, "real": real,
                      "menu": menu, "title": title, "start": datetime.now()}
        sys.stdout = tee
    except Exception:
        _FLASH_LOG = {"active": False, "tee": None, "real": None,
                      "menu": None, "title": "", "start": None}

def _resolve_flash_mac():
    """尽力解析光猫 MAC：本次选定 factory 目录 > 本次生成 factory > backup_mtd 目录 > deviceinfo > ARP"""
    # 优先：本次刷机实际选定的板级数据（菜单 8 用 find_factory_bin 手选，最准）
    for p in (_LAST_FLASH_FACTORY, CURRENT_FACTORY_BIN):
        if p:
            d = os.path.basename(os.path.dirname(p))
            if re.fullmatch(r"[0-9A-Fa-f]{12}", d):
                return d.upper()
    cand = []
    model_dir = os.path.join(BASE_DIR, CURRENT_MODEL["dir"]) if CURRENT_MODEL else None
    if model_dir:
        root = os.path.join(model_dir, "backup_mtd")
        if os.path.isdir(root):
            for d in os.listdir(root):
                if re.fullmatch(r"[0-9A-Fa-f]{12}", d):
                    try: cand.append((os.path.getmtime(os.path.join(root, d)), d.upper()))
                    except Exception: pass
        info = os.path.join(model_dir, "info")
        if os.path.isdir(info):
            for fn in os.listdir(info):
                mm = re.match(r"deviceinfo_([0-9A-Fa-f]{12})\.txt$", fn)
                if mm:
                    try: cand.append((os.path.getmtime(os.path.join(info, fn)), mm.group(1).upper()))
                    except Exception: pass
    if cand:
        cand.sort(reverse=True)
        return cand[0][1]
    try:
        m = get_mac_from_arp(GATEWAY_IP)
        if m: return m.upper()
    except Exception:
        pass
    return "UNKNOWN_MAC"

def _flash_log_clean(raw):
    """去 ANSI 颜色/控制码、折叠 \r 进度条覆盖、压缩连续空行"""
    raw = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", raw)
    out = []
    for ln in raw.split("\n"):
        if "\r" in ln:
            ln = ln.split("\r")[-1]
        out.append(ln.rstrip())
    res, blank = [], 0
    for ln in out:
        if not ln:
            blank += 1
            if blank > 1: continue
        else:
            blank = 0
        res.append(ln)
    return "\n".join(res)

def _flash_log_end(success, menu, title):
    """结束记录；成功则把全流程写入 机型目录/info/flash_menu<N>_<MAC>_<时间>.log"""
    global _FLASH_LOG
    if not _FLASH_LOG["active"]:
        return
    info = dict(_FLASH_LOG)
    try: sys.stdout = info["real"]        # 无论如何先恢复
    except Exception: pass
    _FLASH_LOG = {"active": False, "tee": None, "real": None,
                  "menu": None, "title": "", "start": None}
    if not success:
        return
    try:
        mac = _resolve_flash_mac()
        end = datetime.now()
        start = info["start"] or end
        body = _flash_log_clean("".join(info["tee"]._lines))
        model_name = CURRENT_MODEL["name"] if CURRENT_MODEL else "未选择机型"
        head = (
            "=" * 60 + "\n"
            " 烽火光猫维护与刷机辅助工具 v3.3 · 刷机全流程日志\n"
            + "=" * 60 + "\n"
            f" 菜单     : [{menu}] {title}\n"
            f" 机型     : {model_name}\n"
            f" 光猫 MAC : {mac}\n"
            f" 开始时间 : {start.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f" 结束时间 : {end.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f" 结果     : 成功\n"
            + "=" * 60 + "\n\n"
        )
        info_dir = os.path.join(BASE_DIR, CURRENT_MODEL["dir"] if CURRENT_MODEL else "", "info")
        os.makedirs(info_dir, exist_ok=True)
        fname = f"flash_menu{menu}_{mac}_{end.strftime('%Y%m%d_%H%M%S')}.log"
        fpath = os.path.join(info_dir, fname)
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(head)
            f.write(body)
            f.write("\n\n" + "=" * 60 + "\n 日志结束（本次刷机成功）\n" + "=" * 60 + "\n")
        Logger.success(f"刷机全流程日志已保存: {fpath}")
    except Exception as e:
        try: Logger.warning(f"刷机日志保存失败（不影响刷机结果）: {e}")
        except Exception: pass

def _restore_after_interrupt():
    """Ctrl+C 等中断后的兜底清理：确保刷机日志代理（Tee）不残留在 sys.stdout 上。
    正常路径由分发层 finally 的 _flash_log_end 负责，这里只兜住极短的窗口期。"""
    try:
        if _FLASH_LOG.get("active"):
            _flash_log_end(False, _FLASH_LOG.get("menu"), _FLASH_LOG.get("title"))
    except Exception:
        try: sys.stdout = _FLASH_LOG.get("real") or sys.__stdout__
        except Exception: pass

# ================= 鼠标+键盘 混合交互复选框 =================
def interactive_checkbox(options):
    kernel32 = ctypes.windll.kernel32
    hStdin = kernel32.GetStdHandle(-10)
    hStdout = kernel32.GetStdHandle(-11)

    old_mode = wintypes.DWORD()
    kernel32.GetConsoleMode(hStdin, ctypes.byref(old_mode))
    new_mode = (old_mode.value & ~0x0040 & ~0x0002 & ~0x0004) | 0x0010 | 0x0080
    kernel32.SetConsoleMode(hStdin, new_mode)

    class COORD(ctypes.Structure):
        _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]
    class CONSOLE_SCREEN_BUFFER_INFO(ctypes.Structure):
        _fields_ = [("dwSize", COORD), ("dwCursorPosition", COORD), ("wAttributes", wintypes.WORD),
                    ("srWindow", wintypes.SMALL_RECT), ("dwMaximumWindowSize", COORD)]
    class MOUSE_EVENT_RECORD(ctypes.Structure):
        _fields_ = [("dwMousePosition", COORD), ("dwButtonState", wintypes.DWORD),
                    ("dwControlKeyState", wintypes.DWORD), ("dwEventFlags", wintypes.DWORD)]
    class KEY_EVENT_RECORD(ctypes.Structure):
        _fields_ = [("bKeyDown", wintypes.BOOL), ("wRepeatCount", wintypes.WORD),
                    ("wVirtualKeyCode", wintypes.WORD), ("wVirtualScanCode", wintypes.WORD),
                    ("uChar", wintypes.WCHAR), ("dwControlKeyState", wintypes.DWORD)]
    class EVENT_UNION(ctypes.Union):
        _fields_ = [("KeyEvent", KEY_EVENT_RECORD), ("MouseEvent", MOUSE_EVENT_RECORD)]
    class INPUT_RECORD(ctypes.Structure):
        _fields_ = [("EventType", wintypes.WORD), ("Event", EVENT_UNION)]

    csbi = CONSOLE_SCREEN_BUFFER_INFO()
    kernel32.GetConsoleScreenBufferInfo(hStdout, ctypes.byref(csbi))
    start_y = csbi.dwCursorPosition.Y

    selected = [False] * len(options)
    current_idx = 0

    def render():
        sys.stdout.write(f"\033[{start_y + 1};1H")
        for i, opt in enumerate(options):
            prefix = paint("  > ", "cyan", "bold") if i == current_idx else "    "
            box = paint("[*]", "green", "bold") if selected[i] else paint("[ ]", "gray")
            text = paint(opt, "white") if selected[i] else paint(opt, "gray")
            sys.stdout.write(f"\r{prefix}{box} {text}\033[K\n")
        sys.stdout.flush()

    sys.stdout.write("\033[?25l")
    render()

    try:
        while True:
            events = (INPUT_RECORD * 10)()
            num_read = wintypes.DWORD()
            kernel32.ReadConsoleInputW(hStdin, events, 10, ctypes.byref(num_read))
            need_render = False
            for i in range(num_read.value):
                ev = events[i]
                if ev.EventType == 0x0002:
                    mouse = ev.Event.MouseEvent
                    if mouse.dwButtonState & 0x0001:
                        click_y = mouse.dwMousePosition.Y
                        clicked_idx = click_y - start_y
                        if 0 <= clicked_idx < len(options):
                            current_idx = clicked_idx
                            selected[current_idx] = not selected[current_idx]
                            need_render = True
                elif ev.EventType == 0x0001:
                    key = ev.Event.KeyEvent
                    if key.bKeyDown:
                        if key.wVirtualKeyCode == 38:
                            current_idx = (current_idx - 1) % len(options)
                            need_render = True
                        elif key.wVirtualKeyCode == 40:
                            current_idx = (current_idx + 1) % len(options)
                            need_render = True
                        elif key.wVirtualKeyCode == 32:
                            selected[current_idx] = not selected[current_idx]
                            need_render = True
                        elif key.wVirtualKeyCode == 13:
                            return [i for i, v in enumerate(selected) if v]
            if need_render:
                render()
    finally:
        kernel32.SetConsoleMode(hStdin, old_mode.value)
        sys.stdout.write("\033[?25h")
        sys.stdout.write(f"\033[{start_y + len(options) + 1};1H\n")

# ================= 极简原生 Telnet 客户端 =================
class MiniTelnet:
    def __init__(self, host, port=23, timeout=5):
        self.sock = socket.create_connection((host, port), timeout)
        self.sock.settimeout(1.0)
    def write(self, data_bytes): self.sock.sendall(data_bytes)
    def read_all(self, wait_time=1.0):
        time.sleep(wait_time)
        data = b""
        self.sock.settimeout(0.2)
        try:
            while True:
                chunk = self.sock.recv(4096)
                if not chunk: break
                data += chunk
        except socket.timeout: pass
        finally: self.sock.settimeout(5.0)
        return re.sub(b'\xff[\xfb-\xfe].', b'', data)
    def close(self): self.sock.close()

# ================= 临时 HTTP 上传服务（替代 TFTP） =================
def get_all_local_ips():
    """枚举本机所有 IPv4（用于提示光猫可能看到哪个地址）"""
    ips = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    return ips

def start_temp_http(filename, bind_ip):
    candidates = [
        os.path.join(BASE_DIR, "tools", filename),
        os.path.join(BASE_DIR, filename),
        os.path.join(INTERNAL_DIR, filename),
    ]
    filepath = next((p for p in candidates if os.path.exists(p)), None)
    if not filepath:
        Logger.error(f"本地找不到文件: {filename}（已查找 tools/ 与根目录）")
        return None, None, None, None

    total = os.path.getsize(filepath)
    done_event = threading.Event()
    result = {"success": False}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != f"/{filename}":
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(total))
            self.end_headers()
            sent = 0
            start_time = time.time()
            try:
                with open(filepath, "rb") as f:
                    while True:
                        chunk = f.read(64 * 1024)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        sent += len(chunk)
                        elapsed = time.time() - start_time
                        speed = sent / elapsed if elapsed > 0 else 0
                        draw_progress("工具上传", sent, total, speed)
                result["success"] = (sent == total)
            except Exception as e:
                Logger.error(f"HTTP 发送异常: {e}")
            finally:
                done_event.set()

        def log_message(self, *args):
            pass

    try:
        # 绑定 0.0.0.0：所有网卡都监听，避免选错网卡 / 多网卡 / 防火墙改写导致连不上
        server = http.server.ThreadingHTTPServer(("", 0), Handler)
        server.daemon_threads = True
    except OSError as e:
        Logger.error(f"临时 HTTP 服务启动失败: {e}")
        return None, None, None, None

    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    addrs = get_all_local_ips()
    if bind_ip and bind_ip not in addrs:
        addrs.insert(0, bind_ip)
    Logger.info(f"临时 HTTP 已监听全部网卡 0.0.0.0:{port}"
                f"（本机地址: {', '.join(addrs) if addrs else bind_ip}）")
    Logger.warning(f"提示：端口 {port} 若连不上，请手动放行防火墙入站或临时关闭防火墙。")
    return server, port, done_event, result

# ================= 辅助函数 =================
def get_local_ip(target_ip):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1)
        s.connect((target_ip, 80))
        local_ip = s.getsockname()[0]
        s.close()
        if local_ip and local_ip != "127.0.0.1": return local_ip
    except Exception: pass
    return input(paint("  >> 输入本机 IP (如 192.168.1.2): ", "cyan")).strip()

def get_mac_from_arp(ip):
    subprocess.run(["ping", "-n", "1", "-w", "500", ip],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        result = subprocess.check_output(["arp", "-a", ip], encoding="gbk", errors="ignore")
        match = re.search(r'([0-9a-fA-F]{2}[-:]){5}[0-9a-fA-F]{2}', result)
        if match:
            return match.group(0).replace("-", "").replace(":", "").upper()
    except Exception:
        pass
    return None

# ================= 网络配置（双 IP 工具） =================
def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except Exception:
        return False

# ---- 编码自适应解码：中文 Windows 的 netsh / powershell 输出可能是 GBK/mbcs ----
def decode_bytes(b):
    if not b: return ""
    for enc in ("utf-8", "gbk", "mbcs"):
        try:
            return b.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return b.decode("utf-8", errors="replace")

def _run(args):
    """执行外部命令，返回 (returncode, stdout, stderr)，输出按编码自适应解码"""
    try:
        r = subprocess.run(args, capture_output=True)
        return r.returncode, decode_bytes(r.stdout), decode_bytes(r.stderr)
    except Exception as e:
        return -1, "", str(e)

def _ps(script):
    """保留：返回 CompletedProcess 对象（get_current_interface_index 依赖 .returncode/.stdout）"""
    try:
        return subprocess.run(["powershell", "-NoProfile", "-Command", script],
                              capture_output=True, text=True, encoding="gbk", errors="ignore")
    except Exception:
        return None

def get_interfaces():
    ifs = []
    try:
        _, out, _ = _run(["netsh", "interface", "ipv4", "show", "interfaces"])
        lines = out.strip().splitlines()
        hi = next((i for i, l in enumerate(lines) if "Idx" in l), 0)
        for l in lines[hi + 2:]:
            p = l.strip().split()
            if len(p) < 5: continue
            idx, state, name = p[0], p[3], " ".join(p[4:])
            # 修正：'connected' 是 'disconnected' 的子串，不能用 in 判定
            state = "connected" if state.lower() in ("connected", "已连接") else "disconnected"
            if "Loopback" in name or "环回" in name: continue
            ip = "无 IP"
            try:
                _, o, _ = _run(["netsh", "interface", "ipv4", "show", "addresses", f"name={name}"])
                m = re.findall(r"(?:IP Address|IP 地址):\s*(\d{1,3}(?:\.\d{1,3}){3})", o)
                if m: ip = ", ".join(m)
            except Exception:
                pass
            ifs.append({"idx": idx, "name": name, "state": state, "ip": ip})
    except Exception:
        pass
    return ifs

def get_name_by_idx(idx):
    """由接口索引反查网卡名（提权子进程只拿到 idx 时用）"""
    for f in get_interfaces():
        if f["idx"] == str(idx):
            return f["name"]
    return None

def run_netsh(args):
    """执行 netsh 并打印实际完整命令行；对 DHCP 幂等情况容错"""
    display = [f'"{a}"' if (' ' in a and not a.startswith('"')) else a for a in args]
    print(paint(f"  > netsh {' '.join(display)}", "gray"))
    rc, out, err = _run(["netsh"] + args)
    msg = (out or err).strip().replace("\n", " | ").replace("\r", "")
    if rc == 1 and "已在此接口上启用 DHCP" in msg:
        print(paint("  [已完成] 该接口当前已是 DHCP 状态", "gray"))
        return 0
    if rc != 0 and msg:
        print(paint(f"  [执行失败] rc={rc} {msg}", "gray"))
    elif rc == 0:
        print(paint("  [执行成功] rc=0", "gray"))
    return rc

def _show_ips(idx):
    """读取并打印该网卡实际绑定的 IPv4（验证用）"""
    try:
        _, out, _ = _run(["powershell", "-NoProfile", "-Command",
                          f"(Get-NetIPAddress -InterfaceIndex {idx} -AddressFamily IPv4 -EA SilentlyContinue).IPAddress"])
        ips = [x.strip() for x in out.strip().splitlines()
               if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", x.strip())]
    except Exception:
        ips = []
    print(paint("\n  [当前网卡实际绑定的 IPv4]:", "cyan"))
    for ip in ips: print(f"    - {ip}")
    if not ips: print("    (无)")
    return ips

def _do_fix(idx, name=None):
    """追加 1.10 / 0.10 双 IP。name 可省（提权子进程靠 idx 反查）。
    签名保持向后兼容：_auto_action / 菜单9 结尾 / 主菜单都只传 idx。"""
    if not name:
        name = get_name_by_idx(idx)
    if not name:
        print(paint("  [×] 无法定位网卡名（接口索引可能已变化），请从菜单 [1] 重新选择网卡。", "red", "bold"))
        return

    print(paint("  [1/3] 正在全局清理冲突的旧 IP...", "cyan"))
    ps_cmd = ('Get-NetIPAddress -IPAddress "192.168.1.10" | Remove-NetIPAddress -Confirm:$false -EA SilentlyContinue; '
              'Get-NetIPAddress -IPAddress "192.168.0.10" | Remove-NetIPAddress -Confirm:$false -EA SilentlyContinue')
    print(paint(f'  > powershell -Command "{ps_cmd}"', "gray"))
    _run(["powershell", "-NoProfile", "-Command", ps_cmd])
    time.sleep(0.5)

    print(paint("  [2/3] 正在追加双网段 IP...", "cyan"))
    name_arg = f"name={name}"
    run_netsh(["interface", "ipv4", "add", "address", name_arg, "address=192.168.1.10", "mask=255.255.255.0"])
    run_netsh(["interface", "ipv4", "add", "address", name_arg, "address=192.168.0.10", "mask=255.255.255.0"])

    print(paint("  [3/3] 等待网卡驱动同步并验证...", "cyan"))
    time.sleep(2)
    _, status, _ = _run(["powershell", "-NoProfile", "-Command",
                         f"(Get-NetAdapter -InterfaceIndex {idx} -EA SilentlyContinue).Status"])
    if status.strip() and status.strip() != "Up":
        print(paint(f"  [!] 网卡状态为 {status.strip()}（可能网线未插好），仍尝试绑定 IP。", "yellow"))
    ips = _show_ips(idx)
    ok1 = "192.168.1.10" in ips
    ok0 = "192.168.0.10" in ips
    print()
    if ok1 and ok0:
        print(paint("  [√] 双 IP 配置成功！", "green", "bold"))
        print(paint("     1.x -> http://192.168.1.1 (光猫 Web)", "gray"))
        print(paint("     0.x -> http://192.168.0.1 (刷机后 U-Boot)", "gray"))
    else:
        print(paint("  [×] IP 配置失败，请检查权限、网卡名或网卡驱动。", "red", "bold"))

def _do_reset(idx, name=None):
    """恢复 DHCP。签名保持向后兼容（同 _do_fix）。"""
    if not name:
        name = get_name_by_idx(idx)
    if not name:
        print(paint("  [×] 无法定位网卡名，请从菜单 [1] 重新操作。", "red", "bold"))
        return
    print(paint("  [1/2] 正在恢复 DHCP 自动获取...", "cyan"))
    name_arg = f"name={name}"
    run_netsh(["interface", "ipv4", "set", "address", name_arg, "source=dhcp"])
    run_netsh(["interface", "ipv4", "set", "dnsservers", name_arg, "source=dhcp"])
    print(paint("  [2/2] 刷新 DHCP 租约 (最多等待 5 秒)...", "cyan"))
    print(paint(f'  > ipconfig /renew "{name}"', "gray"))
    try:
        subprocess.run(["ipconfig", "/renew", name], capture_output=True, timeout=5)
    except Exception:
        pass
    print(paint("\n  [√] 网卡已恢复自动获取 IP。", "green", "bold"))

def _need_admin(idx, mode):
    print(paint("\n  [系统] 正在以管理员权限应用网络配置（若弹出 UAC 请允许）...", "yellow"))
    flag = "--fix" if mode == "fix" else "--reset"

    if getattr(sys, 'frozen', False):
        exe = sys.executable
        params = f'{flag} "{idx}"'
    else:
        exe = sys.executable
        params = f'"{os.path.abspath(__file__)}" {flag} "{idx}"'

    ps_cmd = f'Start-Process -FilePath "{exe}" -ArgumentList \'{params}\' -Verb RunAs -Wait'
    subprocess.run(["powershell", "-NoProfile", "-Command", ps_cmd],
                   creationflags=subprocess.CREATE_NO_WINDOW)

def get_current_interface_index():
    """通过当前连接光猫的 IP，反向查询对应的网卡 InterfaceIndex"""
    try:
        local_ip = get_local_ip(GATEWAY_IP)
        if not local_ip or local_ip == "127.0.0.1":
            return None
        cmd = f"(Get-NetIPAddress -IPAddress '{local_ip}' -AddressFamily IPv4 -EA SilentlyContinue).InterfaceIndex"
        r = _ps(cmd)
        if r and r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    return None

def run_network_fix(auto_mode=False):
    """菜单 [1] 网络配置。auto_mode=True 时（菜单 8/9 联动）选完网卡即自动配置双 IP。"""
    clear(); print()
    print(paint("  NETWORK ADAPTER TOOL", "bold", "cyan"))
    line("═", 76, "cyan")
    ifs = get_interfaces()
    if not ifs:
        print(paint("  未获取到网卡。请检查权限或网卡驱动。", "red"))
        return
    print(paint("  系统物理网卡列表：", "white"))
    for i, f in enumerate(ifs):
        st, sc = ("已连接", "green") if f["state"] == "connected" else ("未连接", "gray")
        ip_str = f["ip"] if f["ip"] != "无 IP" else "无 IP"
        if ip_str.startswith("169.254."): ip_str = "无 IP"
        ipc = "green" if ip_str != "无 IP" else "gray"
        print(f"    {paint(f'[{i+1}]'.ljust(5),'white')} {paint(pad_text(f['name'], 30),'white')} | "
              f"{paint(pad_text(st, 8), sc)} | IP: {paint(ip_str, ipc)}")
    print(f"    {paint('[0]', 'white')}   取消操作")

    ch = input(paint("\n  >> 请选择连接光猫的网卡编号: ", "cyan")).strip()
    if not ch.isdigit() or int(ch) == 0 or int(ch) > len(ifs):
        return
    t = ifs[int(ch) - 1]
    print(); print(paint(f"  >>> 您已选中网卡: 【{t['name']}】", "cyan", "bold"))

    if auto_mode:
        print(paint("  正在自动追加双网段 IP...", "cyan"))
        if not is_admin():
            _need_admin(t["idx"], "fix")
        else:
            _do_fix(t["idx"], name=t["name"])
        return

    print("\n  请选择操作：")
    print("    [1] 自动追加 1.10 和 0.10 双 IP")
    print("    [2] 恢复该网卡为 DHCP 自动获取")
    print("    [0] 取消")
    a = input(paint("  >> 请输入: ", "cyan")).strip()
    if a not in ("1", "2"):
        return
    if not is_admin():
        _need_admin(t["idx"], "fix" if a == "1" else "reset")
        return
    if a == "1":
        print(paint(f"\n  正在为网卡 [{t['name']}] 配置双网段 IP...", "white"))
        _do_fix(t["idx"], name=t["name"])
    else:
        print(paint(f"\n  正在将网卡 [{t['name']}] 恢复为 DHCP...", "white"))
        _do_reset(t["idx"], name=t["name"])

def _auto_action(mode, idx):
    """提权子进程入口：只拿到 idx，靠 get_name_by_idx 反查网卡名"""
    clear(); print()
    print(paint("  ADMIN MODE", "bold", "cyan"))
    line("═", 76, "cyan")
    if mode == "fix":
        print(paint("  正在为网卡追加双网段 IP...\n", "white"))
        _do_fix(idx)
    else:
        print(paint("  正在恢复 DHCP...\n", "white"))
        _do_reset(idx)
    # 提权窗口跑完停留 2 秒自动关闭，不按回车，避免卡住后台
    time.sleep(2)

# ================= 通用 HTTP fh_tool 请求器 =================
def send_fh_tool_request(func_name, extra_params=None, timeout=10):  
    global CURRENT_FH_PORT
    mac = get_mac_from_arp(GATEWAY_IP)
    if not mac: return None, "无法获取 MAC"

    h = hashlib.sha256(mac.encode("ascii")).hexdigest()
    key = "".join(h[2*i+2] for i in range(16)).encode("ascii")
    iv  = "".join(h[3*i+3] for i in range(16)).encode("ascii")
    payload = {"index": "1", "func": func_name}
    if extra_params: payload.update(extra_params)
    pt = json.dumps(payload, separators=(",", ":"))
    cipher_enc = AES.new(key, AES.MODE_CBC, iv)
    encrypted_payload = base64.b64encode(cipher_enc.encrypt(pad(pt.encode(), 16))).decode()

    if CURRENT_FH_PORT:
        ports_to_try = [CURRENT_FH_PORT, 8080 if CURRENT_FH_PORT == 80 else 80]
    else:
        ports_to_try = [8080, 80]

    for p in ports_to_try:
        try:
            conn = http.client.HTTPConnection(GATEWAY_IP, p, timeout=timeout)
            conn.request("POST", "/fh_tool/api", body=encrypted_payload, headers={"Content-Type": "text/plain"})
            resp = conn.getresponse()
            res_body = resp.read().decode('utf-8')
            conn.close()

            cipher_dec = AES.new(key, AES.MODE_CBC, iv)
            decrypted = unpad(cipher_dec.decrypt(base64.b64decode(res_body)), 16).decode('utf-8')
            res_json = json.loads(decrypted)
            CURRENT_FH_PORT = p
            return res_json, None
        except Exception:
            pass
    return None, "接口请求或解密失败"

def connect_and_escalate():
    global CURRENT_FH_PORT
    Logger.info("正在连接设备...")
    pc_ip = get_local_ip(GATEWAY_IP)
    mac = get_mac_from_arp(GATEWAY_IP)
    if not mac:
        Logger.error("获取 MAC 失败，未检测到光猫连接。")
        return None, None
    mac_suffix = mac[-6:]

    if CURRENT_FH_PORT is None:
        send_fh_tool_request("GetDevInfo", timeout=1.5)
        if CURRENT_FH_PORT is None:
            Logger.error("接口验证失败，请确认是否连接了正确的光猫。")
            return None, None

    if CURRENT_FH_PORT == 80:
        tn_user, tn_pwd, su_pwd = "admin", f"Fh@{mac_suffix}", f"hg2x0{mac_suffix}"
    else:
        tn_user, tn_pwd, su_pwd = "telnetadmin", f"FH-nE7jA%5m{mac_suffix}", f"Fh@{mac_suffix}"

    try:
        tn = MiniTelnet(GATEWAY_IP, 23, timeout=5)
        time.sleep(0.5)
        tn.read_all(wait_time=0.5)
        tn.write(tn_user.encode('ascii') + b"\n")
        time.sleep(1)
        tn.read_all(wait_time=0.5)
        tn.write(tn_pwd.encode('ascii') + b"\n")
        login_success = False
        for _ in range(10):
            time.sleep(0.5)
            chunk = tn.read_all(wait_time=0.2).decode('ascii', errors='ignore')
            if "login incorrect" in chunk.lower() or "login failed" in chunk.lower():
                Logger.error("Telnet 登录失败！账号或密码错误。")
                tn.close()
                return None, None
            if "#" in chunk or ">" in chunk or "$" in chunk:
                login_success = True
                break
        if not login_success:
            Logger.error("Telnet 登录超时，未识别到系统提示符！")
            tn.close()
            return None, None
        Logger.success("Telnet 登录成功！")
        tn.write(b"su\n")
        time.sleep(0.5)
        tn.write(su_pwd.encode('ascii') + b"\n")
        time.sleep(0.5)
        tn.write(b"id\n")
        res = ""
        for _ in range(6):
            time.sleep(0.5)
            chunk = tn.read_all(wait_time=0.2).decode('ascii', errors='ignore')
            res += chunk
            if "uid=0" in res:
                Logger.success("提权成功！已获得 Root 权限。")
                return tn, pc_ip
            if "su: incorrect" in res.lower() or "su: not found" in res.lower() or "authentication failure" in res.lower():
                Logger.error(f"提权失败！SU 密码错误或命令被禁用。")
                tn.close()
                return None, None
        Logger.error(f"提权超时，未检测到 Root 权限！回显: {res.strip()}")
        tn.close()
        return None, None       
    except Exception as e:
        Logger.error(f"Telnet 连接异常: {e}，请按 [R] 刷新状态后重试。")
        return None, None

# ================= Telnet 自动开启与合并入口 =================
def ensure_telnet():
    global CURRENT_FH_PORT
    try:
        s = socket.create_connection((GATEWAY_IP, 23), timeout=2)
        s.close()
        return True
    except Exception:
        pass

    Logger.info("正在验证目标设备身份...")
    res, err = send_fh_tool_request("GetDevInfo", timeout=3.0)
    
    if not res or err:
        Logger.error(f"光猫接口无响应 ({GATEWAY_IP})！")
        Logger.warning("原因：1. 网线未插或 IP 未配置 (按 [1] 修复)  2. 固件升级导致后门接口被封禁")
        return False

    Logger.info("检测到光猫在线，但 Telnet 未开启，正在尝试自动开启...")
    _, err = send_fh_tool_request("TelnetEnable", {"telnet": "1"}, timeout=2)
    
    if err:
        Logger.error(f"自动开启 Telnet 指令下发失败: {err}")
        return False

    time.sleep(1.5)
    try:
        s = socket.create_connection((GATEWAY_IP, 23), timeout=3)
        s.close()
    except Exception:
        Logger.error("Telnet 开启指令已发送，但 23 端口仍未开放。")
        return False

    Logger.success("Telnet 已自动开启，继续执行。")
    return True

def connect_telnet():
    if not ensure_telnet():
        return None, None
    return connect_and_escalate()

# ================= Stock2UBI 部署（先探测 3333） =================
def _deploy_stock2ubi(tn, pc_ip):
    Logger.info("正在清理光猫端残留进程与文件...")
    tn.write(f"killall {UBI_TOOL_NAME} 2>/dev/null\n".encode('ascii'))
    time.sleep(1)
    tn.write(f"rm -f /tmp/{UBI_TOOL_NAME}\n".encode('ascii'))
    time.sleep(0.5)
    tn.read_all(wait_time=0.5)

    Logger.info(f"准备通过 HTTP 上传 {UBI_TOOL_NAME} 到光猫 /tmp 目录...")
    server, port, done_event, result = start_temp_http(UBI_TOOL_NAME, pc_ip)
    if not server:
        return False
    try:
        tn.write(
            f"cd /tmp && wget -T 30 -O {UBI_TOOL_NAME} "
            f"http://{pc_ip}:{port}/{UBI_TOOL_NAME}\n".encode('ascii')
        )
        if not done_event.wait(timeout=60) or not result["success"]:
            Logger.error("文件上传至光猫失败！")
            return False
    finally:
        server.shutdown()
        server.server_close()
    print()

    Logger.success("文件上传成功！")
    time.sleep(1)
    tn.read_all(wait_time=0.5)
    Logger.info("赋予执行权限...")
    tn.write(f"chmod +x /tmp/{UBI_TOOL_NAME}\n".encode('ascii'))
    time.sleep(1)
    tn.read_all(wait_time=0.5)

    Logger.info("后台静默运行 Stock2UBI...")
    tn.write(f"nohup /tmp/{UBI_TOOL_NAME} > /tmp/ubi.log 2>&1 &\n".encode('ascii'))
    time.sleep(3)
    tn.read_all(wait_time=0.5)
    tn.write(b"cat /tmp/ubi.log\n")
    res = tn.read_all(wait_time=1.0).decode('ascii', errors='ignore')

    clean_res = res.replace("cat /tmp/ubi.log", "").replace("\r", "").strip()
    if clean_res.endswith("#"): clean_res = clean_res[:-1].strip()

    print()
    line("─", 76, "cyan")
    print(paint("  [!] 光猫端日志输出：", "bold", "cyan"))
    print(f"  {clean_res}")
    line("─", 76, "cyan")

    if "listening on" in res:
        return True
    if "expected 5.4.55" in clean_res:
        print(paint("\n  [!] 检测到内核不是 5.4.55，工具拒绝运行。", "yellow", "bold"))
        ask = input(paint("  >> 是否强制添加参数重试？(y/n): ", "cyan")).strip().lower()
        if ask == 'y':
            Logger.info("正在清理旧进程并强制重试...")
            tn.write(f"killall {UBI_TOOL_NAME} 2>/dev/null\n".encode('ascii'))
            time.sleep(0.5)
            tn.write(f"nohup /tmp/{UBI_TOOL_NAME} --ignore-kernel-version > /tmp/ubi.log 2>&1 &\n".encode('ascii'))
            time.sleep(3)
            tn.read_all(wait_time=0.5)
            tn.write(b"cat /tmp/ubi.log\n")
            res_retry = tn.read_all(wait_time=1.0).decode('ascii', errors='ignore')
            clean_retry = res_retry.replace("cat /tmp/ubi.log", "").replace("\r", "").strip()
            if clean_retry.endswith("#"): clean_retry = clean_retry[:-1].strip()    
            print()
            print(paint("  [!] 强制运行后日志：", "bold", "yellow"))
            print(f"  {clean_retry}")
            return "listening on" in res_retry
        else:
            Logger.info("已取消强制重试。")
    return False

def is_stock2ubi_running():
    try:
        s = socket.create_connection((GATEWAY_IP, 3333), timeout=2)
        s.close()
        return True
    except Exception:
        return False

def ensure_stock2ubi(tn, pc_ip):
    if is_stock2ubi_running():
        Logger.success("Stock2UBI 服务已在运行，跳过部署。")
        return True
    Logger.info("检测到 Stock2UBI 未运行，正在自动部署...")
    return _deploy_stock2ubi(tn, pc_ip)

# ================= 核心业务：基础操作 =================
def run_enable_telnet():
    global CURRENT_FH_PORT
    Logger.info("正在探测光猫信息...")
    mac = get_mac_from_arp(GATEWAY_IP)
    if not mac:
        Logger.error("获取 MAC 失败。")
        return
    mac_suffix = mac[-6:]

    Logger.info(f"正在发送开启指令 (MAC: {mac})...")
    res_data, err = send_fh_tool_request("TelnetEnable", {"telnet": "1"}, timeout=2)
    if err:
        Logger.error(f"请求失败: {err}")
        return

    time.sleep(1)
    try:
        s = socket.create_connection((GATEWAY_IP, 23), timeout=3)
        Logger.success("TCP/23 端口已开放！")
        s.close()
    except Exception:
        Logger.warning("无法连接 23 端口，请稍等几秒后重试。")
    print()
    line("═", 76, "cyan")
    print(paint("  [!] 开启成功，请保存以下登录信息：", "bold", "cyan"))
    if CURRENT_FH_PORT == 80:
        print(f"    【版 本】 : 联通版")
        print(f"    【账 号】 : {paint('admin', 'white')}")
        print(f"    【密 码】 : {paint(f'Fh@{mac_suffix}', 'white')}")
        print(f"    【提 权】 : {paint('su', 'yellow')}")
        print(f"    【密 码】 : {paint(f'hg2x0{mac_suffix}', 'white')}")
    else:
        print(f"    【版 本】 : 电信版")
        print(f"    【账 号】 : {paint('telnetadmin', 'white')}")
        print(f"    【密 码】 : {paint(f'FH-nE7jA%5m{mac_suffix}', 'white')}")
        print(f"    【提 权】 : {paint('su', 'yellow')}")
        print(f"    【密 码】 : {paint(f'Fh@{mac_suffix}', 'white')}")
    line("═", 76, "cyan")

def run_disable_telnet():
    Logger.info("正在发送关闭 Telnet 指令...")
    res_data, err = send_fh_tool_request("TelnetEnable", {"telnet": "0"})
    if err: Logger.error(f"请求失败: {err}"); return
    time.sleep(1)
    try:
        s = socket.create_connection((GATEWAY_IP, 23), timeout=2)
        Logger.warning("23 端口依然开放，可能关闭失败。")
        s.close()
    except Exception:
        Logger.success("Telnet 已成功关闭，23 端口已关闭。")

def run_get_devinfo():
    Logger.info("正在获取设备信息...")
    res_data, err = send_fh_tool_request("GetDevInfo")
    if err: Logger.error(f"获取失败: {err}"); return
    if res_data:
        print()
        line("─", 76, "cyan")
        print(paint("  DEVICE INFORMATION", "bold", "cyan"))
        line("─", 76, "cyan")
        for k, v in res_data.items():
            if k not in ["index", "result"]:
                print(f"  {paint(k, 'gray'):<22} {paint(v, 'white', 'bold')}")
        line("─", 76, "cyan")
        Logger.success("设备信息读取完成")

def run_reboot():
    print()
    print(paint("  [!] 重启设备会断开当前所有网络连接！", "yellow", "bold"))
    if input(paint("  是否确认重启光猫？(y/n): ", "cyan")).strip().lower() != 'y': return
    Logger.info("正在发送重启指令...")
    res, err = send_fh_tool_request("DeviceReboot")
    if err:
        Logger.error(f"重启指令发送失败: {err}")
    elif res.get("result") == 0:
        Logger.success("重启指令已确认，光猫正在重启，网络将断开约 2 分钟。")
    else:
        Logger.error(f"设备拒绝重启，result={res.get('result')}")

def run_restore_factory():
    print()
    line("═", 76, "yellow")
    print(paint("  [!] 危险操作：恢复出厂设置将清空所有配置！", "yellow"))
    line("═", 76, "yellow")
    if input(paint("  确认恢复出厂设置吗？(输入 yes 确认): ", "cyan")).strip().lower() != 'yes': return
    Logger.info("正在发送恢复出厂指令...")
    res, err = send_fh_tool_request("RestoreDefaultSettings")
    if err:
        Logger.error(f"恢复出厂失败: {err}")
    elif res.get("result") == 0:
        Logger.success("恢复出厂指令已确认，设备正在清空配置并重启，请稍等 2 分钟。")
    else:
        Logger.error(f"设备拒绝，result={res.get('result')}")

# ================= 核心业务：账号与日志 =================
def run_get_admin():
    Logger.info("正在获取管理员账号...")
    res_data, err = send_fh_tool_request("GetAdminAccount")
    if err: Logger.error(f"获取失败: {err}"); return
    if res_data:
        print()
        line("─", 76, "cyan")
        print(paint("  ADMIN ACCOUNT", "bold", "cyan"))
        line("─", 76, "cyan")
        for k, v in res_data.items():
            if k not in ["index", "result"]:
                print(f"  {paint(k, 'gray'):<22} {paint(v, 'white', 'bold')}")
        line("─", 76, "cyan")

def run_get_pppoe():
    Logger.info("正在获取 PPPoE 账号...")
    res_data, err = send_fh_tool_request("GetPppoeAccount")
    if err: Logger.error(f"获取失败: {err}"); return
    if res_data:
        print()
        line("─", 76, "cyan")
        print(paint("  PPPOE ACCOUNT", "bold", "cyan"))
        line("─", 76, "cyan")
        for k, v in res_data.items():
            if k not in ["index", "result"]:
                print(f"  {paint(k, 'gray'):<22} {paint(v, 'white', 'bold')}")
        line("─", 76, "cyan")

def run_get_reg():
    Logger.info("正在获取注册信息...")
    res, err = send_fh_tool_request("GetRegAccount")
    if not err and res:
        regname = res.get("regname", "")
        regpwd = res.get("regpwd", "")
        print()
        print(f"  【注册账号】 : {paint(regname if regname else '(空)', 'white', 'bold')}")
        print(f"  【注册密码】 : {paint(regpwd if regpwd else '(空)', 'white', 'bold')}")
        Logger.success("注册信息读取完成")
    else:
        Logger.error(f"获取失败: {err}")

def run_set_reg():
    acc = input(paint("  >> 请输入新的注册账号 (直接回车即清空): ", "cyan")).strip()
    pwd = input(paint("  >> 请输入新的注册密码 (直接回车即清空): ", "cyan")).strip()
    payload = {"regname": acc, "regpwd": pwd}
    Logger.info("正在下发设置指令...")
    res, err = send_fh_tool_request("SetRegAccount", payload)
    if not err and res:
        if res.get("result") == 0:
            Logger.success("设置指令已发送！请按 [04] 重新获取验证。")
        else:
            Logger.warning(f"光猫返回异常: {json.dumps(res)}")
    else:
        Logger.error(f"设置失败: {err}")

def run_log_download():
    """菜单 [10]：光猫返回的是打包日志的下载地址，这里只给出完整下载链接。"""
    Logger.info("正在请求下载系统日志...")
    res_data, err = send_fh_tool_request("LogDownload")
    if err:
        Logger.error(f"请求失败: {err}")
        return
    if not isinstance(res_data, dict):
        Logger.error("光猫未返回有效内容。")
        return
    # 固件返回字段名拼写不一（Dowloadurl / Downloadurl / DownloadUrl / url），逐个兼容
    raw = ""
    for k, v in res_data.items():
        if "url" in str(k).lower() and isinstance(v, str) and v.strip():
            raw = v.strip()
            break
    if not raw:
        Logger.error("返回内容中没有下载地址。")
        print(paint("  " + json.dumps(res_data, ensure_ascii=False), "gray"))
        return
    full = raw if raw.startswith(("http://", "https://")) else \
        f"http://{GATEWAY_IP}/{raw.lstrip('/')}"
    print()
    print(paint("  【日志下载链接】", "white", "bold"))
    print(paint(f"  {full}", "cyan", "bold"))
    print(paint("  粘贴到浏览器即可下载光猫系统日志（.tar.gz）。", "gray"))
    Logger.success("已获取日志下载链接。")

# ================= 核心业务：刷机文件准备 =================
def calculate_sha256(filepath):
    sha256_hash = hashlib.sha256()
    try:
        with open(filepath, "rb") as f:
            for byte_block in iter(lambda: f.read(4096 * 1024), b""):
                sha256_hash.update(byte_block)
        return sha256_hash.hexdigest()
    except Exception:
        return None

def get_official_hashes():
    hashes = {}
    if os.path.exists(HASH_CACHE_FILE):
        try:
            with open(HASH_CACHE_FILE, "r", encoding="utf-8") as f:
                cached_data = json.load(f)
                if cached_data:
                    return cached_data
        except Exception:
            pass
    Logger.info("正在从 GitHub API 获取官方 SHA256 校验表...")
    ssl_context = ssl._create_unverified_context()

    for repo, tag in HASH_REPOS:
        if tag:
            api_url = f"https://api.github.com/repos/{repo}/releases/tags/{tag}"
        else:
            api_url = f"https://api.github.com/repos/{repo}/releases/latest"
        for proxy in _ordered_proxies():
            target_url = f"{proxy}{api_url}"
            try:
                req = urllib.request.Request(target_url, headers={
                    'User-Agent': 'Mozilla/5.0', 'Accept': 'application/vnd.github.v3+json'
                })
                with urllib.request.urlopen(req, timeout=5, context=ssl_context) as response:
                    data = json.loads(response.read().decode('utf-8'))
                    for asset in data.get("assets", []):
                        digest = asset.get("digest", "")
                        if digest and digest.startswith("sha256:"):
                            hashes[asset["name"]] = digest.split(":")[1].lower()
                    _mark_proxy_ok(proxy)
                    break  # 成功查到就换下一个仓库，静默跳过报错
            except Exception:
                continue
    if hashes:
        hashes = {k: v for k, v in hashes.items() if any(x in k for x in ["hg5585f", "hg5382a", "stock2ubi", "fiberhome-factory"])}
        try:
            with open(HASH_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(hashes, f, indent=4, ensure_ascii=False)
        except Exception:
            pass
    return hashes

def smart_download(filename, url, expected_hash=None, subdir=""):
    target_dir = os.path.join(BASE_DIR, subdir) if subdir else BASE_DIR
    filepath = os.path.join(target_dir, filename)
    ssl_context = ssl._create_unverified_context()
    for proxy in _ordered_proxies():
        target_url = f"{proxy}{url}"
        domain = target_url.split('/')[2] if target_url.startswith("http") else "直连"
        Logger.info(f"尝试从 {paint(domain, 'cyan')} 下载 {filename}...")
        try:
            req = urllib.request.Request(target_url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=15, context=ssl_context) as response:
                if subdir:
                    os.makedirs(target_dir, exist_ok=True)
                total_size = int(response.headers.get('Content-Length', 0))
                current_size = 0
                with open(filepath, "wb") as f:
                    while True:
                        chunk = response.read(8192)
                        if not chunk: break
                        f.write(chunk)
                        current_size += len(chunk)
                        if total_size > 0:
                            percent = min(1.0, current_size / total_size)
                            filled = int(percent * 28)
                            bar = paint("█" * filled, "cyan") + paint("░" * (28 - filled), "gray")
                            sys.stdout.write(f"\r  {'文件下载':<16} {bar} {int(percent * 100):3d}%\033[K")
                            sys.stdout.flush()
            print()
            if expected_hash:
                local_hash = calculate_sha256(filepath)
                if local_hash == expected_hash:
                    Logger.success(f"{filename} 下载完成，SHA256 校验通过！")
                else:
                    Logger.error(f"{filename} 校验失败！文件可能已损坏。")
                    os.remove(filepath)
                    return False
            else:
                Logger.success(f"{filename} 下载完成 (无官方哈希)。")
            return True
        except Exception:
            print()
            Logger.warning(f"从 {domain} 下载失败，切换节点...")
            if os.path.exists(filepath): os.remove(filepath)
    Logger.error(f"{filename} 所有节点下载均失败！")
    return False

def run_env_check():
    clear()
    print()
    print(paint("  DOWNLOAD DEPENDENCIES", "bold", "cyan"))
    line("═", 76, "cyan")
    print(paint("  请使用【鼠标点击】或【空格键】勾选需要的型号：", "white"))
    print()

    options = [m["name"] for m in MODEL_DATA]
    selected_indices = interactive_checkbox(options)
    if not selected_indices:
        Logger.warning("未勾选任何型号，已取消。")
        return

    selected_models = [MODEL_DATA[i] for i in selected_indices]
    Logger.info(f"已选择 {len(selected_models)} 个机型，准备拉取...")

    official_hashes = get_official_hashes()
    
    print("\n  " + "─" * 76)
    print(paint("  [ 开始检查与下载所需文件 ]", "white", "bold"))
    print("  " + "─" * 76)

    files_to_download = []

    for f in TOOLS_FILES:
        files_to_download.append({"subdir": "tools", "name": f["name"], "url": f["url"]})

    for m in selected_models:
        for sfx in UBOOT_SUFFIXES:
            fn = f"{m['prefix']}-{sfx}"
            files_to_download.append({
                "subdir": f"{m['dir']}/uboot", "name": fn, "url": UBOOT_BASE + fn,
            })
        for sfx in FIX_UBOOT_SUFFIXES:
            fn = f"{m['prefix']}-{sfx}"
            files_to_download.append({
                "subdir": f"{m['dir']}/fix_uboot", "name": fn, "url": FIX_UBOOT_BASE + fn,
            })
        files_to_download.append({
            "subdir": f"{m['dir']}/firmware", "name": m["firmware"],
            "url": FIRMWARE_BASE + m["firmware"],
        })

    unique_files = {}
    for f in files_to_download:
        unique_files[(f["subdir"], f["name"])] = f
    unique_files = list(unique_files.values())

    success_count = 0
    for item in unique_files:
        subdir, filename = item["subdir"], item["name"]
        filepath = os.path.join(BASE_DIR, subdir, filename)
        if os.path.exists(filepath):
            expected = official_hashes.get(filename)
            if expected:
                local = calculate_sha256(filepath)
                if local == expected:
                    Logger.success(f"{filename} 已存在，校验通过。")
                    success_count += 1
                    continue
                else:
                    Logger.warning(f"{filename} 已存在但校验失败，重下。")
                    os.remove(filepath)
            else:
                Logger.success(f"{filename} 已存在。")
                success_count += 1
                continue
        if smart_download(filename, item["url"], official_hashes.get(filename), subdir=subdir):
            success_count += 1

    print("\n  " + "─" * 76)
    if success_count == len(unique_files):
        Logger.success(f"依赖下载完毕！所有 {len(unique_files)} 个文件均就绪。")
    else:
        Logger.warning(f"下载完成，但仍缺失 {len(unique_files) - success_count} 个文件。")

# ================= 核心业务：当前机型选择 =================
def ensure_model_selected():
    """如果未选择机型，自动弹出选机型界面。返回 True 表示已选择。"""
    if CURRENT_MODEL:
        return True
    Logger.info("当前未选择工作机型，请先选择：")
    time.sleep(0.5)
    run_select_model()
    if CURRENT_MODEL:
        return True
    # 直接回车取消时，run_select_model() 已打印过一次「未选择机型，已取消。」，
    # 这里不再重复告警（菜单 [6][7][8][9] 共用本入口，避免连弹两条一样的 WARN）。
    return False

def run_select_model():
    global CURRENT_MODEL
    clear()
    print()
    print(paint("  SELECT WORKING MODEL", "bold", "cyan"))
    line("═", 76, "cyan")
    print(paint("  请选择当前要操作的设备型号：", "white"))
    print()
    for idx, m in enumerate(MODEL_DATA):
        print(f"    {paint(f'[{idx + 1}]', 'cyan', 'bold')}  {paint(m['name'], 'white')}")
    print()
    line("─", 76)

    while True:
        choice = input(paint(f"  >> 请输入机型编号 (1-{len(MODEL_DATA)}，直接回车取消): ", "cyan")).strip()
        if choice == "":
            Logger.warning("未选择机型，已取消。")
            return
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(MODEL_DATA):
                CURRENT_MODEL = MODEL_DATA[idx]
                Logger.success(f"当前工作机型: {paint(CURRENT_MODEL['name'], 'white', 'bold')}")
                Logger.info(f"机型目录: {os.path.join(BASE_DIR, CURRENT_MODEL['dir'])}")
                return
        Logger.warning(f"无效编号: {choice}，请输入 1-{len(MODEL_DATA)}")

# ================= 核心业务：闪存信息识别 =================
def parse_flash_info(raw):
    """从 /proc/driver/spi_nand_debug 的原始输出中解析闪存信息。"""
    size_match = re.search(r'flash\s+size\s*=\s*\[(\d+)\]', raw, re.IGNORECASE)
    chip_match = re.search(r'SPI\s+NAND\s+Flash\s*=\s*([^\s,]+)', raw, re.IGNORECASE)
    mfr_match  = re.search(r'mfr_id\s*=\s*(0x[0-9a-fA-F]+)', raw, re.IGNORECASE)
    dev_match  = re.search(r'dev_id\s*=\s*(0x[0-9a-fA-F]+)', raw, re.IGNORECASE)

    mfr_id = int(mfr_match.group(1), 16) if mfr_match else None
    return {
        "chip": chip_match.group(1) if chip_match else "未知",
        "size_mb": int(size_match.group(1)) / 1024 / 1024 if size_match else None,
        "mfr_id": mfr_id,
        "dev_id": dev_match.group(1) if dev_match else "未知",
        "vendor": SPI_NAND_VENDORS.get(mfr_id, f"未知 (mfr_id=0x{mfr_id:02x})") if mfr_id is not None else "未知",
        "has_chip_or_size": bool(size_match or chip_match),
    }

def print_flash_info(info):
    """格式化打印闪存信息表格（与菜单 [4] 原输出完全一致）。"""
    size_mb = info["size_mb"]
    mfr_id = info["mfr_id"]
    size_str = f"{size_mb:.0f} MB" if size_mb is not None else None
    mfr_str = f"0x{mfr_id:02x}" if mfr_id is not None else "未知"

    print()
    line("─", 76, "cyan")
    print(paint("  NAND FLASH", "bold", "cyan"))
    line("─", 76, "cyan")
    print(f"  【芯片型号】 : {paint(info['chip'], 'white', 'bold')}")
    print(f"  【芯片厂商】 : {paint(info['vendor'], 'white', 'bold')}")
    if size_str is not None:
        print(f"  【闪存容量】 : {paint(size_str, 'white', 'bold')}")
    print(f"  【厂商 ID 】 : {paint(mfr_str, 'white')}")
    print(f"  【设备 ID 】 : {paint(info['dev_id'], 'white')}")
    line("─", 76, "cyan")

def run_flash_info():
    tn, pc_ip = connect_telnet()
    if not tn: return

    Logger.info("正在读取 NAND 闪存信息...")
    tn.write(b"cat /proc/driver/spi_nand_debug\n")
    time.sleep(1.0)
    raw = tn.read_all(wait_time=0.5).decode('ascii', errors='ignore')
    tn.close()
    raw = raw.replace("cat /proc/driver/spi_nand_debug", "").replace("\r", "")

    if "No such file" in raw or "not found" in raw:
        Logger.error("设备上不存在 /proc/driver/spi_nand_debug，可能不是 NAND 机型。")
        return

    info = parse_flash_info(raw)
    if not info["has_chip_or_size"]:
        Logger.error("未能解析出闪存信息，原始输出如下：")
        print(raw.strip())
        return

    print_flash_info(info)

    if chip_needs_fixboot(info):
        Logger.info(f"型号 {info['chip']} 需要临时过渡引导：第一阶段自动使用 fix_uboot 临时引导。")
    else:
        Logger.info(f"当前为 {info['vendor']} 闪存：第一阶段直接使用 uboot 正式引导。")
    Logger.success("闪存信息读取完成")

# ================= 刷机指引（手动） =================
def run_flash_guide(standalone=True):
    if standalone:
        print()
        line("═", 76, "cyan")
        print(paint("  刷机操作指南", "bold", "cyan"))
        line("═", 76, "cyan")

    print()
    line("═", 76, "red")
    print(paint("  [!] 刷机有风险：写错文件或刷写中断电，会直接变砖！", "red", "bold"))
    print(paint("  [!] 重要！重要！重要！刷写前务必先备份！", "red", "bold"))
    line("═", 76, "red")

    print(paint("  [推荐方案：程序全自动刷写]", "green", "bold"))
    print("   如果您不想手动折腾网页，请在主菜单直接按 [9] 原厂到PonWrt 一键刷机。")
    print("   程序会自动为您检测闪存、强制备份并匹配正确的文件刷入。")
    print()

    print("  [手动方案：第一阶段 - 刷入引导 (192.168.1.1:3333)]")
    print("   1. 在主菜单按 [5] 启动 Stock2UBI 服务并打开网页。")
    print("   2. 未备份请先点“极速备份”或下载分区，已备份直接点【继续】。")
    print(f"   3. {paint('核心警告：必须先按 [4] 确认您的闪存型号！', 'yellow', 'bold')}")
    print(f"      - 如果是【{paint('需要临时引导的型号', 'cyan')}】（W29N02KVSIAF、S34ML02H200TFI000、S34ML02G300TFI00）：")
    print(f"        请前往 {paint('fix_uboot', 'yellow', 'bold')} 文件夹，上传里面的 {paint('临时引导文件 (带 tmp 字样)', 'white')}！")
    print(f"      - 如果是【{paint('其他标准闪存', 'cyan')}】（铠侠/美光/旺宏等）：")
    print(f"        请前往 {paint('uboot', 'yellow', 'bold')} 文件夹，上传里面的 {paint('正式引导文件', 'white')}。")
    print("   4. 点击【刷入引导】，完成后设备会重启，请等待约 1 分钟。")
    print("   5. 后续进入 U-Boot 界面后，无论什么闪存，都用 uboot 目录下的正式引导文件。")
    print("   6. 用 fiberhome-factory 工具把备份出的 mtdN-factory.bin（factory 分区原始镜像）")
    print("      转换成 1MiB 的 factory-1m.bin；两者不是同一个文件。")
    print()
    print("  [手动方案：第二阶段 - U-Boot 恢复页 (192.168.0.1)]")
    print("   请确保电脑网卡有 192.168.0.x 的 IP，然后在该页面进行：")
    print("   1. 重建 UBI      点击按钮 (首次必点)")
    print("   2. 刷写 BL2      上传机型对应的 preloader.bin 并写入")
    print("   3. 刷写 U-Boot   上传机型对应的 bl31-uboot.fip 并写入")
    print("   4. 刷写系统固件  上传机型对应的 sysupgrade.itb 并写入")
    print("   5. 写入 factory  上传机型对应的 factory-1m.bin 并写入")
    print("   6. 启动系统      全部写完后，点击【启动系统】")
    print()
    print(paint("  [!] 异常排查：", "red"))
    print("   - 灯不亮进不去：U-Boot 可能写入失败，需使用 XMODEM 救砖。")
    print("   - 灯亮进不去：请勿插 2.5G 网口（无驱动），换普通千兆 LAN 口重试。")
    line("─", 76, "gray")

# ================= 核心业务：Stock2UBI 服务与备份 =================
def run_stock2ubi(silent=False):
    if is_stock2ubi_running():
        Logger.success("Stock2UBI 服务已在运行，跳过 Telnet 和部署。")
    else:
        tn, pc_ip = connect_telnet()
        if not tn: return
        if not ensure_stock2ubi(tn, pc_ip):
            Logger.error("Stock2UBI 部署失败，请检查上方日志。")
            tn.close()
            return
        tn.close()

    print()
    line("═", 76, "cyan")
    print(paint("  [!] 刷机环境准备就绪：", "cyan"))
    print(f"   1. 请访问: {paint('http://192.168.1.1:3333', 'cyan', 'bold')}")
    print("   2. 上传对应的引导文件。")
    print()
    if not silent:
        choice = input(paint("  是否现在为你自动打开页面？(y/n): ", "cyan")).strip().lower()
    else:
        choice = 'y'
    line("═", 76, "cyan")

    if choice == 'y':
        try:
            webbrowser.open("http://192.168.1.1:3333")
            Logger.success("已自动打开浏览器。")
        except Exception as e:
            Logger.warning(f"自动打开浏览器失败: {e}")
    else:
        Logger.info("请自行复制上方地址到浏览器中打开。")

    Logger.info("提示：如需查看详细的刷机步骤，请返回主菜单按 [G] 查看刷机指引。")

def save_device_info(tn, mac):
    """获取系统接口与底层信息，并写入 deviceinfo_MAC.txt"""
    if not mac: mac = "UNKNOWN_MAC"
    model_name = CURRENT_MODEL["name"] if CURRENT_MODEL else "未知机型"
    
    Logger.info("正在收集设备全量信息 (接口+底层)...")
    
    # 1. 获取系统信息
    fh_info_str = "获取失败"
    try:
        res_data, err = send_fh_tool_request("GetDevInfo", timeout=2)
        if res_data and not err:
            filtered_info = {k: v for k, v in res_data.items() if k not in ["index", "result"]}
            fh_info_str = json.dumps(filtered_info, indent=4, ensure_ascii=False)
    except Exception as e:
        fh_info_str = f"接口异常: {e}"

    # 2. 获取分区表
    tn.write(b"cat /proc/mtd\n")
    time.sleep(0.8)
    mtd_raw = tn.read_all(wait_time=0.5).decode('ascii', errors='ignore')
    mtd_raw = mtd_raw.replace("cat /proc/mtd", "").replace("\r", "").strip()
    if mtd_raw.endswith("#"): mtd_raw = mtd_raw[:-1].strip()

    # 3. 获取闪存信息
    tn.write(b"cat /proc/driver/spi_nand_debug\n")
    time.sleep(0.8)
    flash_raw = tn.read_all(wait_time=0.5).decode('ascii', errors='ignore')
    flash_raw = flash_raw.replace("cat /proc/driver/spi_nand_debug", "").replace("\r", "").strip()
    if flash_raw.endswith("#"): flash_raw = flash_raw[:-1].strip()

    # 4. 组装并写入
    content = (
        f"==================================================\n"
        f"设备全量信息归档 (Device Info)\n"
        f"==================================================\n"
        f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"工作机型: {model_name}\n"
        f"光猫 MAC: {mac}\n"
        f"==================================================\n\n"
        f"【一、系统信息 (GetDevInfo 接口返回)】\n{fh_info_str}\n\n"
        f"【二、底层分区表 (/proc/mtd)】\n{mtd_raw}\n\n"
        f"【三、闪存芯片信息 (/proc/driver/spi_nand_debug)】\n{flash_raw}\n"
    )

    # 存放路径：机型目录/info/deviceinfo_MAC.txt
    info_dir = os.path.join(BASE_DIR, CURRENT_MODEL["dir"] if CURRENT_MODEL else "", "info")
    os.makedirs(info_dir, exist_ok=True)
    filepath = os.path.join(info_dir, f"deviceinfo_{mac}.txt")
    
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
        Logger.success(f"设备信息已归档: {filepath}")
    except Exception as e:
        Logger.error(f"归档失败: {e}")

# ================= 备份策略：范围选择 / 宽容分区 / 超时选择 =================
TOLERANT_PARTITIONS = {"apps", "userdata"}   # 仅这两个分区失败时交用户判断
BACKUP_SCOPE = "all"                         # all = 全分区, factory = 仅 factory

def _is_tolerant(name):
    return name.strip().lower() in TOLERANT_PARTITIONS

def timed_choice(prompt, valid=("1", "2", "3"), timeout=30, default="3", action="中止"):
    """带倒计时的单键选择；超时无人选择则返回 default。action 为超时后的动作描述"""
    deadline = time.time() + timeout
    got = None
    last_left = None
    sys.stdout.write(prompt)
    sys.stdout.flush()

    if os.name == "nt":
        import msvcrt
        while time.time() < deadline:
            left = int(deadline - time.time()) + 1
            if left != last_left:
                sys.stdout.write(f"\r{prompt}{paint(f'〔{left:2d}s 后自动执行 {default}〕', 'yellow')}\033[K")
                sys.stdout.flush()
                last_left = left
            if msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch in valid:
                    got = ch
                    break
            time.sleep(0.05)
    else:
        import select
        try:
            fd = sys.stdin.fileno()
        except Exception:
            fd = None
        while fd is not None and time.time() < deadline:
            left = int(deadline - time.time()) + 1
            if left != last_left:
                sys.stdout.write(f"\r{prompt}{paint(f'〔{left:2d}s 后自动执行 {default}〕', 'yellow')}\033[K")
                sys.stdout.flush()
                last_left = left
            try:
                r, _, _ = select.select([fd], [], [], 0.15)
            except Exception:
                break
            if r:
                ch = sys.stdin.read(1)
                if not ch:
                    break
                if ch in valid:
                    got = ch
                    break

    sys.stdout.write("\r\033[K")
    if got is None:
        print(f"{prompt}{paint(f'超时未选择 → 自动执行 [{default}] {action}', 'yellow', 'bold')}")
        return default
    print(f"{prompt}{paint(got, 'white', 'bold')}")
    return got

def ask_backup_scope(silent=False):
    """前置：备份范围选择，5 秒无操作默认全分区备份。
    silent=True 时直接返回已选范围，不再提问。选完即视为确认，不再追加二次询问。"""
    global BACKUP_SCOPE
    if silent:
        return BACKUP_SCOPE
    opts = [
        ("1", "全分区备份", "逐分区下载 + MD5 校验，最稳（推荐）"),
        ("2", "只备份 factory 分区", "单分区下载 + MD5 校验，刷机够用"),
    ]
    name_w = max(_vwidth(n) for _, n, _ in opts)
    print()
    line("─", 76, "cyan")
    print(paint("  [ 备份范围 ]", "bold", "cyan"))
    for key, name, desc in opts:
        print(f"    {paint(f'[{key}]', 'cyan', 'bold')} {_padv(name, name_w)}   {desc}")
    line("─", 76, "cyan")
    ch = timed_choice("  >> 请选择: ", ("1", "2"), timeout=5, default="1", action="按全分区备份开始")
    BACKUP_SCOPE = "factory" if ch == "2" else "all"
    Logger.info(f"本次备份范围：{paint('全分区' if BACKUP_SCOPE == 'all' else '仅 factory 分区', 'white', 'bold')}")
    return BACKUP_SCOPE

def _ask_tolerant_choice(dev, name, reason, flash=False):
    """宽容分区（apps / userdata）失败后的三选：
    菜单 6（纯备份）：10 秒无操作默认保留文件继续备份；
    菜单 7/9（刷机流程）：30 秒无操作默认中止刷机。"""
    act = "刷机" if flash else "备份"
    timeout = 30 if flash else 10
    default = "3" if flash else "1"
    default_desc = f"中止{act}" if flash else f"保留文件继续{act}"
    print()
    line("─", 76, "yellow")
    print(paint(f"  [!] 分区备份失败：{dev} ({name})", "yellow", "bold"))
    print(f"      原因：{paint(reason or '下载中断', 'white')}")
    print(paint("      提示：apps / userdata 属动态数据分区，备份期间数据可能仍在写入，MD5 不符不一定是备份出错；", "gray"))
    print(paint("            可尝试重新备份，即使保留该文件也不影响后续刷机。", "gray"))
    print(f"    {paint('[1]', 'cyan', 'bold')} 保留文件继续{act}")
    print(f"    {paint('[2]', 'cyan', 'bold')} 重新下载一次")
    print(f"    {paint('[3]', 'cyan', 'bold')} 中止{act}")
    print(paint(f"      {timeout} 秒内未选择，将自动{default_desc}。", "yellow"))
    line("─", 76, "yellow")
    return timed_choice("  >> 请选择: ", ("1", "2", "3"), timeout=timeout, default=default, action=default_desc)

def _download_partition(tn, p, backup_dir):
    """下载并校验单个分区。返回 (是否成功, 失败原因, 本地文件路径)"""
    dev, idx, name, size_mb = p["dev"], p["index"], p["name"], p["size_mb"]
    filename = f"{dev}-{name}.bin"
    local_filepath = os.path.join(backup_dir, filename)
    if os.path.exists(local_filepath):
        try: os.remove(local_filepath)
        except Exception: pass

    md5_hash = hashlib.md5()
    total_read = 0
    total_expected = int(size_mb * 1024 * 1024)
    start_time = time.time()

    try:
        conn = http.client.HTTPConnection(GATEWAY_IP, 3333, timeout=30)
        conn.request("GET", f"/backup/{idx}")
        resp = conn.getresponse()
        if resp.status != 200:
            conn.close()
            return False, f"HTTP 状态码异常 {resp.status}", local_filepath

        with open(local_filepath, "wb") as f:
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk: break
                f.write(chunk)
                md5_hash.update(chunk)
                total_read += len(chunk)
                elapsed = time.time() - start_time
                speed = total_read / elapsed if elapsed > 0 else 0
                draw_progress("HTTP 下载", total_read, total_expected, speed)
        conn.close()
        print()
        local_md5 = md5_hash.hexdigest()
        Logger.success(f"{filename} 接收完毕 ({total_read / 1024 / 1024:.2f} MB)")
    except Exception as e:
        print()
        return False, f"下载中断: {e}", local_filepath

    tn.read_all(wait_time=0.2)
    Logger.info(f"正在校验 {dev} 的 MD5...")
    tn.write(f"dd if=/dev/{dev} bs=64k 2>/dev/null | md5sum\n".encode('ascii'))

    dev_md5 = "unknown"
    t0 = time.time()
    buf = ""
    while time.time() - t0 < 60:
        buf += tn.read_all(wait_time=0.5).decode('ascii', errors='ignore')
        m = re.search(r'\b([a-fA-F0-9]{32})\b', buf)
        if m:
            dev_md5 = m.group(1).lower()
            break

    if dev_md5 != "unknown" and dev_md5 == local_md5:
        Logger.success(f"{filename} MD5 校验通过！")
        return True, "", local_filepath
    return False, f"MD5 不符（设备 {dev_md5} / 本地 {local_md5}）", local_filepath

def run_web_backup(silent=False, tn=None, pc_ip=None, scope=None):
    global CURRENT_FACTORY_BIN
    if not ensure_model_selected():
        return False

    standalone = False
    if not tn:
        tn, pc_ip = connect_telnet()
        if not tn: return False
        standalone = True

    if not ensure_stock2ubi(tn, pc_ip):
        Logger.error("Stock2UBI 部署失败，无法执行备份。")
        if standalone: tn.close()
        return False

    mac = get_mac_from_arp(GATEWAY_IP)
    if not mac:
        Logger.error("无法获取 MAC，备份中止。")
        if standalone: tn.close()
        return False
    save_device_info(tn, mac)

    backup_dir = os.path.join(BASE_DIR, CURRENT_MODEL["dir"], "backup_mtd", mac)
    os.makedirs(backup_dir, exist_ok=True)
    Logger.info(f"备份目录: {backup_dir}")

    # ---------- 1. 读分区表：原样内容已由设备信息归档保存，此处不再重复整表打印 ----------
    Logger.info("正在读取底层分区表...")
    tn.write(b"cat /proc/mtd\n")
    mtd_out = tn.read_all(wait_time=1.0).decode('ascii', errors='ignore')

    pattern = r'(mtd\d+):\s+([0-9a-fA-F]+)\s+[0-9a-fA-F]+\s+"(.*?)"'
    raw_partitions = re.findall(pattern, mtd_out)
    if not raw_partitions:
        Logger.error("未找到任何分区信息。")
        if standalone: tn.close()
        return False

    partitions = []
    total_size_bytes = 0
    for dev, size_hex, name in raw_partitions:
        size_int = int(size_hex, 16)
        total_size_bytes += size_int
        partitions.append({
            "dev": dev, "index": int(dev.replace("mtd", "")),
            "name": name, "size_mb": size_int / 1024 / 1024
        })

    # ---------- 2. 分区清单在上，备份范围询问在下，选完直接开始 ----------
    print()
    print(paint("  STOCK2UBI PARTITION BACKUP", "bold", "white"))
    line("─", 76)
    print(f"  共发现 {paint(len(partitions), 'cyan')} 个分区 "
          f"(总大小约 {paint(f'{total_size_bytes / 1024 / 1024:.2f}', 'cyan')} MB)：")

    if silent:
        # 一键流程（菜单 7 / 9）：范围已在外部选定，这里标注 [备份]/[跳过] 供确认
        scope = scope or ask_backup_scope(True)
        if scope == "factory":
            targets = [p for p in partitions if p["name"].strip().lower() == "factory"]
            if not targets:
                Logger.error("分区表中没有 factory 分区，无法只备份 factory。")
                if standalone: tn.close()
                return False
        else:
            targets = list(partitions)
        target_devs = {p["dev"] for p in targets}
        for p in partitions:
            mark = paint("[备份]", "green", "bold") if p["dev"] in target_devs else paint("[跳过]", "gray")
            print(f"    {mark} {p['dev']:<6} {paint(_padv(p['name'], 14), 'cyan')} {p['size_mb']:>6.2f} MB")
    else:
        # 手动流程：尚未选择，清单不带标记，选择项放在清单下方
        for p in partitions:
            print(f"    {p['dev']:<6} {paint(_padv(p['name'], 14), 'cyan')} {p['size_mb']:>6.2f} MB")

    line("─", 76)

    if not silent:
        scope = ask_backup_scope(False)
        if scope == "factory":
            targets = [p for p in partitions if p["name"].strip().lower() == "factory"]
            if not targets:
                Logger.error("分区表中没有 factory 分区，无法只备份 factory。")
                if standalone: tn.close()
                return False
        else:
            targets = list(partitions)
        names = ", ".join(f"{p['dev']}({p['name']})" for p in targets)
        Logger.info(f"本次备份 {len(targets)} 个分区：{names}")
    print()

    # ---------- 3. 逐分区备份 ----------
    success_count = 0
    unverified = []
    aborted = False

    for p in targets:
        dev, name = p["dev"], p["name"]
        attempt = 0
        while True:
            attempt += 1
            suffix = "  [重试]" if attempt > 1 else ""
            Logger.info(f"开始处理: {dev} ({name}) {p['size_mb']:.2f}MB{suffix}")
            ok, reason, path = _download_partition(tn, p, backup_dir)
            if ok:
                success_count += 1
                break

            Logger.error(f"{dev} ({name}) 备份失败：{reason}")

            # 宽容分区：交用户判断，30 秒无操作默认中止（菜单 6 说"备份"，7/9 说"刷机"）
            if _is_tolerant(name):
                ch = _ask_tolerant_choice(dev, name, reason, flash=not standalone)
                if ch == "1":
                    if os.path.exists(path):
                        Logger.warning(f"{os.path.basename(path)} 保留为未校验文件，"
                                       f"继续{'刷机' if not standalone else '备份'}流程。")
                        unverified.append(f"{dev}-{name}")
                        success_count += 1
                        break
                    Logger.error("本地文件不存在，无法保留 → 中止。")
                    aborted = True
                    break
                if ch == "2":
                    if attempt >= 3:
                        Logger.error("已重试 3 次仍失败 → 中止。")
                        aborted = True
                        break
                    continue
                Logger.warning(f"已选择中止{'刷机' if not standalone else '备份'}。")
                aborted = True
                break

            # 非宽容分区：一刀切，立即终止（菜单 6 只终止备份，7/9 终止刷机）
            print()
            line("═", 76, "red")
            if standalone:
                print(paint(f"  [!] 关键分区 {dev} ({name}) 备份失败 → 立即终止备份！", "red", "bold"))
                print(paint("      请检查网线 / 防火墙后重新执行本功能。", "yellow"))
            else:
                print(paint(f"  [!] 关键分区 {dev} ({name}) 备份失败 → 立即终止刷机！", "red", "bold"))
                print(paint("      未备份完成就刷机极易变砖，请检查网线/防火墙后重新执行本功能。", "yellow"))
            line("═", 76, "red")
            aborted = True
            break

        if aborted:
            break

    # ---------- 4. 结果汇总 ----------
    print()
    line("─", 76, "cyan")
    if aborted:
        Logger.error(f"备份中止：已完成 {success_count}/{len(targets)} 个分区。")
    else:
        msg = f"备份操作完成：{success_count}/{len(targets)} 个分区"
        if unverified:
            msg += f"（{len(unverified)} 个未通过 MD5 但已保留：{'、'.join(unverified)}）"
        Logger.success(msg)
        if unverified:
            Logger.warning("上述未校验分区属动态数据区（apps / userdata）：备份期间数据仍在写入时 MD5 会不符，"
                           "不一定是备份出错；文件已保留在备份目录，可重新执行 [6] 再备份，不影响后续刷机。")

    # factory 备份成功后自动生成 factory-1m.bin（供 U-Boot 一键刷使用）
    factory_part = next((p for p in partitions if p["name"].strip().lower() == "factory"), None)
    if factory_part:
        factory_src = os.path.join(backup_dir, f"{factory_part['dev']}-{factory_part['name']}.bin")
        if os.path.exists(factory_src):
            dst_1m = os.path.join(backup_dir, "factory-1m.bin")
            if convert_factory_to_1m(factory_src, dst_1m):
                Logger.success("factory-1m.bin 已生成")
                CURRENT_FACTORY_BIN = dst_1m
            else:
                Logger.warning("factory-1m.bin 转换失败（分区备份本身不受影响，但刷系统需要该文件）。")
        else:
            Logger.warning("factory 分区文件不存在，跳过 factory-1m.bin 生成。")

    if standalone:
        tn.close()

    if success_count > 0 and not silent:
        try: os.startfile(backup_dir)
        except: pass

    return (not aborted) and success_count == len(targets)

# ================= 内部辅助：factory.bin -> factory-1m.bin（纯转换） =================
def convert_factory_to_1m(src, dst):
    """调用 fiberhome-factory-cli.exe 把 src 转成 1MiB 的 dst。
    成功返回 True，失败返回 False。CLI 的 stdout/stderr 以原始字节实时转发。
    """
    cli = os.path.join(BASE_DIR, "tools", "fiberhome-factory-cli.exe")
    if not os.path.exists(cli):
        Logger.error("未找到 tools/fiberhome-factory-cli.exe，请先按 [P] 下载依赖。")
        return False

    if os.path.exists(dst):
        try:
            os.remove(dst)
        except Exception as e:
            Logger.error(f"无法删除旧 {os.path.basename(dst)}: {e}")
            return False

    Logger.info(f"正在把 factory 分区原始备份转换成 1MiB 板级数据: {os.path.basename(src)}")
    print()
    sys.stdout.flush()

    # 关键：字节流实时转发，原样输出 CLI 日志
    try:
        p = subprocess.Popen(
            [cli, src, dst],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
        )
        while True:
            chunk = os.read(p.stdout.fileno(), 4096)
            if not chunk:
                break
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
        rc = p.wait()
    except Exception as e:
        Logger.error(f"调用 fiberhome-factory-cli.exe 失败: {e}")
        return False

    print()
    if rc != 0:
        Logger.error(f"fiberhome-factory-cli.exe 退出码 {rc}，转换失败。")
        return False
    if not os.path.exists(dst):
        Logger.error("CLI 返回成功，但输出文件不存在。")
        return False
    size = os.path.getsize(dst)
    if size != 1048576:
        Logger.error(f"输出大小异常: {size} 字节（预期 1048576）。")
        return False

    return True

# ================= 固件可选：按 .itb 识别，唯一直接用，多个刷写前询问 =================
def _list_firmware_itbs(model_dir):
    """列出 firmware/ 下全部 .itb 系统固件（按文件名排序）"""
    fw_dir = os.path.join(model_dir, "firmware")
    if not os.path.isdir(fw_dir):
        return []
    return sorted(f for f in os.listdir(fw_dir) if f.lower().endswith(".itb"))

def pick_firmware_itb(model_dir, auto_mode=False):
    """挑选本次要刷的系统固件。
    0 个 → 返回 None；1 个 → 直接用；多个 → 列出文件名与大小供用户选择。
    auto_mode=True（一键流程兜底，正常不会走到）时不交互：优先机型默认，否则按文件名首个。"""
    itbs = _list_firmware_itbs(model_dir)
    if not itbs:
        return None
    fw_dir = os.path.join(model_dir, "firmware")

    if len(itbs) == 1:
        Logger.info(f"系统固件: {paint(itbs[0], 'white', 'bold')}（firmware/ 下唯一，直接使用）")
        return os.path.join(fw_dir, itbs[0])

    default_name = CURRENT_MODEL.get("firmware") if CURRENT_MODEL else None
    if auto_mode:
        chosen = default_name if default_name in itbs else itbs[0]
        why = "机型默认" if chosen == default_name else "按文件名排序首个"
        Logger.info(f"系统固件: {paint(chosen, 'white', 'bold')}（一键流程自动锁定：{why}，不询问）")
        return os.path.join(fw_dir, chosen)

    print()
    line("─", 76, "cyan")
    print(paint(f"  [ 固件选择 ] firmware/ 下发现 {len(itbs)} 个 .itb 固件，请选择本次要刷入的：", "bold", "cyan"))
    for i, f in enumerate(itbs, 1):
        try: size = f"{os.path.getsize(os.path.join(fw_dir, f)) / 1024 / 1024:.1f} MB"
        except Exception: size = "?"
        tag = paint("  ← 机型默认", "green") if f == default_name else ""
        print(f"    [{i}] {_padv(f, 52)} {size}{tag}")
    line("─", 76, "cyan")
    while True:
        ch = input(paint(f"  >> 请选择 (1-{len(itbs)}): ", "cyan")).strip()
        if ch.isdigit() and 1 <= int(ch) <= len(itbs):
            idx = int(ch) - 1
            break
        print(paint(f"  无效输入，请输入 1-{len(itbs)}。", "red"))
    Logger.info(f"已选择固件: {paint(itbs[idx], 'white', 'bold')}")
    return os.path.join(fw_dir, itbs[idx])


def lock_firmware_for_onekey():
    """菜单 9：前置文件检查通过后立即锁定本次要刷的系统固件。
    与菜单 8 一致：检查完文件就定固件；此时设备仍在原厂、可无损回退。
    锁定后第一阶段 / 第二阶段全程复用，绝不在一键流程中途再弹选择。"""
    global CURRENT_FW_ITB
    CURRENT_FW_ITB = None
    if not CURRENT_MODEL:
        Logger.error("未选择机型，无法锁定系统固件。")
        return False
    model_dir = os.path.join(BASE_DIR, CURRENT_MODEL["dir"])
    print()
    line("─", 76, "cyan")
    print(paint("  [ 固件锁定 ] 一键流程开始前先定固件（后续阶段不再询问）", "bold", "cyan"))
    line("─", 76, "cyan")
    path = pick_firmware_itb(model_dir)
    if not path or not os.path.exists(path):
        Logger.error("firmware/ 下没有可用的 .itb 系统固件，已中止一键刷机（设备未做任何操作）。")
        Logger.warning("请按 [P] 联网下载依赖，或按 [F] 目录/来源核对固件位置后重试。")
        return False
    CURRENT_FW_ITB = path
    Logger.success(f"本次一键刷机固件已锁定: {paint(os.path.basename(path), 'white', 'bold')}（全程复用，不再询问）")
    return True

def run_web_flash(auto_mode=False):
    if not ensure_model_selected():
        return False

    Logger.info(f"当前目标机型: {paint(CURRENT_MODEL['name'], 'white', 'bold')}")
    Logger.info("正在检查一键刷写所需的核心文件...")

    tool_path = os.path.join(BASE_DIR, "tools", UBI_TOOL_NAME)
    model_dir = os.path.join(BASE_DIR, CURRENT_MODEL["dir"])
    prefix = CURRENT_MODEL["prefix"]

    uboot_bl2 = os.path.join(model_dir, "uboot", f"{prefix}-preloader.bin")
    uboot_fip = os.path.join(model_dir, "uboot", f"{prefix}-bl31-u-boot.fip")
    fix_bl2 = os.path.join(model_dir, "fix_uboot", f"{prefix}-preloader-tmp-8bit-read-4bit-write.bin")
    fix_fip = os.path.join(model_dir, "fix_uboot", f"{prefix}-bl31-u-boot-tmp-8bit-read-4bit-write.fip")

    # fix_uboot 已在前置检查中作为刷机预检必需项、动手前查齐；这里只复核必备的正式引导，
    # 探测到需临时引导的型号后再对 fix_uboot 做一次兜底检查（正常情况不会触发）
    missing = []
    if not os.path.exists(tool_path): missing.append(f"tools/{UBI_TOOL_NAME}")
    if not os.path.exists(uboot_bl2): missing.append(f"uboot/{os.path.basename(uboot_bl2)}")
    if not os.path.exists(uboot_fip): missing.append(f"uboot/{os.path.basename(uboot_fip)}")

    if missing:
        print()
        line("═", 76, "red")
        print(paint("  [!] 刷机中止：本地依赖文件不完整！", "red", "bold"))
        print(f"      目标机型: {paint(CURRENT_MODEL['name'], 'cyan', 'bold')}")
        for m in missing:
            print(f"      {paint('缺失:', 'red')} {m}")
        print("      请先按 [P] 联网下载依赖，然后再执行一键刷机。")
        line("═", 76, "red")
        return False

    Logger.success("本地依赖文件检查完毕，一切就绪！")

    tn, pc_ip = connect_telnet()
    if not tn: return False

    if not ensure_stock2ubi(tn, pc_ip):
        Logger.error("Stock2UBI 部署失败，无法执行刷机。")
        tn.close(); return False

    Logger.info("正在探测光猫闪存类型...")
    tn.write(b"cat /proc/driver/spi_nand_debug\n")
    time.sleep(1.0)
    raw = tn.read_all(wait_time=0.5).decode('ascii', errors='ignore')

    info = parse_flash_info(raw)
    print_flash_info(info)

    if chip_needs_fixboot(info):
        Logger.warning(f"检测到【需临时引导】闪存型号 {info['chip']}，已匹配 fix_uboot 临时过渡引导文件！")
        missing = []
        if not os.path.exists(fix_bl2): missing.append(f"fix_uboot/{os.path.basename(fix_bl2)}")
        if not os.path.exists(fix_fip): missing.append(f"fix_uboot/{os.path.basename(fix_fip)}")
        if missing:
            print()
            line("═", 76, "red")
            print(paint("  [!] 该闪存型号需要临时过渡引导，但文件缺失：", "red", "bold"))
            for m in missing:
                print(f"      {paint('缺失:', 'red')} {m}")
            print(paint("      请按 [P] 下载依赖后再刷，否则第一阶段刷入的引导无法启动。", "yellow"))
            line("═", 76, "red")
            tn.close(); return False
        bl2_name = os.path.basename(fix_bl2)
        fip_name = os.path.basename(fix_fip)
        bl2_path, fip_path = fix_bl2, fix_fip
    else:
        Logger.success(f"检测到【标准】闪存型号 {info['chip']}，已匹配正式 U-Boot 引导文件！")
        bl2_name = os.path.basename(uboot_bl2)
        fip_name = os.path.basename(uboot_fip)
        bl2_path, fip_path = uboot_bl2, uboot_fip

    bl2_size = os.path.getsize(bl2_path)
    fip_size = os.path.getsize(fip_path)
    if bl2_size > 4 * 1024 * 1024 or fip_size > 4 * 1024 * 1024:
        Logger.error("文件超过 4MB 限制，无法上传！")
        tn.close(); return False

    print()
    line("═", 76, "red")
    print(paint("  [!] 安全机制触发：刷写前必须备份！", "red", "bold"))
    line("═", 76, "red")

    # 前置：先选备份范围（一键流程里已选过则直接复用）
    scope = ask_backup_scope(silent=auto_mode)

    backup_success = run_web_backup(silent=True, tn=tn, pc_ip=pc_ip, scope=scope)

    if not backup_success:
        print()
        Logger.error("备份未完全成功！为保护设备安全，已强制中止刷写操作！")
        tn.close(); return False

    # 一键流程（菜单 9）：刷引导之前必须确认 1MiB 板级数据 factory-1m.bin 已生成。
    # 刷入引导后设备进入 U-Boot、原厂 Telnet 消失，届时既无法备份也无法转换，
    # 缺板级数据将导致第二阶段刷不了系统。此处设备仍在原厂，中止可无损回退。
    if auto_mode and (not CURRENT_FACTORY_BIN or not os.path.exists(CURRENT_FACTORY_BIN)):
        print()
        line("═", 76, "red")
        print(paint("  [!] 刷引导前检查未通过：缺少 factory-1m.bin（1MiB 板级数据）", "red", "bold"))
        print(paint("      分区备份已完成（mtdN-factory.bin 在备份目录），但没能转换成刷系统", "gray"))
        print(paint("      要用的 factory-1m.bin —— 两者不是同一个文件。常见原因：", "gray"))
        print(paint("        · tools/fiberhome-factory-cli.exe 缺失 → 请按 [P] 下载依赖", "yellow"))
        print(paint("        · 转换报错 / 备份范围不含 factory 分区 → 请按 [6] 重新备份", "yellow"))
        print(paint("      设备仍在原厂系统、未刷引导，未做任何改动；补好依赖后重新按 [9] 即可。", "gray"))
        line("═", 76, "red")
        tn.close(); return False

    print()
    line("═", 76, "red")
    print(paint("  [!] 备份就绪，即将刷写 BL2 和 U-Boot，此操作极其危险！", "red", "bold"))
    print(f"      目标机型: {paint(CURRENT_MODEL['name'], 'cyan', 'bold')}")
    print(f"      BL2 文件: {bl2_name}  ({bl2_size/1024:.1f} KB)")
    print(f"      FIP 文件: {fip_name}  ({fip_size/1024:.1f} KB)")
    print(paint("      刷写成功后设备将自动重启，过程中请勿断电！", "yellow"))
    line("═", 76, "red")

    # 修改：在原厂到PonWrt 模式下跳过确认
    if not auto_mode:
        if input(paint("  备份已完成，确认继续刷写？(输入 yes 确认): ", "cyan")).strip().lower() != "yes":
            Logger.info("已取消刷写操作。")
            tn.close(); return False

    Logger.info("已断开 Telnet，准备通过 HTTP 独立通道执行刷写...")
    tn.close()

    bl2_data = open(bl2_path, "rb").read()
    fip_data = open(fip_path, "rb").read()
    boundary = "----An758x" + uuid.uuid4().hex

    def part(name, filename, data):
        head = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        return head + data + b"\r\n"

    body = (
        part("bl2", bl2_name, bl2_data)
        + part("fip", fip_name, fip_data)
        + f"--{boundary}--\r\n".encode()
    )

    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Content-Length": str(len(body)),
    }

    Logger.info(f"正在上传并刷写... 请稍候")
    status, msg = None, ""

    try:
        conn = http.client.HTTPConnection(GATEWAY_IP, 3333, timeout=120)
        conn.request("POST", "/api/flash", body=body, headers=headers)
        resp = conn.getresponse()
        status = resp.status
        try:
            raw_json = resp.read().decode("utf-8", errors="ignore")
            msg = json.loads(raw_json).get("message", raw_json)
        except Exception:
            if status == 200:
                msg = "设备已执行写入并开始重启（响应被重启中断，属正常现象）"
            else:
                msg = "(读取响应体失败)"
        conn.close()
    except (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError):
        Logger.warning("连接被设备重启中断，刷写指令已下发，请等待 U-Boot 就绪。")
        return True   # 视同成功
    except Exception as e:
        Logger.error(f"网络请求失败: {e}")
        return False

    print()
    line("─", 76, "cyan")
    if status == 200:
        Logger.success(f"刷写成功！")
        print(paint(f"  光猫返回日志: {msg}", "white", "bold"))
        if not auto_mode:
            Logger.warning("设备即将自动重启，请等待约 1 分钟。")
            Logger.info("之后请用浏览器访问 http://192.168.0.1 进入 U-Boot 恢复页。")
        return True
    elif status == 400:
        Logger.error(f"刷写失败（工具内部错误）")
        print(paint(f"  光猫返回错误: {msg}", "red", "bold"))
        return False
    elif status == 409:
        Logger.warning(f"刷写冲突: 工具正在执行其他刷写任务。")
        return False
    elif status == 413:
        Logger.error(f"文件过大: 超过了工具的 4MB 限制。")
        return False
    else:
        Logger.error(f"未知状态码 {status}: {msg}")
        return False
    line("─", 76, "cyan")

# ================= 核心业务：U-Boot 环境一键刷写 =================
def check_uboot_api(ip=UBOOT_IP):
    """校验目标 IP 是否为真正的 U-Boot Web API"""
    try:
        conn = http.client.HTTPConnection(ip, 80, timeout=2)
        conn.request("GET", "/api/device", headers={"Cache-Control": "no-store"})
        resp = conn.getresponse()
        if resp.status == 200:
            data = json.loads(resp.read().decode('utf-8'))
            if "model" in data or "version" in data:
                conn.close()
                return True, data
        conn.close()
    except Exception:
        pass
    return False, None

def uboot_api_upload(filepath, ip=UBOOT_IP):
    """向 U-Boot 上传文件并返回 upload_id"""
    if not os.path.exists(filepath):
        return None
    filename = os.path.basename(filepath)
    total_size = os.path.getsize(filepath)
    
    try:
        conn = http.client.HTTPConnection(ip, 80, timeout=10)
        headers = {
            "Content-Type": "application/octet-stream",
            "Content-Length": str(total_size)
        }
        conn.request("POST", "/api/upload", headers=headers)
        sent = 0
        start_time = time.time()
        with open(filepath, "rb") as f:
            while True:
                chunk = f.read(64 * 1024)
                if not chunk:
                    break
                conn.send(chunk)
                sent += len(chunk)
                elapsed = time.time() - start_time
                speed = sent / elapsed if elapsed > 0 else 0
                draw_progress(f"上传 {filename[:10]}", sent, total_size, speed)
        print()
        
        resp = conn.getresponse()
        if resp.status == 200:
            data = json.loads(resp.read().decode('utf-8'))
            conn.close()
            return data.get("upload_id")
        conn.close()
    except Exception as e:
        print()
        Logger.error(f"上传接口异常: {e}")
    return None

def uboot_api_task(payload, ip=UBOOT_IP):
    """下发任务，返回 task_id"""
    try:
        conn = http.client.HTTPConnection(ip, 80, timeout=10)
        body = json.dumps(payload)
        headers = {"Content-Type": "application/json"}
        conn.request("POST", "/api/task", body=body, headers=headers)
        resp = conn.getresponse()
        if resp.status == 200:
            data = json.loads(resp.read().decode('utf-8'))
            conn.close()
            return data.get("id")
        elif resp.status == 409:
            Logger.error("设备忙，有其他任务正在执行！")
        conn.close()
    except Exception as e:
        Logger.error(f"任务下发异常: {e}")
    return None

def uboot_wait_task(task_id, is_reboot=False, ip=UBOOT_IP):
    """轮询等待任务完成，原样打印 U-Boot 串口日志"""
    last_output = ""
    misses = 0
    print()
    
    start_time = time.time()
    MAX_TIMEOUT = 180  # 单位：秒。3 分钟。足够应对大文件写入+校验

    while True:
        if time.time() - start_time > MAX_TIMEOUT:
            Logger.error("\n轮询超时！设备可能已死机或网络异常。")
            return False

        time.sleep(0.7)
        try:
            conn = http.client.HTTPConnection(ip, 80, timeout=5)
            conn.request("GET", "/api/task")
            resp = conn.getresponse()
            s = json.loads(resp.read().decode('utf-8'))
            conn.close()
            misses = 0
        except Exception:
            if is_reboot:
                misses += 1
                sys.stdout.write(f"\r  {paint('[等待重启]', 'yellow')} 连续丢失 {misses}/3...\033[K")
                sys.stdout.flush()
                if misses >= 3:
                    print()
                    return True
                continue
            else:
                sys.stdout.write(f"\r  {paint('[网络波动]', 'yellow')} 等待设备响应...\033[K")
                sys.stdout.flush()
                continue

        if s.get("id") != task_id:
            Logger.error("\n任务 ID 变化，可能被其他控制台抢占！")
            return False

        # 原样日志提取
        current_output = s.get("output", "")
        if current_output and current_output != last_output:
            new_text = current_output[len(last_output):] if current_output.startswith(last_output) else current_output
            if new_text:
                sys.stdout.write('\r\033[K')
                clean_text = new_text.replace('\r\n', '\n').replace('\r', '\n')
                print(paint(clean_text, "white"), end='') 
                if not clean_text.endswith('\n'):
                    print()
            last_output = current_output

        phase = s.get("phase", "")
        state = s.get("state", "")
        sys.stdout.write(f"\r  {paint('[状态]', 'cyan')} {paint(phase.upper(), 'yellow')} \033[K")
        sys.stdout.flush()

        if state == "done":
            print()
            if s.get("code") != 0:
                Logger.error(f"操作失败 (Code {s.get('code')})")
                return False
            return True

def _scan_factory_1m(model_dir):
    """静默扫描某机型下所有可用的 factory-1m.bin（不打印、不交互）"""
    backup_root = os.path.join(model_dir, "backup_mtd")
    candidates = []
    if os.path.exists(backup_root):
        for mac_dir in os.listdir(backup_root):
            mac_path = os.path.join(backup_root, mac_dir)
            if os.path.isdir(mac_path):
                f1m = os.path.join(mac_path, "factory-1m.bin")
                try:
                    if os.path.exists(f1m) and os.path.getsize(f1m) == 1048576:
                        candidates.append(f1m)
                except Exception:
                    pass
    return candidates

def find_factory_bin(model_dir):
    """在 backup_mtd 目录下智能寻找 factory-1m.bin"""
    candidates = _scan_factory_1m(model_dir)

    if len(candidates) == 1:
        mac_name = os.path.basename(os.path.dirname(candidates[0]))
        Logger.success(f"自动匹配到板级数据: {paint(mac_name, 'white')} / factory-1m.bin")
        return candidates[0]
    elif len(candidates) > 1:
        print()
        line("─", 76, "yellow")
        print(paint("  [!] 发现多个历史备份，请选择要刷入的 MAC：", "yellow", "bold"))
        for i, path in enumerate(candidates):
            mac_name = os.path.basename(os.path.dirname(path))
            print(f"    {paint(f'[{i+1}]', 'cyan')} MAC: {paint(mac_name, 'white')} | 文件: factory-1m.bin")
        line("─", 76, "yellow")
        
        while True:
            ch = input(paint("  >> 请输入编号选择 (或按回车取消): ", "cyan")).strip()
            if not ch: return None
            if ch.isdigit() and 1 <= int(ch) <= len(candidates):
                return candidates[int(ch) - 1]
            print(paint("  无效编号，请重新输入！", "red"))
            
    return None

# ================= 菜单 [8]/[9] 共用：文件检查与接口连接 =================

def uboot_pick_flash_files(auto_mode=False, use_current_factory=False):
    """菜单 8/9 共用：本地文件检查 + 固件定位 + 板级数据定位。
    在连接 U-Boot 之前执行（先定固件，再连设备，避免"刷一半才发现缺文件"）。
    返回 (bl2_path, fip_path, sys_path, factory_path)；失败返回 None（已打印原因）"""
    model_dir = os.path.join(BASE_DIR, CURRENT_MODEL["dir"])
    prefix = CURRENT_MODEL["prefix"]

    bl2_path = os.path.join(model_dir, "uboot", f"{prefix}-preloader.bin")
    fip_path = os.path.join(model_dir, "uboot", f"{prefix}-bl31-u-boot.fip")
    # 固件：菜单 9 已在「检查完文件」时锁定，一键流程直接沿用，绝不中途询问；
    #        菜单 8 在此处（连设备前）选择；异常未锁定时也不询问，自动兜底。
    if auto_mode:
        if CURRENT_FW_ITB and os.path.exists(CURRENT_FW_ITB):
            sys_path = CURRENT_FW_ITB
            Logger.info(f"系统固件（沿用预检锁定结果）: {paint(os.path.basename(sys_path), 'white', 'bold')}")
        elif CURRENT_FW_ITB:
            Logger.error("预检时锁定的系统固件已不存在，已中止刷系统（不在一键流程中途重新选择）。")
            Logger.warning("请把该 .itb 放回 firmware/ 后改按 [8] 继续，或重新按 [9] 完整执行；")
            Logger.warning("设备此刻可能停在 U-Boot，请勿断电。")
            return None
        else:
            sys_path = pick_firmware_itb(model_dir, auto_mode=True)
    else:
        sys_path = pick_firmware_itb(model_dir)

    if use_current_factory:
        factory_path = CURRENT_FACTORY_BIN
        if not factory_path or not os.path.exists(factory_path):
            # 刷引导前已校验过 factory-1m.bin，能走到这里只可能是文件被移动/删除等异常
            Logger.error("本次流程生成的 factory-1m.bin 已不可用（文件不存在），已中止刷系统。")
            Logger.warning("第一阶段刷引导前该文件是齐全的；现在丢失通常是被移动或删除。")
            Logger.warning("请从 backup_mtd/<MAC>/ 找回 factory-1m.bin 后改按 [8] 继续，")
            Logger.warning("或重新按 [9] 完整执行一遍；设备此刻可能停在 U-Boot，请勿断电。")
            return None
        Logger.success(f"已锁定本次流程刚备份的板级数据: {os.path.basename(factory_path)}")
    else:
        factory_path = find_factory_bin(model_dir)

    missing = []
    if not os.path.exists(bl2_path): missing.append("uboot/preloader.bin")
    if not os.path.exists(fip_path): missing.append("uboot/bl31-u-boot.fip")
    if not sys_path: missing.append("firmware/*.itb（系统固件 sysupgrade 镜像，至少放一个）")
    if not factory_path:
        missing.append("板级数据 factory-1m.bin（需先 [6] 备份自动转换生成）")

    if missing:
        print()
        line("═", 76, "red")
        print(paint("  [!] 本地文件不完整，无法执行一键刷写：", "red", "bold"))
        for m in missing: print(f"      - {m}")
        if factory_path is None:
            print(paint("  factory-1m.bin 由 [6] 备份自动转换生成，不能用 [P] 下载。", "yellow"))
        else:
            print(paint("  请按 [P] 联网下载依赖后重试。", "yellow"))
        line("═", 76, "red")
        if not auto_mode: input(paint("  按回车返回主菜单...", "gray"))
        return None

    Logger.success("刷写文件全部就绪（引导 + 固件 + 板级数据）！")
    # 记录本次实际选定的板级数据路径，供刷机成功日志反推 MAC（避免扫到别的机器）
    global _LAST_FLASH_FACTORY
    _LAST_FLASH_FACTORY = factory_path
    return bl2_path, fip_path, sys_path, factory_path

def uboot_wait_api(auto_mode=False):
    """菜单 8/9 共用：等待 U-Boot 恢复接口连通。返回 True 继续 / False 用户放弃"""
    while True:
        Logger.info(f"正在验证接口 (http://{UBOOT_IP}/api/device)...")
        is_valid, dev_info = check_uboot_api()

        if is_valid:
            ver = dev_info.get("version", "未知版本")
            model = dev_info.get("model", "未知型号")
            Logger.success(f"验证通过！已连接目标设备: {paint(model, 'white', 'bold')} (U-Boot {ver})")
            return True

        print()
        line("─", 76, "red")
        Logger.error(f"无法连接到有效的 U-Boot 接口 ({UBOOT_IP})！")
        print(paint("  可能原因: 1.网卡未配置IP 2.未进入恢复模式 3.网线没插好", "yellow"))
        line("─", 76, "red")

        print("\n  请选择下一步操作：")
        print("    [1] 自动配置网卡 IP (添加 192.168.0.10)")
        print("    [2] 我已配好网络/插好网线，重新检测")
        print("    [0] 放弃刷机，返回主菜单")

        choice = input(paint("  >> 请输入: ", "cyan")).strip()

        if choice == '1':
            run_network_fix(auto_mode=True)
            print()
            input(paint("  网卡配置结束，请按回车键重新检测网络...", "cyan"))
            print()
            line("═", 76, "cyan")
        elif choice == '2':
            print()
            line("═", 76, "cyan")
        else:
            Logger.warning("已取消刷机操作。")
            return False

def run_uboot_flash(auto_mode=False):
    """U-Boot 环境下的一键刷机（菜单 8）"""
    global CURRENT_MODEL
    
    if not auto_mode:
        line("═", 76, "cyan")
        print(paint("  U-BOOT 一键刷写向导", "bold", "cyan"))
        line("═", 76, "cyan")

    # 阶段 1：机型检查
    if not CURRENT_MODEL:
        Logger.warning("检测到尚未选择工作机型，正在为您拉起选型菜单...")
        time.sleep(1)
        run_select_model()
        if not CURRENT_MODEL:
            Logger.error("未选择机型，已取消一键刷写。")
            if not auto_mode: input(paint("  按回车返回...", "gray"))
            return

    # 阶段 2：本地文件检查 + 固件选择 + 板级数据定位（先定固件，再连设备）
    print()
    Logger.info(f"当前工作机型: {paint(CURRENT_MODEL['name'], 'white', 'bold')}")

    files = uboot_pick_flash_files(auto_mode=auto_mode, use_current_factory=auto_mode)
    if not files:
        return False
    bl2_path, fip_path, sys_path, factory_path = files

    # 阶段 3：连接 U-Boot 恢复接口（文件齐了才碰设备）
    if not uboot_wait_api(auto_mode):
        return False

    # 阶段 4：准备刷写
    print()
    line("═", 76, "cyan")
    print(paint("  [√] 接口验证通过 | 机型已定 | 文件充足", "green", "bold"))
    print(f"  即将自动按标准顺序刷写设备。请勿断电！")
    line("═", 76, "cyan")

    is_success = False
    try:
        Logger.info("[1/6] 正在重构 UBI (清空旧数据)...")
        tid = uboot_api_task({"operation": "ubi-rebuild"})
        if not tid or not uboot_wait_task(tid): raise Exception("UBI 重构失败")

        Logger.info("[2/6] 正在上传并刷写 BL2...")
        uid = uboot_api_upload(bl2_path)
        if not uid: raise Exception("BL2 上传失败")
        tid = uboot_api_task({"operation": "flash-bl2", "upload_id": uid})
        if not tid or not uboot_wait_task(tid): raise Exception("BL2 刷写失败")

        Logger.info("[3/6] 正在上传并刷写 U-Boot (FIP)...")
        uid = uboot_api_upload(fip_path)
        if not uid: raise Exception("FIP 上传失败")
        tid = uboot_api_task({"operation": "flash-uboot", "upload_id": uid})
        if not tid or not uboot_wait_task(tid): raise Exception("FIP 刷写失败")

        Logger.info("[4/6] 正在上传并刷写系统固件 (sysupgrade)...")
        uid = uboot_api_upload(sys_path)
        if not uid: raise Exception("系统固件上传失败")
        tid = uboot_api_task({"operation": "flash", "upload_id": uid})
        if not tid or not uboot_wait_task(tid): raise Exception("系统固件刷写失败")

        Logger.info("[5/6] 正在上传并刷写板级数据 (Factory)...")
        uid = uboot_api_upload(factory_path)
        if not uid: raise Exception("Factory 上传失败")
        tid = uboot_api_task({"operation": "flash-board-data", "upload_id": uid, "target": "factory"})
        if not tid or not uboot_wait_task(tid): raise Exception("Factory 刷写失败")

        Logger.info("[6/6] 全部刷写完毕，正在下发启动指令...")
        tid = uboot_api_task({"operation": "boot-production"})
        if tid:
            uboot_wait_task(tid, is_reboot=True)

        if not auto_mode:
            print()
            line("═", 76, "green")
            Logger.success(" U-Boot 全自动刷写完成！设备正在重启进入新系统。")
            line("═", 76, "green")
        is_success = True

    except Exception as e:
        print()
        Logger.error(f"刷机流程中止: {e}")
        Logger.warning("请查看上方日志，排错后重新操作。")
    
    return is_success

# ================= 原厂到PonWrt 一键刷机 =================
def run_ultimate_flash():
    """菜单 9：原厂到 PonWrt 一键刷机"""
    global CURRENT_MODEL, CURRENT_FACTORY_BIN
    clear()
    print()
    line("═", 76, "red")
    print(paint("  [!] 原厂到PonWrt 一键刷机向导 [!]", "red", "bold"))
    print("  此操作将全自动执行：原厂备份 -> 刷入引导 -> 等待重启 -> 刷入系统。")
    line("═", 76, "red")

    if not CURRENT_MODEL:
        Logger.warning("尚未选择机型，正在拉起选型菜单...")
        run_select_model()
        if not CURRENT_MODEL:
            return False
    if input(paint("\n  >> 确认开始原厂到PonWrt 一键刷机吗？此操作不可逆 (输入 yes 确认): ", "cyan")).strip().lower() != "yes":
        Logger.info("已取消。")
        return False
    # 每次原厂到PonWrt 一键开始，清空旧缓存
    CURRENT_FACTORY_BIN = None

    # 前置：备份范围二选一（后面两阶段都会用到这个选择）
    ask_backup_scope(silent=False)

    print()
    Logger.info("正在静默检查网络环境 (需要 1.x 与 0.x 双 IP)...")
    
    # 1. 检查光猫是否在线
    fh_online = False
    res, _ = send_fh_tool_request("GetDevInfo", timeout=3)
    if res:
        fh_online = True

    # 2. 检查本机是否有 192.168.0.x 的 IP
    has_0x = False
    for f in get_interfaces():
        if "192.168.0." in f["ip"]:
            has_0x = True
            break

    # 3. 判断结果
    if fh_online and has_0x:
        Logger.success("网络环境已就绪 (光猫在线，双 IP 已绑定)，跳过网卡配置。")
    else:
        print(paint("  [!] 发现网络环境未完全就绪：", "yellow"))
        if not fh_online: print(paint("     - 未连接光猫 (或 1.x IP 未配置)", "yellow"))
        if not has_0x: print(paint("     - 缺少 192.168.0.x 的 IP (进不了后续的 U-Boot)", "yellow"))
        
        if input(paint("  >> 是否现在自动配置网卡？(y/n): ", "cyan")).strip().lower() == 'y':
            run_network_fix(auto_mode=True)
    
    # =========================================================
    # 第一阶段
    # =========================================================
    print()
    line("═", 76, "cyan")
    print(paint("  [第一阶段] 原厂环境：备份并刷入 U-Boot 引导", "bold", "cyan"))
    line("═", 76, "cyan")
    
    try:
        success = run_web_flash(auto_mode=True)
        
        if not success:
            print()
            line("═", 76, "red")
            Logger.error("[×] 第一阶段（Stock2UBI）刷机失败！请检查上方日志。")
            line("═", 76, "red")
            return False
    except Exception as e:
        Logger.error(f"第一阶段执行发生异常: {e}")
        return False
    # =========================================================
    # 过渡阶段：无缝等待
    # =========================================================
    print()
    line("═", 76, "cyan")
    print(paint("  [过渡阶段] 设备正在重启进入 U-Boot 恢复模式...", "bold", "cyan"))
    print("  正在循环探测 192.168.0.1 接口上线（最长等待 120 秒），请勿断电...")
    line("═", 76, "cyan")

    print(paint("  [1/2] 设备正在重启，冷却等待 8 秒...", "yellow"))
    time.sleep(8) 
    
    max_wait_time = 120
    waited_time = 0
    is_uboot_ready = False

    print(paint("  [2/2] 开始探测 U-Boot 接口...", "cyan"))
    while waited_time < max_wait_time:
        is_valid, dev_info = check_uboot_api()
        
        if is_valid:
            is_uboot_ready = True
            print() 
            ver = dev_info.get("version", "未知版本")
            model = dev_info.get("model", "未知型号")
            Logger.success(f" 无缝衔接成功！设备已上线: {model} (U-Boot {ver})")
            break
        
        sys.stdout.write(f"\r  探测中... 已等待 {waited_time} 秒 / 最多 {max_wait_time} 秒   ")
        sys.stdout.flush()
        
        time.sleep(2)
        waited_time += 2

    if not is_uboot_ready:
        print()
        line("═", 76, "red")
        Logger.error("[×] 等待超时！设备未能成功进入 U-Boot 恢复模式。")
        Logger.warning("可能原因：")
        Logger.warning("  1. 引导文件刷写失败（变砖）。")
        Logger.warning("  2. 电脑没配 192.168.0.x 的 IP。")
        Logger.warning("  3. 网线没插在千兆 LAN 口（不支持 2.5G）。")
        line("═", 76, "red")
        return False
    # =========================================================
    # 第二阶段
    # =========================================================
    print()
    line("═", 76, "cyan")
    print(paint("  [第二阶段] U-Boot 环境：全自动刷写系统", "bold", "cyan"))
    line("═", 76, "cyan")
    
    # 接收第二阶段结果
    uboot_success = run_uboot_flash(auto_mode=True)

    if not uboot_success:
        print()
        line("═", 76, "red")
        Logger.error("[×] 第二阶段（U-Boot）刷机失败！请检查上方日志。")
        line("═", 76, "red")
        return False
    print()
    line("═", 76, "green")
    Logger.success(" 原厂到PonWrt 一键刷机全部完成！设备已重启进入 PonWrt！")
    line("═", 76, "green")

    # 提示恢复网卡
    if input(paint("  是否将本机网卡恢复为自动获取 IP (DHCP)？(y/n): ", "cyan")).strip().lower() == 'y':
        idx = get_current_interface_index()
        if idx:
            if not is_admin():
                _need_admin(idx, "reset")
            else:
                _do_reset(idx)
    return True

# ================= 状态面板 =================
def status_panel(force=False, use_cache_only=False):
    global CURRENT_FH_PORT, _STATUS_CACHE
    now = time.time()

    if _STATUS_CACHE["data"] and (use_cache_only or not force):
        d = _STATUS_CACHE["data"]
        CURRENT_FH_PORT = d["port"]
        local_ip = d["local_ip"]
        mac_str = d["mac_str"]
        status_str = d["status_str"]
        dot = d["dot"]
        port_str = d["port_str"]
    else:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.5)
            s.connect((GATEWAY_IP, 80))
            local_ip = s.getsockname()[0]
            s.close()
        except Exception:
            local_ip = "未连接"

        mac = get_mac_from_arp(GATEWAY_IP)
        status_str, dot, port_str, mac_str = "未连接", "red", "未知", "未知"

        if mac:
            mac_str = mac
            CURRENT_FH_PORT = None
            is_port_open = False
            for p in [8080, 80]:
                try:
                    s = socket.create_connection((GATEWAY_IP, p), timeout=0.2)
                    s.close()
                    is_port_open = True
                    CURRENT_FH_PORT = p
                    break
                except Exception:
                    pass
            if is_port_open:
                res, err = send_fh_tool_request("GetDevInfo", timeout=3.0)
                if res and not err:
                    status_str = paint("已连接", "green", "bold")
                    dot = "green"
                    port_str = f"{CURRENT_FH_PORT} ({'电信' if CURRENT_FH_PORT == 8080 else '联通'})"
                else:
                    status_str = paint("在线 (接口异常)", "yellow", "bold")
                    dot = "yellow"
                    port_str =  paint("端口通 (接口异常)", "yellow", "bold")

        d = {
            "local_ip": local_ip, "mac_str": mac_str,
            "status_str": status_str, "dot": dot,
            "port_str": port_str, "port": CURRENT_FH_PORT
        }
        _STATUS_CACHE.update({"time": now, "data": d})

    print(paint("  DEVICE OVERVIEW", "bold", "white"))
    _conn_line = f"  {paint('●', dot)}  设备连接状态     {status_str}"
    _conn_gap = max(35 - _vwidth(re.sub(r"\033\[[0-9;]*m", "", _conn_line)), 2)
    print(_conn_line + " " * _conn_gap + paint("按【R】可刷新连接状态", "cyan"))
    print("  " + paint("●", "cyan") + "  本机局域网 IP    " + paint(local_ip, "white"))
    print("  " + paint("●", "cyan") + "  光猫 MAC 地址    " + paint(mac_str, "white", "bold"))
    print("  " + paint("●", "cyan") + "  fh_tool 接口     " + paint(port_str, "white", "bold"))

    model_str = paint(CURRENT_MODEL["name"], "white", "bold") if CURRENT_MODEL else paint("未选择", "gray")
    print("  " + paint("●", "cyan") + "  当前工作机型     " + model_str)
    print()

# ================= 目录架构 / 来源：目录树 + 用途 + 项目来源 =================
def _fmt_size(n):
    try:
        n = float(n)
    except Exception:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024

def _fit(text, width):
    """按宽度对齐；超长时中间省略，避免撑破 76 列布局"""
    if len(text) <= width:
        return pad_text(text, width)
    keep = max(width - 1, 4)
    head = keep // 2
    return text[:head] + "…" + text[-(keep - head):]

# ---------------- 目录架构：静态目录说明（不读取真实文件） ----------------
def _vwidth(text):
    """视觉宽度：框线按 1 列、中文按 2 列（pad_text 会把框线算成 2，导致右列不对齐）"""
    w = 0
    for c in text:
        o = unicodedata.east_asian_width(c)
        if o in ("F", "W"):
            w += 2
        elif o == "A":
            w += 1 if (0x2500 <= ord(c) <= 0x259F or c == "·") else 2
        else:
            w += 1
    return w

def _padv(text, width):
    return text + " " * max(width - _vwidth(text), 0)

MAP_COL = 46   # 用途说明的起始列

# (缩进, 名称, 用途, 是否目录)  —— 纯静态说明，与实际文件无关
FILE_MAP_STATIC = [
    ("",                 "程序根目录/",                    "放哪都行：桌面 / D 盘 / U 盘，中文路径也可以", True),
    ("├── ",             "FiberHomeAutoFlash.exe",                    "主程序", False),
    ("├── ",             "files_sha256.json",              "官方 SHA256 校验表【自动生成】", False),
    ("├── ",             "tools/",                         "依赖工具目录（[P] 会下载到这里）", True),
    ("│   ├── ",         "an758x-stock2ubi",               "Stock2UBI 服务端：分区备份 / 刷引导（3333）", False),
    ("│   ├── ",         "fiberhome-factory.exe",          "GUI 版 factory 转换工具", False),
    ("│   └── ",         "fiberhome-factory-cli.exe",      "CLI 版 factory 转 factory-1m.bin", False),
    ("└── ",             "<机型目录>/",                    "5585F-CT / 5585F-CU / 5382A，选机型后自动定位", True),
    ("    ├── ",         "uboot/",                         "正式引导（标准闪存第一阶段直接刷这里的）", True),
    ("    │   ├── ",     "<前缀>-preloader.bin",           "引导 BL2", False),
    ("    │   └── ",     "<前缀>-bl31-u-boot.fip",         "引导 U-Boot（FIP）", False),
    ("    ├── ",         "fix_uboot/",                     "特定闪存型号的临时过渡引导（文件名带 tmp，标准闪存不用）", True),
    ("    │   ├── ",     "<前缀>-preloader-tmp-8bit-*.bin", "临时 BL2，进 U-Boot 后换正式", False),
    ("    │   └── ",     "<前缀>-bl31-u-boot-tmp-8bit-*.fip", "临时 U-Boot，进 U-Boot 后换正式", False),
    ("    ├── ",         "firmware/",                      "系统固件（[8]/[9] 第二阶段刷入，按 .itb 识别，可放多个）", True),
    ("    │   └── ",     "ponwrt-*-sysupgrade.itb",        "PonWrt 系统固件", False),
    ("    ├── ",         "info/",                          "设备信息归档【自动生成】", True),
    ("    │   └── ",     "deviceinfo_<MAC>.txt",           "GetDevInfo + 分区表 + 闪存信息", False),
    ("    │   └── ",     "flash_menu<N>_<MAC>_<时间>.log", "[7]/[8]/[9] 刷成功的全流程日志", False),
    ("    └── ",         "backup_mtd/",                    "分区备份【自动生成】", True),
    ("        └── ",     "<光猫MAC>/",                     "每台光猫一个目录", True),
    ("            ├── ", "mtdN-<分区名>.bin",              "各分区原始备份（带 MD5 校验）", False),
    ("            └── ", "factory-1m.bin",                 "1MiB 板级数据（由 factory 分区备份转换）", False),
]

# ---------------- 项目来源（第 2 页固定内容，不联网、不读磁盘） ----------------
SOURCES_DOWNLOAD = [
    ("https://github.com/pbs05/uboot-an758x",                  "v2026.07-4"),
    ("https://github.com/pbs05/an758x-stock2ubi",              "v0.1.14"),
    ("https://github.com/inonzi/fiberhome-factory",            "v0.1.1.cli"),
    ("https://github.com/inonzi/uboot-an758x-skyhigh-winbond", "v2026.07-4-skyhigh-winbond"),
    ("https://github.com/inonzi/Actions-OpenWrt-AN758X",       "ponwrt-an7581-all-master"),
]
SOURCES_REFERENCE = [
    "https://github.com/pbs05/uboot-an758x",
    "https://github.com/pbs05/an758x-stock2ubi",
    "https://github.com/pbs05/fiberhome-factory",
    "https://github.com/qwe-qaq/Actions-OpenWrt-AN758X",
    "https://github.com/xe5700/uboot-an758x-winbond",
]

def run_file_map():
    """[F] 目录架构/来源：分两页，避免一屏显示不完。
    第 1 页：目录架构 + 每项文件说明；第 2 页：项目来源（下载依赖来源 + 参考开源项目）。"""
    # ---- 第 1 页：目录架构 ----
    clear(); print()
    print(paint("  目录架构 / 来源  ·  第 1 页 / 共 2 页  [ 目录架构 ]", "bold", "cyan"))
    line("═", 76, "cyan")
    print(f"  固定说明页，不读取实际文件；本机实际路径：{paint(BASE_DIR, 'white')}")
    line("─", 76)
    for prefix, name, note, is_dir in FILE_MAP_STATIC:
        body = _padv(f"{prefix}{name}", MAP_COL)
        if not body.endswith(" "):
            body += "  "
        print(f"  {paint(body, *(('cyan', 'bold') if is_dir else ('white',)))}{paint(note, 'gray')}")
    line("─", 76)
    input(paint("  按回车查看第 2 页「项目来源」...", "cyan"))

    # ---- 第 2 页：项目来源 ----
    clear(); print()
    print(paint("  目录架构 / 来源  ·  第 2 页 / 共 2 页  [ 项目来源 ]", "bold", "cyan"))
    line("═", 76, "cyan")
    _print_sources()
    line("─", 76)
    print(paint("  [!] 本工具仅供个人设备维护与技术研究使用，刷机风险自负。", "yellow"))
    line("═", 76, "cyan")


def _print_sources():
    """第 2 页：下载依赖来源 + 参考的 Github 开源项目（固定内容，不联网）"""
    print(paint("  下载依赖来源 / SOURCES", "white", "bold"))
    print()
    for url, tag in SOURCES_DOWNLOAD:
        print(f"    {paint('·', 'cyan')} {paint(_padv(url, 56), 'white')} {paint(tag, 'gray')}")
    print()
    print(paint("  参考的Github开源项目：", "white", "bold"))
    print()
    for url in SOURCES_REFERENCE:
        print(f"    {paint('·', 'cyan')} {paint(url, 'white')}")

# ================= 菜单 [5][6][7][8][9] 前置文件检查 =================
def collect_requirements(menu):
    """按菜单编号汇总本次操作需要的本地文件（path=None 的是提示项）"""
    reqs = []

    def add_file(label, path, required=True, hint=""):
        reqs.append({"label": label, "path": path, "required": required, "hint": hint})

    def add_note(label, note, ok=True):
        reqs.append({"label": label, "path": None, "note": note, "note_ok": ok})

    tool_dir = os.path.join(BASE_DIR, "tools")
    model = CURRENT_MODEL
    mdir = os.path.join(BASE_DIR, model["dir"]) if model else ""
    pfx = model["prefix"] if model else ""

    if menu in ("5", "6", "7", "9"):
        add_file(f"tools/{UBI_TOOL_NAME}", os.path.join(tool_dir, UBI_TOOL_NAME), True, "Stock2UBI 服务端")
    if menu in ("6", "9"):
        # 菜单 [9] 全流程全量：本次必须把 factory 分区备份转换成 factory-1m.bin，
        # 转换工具缺失会导致备份跑完却刷不了系统，因此在动手前就列为必需并拦下。
        # 菜单 [6] 只是备份，缺它不影响备份本身，仍按可选处理。
        if menu == "9":
            add_file("tools/fiberhome-factory-cli.exe",
                     os.path.join(tool_dir, "fiberhome-factory-cli.exe"),
                     True, "全流程全量检查：把 factory 分区备份转换成 factory-1m.bin")
        else:
            add_file("tools/fiberhome-factory-cli.exe",
                     os.path.join(tool_dir, "fiberhome-factory-cli.exe"),
                     False, "factory-1m 转换（缺失时跳过转换，不影响备份）")

    if menu in ("7", "8", "9") and model:
        add_file(f"uboot/{pfx}-preloader.bin", os.path.join(mdir, "uboot", f"{pfx}-preloader.bin"),
                 True, "正式引导 BL2")
        add_file(f"uboot/{pfx}-bl31-u-boot.fip", os.path.join(mdir, "uboot", f"{pfx}-bl31-u-boot.fip"),
                 True, "正式引导 U-Boot")
    if menu in ("7", "9") and model:
        # 刷机预检：fix_uboot 临时过渡引导必须在动手之前就查齐，并列为必需。
        # 预检阶段还没连设备、读不到 /proc/driver/spi_nand_debug，根本不知道闪存型号，
        # 因此不能标成「按需/可选」，更不能等走完备份、连上设备、探测到型号才发现缺文件。
        # [P] 下载依赖会一并把这两个文件备好，缺失即在此拦下（设备零改动）。
        # 流程内 run_web_flash 探测型号后的检查仅作兜底，不再作为首次发现点。
        fix_hint = "刷机预检必需：预检时还不知道闪存型号，须提前备齐；按 [P] 一键下载"
        add_file(f"fix_uboot/{pfx}-preloader-tmp-8bit-read-4bit-write.bin",
                 os.path.join(mdir, "fix_uboot", f"{pfx}-preloader-tmp-8bit-read-4bit-write.bin"),
                 True, f"临时引导 BL2（{fix_hint}）")
        add_file(f"fix_uboot/{pfx}-bl31-u-boot-tmp-8bit-read-4bit-write.fip",
                 os.path.join(mdir, "fix_uboot", f"{pfx}-bl31-u-boot-tmp-8bit-read-4bit-write.fip"),
                 True, f"临时引导 U-Boot（{fix_hint}）")

    if menu in ("8", "9") and model:
        # 固件可选：firmware/ 下按 .itb 识别，0 个缺失、1 个直接用、多个刷写前询问
        itbs = _list_firmware_itbs(mdir)
        if len(itbs) == 0:
            add_file("firmware/*.itb", os.path.join(mdir, "firmware", model["firmware"]),
                     True, "系统固件 sysupgrade 镜像（放 firmware/ 目录，扩展名 .itb）")
        elif len(itbs) == 1:
            add_file(f"firmware/{itbs[0]}", os.path.join(mdir, "firmware", itbs[0]),
                     True, "系统固件 sysupgrade.itb（唯一直接使用）")
        else:
            note = ("预检通过后立即选定并锁定，全程不再询问" if menu == "9"
                    else "前置检查通过后，下一步即选择固件")
            add_note(f"firmware/ 共 {len(itbs)} 个 .itb", note, True)
        cnt = len(_scan_factory_1m(mdir))
        if menu == "9":
            add_note("factory-1m.bin", "本次流程由 factory 分区备份转换生成（刷引导前校验）")
        elif cnt:
            note = f"{cnt} 份可用，刷写时选择" if cnt > 1 else "1 份可用"
            add_file(f"factory-1m.bin（{note}）", _scan_factory_1m(mdir)[0],
                     True, "U-Boot 阶段刷入的板级数据")
        else:
            # 菜单 8 必需：没有板级数据就不允许刷，避免连上 U-Boot 后才发现刷不了
            add_file("factory-1m.bin（历史备份）",
                     os.path.join(mdir, "backup_mtd", "<MAC>", "factory-1m.bin"),
                     True, "未找到：需先 [6] 备份自动转换生成")

    if menu == "6" and model:
        add_note("备份目录", os.path.join(model["dir"], "backup_mtd", "<MAC>"))
    return reqs

def preflight_gate(menu):
    """菜单 [5][6][7][8][9] 统一前置检查。返回 True 表示可以继续。"""
    if menu in ("6", "7", "8", "9") and not CURRENT_MODEL:
        Logger.error("尚未选择机型，无法执行本次操作。请先按 [2] 选择机型。")
        return False

    reqs = collect_requirements(menu)
    print()
    title = f"  [ 前置文件检查 ]   菜单 [{menu}]"
    if menu == "9":
        title += "  ·  全量（实际所需）"
    if CURRENT_MODEL:
        title += f"  ·  {CURRENT_MODEL['name']}"
    line("═", 76, "cyan")
    print(paint(title, "bold", "cyan"))
    if menu == "9":
        print(paint("  菜单 [9] 涵盖 [6][7][8] 流程，按本次实际所需文件逐项检查，缺必需项在动手前拦下。", "gray"))
    line("═", 76, "cyan")

    missing = []
    for r in reqs:
        if r.get("path"):
            label = _fit(r["label"], 50)
            if os.path.exists(r["path"]):
                try: sz = _fmt_size(os.path.getsize(r["path"]))
                except Exception: sz = "?"
                print(f"  {paint('[√]', 'green', 'bold')} {paint(label, 'white')} {paint(sz, 'gray')}")
            elif r["required"]:
                print(f"  {paint('[×]', 'red', 'bold')} {paint(label, 'gray')} {paint('缺失（必需）', 'red', 'bold')}")
                missing.append(r)
            else:
                print(f"  {paint('[!]', 'yellow', 'bold')} {paint(label, 'gray')} {paint('缺失（可选）', 'yellow')}")
        else:
            tag = paint("[√]", "green", "bold") if r.get("note_ok", True) else paint("[!]", "yellow", "bold")
            print(f"  {tag} {paint(_fit(r['label'], 50), 'white')} {paint(r.get('note', ''), 'gray')}")
    line("─", 76)

    if not missing:
        ok_cnt = len([r for r in reqs if r.get("path") and os.path.exists(r["path"])])
        opt_missing = len([r for r in reqs if r.get("path") and not os.path.exists(r["path"])])
        tail = ""
        if opt_missing:
            tail = f"，另有 {opt_missing} 个可选项缺失（不影响本次操作）"
        Logger.success(f"前置检查通过（{ok_cnt} 个必备文件就绪{tail}），继续执行。")
        return True

    print(paint(f"  [!] 缺少 {len(missing)} 个必需文件，本次操作已拦下（未对设备做任何操作）。", "red", "bold"))
    for r in missing:
        if r.get("hint"):
            print(f"      - {paint(r['label'], 'gray')}  →  {paint(r['hint'], 'gray')}")
    has_f1m = any("factory-1m" in r["label"] for r in missing)
    line("═", 76, "red")
    if has_f1m:
        # 板级数据是 [6] 备份的转换产物，不是 [P] 能下载的依赖，不能提示"下载依赖"
        print(paint("  factory-1m.bin 由 [6] 备份自动转换生成，不是下载依赖；", "yellow", "bold"))
        print(paint("  未刷引导请先 [6] 备份（或 [7] 刷引导，内含备份），完成后再按 [8]。", "yellow"))
    elif input(paint("  >> 现在就去下载依赖？(y/n): ", "cyan")).strip().lower() == "y":
        print()
        run_env_check()
    return False

# ================= 菜单绘制 =================
def draw_menu(warning=None, use_cache_only=False):
    global CURRENT_VIEW
    clear()
    print()
    print(paint("  FIBERHOME  /  DEVICE TOOLKIT", "bold", "cyan"))
    print(paint("  烽火光猫维护与刷机辅助工具 v3.0", "white", "bold"))
    print(paint("  支持型号: 5585F-CT / 5585F-CU / 5382A", "gray"))
    line("═", 76, "cyan")
    print()

    status_panel(use_cache_only=use_cache_only)

    if CURRENT_VIEW == "FLASH":
        print(paint("  FLASHING SUPPORT (刷机辅助)", "bold", "white"))
        line("─", 76)
        print(f"  {paint('[P] ', 'cyan', 'bold')}   {paint(pad_text('下载依赖（联网）', 24), 'white', 'bold')} {paint('选择机型所需工具、正式/临时引导及系统固件', 'gray')}")
        line("─", 76)
        print(f"  {paint('[1] ', 'cyan', 'bold')}   {paint(pad_text('网络配置', 24), 'white', 'bold')} {paint('自动配置 1.10 / 0.10 双 IP', 'gray')}")
        print(f"  {paint('[2] ', 'cyan', 'bold')}   {paint(pad_text('选择当前机型', 24), 'white', 'bold')} {paint('切换当前要操作的设备型号', 'gray')}")
        print(f"  {paint('[3] ', 'cyan', 'bold')}   {paint(pad_text('开启 Telnet', 24), 'white', 'bold')} {paint('开启并推算登录 / 提权信息', 'gray')}")
        print(f"  {paint('[4] ', 'cyan', 'bold')}   {paint(pad_text('检测闪存信息', 24), 'white', 'bold')} {paint('Telnet→读取 NAND 型号与容量', 'gray')}")
        print(f"  {paint('[5] ', 'cyan', 'bold')}   {paint(pad_text('启动 Stock2UBI 服务', 24), 'white', 'bold')} {paint('Telnet→上传文件→启动服务', 'gray')}")
        print(f"  {paint('[6] ', 'cyan', 'bold')}   {paint(pad_text('Stock2UBI 极速备份', 24), 'white', 'bold')} {paint('Telnet→拉服务→备份分区（校验 MD5）', 'gray')}")
        print(f"  {paint('[7] ', 'cyan', 'bold')}   {paint(pad_text('Stock2UBI 一键刷引导', 24), 'white', 'bold')} {paint('Telnet→拉服务→判别闪存→备份→刷bl2/bl31', 'gray')}")
        print(f"  {paint('[8] ', 'cyan', 'bold')}   {paint(pad_text('UBOOT 一键刷固件', 24), 'white', 'bold')} {paint('重建UBI→bl2/bl31→固件→板级数据→启动', 'gray')}")
        print(f"  {paint('[9] ', 'cyan', 'bold')}   {paint(pad_text('一键原厂到 PonWrt', 24), 'white', 'bold')} {paint('原厂直通 PonWrt 全流程', 'gray')}")
        print(f"  {paint('[G] ', 'cyan', 'bold')}   {paint(pad_text('刷机指引（手动） ', 24), 'white', 'bold')} {paint('查看完整刷机步骤与异常排查', 'gray')}")
        print()
        line("─", 76)
        print(f"  {paint('[M]', 'cyan', 'bold')} 切换至工具箱    {paint('[R]', 'cyan', 'bold')} 刷新状态    {paint('[F]', 'cyan', 'bold')} 目录/来源    {paint('[0]', 'cyan', 'bold')} 退出程序")

    elif CURRENT_VIEW == "TOOLS":
        print(paint("  FIBERHOME TOOLS (烽火工具箱)", "bold", "white"))
        line("─", 76)
        print(f"  {paint('[01]', 'cyan', 'bold')}   {paint(pad_text('获取设备信息', 24), 'white', 'bold')} {paint('读取软件、硬件版本与区域信息', 'gray')}")
        print(f"  {paint('[02]', 'cyan', 'bold')}   {paint(pad_text('获取管理员账号', 24), 'white', 'bold')} {paint('读取光猫的管理员账号密码', 'gray')}")
        print(f"  {paint('[03]', 'cyan', 'bold')}   {paint(pad_text('获取 PPPoE 账号', 24), 'white', 'bold')} {paint('读取宽带拨号账号', 'gray')}")
        print(f"  {paint('[04]', 'cyan', 'bold')}   {paint(pad_text('获取注册信息', 24), 'white', 'bold')} {paint('读取注册账号与密码', 'gray')}")
        print(f"  {paint('[05]', 'cyan', 'bold')}   {paint(pad_text('设置注册信息', 24), 'white', 'bold')} {paint('修改注册账号与密码', 'gray')}")
        print(f"  {paint('[06]', 'cyan', 'bold')}   {paint(pad_text('开启 Telnet', 24), 'white', 'bold')} {paint('自动推算登录 / 提权信息', 'gray')}")
        print(f"  {paint('[07]', 'cyan', 'bold')}   {paint(pad_text('关闭 Telnet', 24), 'white', 'bold')} {paint('关闭光猫的 Telnet 服务', 'gray')}")
        print(f"  {paint('[08]', 'cyan', 'bold')}   {paint(pad_text('重启设备', 24), 'white', 'bold')} {paint('发送设备重启指令', 'gray')}")
        print(f"  {paint('[09]', 'cyan', 'bold')}   {paint(pad_text('恢复出厂设置', 24), 'white', 'bold')} {paint('清除设备配置', 'gray')}")
        print(f"  {paint('[10]', 'cyan', 'bold')}   {paint(pad_text('下载系统日志', 24), 'white', 'bold')} {paint('获取光猫系统日志的下载链接', 'gray')}")
        print()
        line("─", 76)
        print(f"  {paint('[M]', 'cyan', 'bold')} 切换至刷机台    {paint('[R]', 'cyan', 'bold')} 刷新状态    {paint('[F]', 'cyan', 'bold')} 目录/来源    {paint('[0]', 'cyan', 'bold')} 退出程序")

    line("─", 76)
    print()

    if warning:
        print(paint(f"  [!] {warning}", "red", "bold"))
        print()

# ================= 主菜单 =================
def main_menu():
    global _STATUS_CACHE, CURRENT_VIEW
    while True:
        _danger = False
        try:
            draw_menu()

            while True:
                choice = input(paint("  请输入功能编号  › ", "cyan", "bold")).strip().upper()
                valid = FLASH_CHOICES if CURRENT_VIEW == "FLASH" else TOOLS_CHOICES
                if choice in valid:
                    break
                draw_menu(warning=f"无效的选项: {choice}，请重新输入", use_cache_only=True)

            if choice == "M":
                CURRENT_VIEW = "TOOLS" if CURRENT_VIEW == "FLASH" else "FLASH"
                continue
            elif choice == "R":
                _STATUS_CACHE.update({"time": 0, "data": None})
                continue
            elif choice in ["0", "00"]:
                print("\n  感谢使用，再见！")
                break

            # 刷机 / U-Boot 类操作：Ctrl+C 中断时需额外告警（设备侧可能已在写入）
            _danger = (CURRENT_VIEW == "FLASH" and choice in ("7", "8", "9"))

            if CURRENT_VIEW == "FLASH":
                if choice == "P": print("\n"); run_env_check()
                elif choice == "1": print("\n"); run_network_fix()
                elif choice == "2": print("\n"); run_select_model()
                elif choice == "3": print("\n"); run_enable_telnet()
                elif choice == "4": print("\n"); run_flash_info()
                elif choice == "5":
                    print("\n")
                    if preflight_gate("5"): run_stock2ubi()
                elif choice == "6":
                    print("\n")
                    if ensure_model_selected() and preflight_gate("6"): run_web_backup()
                elif choice == "7":
                    print("\n")
                    _flash_log_begin("7", "Stock2UBI 一键刷引导")
                    _fl_ok = False
                    try:
                        if ensure_model_selected() and preflight_gate("7"):
                            _fl_ok = bool(run_web_flash())
                    finally:
                        _flash_log_end(_fl_ok, "7", "Stock2UBI 一键刷引导")
                elif choice == "8":
                    print("\n")
                    _flash_log_begin("8", "U-Boot 一键刷写")
                    _fl_ok = False
                    try:
                        if ensure_model_selected() and preflight_gate("8"):
                            _fl_ok = bool(run_uboot_flash())
                    finally:
                        _flash_log_end(_fl_ok, "8", "U-Boot 一键刷写")
                elif choice == "9":
                    print("\n")
                    _flash_log_begin("9", "原厂到PonWrt 一键刷机")
                    _fl_ok = False
                    try:
                        if ensure_model_selected() and preflight_gate("9") and lock_firmware_for_onekey():
                            _fl_ok = bool(run_ultimate_flash())
                    finally:
                        _flash_log_end(_fl_ok, "9", "原厂到PonWrt 一键刷机")
                elif choice == "G": run_flash_guide(standalone=True)
                elif choice == "F": run_file_map()
            else:
                if choice in ["1", "01"]: print("\n"); run_get_devinfo()
                elif choice in ["2", "02"]: print("\n"); run_get_admin()
                elif choice in ["3", "03"]: print("\n"); run_get_pppoe()
                elif choice in ["4", "04"]: print("\n"); run_get_reg()
                elif choice in ["5", "05"]: print("\n"); run_set_reg()
                elif choice in ["6", "06"]: print("\n"); run_enable_telnet()
                elif choice in ["7", "07"]: print("\n"); run_disable_telnet()
                elif choice in ["8", "08"]: print("\n"); run_reboot()
                elif choice in ["9", "09"]: print("\n"); run_restore_factory()
                elif choice == "10": print("\n"); run_log_download()
                elif choice == "G": run_flash_guide(standalone=True)
                elif choice == "F": run_file_map()

            print()
            input(paint("  按回车返回主菜单...", "gray"))
        except KeyboardInterrupt:
            # 致命问题修复：任何位置按 Ctrl+C 都不退出程序，清理后回到主菜单
            _restore_after_interrupt()
            print()
            Logger.warning("已收到 Ctrl+C：当前操作已中止，返回主菜单（退出请按 [0]）。")
            if _danger:
                Logger.warning("刷机 / U-Boot 写入可能已在设备侧开始，请勿断电、勿拔网线；")
                Logger.warning("等设备自行重启后，用 [4] / [8] 核实状态，切勿重复刷写。")
            try:
                input(paint("  按回车返回主菜单...", "gray"))
            except KeyboardInterrupt:
                pass
            continue

if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] in ["--fix", "--reset"]:
        try:
            _auto_action(sys.argv[1].replace("--", ""), sys.argv[2])
        except Exception as e:
            print(f"操作失败: {e}")
            time.sleep(2)
        sys.exit(0)
    # 兜底：即使有漏网的 KeyboardInterrupt，也不让程序直接退出，而是回到主菜单
    while True:
        try:
            main_menu()          # 选择 [0] 时正常返回
            break
        except KeyboardInterrupt:
            _restore_after_interrupt()
            print(paint("\n  [!] 已收到 Ctrl+C，返回主菜单（退出请按 [0]）。", "yellow"))
            continue
