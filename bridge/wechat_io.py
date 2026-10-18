# -*- coding: utf-8 -*-
"""wechat_io.py - Win32/UI automation layer for the WeChat (Weixin 4.x) bridge.

Based on the verified probe logic in 工具/wechat-bridge/probe/:
  capture-main.ps1  -> PrintWindow(flag=2), DPI aware, window enum
  wx-input.ps1      -> SendInput Unicode typing, clicks, key events
  analyze_bubbles.py-> message-area segmentation (background #FAFAFA)
  umi_ocr.py        -> Umi-OCR HTTP 127.0.0.1:1224

Only the narrow fixed layout is supported (window auto-resized to config.rect),
but all coordinates are recomputed from the live window rect on every use.

Public API (class WeChatIO):
  ensure_geometry()        -> restore minimized window + SetWindowPos to config.rect
  capture()                -> PIL.Image of the whole window (PrintWindow flag 2)
  message_area(img)        -> crop of the message area
  region_hash(crop)        -> fast 16-byte hash of the crop
  scan_message_area(img)   -> list of message items (after OCR + segmentation)
  ocr_png(bytes)           -> list of OCR lines
  verify_conversation()    -> OCR chat header, True if it is config.wechat.group
  switch_to_conversation() -> best effort: back to list, click matching row, verify
  send_text(text)          -> SendInput Unicode + Enter, verify after send
  send_file(path)          -> toolbar "send file" -> native dialog -> path -> Enter x2
  ensure_empty_input()     -> clear input box (never send on top of user text)
  minimize_and_restore()   -> self test helper
"""
import base64
import ctypes
import hashlib
import io
import json
import math
import os
import re
import sys
import time
import urllib.request
from ctypes import wintypes

import numpy as np
from PIL import Image

# --------------------------------------------------------------------------
# DPI awareness: MUST happen before any window/rect query.
# --------------------------------------------------------------------------
def _set_dpi_aware():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE
        return "shcore:2"
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
            return "user32"
        except Exception:
            return "none"

_DPI_MODE = _set_dpi_aware()

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
try:
    shcore = ctypes.WinDLL("shcore", use_last_error=True)
except Exception:  # pragma: no cover
    shcore = None


# --------------------------------------------------------------------------
# ctypes structures / prototypes
# --------------------------------------------------------------------------
class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
        ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
        ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
        ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", ctypes.c_uint32 * 3)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_uint32), ("time", ctypes.c_uint32),
                ("dwExtraInfo", ctypes.c_void_p)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_uint32), ("dwFlags", ctypes.c_uint32),
                ("time", ctypes.c_uint32), ("dwExtraInfo", ctypes.c_void_p)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", ctypes.c_uint32), ("wParamL", ctypes.c_ushort),
                ("wParamH", ctypes.c_ushort)]


class INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint32), ("u", INPUTUNION)]


user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
user32.GetClientRect.restype = wintypes.BOOL
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindow.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsIconic.restype = wintypes.BOOL
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
user32.SetWindowPos.restype = wintypes.BOOL
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.GetForegroundWindow.restype = wintypes.HWND
user32.BringWindowToTop.argtypes = [wintypes.HWND]
user32.BringWindowToTop.restype = wintypes.BOOL
user32.SwitchToThisWindow.argtypes = [wintypes.HWND, wintypes.BOOL]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(ctypes.c_uint32)]
user32.GetWindowThreadProcessId.restype = ctypes.c_uint32
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetParent.argtypes = [wintypes.HWND]
user32.GetParent.restype = wintypes.HWND
user32.AttachThreadInput.argtypes = [ctypes.c_uint32, ctypes.c_uint32, wintypes.BOOL]
user32.AttachThreadInput.restype = wintypes.BOOL
user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, ctypes.c_uint]
user32.PrintWindow.restype = wintypes.BOOL
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.ReleaseDC.restype = ctypes.c_int
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.SetCursorPos.restype = wintypes.BOOL
user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
user32.GetCursorPos.restype = wintypes.BOOL
user32.mouse_event.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_int,
                               ctypes.c_uint32, ctypes.c_void_p]
user32.mouse_event.restype = None
user32.MapVirtualKeyW.argtypes = [ctypes.c_uint, ctypes.c_uint]
user32.MapVirtualKeyW.restype = ctypes.c_uint
user32.SendInput.argtypes = [ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = ctypes.c_uint
user32.VkKeyScanW.argtypes = [wintypes.WCHAR]
user32.VkKeyScanW.restype = ctypes.c_short
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = ctypes.c_long
user32.PostMessageW.argtypes = [wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]
user32.PostMessageW.restype = wintypes.BOOL
user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.OpenClipboard.restype = wintypes.BOOL
user32.EmptyClipboard.restype = wintypes.BOOL
user32.CloseClipboard.restype = wintypes.BOOL
user32.GetClipboardData.argtypes = [ctypes.c_uint]
user32.GetClipboardData.restype = wintypes.HANDLE
user32.SetClipboardData.argtypes = [ctypes.c_uint, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE
kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalUnlock.restype = wintypes.BOOL

gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteObject.restype = wintypes.BOOL
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.DeleteDC.restype = wintypes.BOOL
gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, ctypes.c_uint, ctypes.c_uint,
                            ctypes.c_void_p, ctypes.POINTER(BITMAPINFO), ctypes.c_uint]
gdi32.GetDIBits.restype = ctypes.c_int

kernel32.GetCurrentThreadId.restype = ctypes.c_uint32
kernel32.OpenProcess.argtypes = [ctypes.c_uint32, wintypes.BOOL, ctypes.c_uint32]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, ctypes.c_uint32,
                                                wintypes.LPWSTR, ctypes.POINTER(ctypes.c_uint32)]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SW_HIDE = 0
SW_SHOW = 5
SW_RESTORE = 9
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
PW_RENDERFULLCONTENT = 2
DIB_RGB_COLORS = 0

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _get_class(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _get_title(hwnd):
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value


def _get_rect(hwnd):
    r = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return None
    return (int(r.left), int(r.top), int(r.right - r.left), int(r.bottom - r.top))


def _proc_exe(pid):
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return ""
    try:
        size = ctypes.c_uint32(1024)
        buf = ctypes.create_unicode_buffer(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        kernel32.CloseHandle(h)


def _enum_windows():
    """Yield (hwnd, pid, class, title, visible, rect)."""
    out = []
    CB = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, lparam):
        pid = ctypes.c_uint32(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        r = _get_rect(hwnd)
        out.append((int(hwnd), int(pid.value), _get_class(hwnd), _get_title(hwnd),
                    bool(user32.IsWindowVisible(hwnd)), r, int(user32.GetParent(hwnd) or 0)))
        return True

    user32.EnumWindows(CB(_cb), 0)
    return out


def _norm(s):
    """Normalise OCR text for tolerant matching: lowercase, keep CJK/alnum only."""
    if not s:
        return ""
    s = s.strip().lower()
    s = s.replace("（", "(").replace("）", ")").replace(" ", "")
    s = re.sub(r"[\s\u3000]+", "", s)
    s = re.sub(r"[，。！？、：:；;\"'“”‘’·—\-_()\[\]【】<>《》|/\\,.]+", "", s)
    return s


_TIME_RE = re.compile(
    r"^(?:昨天|今天|前天|星期[一二三四五六日天]|周[一二三四五六日天])?\s*"
    r"(?:上午|下午|凌晨|晚上)?\s*\d{1,2}:\d{2}$|^\d{4}年\d{1,2}月\d{1,2}日"
    r"|^(?:昨天|今天|前天)\s*\d{1,2}:\d{2}$|^\d{1,2}月\d{1,2}日")
_SYSTEM_RE = re.compile(
    r"撤回了一条消息|加入了群聊|退出了群聊|移出了群聊|修改群名|以下是新消息|"
    r"你已添加|以上是打招呼|群公告|拍了拍|已取消|对方正在输入|"
    r"换行|发送失败|重新编辑|邀你加入|成为新成员|设置了群公告")
_RECALL_RE = re.compile(r"撤回了一条消息")
_SO_RE = re.compile(r"\b[A-Z]{2,6}\d{6,12}[A-Z]?\b|\b[A-Z]{1,4}\d{5,14}[A-Z0-9]*\b")
_FILE_EXT_RE = re.compile(r"\.(pdf|xlsx?|docx?|pptx?|zip|rar|7z|csv|txt|png|jpe?g|bmp|gif)\b", re.I)
_SIZE_RE = re.compile(r"^\d+(?:\.\d+)?\s*[KMG]B?$", re.I)
_FILE_HINT_RE = re.compile(r"微信电脑版|微信文件|PDF|文档|表格|压缩包", re.I)
_NUM_RE = re.compile(r"^\d{6,}$")


def _txt_key(text):
    t = _norm(text)
    return hashlib.sha1(t.encode("utf-8", "replace")).hexdigest()[:16]


def _box_rect(box):
    """Umi-OCR box -> (x0, y0, x1, y1) floats. Accepts [[x,y]*4] or [x0,y0,x1,y1]."""
    try:
        if not box:
            return None
        if isinstance(box[0], (list, tuple)):
            xs = [float(p[0]) for p in box]
            ys = [float(p[1]) for p in box]
        else:
            xs = [float(box[0]), float(box[2])]
            ys = [float(box[1]), float(box[3])]
        return (min(xs), min(ys), max(xs), max(ys))
    except Exception:
        return None


# --------------------------------------------------------------------------
# OCR client
# --------------------------------------------------------------------------
class OcrError(RuntimeError):
    pass


class OcrClient(object):
    def __init__(self, url, timeout_s=8.0, retries=2, logger=None):
        self.url = url
        self.timeout_s = timeout_s
        self.retries = retries
        self.log = logger or (lambda *a, **k: None)
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.ok = False
        self.last_error = ""

    def ocr_png(self, png_bytes):
        b64 = base64.b64encode(png_bytes).decode("ascii")
        payload = json.dumps({
            "base64": b64,
            "options": {"data.format": "dict", "tbpu.parser": "none"},
        }).encode("utf-8")
        last_exc = None
        for attempt in range(self.retries + 1):
            t0 = time.time()
            try:
                req = urllib.request.Request(
                    self.url, data=payload,
                    headers={"Content-Type": "application/json"})
                with self._opener.open(req, timeout=self.timeout_s) as resp:
                    raw = resp.read().decode("utf-8", "replace")
                data = json.loads(raw)
                code = data.get("code")
                if code not in (100, 101, 200):
                    raise OcrError("Umi-OCR code=%r" % (code,))
                blocks = data.get("data")
                lines = []
                if isinstance(blocks, list):
                    for it in blocks:
                        if not isinstance(it, dict):
                            continue
                        text = (it.get("text") or "").strip()
                        if not text:
                            continue
                        rect = _box_rect(it.get("box"))
                        if not rect:
                            continue
                        lines.append({
                            "text": text, "rect": rect,
                            "score": it.get("score"),
                        })
                elif isinstance(blocks, dict):
                    for it in blocks.get("textBlocks", []) or []:
                        text = (it.get("text") or "").strip()
                        rect = _box_rect(it.get("box"))
                        if text and rect:
                            lines.append({"text": text, "rect": rect, "score": None})
                self.ok = True
                self.last_error = ""
                self.log("ocr.ok", n=len(lines), ms=int((time.time() - t0) * 1000))
                return lines
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                self.ok = False
                self.last_error = "%s: %s" % (type(exc).__name__, exc)
                self.log("ocr.error", attempt=attempt, error=self.last_error)
                if attempt < self.retries:
                    time.sleep(0.25 * (attempt + 1))
        raise OcrError("Umi-OCR HTTP failed: %s" % self.last_error) from last_exc


# --------------------------------------------------------------------------
# WeChat IO
# --------------------------------------------------------------------------
class WindowNotReady(RuntimeError):
    pass


class SendBlocked(RuntimeError):
    pass


class WeChatIO(object):
    # segmentation / avatar strips in window-relative coordinates for the fixed rect
    AVATAR_L = (109, 153)
    AVATAR_R = (462, 506)
    PANEL_BG = 250

    def __init__(self, cfg, logger=None):
        self.cfg = cfg or {}
        self.log = logger or (lambda *a, **k: None)
        wcfg = self.cfg.get("wechat", {}) or {}
        self.title = wcfg.get("title", "微信")
        self.cls = wcfg.get("class", "Qt51514QWindowIcon")
        self.group = wcfg.get("group", "WeChat Bridge")
        self.target_rect = wcfg.get("rect") or {"x": 2012, "y": 0, "w": 548, "h": 1549}
        self.auto_resize = bool(wcfg.get("auto_resize", True))
        self.layout = self._default_layout()
        self.layout.update(wcfg.get("layout") or {})
        self.hwnd = 0
        self.rect = None  # (x, y, w, h)
        ocfg = self.cfg.get("ocr", {}) or {}
        self.ocr = OcrClient(
            ocfg.get("url", "http://127.0.0.1:1224/api/ocr"),
            timeout_s=(ocfg.get("timeout_ms", 8000) / 1000.0),
            retries=int(ocfg.get("retry", 2)),
            logger=self.log,
        )
        scfg = self.cfg.get("send", {}) or {}
        self.type_delay_ms = int(scfg.get("type_delay_ms", 28))
        self.dialog_wait_ms = int(scfg.get("dialog_wait_ms", 900))
        self.after_send_wait_ms = int(scfg.get("after_send_wait_ms", 900))
        self._type_failures = 0

    # ---------------- layout ----------------
    def _default_layout(self):
        return {
            "chat_header": [90, 36, -12, 100],   # x0,y0,x1(w-12),y1
            "msg_area": [98, 100, -12, -114],    # x0,y0,x1(w-12),y1(h-114)
            "input_box": [-1, -79],              # x=w/2, y=h-79
            "send_file_btn": [-62, -52],         # x=w/2-62, y=h-52
            "back_btn": [-158, 68],              # x=w/2-158
            "chat_list": [90, 100, -12, 900],
            "msg_right_align": 449,
            "input_text": [96, -113, -12, -69],  # x0,y0,x1(w-12),y1(h-69): above icon row
            "input_ocr": [96, 900, -12, -66],    # OCR window for the whole (growing) input box
            "bottom_verify": [-400],             # last N px of message area
        }

    def _area(self, name, rect=None):
        rect = rect or self.rect
        if not rect:
            raise WindowNotReady("window rect unknown")
        x, y, w, h = rect
        spec = self.layout[name]
        if name == "input_box":
            return (w // 2, h + spec[1])
        if name == "send_file_btn":
            return (w // 2 + spec[0], h + spec[1])
        if name == "back_btn":
            return (w // 2 + spec[0], spec[1])
        if name == "bottom_verify":
            mx0, my0, mx1, my1 = self._area("msg_area", rect)
            return (mx0, max(my0, my1 + spec[0]), mx1, my1)
        x0, y0, x1, y1 = spec
        x0 = x0 if x0 >= 0 else w + x0
        x1 = x1 if x1 >= 0 else w + x1
        y0 = y0 if y0 >= 0 else h + y0
        y1 = y1 if y1 >= 0 else h + y1
        return (int(x0), int(y0), int(x1), int(y1))

    # ---------------- window discovery / geometry ----------------
    def find_main_window(self):
        """Locate the Weixin top-level window even when minimized/hidden.

        Minimized windows report rect -25600,-25600 159x27, and hidden windows
        are not "visible", so neither visibility nor size may be used as a hard
        filter here (a minimized window must still be found so it can be
        restored with ShowWindow(SW_RESTORE) before SetWindowPos).
        """
        cands = []
        for hwnd, pid, cls, title, visible, rect, parent in _enum_windows():
            if parent:
                continue
            if cls != self.cls and not (cls.startswith("Qt") and cls.endswith("QWindowIcon")):
                continue
            if title.strip() != self.title.strip():
                continue
            exe = _proc_exe(pid).lower()
            if "weixin" not in exe and "wechat" not in exe:
                continue
            w, h = (rect[2], rect[3]) if rect else (0, 0)
            iconic = bool(user32.IsIconic(hwnd))
            # rank: normal visible windows first, then minimized/hidden ones
            score = (w * h) if (visible and not iconic and w > 300 and h > 300) else -1
            cands.append((score, w * h, hwnd, rect, exe, visible, iconic))
        if not cands:
            return None
        # keep a still-valid cached hwnd if it is among the candidates
        if self.hwnd:
            for c in cands:
                if c[2] == self.hwnd:
                    self.rect = _get_rect(self.hwnd) or c[3]
                    return {"hwnd": self.hwnd, "rect": self.rect, "exe": c[4],
                            "iconic": c[6], "visible": c[5]}
        cands.sort(key=lambda t: (t[0], t[1]), reverse=True)
        _, _, hwnd, rect, exe, visible, iconic = cands[0]
        self.hwnd = hwnd
        self.rect = _get_rect(hwnd) or rect
        return {"hwnd": hwnd, "rect": self.rect, "exe": exe,
                "iconic": iconic, "visible": visible}

    def _refresh_handle(self):
        if self.hwnd and user32.IsWindow(self.hwnd):
            r = _get_rect(self.hwnd)
            if r:
                self.rect = r
                return True
        self.hwnd = 0
        return self.find_main_window() is not None

    def ensure_geometry(self, force_resize=None):
        """Restore (if minimized/hidden) and set the window to config.rect.

        Returns dict {ok, hwnd, rect, restored, resized, fg}.
        Raises WindowNotReady when the WeChat main window cannot be found.
        """
        force_resize = self.auto_resize if force_resize is None else force_resize
        restored = False
        resized = False
        for attempt in range(3):
            if not self._refresh_handle():
                if attempt < 2:
                    time.sleep(0.4)
                    continue
                raise WindowNotReady("WeChat main window not found")
            hwnd = self.hwnd
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, SW_RESTORE)
                restored = True
                time.sleep(0.45)
            elif not user32.IsWindowVisible(hwnd):
                user32.ShowWindow(hwnd, SW_SHOW)
                restored = True
                time.sleep(0.5)
            else:
                break

        rect = _get_rect(self.hwnd)
        if rect is None:
            raise WindowNotReady("GetWindowRect failed")
        tw = int(self.target_rect.get("w", 548))
        th = int(self.target_rect.get("h", 1549))
        tx = int(self.target_rect.get("x", 2012))
        ty = int(self.target_rect.get("y", 0))
        if force_resize and (abs(rect[0] - tx) > 1 or abs(rect[1] - ty) > 1
                             or abs(rect[2] - tw) > 1 or abs(rect[3] - th) > 1):
            user32.SetWindowPos(self.hwnd, 0, tx, ty, tw, th,
                                SWP_NOZORDER | SWP_NOACTIVATE)
            time.sleep(0.25)
            rect = _get_rect(self.hwnd) or rect
            resized = True
        # after a restore the window may come back with its old size -> force again
        if force_resize and (rect[2] != tw or rect[3] != th):
            user32.SetWindowPos(self.hwnd, 0, tx, ty, tw, th,
                                SWP_NOZORDER | SWP_NOACTIVATE)
            time.sleep(0.2)
            rect = _get_rect(self.hwnd) or rect
            resized = True
        self.rect = rect
        return {"ok": rect is not None and rect[2] > 300 and rect[3] > 300,
                "hwnd": self.hwnd, "rect": rect,
                "restored": restored, "resized": resized}

    def activate(self):
        """Bring the main window to the foreground (needed before mouse/key input)."""
        if not self._refresh_handle():
            raise WindowNotReady("WeChat main window not found")
        hwnd = self.hwnd
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
            time.sleep(0.4)
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.12)
        cur = user32.GetForegroundWindow()
        if cur != hwnd:
            fg_thread = user32.GetWindowThreadProcessId(cur, None)
            my_thread = kernel32.GetCurrentThreadId()
            user32.AttachThreadInput(my_thread, fg_thread, True)
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            user32.AttachThreadInput(my_thread, fg_thread, False)
            time.sleep(0.12)
            cur = user32.GetForegroundWindow()
        if cur != hwnd:
            # synthetic ALT tap unlocks SetForegroundWindow in most cases
            self._key_tap(0x12, 0, 0)
            time.sleep(0.06)
            user32.SwitchToThisWindow(hwnd, True)
            time.sleep(0.25)
            cur = user32.GetForegroundWindow()
        return cur == hwnd

    # ---------------- capture ----------------
    def capture(self):
        if not self._refresh_handle():
            raise WindowNotReady("WeChat main window not found")
        rect = self.rect
        w, h = rect[2], rect[3]
        if w <= 0 or h <= 0:
            raise WindowNotReady("bad window rect %r" % (rect,))
        hdc_screen = user32.GetDC(None)
        mem_dc = gdi32.CreateCompatibleDC(hdc_screen)
        bmp = gdi32.CreateCompatibleBitmap(hdc_screen, w, h)
        old = gdi32.SelectObject(mem_dc, bmp)
        try:
            ok = user32.PrintWindow(self.hwnd, mem_dc, PW_RENDERFULLCONTENT)
            if not ok:
                # fall back to flag 3 once (same in probe, slightly smaller)
                ok = user32.PrintWindow(self.hwnd, mem_dc, 3)
            bi = BITMAPINFO()
            bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bi.bmiHeader.biWidth = w
            bi.bmiHeader.biHeight = -h  # top-down
            bi.bmiHeader.biPlanes = 1
            bi.bmiHeader.biBitCount = 32
            bi.bmiHeader.biCompression = 0
            buf = ctypes.create_string_buffer(w * h * 4)
            n = gdi32.GetDIBits(mem_dc, bmp, 0, h, buf, ctypes.byref(bi), DIB_RGB_COLORS)
            if not ok or n == 0:
                raise WindowNotReady("PrintWindow/GetDIBits failed (window hidden?)")
            img = Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1)
            return img.copy()
        finally:
            gdi32.SelectObject(mem_dc, old)
            gdi32.DeleteObject(bmp)
            gdi32.DeleteDC(mem_dc)
            user32.ReleaseDC(None, hdc_screen)

    def crop(self, img, area):
        x0, y0, x1, y1 = area
        w, h = img.size
        x0 = max(0, min(w, x0)); x1 = max(x0 + 1, min(w, x1))
        y0 = max(0, min(h, y0)); y1 = max(y0 + 1, min(h, y1))
        return img.crop((x0, y0, x1, y1)), (x0, y0)

    def message_area(self, img):
        return self.crop(img, self._area("msg_area"))

    @staticmethod
    def region_hash(img):
        arr = np.asarray(img.convert("RGB"), dtype=np.uint8)
        return hashlib.blake2b(arr.tobytes(), digest_size=16).hexdigest()

    @staticmethod
    def png_bytes(img):
        b = io.BytesIO()
        img.save(b, format="PNG", optimize=False)
        return b.getvalue()

    # ---------------- OCR wrappers ----------------
    def ocr_image(self, img):
        return self.ocr.ocr_png(self.png_bytes(img))

    def ocr_region(self, img, area):
        sub, off = self.crop(img, area)
        lines = self.ocr_image(sub)
        for ln in lines:
            x0, y0, x1, y1 = ln["rect"]
            ln["rect"] = (x0 + off[0], y0 + off[1], x1 + off[0], y1 + off[1])
        return lines

    # ---------------- segmentation ----------------
    def segment(self, crop_img, offset=(0, 0)):
        """Segment bubble/card regions from the message-area crop.

        Mirrors probe/analyze_bubbles.py: background #FAFAFA, row bands, column
        runs, avatar-side detection and colour classification.  Coordinates are
        returned in *window* coordinates (offset applied).
        """
        arr = np.asarray(crop_img.convert("RGB"), dtype=np.int16)
        h, w, _ = arr.shape
        diff = np.abs(arr - self.PANEL_BG).sum(axis=2)
        mask = diff > 12
        if w > 4:
            mask[:, :2] = False
            mask[:, -2:] = False
        ox, oy = offset
        avx0 = max(0, self.AVATAR_L[0] - ox)
        avx1 = min(w, self.AVATAR_L[1] - ox)
        bvx0 = max(0, self.AVATAR_R[0] - ox)
        bvx1 = min(w, self.AVATAR_R[1] - ox)

        rows = mask.sum(axis=1)
        bands = []
        inb = False
        s = 0
        for i, c in enumerate(rows):
            if c > 4 and not inb:
                inb = True; s = i
            elif c <= 4 and inb:
                inb = False; bands.append([s, i - 1])
        if inb:
            bands.append([s, h - 1])
        merged = []
        for b in bands:
            if merged and b[0] - merged[-1][1] <= 4:
                merged[-1][1] = b[1]
            else:
                merged.append(b)
        comps = []
        for (ry0, ry1) in merged:
            seg = mask[ry0:ry1 + 1]
            cols = seg.sum(axis=0)
            runs = []
            inr = False
            s = 0
            for i, c in enumerate(cols):
                if c > 2 and not inr:
                    inr = True; s = i
                elif c <= 2 and inr:
                    inr = False; runs.append([s, i - 1])
            if inr:
                runs.append([s, w - 1])
            merged_runs = []
            for r in runs:
                if merged_runs and r[0] - merged_runs[-1][1] <= 6:
                    merged_runs[-1][1] = r[1]
                else:
                    merged_runs.append(r)
            cand = []
            for (cx0, cx1) in merged_runs:
                if cx1 - cx0 < 8:
                    continue
                wx0 = ox + cx0
                wx1 = ox + cx1
                width = cx1 - cx0 + 1
                height = ry1 - ry0 + 1
                cand.append((cx0, cx1, wx0, wx1, width, height))
            if not cand:
                continue
            has_av_l = any(40 <= cc[4] <= 50 and cc[2] < 160 for cc in cand)
            has_av_r = any(40 <= cc[4] <= 50 and cc[3] > 450 for cc in cand)
            az_l = mask[ry0:ry1 + 1, avx0:avx1].sum() if avx1 > avx0 else 0
            az_r = mask[ry0:ry1 + 1, bvx0:bvx1].sum() if bvx1 > bvx0 else 0
            if has_av_l and not has_av_r:
                side = "left"
            elif has_av_r and not has_av_l:
                side = "right"
            else:
                side = "left" if az_l > az_r else "right"
            for (cx0, cx1, wx0, wx1, width, height) in cand:
                patch = arr[ry0:ry1 + 1, cx0:cx1 + 1]
                m = mask[ry0:ry1 + 1, cx0:cx1 + 1]
                px = patch[m]
                if len(px) == 0:
                    continue
                r = float(px[:, 0].mean()); g = float(px[:, 1].mean()); b = float(px[:, 2].mean())
                white_frac = float(((patch >= 252).all(axis=2)).mean())
                gray_frac = float((((patch >= 230) & (patch <= 246)).all(axis=2)).mean())
                if 32 <= width <= 56 and 32 <= height and (wx0 < 160 or wx1 > 450):
                    cls = "avatar"
                elif g > 180 and g - r > 40 and g - b > 40:
                    cls = "green-self"
                elif white_frac >= 0.35:
                    cls = "white-other"
                elif gray_frac >= 0.28:
                    cls = "light-card"
                elif abs(r - g) < 14 and abs(g - b) < 14 and r > 200:
                    cls = "light-card"
                else:
                    cls = "image/mixed"
                comps.append({
                    "x0": wx0, "y0": oy + ry0, "x1": wx1, "y1": oy + ry1,
                    "w": width, "h": height, "side": side, "kind": cls,
                    "rgb": (int(r), int(g), int(b)),
                })
        comps.sort(key=lambda c: (c["y0"], c["x0"]))
        return comps

    # ---------------- OCR + segmentation -> message items ----------------
    def _line_on_card(self, crop_img, line_rect, offset):
        """True when the thin ring around an OCR line sits on a message card.

        Verified pixel facts (probe shots): page background #FAFAFA (250),
        other-person text/file card #EFEFF1 (239), inline image area #FFFFFF,
        green self bubble ~(149,223,112).  A line whose surroundings are the
        page background is a nickname / time / quote row, not bubble content.
        """
        arr = np.asarray(crop_img.convert("L"), dtype=np.uint8)
        ox, oy = offset
        x0 = int(line_rect[0] - ox); x1 = int(line_rect[2] - ox)
        y0 = int(line_rect[1] - oy); y1 = int(line_rect[3] - oy)
        h, w = arr.shape
        strips = []
        for yy in (y0 - 3, y1 + 3):
            if 0 <= yy < h:
                strips.append(arr[yy, max(0, x0):min(w, x1)])
        for xx in (x0 - 3, x1 + 3):
            if 0 <= xx < w:
                strips.append(arr[max(0, y0):min(h, y1), xx])
        strips = [s.reshape(-1) for s in strips if s.size]
        if not strips:
            return False
        vals = np.concatenate(strips)
        if vals.size < 8:
            return False
        med = float(np.median(vals))
        return med < 247.5

    def _assign_lines(self, lines, comps):
        assigned = {i: [] for i in range(len(comps))}
        loose = []
        for ln in lines:
            x0, y0, x1, y1 = ln["rect"]
            cx = (x0 + x1) / 2.0
            cy = (y0 + y1) / 2.0
            best = None
            best_area = 1e18
            for i, c in enumerate(comps):
                if c["x0"] - 4 <= cx <= c["x1"] + 4 and c["y0"] - 4 <= cy <= c["y1"] + 4:
                    area = c["w"] * c["h"]
                    if area < best_area:
                        best_area = area
                        best = i
            if best is None:
                loose.append(ln)
            else:
                assigned[best].append(ln)
        return assigned, loose

    @staticmethod
    def _join_lines(lines):
        lines = sorted(lines, key=lambda l: (l["rect"][1], l["rect"][0]))
        parts = [l["text"].strip() for l in lines if l["text"].strip()]
        return "\n".join(parts)

    def _looks_file(self, text):
        compact = _norm(text)
        if _FILE_EXT_RE.search(compact):
            return True
        if _FILE_HINT_RE.search(text) and re.search(r"\d+(?:\.\d+)?\s*[KMG]", text, re.I):
            return True
        if "\n" in text and _SIZE_RE.search(text.strip().split("\n")[-2] if len(text.split("\n")) > 1 else ""):
            return True
        return False

    def _extract_filename(self, text):
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        skip = lambda s: bool(_SIZE_RE.match(s)) or bool(_FILE_HINT_RE.match(s))
        for idx, ln in enumerate(lines):
            if _FILE_EXT_RE.search(ln):
                cand = ln
                if idx > 0 and not skip(lines[idx - 1]):
                    prev = lines[idx - 1]
                    # a wrapped filename continues directly (no sentence punctuation)
                    if prev and prev[-1] not in "。！？；;:：,，":
                        cand = prev + ln
                return cand.strip()
        joined = "".join(l for l in lines if not skip(l))
        m = _FILE_EXT_RE.search(joined)
        if m:
            return joined[max(0, m.end() - 90):m.end()].strip()
        return ""

    def build_items(self, crop_img, offset=(0, 0)):
        """OCR the message crop and turn it into ordered message items.

        item = {kind: text|file|image|time|system|recall,
                side: left|right, text, sender, filename,
                bbox:(x0,y0,x1,y1), key, text_norm}
        Self (right / green) bubbles are context only and never returned.
        """
        lines = self.ocr_image(crop_img)
        if offset != (0, 0):
            ox, oy = offset
            shifted = []
            for ln in lines:
                x0, y0, x1, y1 = ln["rect"]
                shifted.append({"text": ln["text"],
                                "rect": (x0 + ox, y0 + oy, x1 + ox, y1 + oy),
                                "score": ln.get("score")})
            lines = shifted
        comps = self.segment(crop_img, offset)
        assigned, loose = self._assign_lines(lines, comps)

        items = []
        # ---- classify segmented components ----
        for i, c in enumerate(comps):
            if c["kind"] == "avatar":
                continue
            clines = assigned.get(i, [])
            content = []
            nick = []
            for ln in clines:
                if self._line_on_card(crop_img, ln["rect"], offset):
                    content.append(ln)
                else:
                    nick.append(ln)
            text_content = self._join_lines(content)
            text_all = self._join_lines(clines)
            bbox = (c["x0"], c["y0"], c["x1"], c["y1"])
            side = c["side"]
            # narrow rows carrying time / system / recall text (not bubbles)
            if c["h"] < 34 and len(clines) <= 2 and text_all:
                if _TIME_RE.search(text_all):
                    items.append({"kind": "time", "side": side, "text": text_all,
                                  "sender": "", "bbox": bbox})
                    continue
                if _RECALL_RE.search(text_all):
                    items.append({"kind": "recall", "side": side, "text": text_all,
                                  "sender": "", "bbox": bbox})
                    continue
                if _SYSTEM_RE.search(text_all):
                    items.append({"kind": "system", "side": side, "text": text_all,
                                  "sender": "", "bbox": bbox})
                    continue
                if not content:
                    # small text row on the page background: quote row below a
                    # bubble, or an unknown system fragment -> never a message
                    above = False
                    for j, c2 in enumerate(comps):
                        if j == i or c2["kind"] == "avatar":
                            continue
                        if 0 <= c["y0"] - c2["y1"] <= 20 and c["x0"] < c2["x1"] and c["x1"] > c2["x0"]:
                            above = True
                            break
                    if above or side == "right":
                        continue
                    if not (re.search(r"\d{5,}", text_all) or _SO_RE.search(text_all)):
                        continue
            if c["kind"] == "green-self":
                continue  # own message: context only (quote rows handled below)
            if c["kind"] == "light-card":
                if _RECALL_RE.search(text_all):
                    items.append({"kind": "recall", "side": side, "text": text_all,
                                  "sender": "", "bbox": bbox})
                    continue
                if "聊天记录" in text_all and c["h"] >= 80:
                    continue  # merged-forward card: recognised, not expanded in v1
                if not text_content and c["h"] >= 45:
                    items.append({"kind": "image", "side": side, "text": "",
                                  "sender": "", "bbox": bbox})
                    continue
                if _SYSTEM_RE.search(text_content) and not _FILE_EXT_RE.search(text_content):
                    items.append({"kind": "system", "side": side, "text": text_content,
                                  "sender": "", "bbox": bbox})
                    continue
                if self._looks_file(text_all):
                    items.append({"kind": "file", "side": side, "text": text_content,
                                  "filename": self._extract_filename(text_all),
                                  "sender": self._join_lines(nick), "bbox": bbox})
                    continue
                items.append({"kind": "text", "side": side, "text": text_content or text_all,
                              "sender": self._join_lines(nick), "bbox": bbox})
            elif c["kind"] == "white-other":
                # white region inside a gray card = inline image / document screenshot
                if c["h"] >= 90 or not text_content:
                    items.append({"kind": "image", "side": side, "text": "",
                                  "sender": "", "bbox": bbox})
                elif len(content) <= 3:
                    items.append({"kind": "text", "side": side, "text": text_content,
                                  "sender": self._join_lines(nick), "bbox": bbox})
                else:
                    items.append({"kind": "image", "side": side, "text": "",
                                  "sender": "", "bbox": bbox})
            else:  # image/mixed (photos, avatars not filtered, stickers)
                if c["h"] >= 40 and c["w"] >= 40:
                    items.append({"kind": "image", "side": side, "text": text_content,
                                  "sender": "", "bbox": bbox})
                elif text_all:
                    items.append({"kind": "text", "side": side, "text": text_all,
                                  "sender": "", "bbox": bbox})

        # ---- loose rows: time / system / recall / nickname / quote ----
        loose.sort(key=lambda l: (l["rect"][1], l["rect"][0]))
        for ln in loose:
            text = ln["text"].strip()
            if not text:
                continue
            x0, y0, x1, y1 = ln["rect"]
            cx = (x0 + x1) / 2.0
            side = "left" if cx < (self.rect[2] / 2.0) else "right"
            bbox = (x0, y0, x1, y1)
            if _TIME_RE.search(text):
                items.append({"kind": "time", "side": side, "text": text,
                              "sender": "", "bbox": bbox})
                continue
            if _RECALL_RE.search(text):
                items.append({"kind": "recall", "side": side, "text": text,
                              "sender": "", "bbox": bbox})
                continue
            if _SYSTEM_RE.search(text):
                items.append({"kind": "system", "side": side, "text": text,
                              "sender": "", "bbox": bbox})
                continue
            # quote row: sits just below any bubble (own or other) and overlaps it
            quote = False
            for c in comps:
                if c["kind"] == "avatar":
                    continue
                if 0 <= y0 - c["y1"] <= 18 and (x0 < c["x1"] and x1 > c["x0"]):
                    quote = True
                    break
            if quote:
                continue
            # nickname row: just above a left message card, sharing its left edge
            attached = False
            for it in items:
                ix0, iy0, ix1, iy1 = it["bbox"]
                if (it["side"] == "left" and it["kind"] in ("text", "file", "image")
                        and 0 <= iy0 - y1 <= 32 and abs(x0 - ix0) <= 26):
                    if not it.get("sender"):
                        it["sender"] = text
                    attached = True
                    break
            if attached:
                continue
            if side == "right":
                continue  # own message fragment / own system text
            # leftover left row: only enqueue business-looking text
            if re.search(r"\d{5,}", text) or len(_norm(text)) >= 2:
                items.append({"kind": "text", "side": side, "text": text,
                              "sender": "", "bbox": bbox})

        items.sort(key=lambda it: (it["bbox"][1], it["bbox"][0]))
        # stable keys (identical texts may appear twice in one frame -> ordinal)
        occ = {}
        for it in items:
            if it["kind"] == "text":
                base = "text|%s|%s" % (it["side"], _txt_key(it["text"]))
            elif it["kind"] == "file":
                base = "file|%s|%s" % (it["side"], _txt_key(it.get("filename") or it.get("text", "")))
            elif it["kind"] == "image":
                base = "image|%s|%d|%d" % (it["side"], it["bbox"][2] - it["bbox"][0],
                                            it["bbox"][3] - it["bbox"][1])
            elif it["kind"] == "recall":
                base = "recall|%s|%s" % (it["side"], _txt_key(it["text"]))
            elif it["kind"] == "time":
                base = "time|%s" % _txt_key(it["text"])
            else:
                base = "%s|%s|%s" % (it["kind"], it["side"], _txt_key(it["text"]))
            n = occ.get(base, 0)
            occ[base] = n + 1
            it["key"] = "%s#%d" % (base, n)
            it["text_norm"] = _norm(it.get("text") or it.get("filename") or "")
        return items

    def scan_message_area(self, img):
        """Full pipeline: crop -> OCR/segment -> items. Returns (items, crop, hash)."""
        crop, off = self.message_area(img)
        h = self.region_hash(crop)
        items = self.build_items(crop, off)
        return items, crop, h

    # ---------------- conversation check / switch ----------------
    def read_header_text(self, img=None):
        if img is None:
            img = self.capture()
        lines = self.ocr_region(img, self._area("chat_header"))
        lines.sort(key=lambda l: (l["rect"][1], l["rect"][0]))
        return " ".join(l["text"] for l in lines)

    def verify_conversation(self, img=None, expected=None):
        expected = expected or self.group
        try:
            header = self.read_header_text(img)
        except Exception as exc:  # noqa: BLE001
            self.log("conv.header_error", error=str(exc))
            return False, ""
        ok = _norm(expected) and _norm(expected) in _norm(header)
        return bool(ok), header

    def _click(self, wx, wy, hold_ms=40):
        self.activate()
        if not self.rect:
            raise WindowNotReady("no rect")
        sx = self.rect[0] + int(wx)
        sy = self.rect[1] + int(wy)
        user32.SetCursorPos(sx, sy)
        time.sleep(0.06)
        user32.mouse_event(0x0002, 0, 0, 0, None)  # LEFTDOWN
        time.sleep(hold_ms / 1000.0)
        user32.mouse_event(0x0004, 0, 0, 0, None)  # LEFTUP

    def _key_tap(self, vk, scan=None, up_delay_ms=25):
        """Press+release a virtual key (proper VK+scan so dialogs/combos work)."""
        if scan is None:
            scan = int(user32.MapVirtualKeyW(vk, 0))
        arr = (INPUT * 2)()
        arr[0].type = 1
        arr[0].u.ki = KEYBDINPUT(vk & 0xFFFF, scan & 0xFFFF, 0, 0, None)
        arr[1].type = 1
        arr[1].u.ki = KEYBDINPUT(vk & 0xFFFF, scan & 0xFFFF, 0x0002, 0, None)
        user32.SendInput(2, arr, ctypes.sizeof(INPUT))
        time.sleep(up_delay_ms / 1000.0)

    def _key_combo(self, mod_vk, vk):
        """Press mod+vk with real vk+scan codes (Ctrl+A / Ctrl+V style)."""
        mod_scan = int(user32.MapVirtualKeyW(mod_vk, 0))
        scan = int(user32.MapVirtualKeyW(vk, 0))
        keys = [(mod_vk, mod_scan, 0), (vk, scan, 0),
                (vk, scan, 0x0002), (mod_vk, mod_scan, 0x0002)]
        arr = (INPUT * len(keys))()
        for i, (kv, sc, fl) in enumerate(keys):
            arr[i].type = 1
            arr[i].u.ki = KEYBDINPUT(kv & 0xFFFF, sc & 0xFFFF, fl, 0, None)
        user32.SendInput(len(keys), arr, ctypes.sizeof(INPUT))
        time.sleep(0.05)

    def _type_unicode(self, text, delay_ms=None, inter_key_ms=2):
        """Type text as SendInput Unicode key events (never Ctrl+V).

        A per-character delay is required: WeChat/Qt drops characters when
        Unicode key events arrive in one burst (observed: "BRIDGE-E2E-OK"
        turned into "BRIDGE--2E--K" with an 8ms burst delay).
        """
        delay = self.type_delay_ms if delay_ms is None else delay_ms
        n = 0
        for ch in text:
            if ch in "\r\n":
                self._key_tap(0x0D)
                time.sleep(0.03)
                continue
            code = ord(ch)
            if code > 0xFFFF:
                code -= 0x10000
                units = [0xD800 + (code >> 10), 0xDC00 + (code & 0x3FF)]
            else:
                units = [code]
            for unit in units:
                sent = self._send_char_unit(unit)
                if sent != 2:
                    self._type_failures += 1
                time.sleep(inter_key_ms / 1000.0)
            n += 1
            time.sleep(delay / 1000.0)
        return n

    @staticmethod
    def _send_char_unit(unit):
        arr = (INPUT * 2)()
        arr[0].type = 1
        arr[0].u.ki = KEYBDINPUT(0, unit & 0xFFFF, 0x0004, 0, None)
        arr[1].type = 1
        arr[1].u.ki = KEYBDINPUT(0, unit & 0xFFFF, 0x0004 | 0x0002, 0, None)
        return int(user32.SendInput(2, arr, ctypes.sizeof(INPUT)))

    def input_has_content(self, img=None):
        """Detect a non-empty input box.

        Two signals (verified live):
          * the 发送 button turns green when the input box has content
            (text draft or a staged attachment preview);
          * dark text pixels in the single-line input area.
        """
        try:
            if img is None:
                img = self.capture()
            w, h = img.size
            # send button area: bottom-right, above the icon row
            try:
                btn, _ = self.crop(img, (max(0, w - 160), max(0, h - 100),
                                         w - 4, max(0, h - 12)))
                arr = np.asarray(btn.convert("RGB"), dtype=np.int16)
                green = ((arr[:, :, 1] > 170)
                         & ((arr[:, :, 1] - arr[:, :, 0]) > 35)
                         & ((arr[:, :, 1] - arr[:, :, 2]) > 35))
                if float(green.mean()) > 0.02:
                    return True
            except Exception:
                pass
            sub, _ = self.crop(img, self._area("input_text"))
            arr = np.asarray(sub.convert("L"), dtype=np.uint8)
            if arr.size == 0:
                return False
            dark = int((arr < 110).sum())
            return dark > 25
        except Exception:
            return True  # fail closed: do not type blindly

    def ensure_empty_input(self):
        """Clear the input box. Returns True if empty; never send on top of junk."""
        if not self.input_has_content():
            return True
        self._click(*self._area("input_box"))
        time.sleep(0.05)
        self._key_combo(0x11, 0x41)   # Ctrl+A
        time.sleep(0.05)
        self._key_tap(0x08)           # Backspace
        time.sleep(0.08)
        if not self.input_has_content():
            return True
        # second attempt: Delete
        self._key_combo(0x11, 0x41)
        time.sleep(0.05)
        self._key_tap(0x2E)           # Delete
        time.sleep(0.08)
        return not self.input_has_content()

    def open_by_list(self, expected):
        """Best effort: back to the chat list, OCR the rows, click the row whose
        text contains `expected`, then verify the header. Never sends anything
        if the header does not match afterwards."""
        back = self._area("back_btn")
        self._click(*back)
        time.sleep(0.5)
        img = self.capture()
        lines = self.ocr_region(img, self._area("chat_list"))
        want = _norm(expected)
        hits = []
        for ln in lines:
            if want and want in _norm(ln["text"]):
                hits.append(ln)
        if not hits:
            self.log("conv.switch_row_not_found", expected=expected,
                     n_lines=len(lines))
            return False, ""
        hits.sort(key=lambda l: l["rect"][1])
        target = hits[0]
        x0, y0, x1, y1 = target["rect"]
        cx = int(min(max((_norm(expected) and x0), 120), self.rect[2] - 30))
        cy = int((y0 + y1) / 2)
        self._click(cx, cy)
        time.sleep(0.6)
        return self.verify_conversation(expected=expected)

    def switch_to_conversation(self, expected=None):
        expected = expected or self.group
        ok, header = self.verify_conversation(expected=expected)
        if ok:
            return True, header
        self.log("conv.switch_start", expected=expected, header=header[:120])
        ok, header = self.open_by_list(expected)
        if not ok:
            # perhaps we were already on the list: try clicking a row directly
            ok, header = self.open_by_list(expected)
        self.log("conv.switch_result", ok=ok, header=header[:120])
        return ok, header

    # ---------------- dialog helper ----------------
    def _find_dialog(self):
        """Find the native file dialog. Owned windows have parent != 0, so do
        NOT filter on parent here (the dialog is owned by the WeChat main window)."""
        for hwnd, pid, cls, title, visible, rect, parent in _enum_windows():
            if not visible or not rect or rect[2] < 200 or rect[3] < 60:
                continue
            if cls == "#32770" and any(k in title for k in ("选择", "打开", "另存为", "文件")):
                return hwnd, title, rect
        return None

    def close_stray_dialogs(self, attempts=2):
        """Close a leftover native file dialog; returns True when none is open."""
        for _ in range(attempts):
            dlg = self._find_dialog()
            if not dlg:
                return True
            hwnd_dlg, title, rect = dlg
            self.log("send.close_stray_dialog", title=title)
            # a modal common dialog blocks SetForegroundWindow on its owner, so
            # close it directly; WM_CLOSE is what the Cancel button does
            user32.PostMessageW(hwnd_dlg, 0x0010, 0, 0)  # WM_CLOSE
            time.sleep(0.5)
            if self._find_dialog():
                # last resort: click the Cancel button (bottom-right of the dialog)
                x = rect[0] + rect[2] - 80
                y = rect[1] + rect[3] - 35
                user32.SetCursorPos(x, y)
                time.sleep(0.05)
                user32.mouse_event(0x0002, 0, 0, 0, None)
                time.sleep(0.05)
                user32.mouse_event(0x0004, 0, 0, 0, None)
                time.sleep(0.5)
        return self._find_dialog() is None

    # ---------------- sending ----------------
    def input_ocr_lines(self, img=None):
        """OCR the whole (possibly grown) input box area.

        Returns a list of dicts [{text, y}] sorted top->bottom, or None when OCR
        itself failed (callers must fail closed: never press Enter unverified).
        """
        try:
            if img is None:
                img = self.capture()
            lines = self.ocr_region(img, self._area("input_ocr"))
            out = [{"text": l["text"].strip(), "y": l["rect"][1]}
                   for l in lines if l["text"].strip()]
            out.sort(key=lambda l: l["y"])
            return out
        except Exception as exc:  # noqa: BLE001
            self.log("send.input_ocr_error", error=str(exc))
            return None

    @staticmethod
    def _verify_input_text(expected, before_lines, after_lines):
        """Compare OCR of the input box after typing against the intended text.

        Only characters that were not present before typing are considered, so
        the chat history above the grown input box does not pollute the check.
        """
        import difflib
        if after_lines is None:
            return False, ""
        before = set(_norm(l["text"]) for l in (before_lines or []))
        new_texts = [_norm(l["text"]) for l in after_lines
                     if _norm(l["text"]) and _norm(l["text"]) not in before]
        joined = "".join(new_texts)
        want = _norm(expected)
        if not want:
            return True, joined
        if want == joined or want in joined:
            return True, joined
        ratio = difflib.SequenceMatcher(None, want, joined).ratio()
        if ratio >= 0.92:
            return True, joined
        # long text may wrap across lines: start and end must both be present
        if len(want) > 30 and want[:12] in joined and want[-10:] in joined and ratio >= 0.85:
            return True, joined
        return False, joined

    def _clear_input(self):
        self._click(*self._area("input_box"))
        time.sleep(0.06)
        self._key_combo(0x11, 0x41)   # Ctrl+A
        time.sleep(0.06)
        self._key_tap(0x08)           # Backspace
        time.sleep(0.12)
        return not self.input_has_content()

    # ---------------- clipboard (verified paste path) ----------------
    def _clipboard_open(self, attempts=8):
        for _ in range(attempts):
            if user32.OpenClipboard(None):
                return True
            time.sleep(0.03)
        return False

    def _get_clipboard_text(self):
        if not self._clipboard_open():
            return None
        try:
            handle = user32.GetClipboardData(13)  # CF_UNICODETEXT
            if not handle:
                return None
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                return None
            try:
                return ctypes.wstring_at(ptr)
            finally:
                kernel32.GlobalUnlock(handle)
        except Exception:
            return None
        finally:
            try:
                user32.CloseClipboard()
            except Exception:
                pass

    def _set_clipboard_text(self, text):
        if not self._clipboard_open():
            return False
        try:
            buf = ctypes.create_unicode_buffer(text)
            size = ctypes.sizeof(buf)
            handle = kernel32.GlobalAlloc(0x0042, size)  # GMEM_MOVEABLE|GMEM_ZEROINIT
            if not handle:
                return False
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                return False
            try:
                ctypes.memmove(ptr, buf, size)
            finally:
                kernel32.GlobalUnlock(handle)
            user32.EmptyClipboard()
            if not user32.SetClipboardData(13, handle):
                return False
            return True
        except Exception as exc:  # noqa: BLE001
            self.log("clipboard.set_error", error=str(exc))
            return False
        finally:
            try:
                user32.CloseClipboard()
            except Exception:
                pass

    @staticmethod
    def _same_text(a, b):
        if a is None or b is None:
            return False
        norm = lambda s: s.replace("\r\n", "\n").replace("\r", "\n").strip()
        return norm(a) == norm(b)

    def _paste_text(self, text, clear_first=False):
        """Paste text via clipboard + Ctrl+V.

        SendInput Unicode per-character injection is NOT reliable in this
        environment (WeChat and even Notepad drop characters when a Chinese
        IME is active; probe/wx-input.ps1 reproduces "BRIDGE-E2E-OK" ->
        "BRIDGE--2E--K"). Clipboard + Ctrl+V with correct vk+scan key events is
        exact in tests (incl. Chinese), so it is used as the primary text input
        method; the caller always verifies the input box content afterwards.
        """
        if clear_first:
            self._clear_input()
        if not self._set_clipboard_text(text):
            return False
        self._click(*self._area("input_box"))
        time.sleep(0.1)
        self._key_combo(0x11, 0x56)  # Ctrl+V
        time.sleep(0.35)
        return True

    def _read_input_via_clipboard(self):
        """Select all + copy in the input box and read the clipboard back."""
        self._key_combo(0x11, 0x41)  # Ctrl+A
        time.sleep(0.08)
        self._key_combo(0x11, 0x43)  # Ctrl+C
        time.sleep(0.18)
        return self._get_clipboard_text()

    def send_text(self, text, expected=None, verify=True):
        """Send one text message via clipboard paste + Enter (verified).

        1) activate WeChat, 2) OCR-verify chat header, 3) clear input box,
        4) clipboard+Ctrl+V paste, 5) select-all/copy and compare the exact
        clipboard text with the intended text (retry once), 6) Enter,
        7) capture + OCR the message area bottom for a final check.
        Raises SendBlocked when the conversation is wrong, the input can't be
        cleared, or the pasted content cannot be verified (never send garbage).
        """
        expected = expected or self.group
        if not text:
            return True
        self.ensure_geometry()
        ok_conf, header = self.switch_to_conversation(expected)
        if not ok_conf:
            raise SendBlocked("会话头不是「%s」（当前：%s），拒绝发送" % (expected, header[:80]))
        if not self.activate():
            raise SendBlocked("无法把微信窗口切到前台")
        self.close_stray_dialogs()
        if not self.ensure_empty_input():
            raise SendBlocked("输入框无法清空，拒绝发送")
        prev_clip = self._get_clipboard_text()
        verified_input = False
        actual = ""
        try:
            for attempt in range(2):
                if not self._paste_text(text, clear_first=(attempt > 0)):
                    actual = "clipboard-set-failed"
                    break
                actual = self._read_input_via_clipboard() or ""
                verified_input = self._same_text(actual, text)
                self.log("send.input_check", attempt=attempt, ok=verified_input,
                         expected_chars=len(text), actual=actual[:160])
                if verified_input:
                    break
                if not self._clear_input():
                    raise SendBlocked("输入框内容校验失败且无法清空，拒绝发送")
            if not verified_input:
                self._clear_input()
                raise SendBlocked("输入框内容与待发文本不一致（读回=%r），拒绝发送"
                                  % actual[:120])
            # collapse the selection to the end before submitting
            self._key_tap(0x23)  # End
            time.sleep(0.05)
            self._key_tap(0x0D)  # Enter
            time.sleep(self.after_send_wait_ms / 1000.0)
        finally:
            if prev_clip is not None:
                self._set_clipboard_text(prev_clip)
        verified = None
        if verify:
            verified = self.verify_sent_text(text)
        self.log("send.text", chars=len(text), input_verified=verified_input,
                 verified=verified, conv=expected)
        return verified if verify else True

    def verify_sent_text(self, text):
        try:
            img = self.capture()
            area = self._area("bottom_verify")
            lines = self.ocr_region(img, area)
            shown = [l["text"] for l in lines]
            ok = self._text_visible(text, shown)
            self.log("send.sent_check", ok=ok, expected=_norm(text)[:60],
                     seen=" | ".join(_norm(s) for s in shown)[:180])
            return ok
        except Exception as exc:  # noqa: BLE001
            self.log("send.verify_error", error=str(exc))
            return None

    @staticmethod
    def _text_visible(text, shown_lines):
        """Strict OCR match for the sent bubble: normalized equality or ratio>=0.9."""
        import difflib
        probe = _norm(text)
        if not probe:
            return True
        best = 0.0
        for line in shown_lines:
            shown = _norm(line)
            if not shown:
                continue
            if probe == shown:
                return True
            if probe in shown:
                return True
            best = max(best, difflib.SequenceMatcher(None, probe, shown).ratio())
        return best >= 0.9

    def send_file(self, path, expected=None, verify=True):
        """Send an image/PDF through the toolbar 发送文件 button + native dialog."""
        expected = expected or self.group
        path = os.path.abspath(path)
        if not os.path.isfile(path):
            raise SendBlocked("文件不存在：%s" % path)
        self.ensure_geometry()
        ok_conf, header = self.switch_to_conversation(expected)
        if not ok_conf:
            raise SendBlocked("会话头不是「%s」（当前：%s），拒绝发送文件" % (expected, header[:80]))
        if not self.activate():
            raise SendBlocked("无法把微信窗口切到前台")
        # a modal file dialog left over from a previous run would eat our clicks
        self.close_stray_dialogs()
        if not self.ensure_empty_input():
            raise SendBlocked("输入框无法清空，拒绝发送文件")
        prev_clip = self._get_clipboard_text()
        hwnd_dlg = 0
        title = ""
        before_img = None
        try:
            try:
                before_img = self.capture()
            except Exception:
                before_img = None
            self._click(*self._area("send_file_btn"))
            deadline = time.time() + 5.0
            dlg = None
            while time.time() < deadline:
                dlg = self._find_dialog()
                if dlg:
                    break
                time.sleep(0.15)
            if not dlg:
                raise SendBlocked("未弹出「选择文件」对话框")
            hwnd_dlg, title, rect = dlg
            self.log("send.file_dialog", hwnd=hwnd_dlg, title=title)
            # focus the native dialog itself so our paste lands in its filename edit
            user32.SetForegroundWindow(hwnd_dlg)
            time.sleep(0.3)
            verified_input = False
            actual = ""
            for attempt in range(2):
                # filename edit may contain a default name: select all + delete
                self._key_combo(0x11, 0x41)
                time.sleep(0.05)
                self._key_tap(0x08)
                time.sleep(0.05)
                self._set_clipboard_text(path)
                self._key_combo(0x11, 0x56)  # Ctrl+V
                time.sleep(0.35)
                actual = self._read_input_via_clipboard() or ""
                verified_input = self._same_text(actual, path)
                self.log("send.file_input_check", attempt=attempt,
                         ok=verified_input, actual=actual[:160])
                if verified_input:
                    break
            if not verified_input:
                user32.PostMessageW(hwnd_dlg, 0x0010, 0, 0)  # WM_CLOSE (cancel)
                time.sleep(0.3)
                raise SendBlocked("文件对话框路径校验失败（读回=%r）" % actual[:120])
            self._key_tap(0x0D)
            time.sleep(0.5)
            # if the dialog is still there the path was rejected: abort, never press
            # a second Enter into an unknown focused control
            if self._find_dialog():
                self._key_tap(0x1B)  # Esc (dialog has focus)
                time.sleep(0.2)
                raise SendBlocked("文件对话框未关闭（路径被拒绝？）")
            # the destroyed native dialog does not always hand the foreground back
            # to WeChat; without this the second Enter can go nowhere
            self.activate()
            time.sleep(0.3)
            self._key_tap(0x0D)  # send the attached file
            time.sleep(max(0.8, self.after_send_wait_ms / 1000.0))
        finally:
            if prev_clip is not None:
                self._set_clipboard_text(prev_clip)
        verified = None
        if verify:
            verified = self.verify_sent_file(path, before_img=before_img)
        self.log("send.file", name=os.path.basename(path), verified=verified, conv=expected)
        return verified if verify else True

    def _count_self_images(self, img):
        try:
            crop, off = self.message_area(img)
            comps = self.segment(crop, off)
            n = 0
            for c in comps:
                if c["side"] != "right" or c["kind"] in ("avatar", "green-self"):
                    continue
                if c["w"] >= 50 and c["h"] >= 50:
                    n += 1
            return n
        except Exception:
            return -1

    def verify_sent_file(self, path, before_img=None):
        ext = os.path.splitext(path)[1].lower()
        is_image = ext in (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp")
        deadline = time.time() + 4.0
        last = None
        before_hash = None
        if before_img is not None:
            try:
                bc, _ = self.message_area(before_img)
                before_hash = self.region_hash(bc)
            except Exception:
                before_hash = None
        while True:
            try:
                img = self.capture()
                if is_image and before_hash is not None:
                    # a sent image disappears from the input box and the message
                    # area changes; this is stable even when old images scroll out
                    ac, _ = self.message_area(img)
                    after_hash = self.region_hash(ac)
                    staged = self.input_has_content(img)
                    last = (not staged) and (after_hash != before_hash)
                    if last:
                        self.log("send.sent_file_check", ok=True, kind="image",
                                 staged=staged)
                        return True
                else:
                    area = self._area("bottom_verify")
                    lines = self.ocr_region(img, area)
                    base = os.path.splitext(os.path.basename(path))[0]
                    last = self._text_visible(base, [l["text"] for l in lines])
                    if last:
                        self.log("send.sent_file_check", ok=True, kind="file",
                                 expected=_norm(base)[:60],
                                 seen=" | ".join(_norm(l["text"]) for l in lines)[:180])
                        return True
            except Exception as exc:  # noqa: BLE001
                self.log("send.verify_file_error", error=str(exc))
                return None
            if time.time() >= deadline:
                self.log("send.sent_file_check", ok=bool(last),
                         kind="image" if is_image else "file",
                         expected=_norm(os.path.basename(path))[:60])
                return bool(last)
            time.sleep(0.3)


# --------------------------------------------------------------------------
# CLI self tests
# --------------------------------------------------------------------------
def _load_cfg(path=None):
    here = os.path.dirname(os.path.abspath(__file__))
    path = path or os.path.join(here, "config.json")
    with io.open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _main(argv):
    cmd = argv[1] if len(argv) > 1 else "info"
    cfg = _load_cfg()
    io_ = WeChatIO(cfg, logger=lambda e, **kw: print("[io]", e, kw))

    if cmd == "info":
        w = io_.find_main_window()
        print(json.dumps({"dpi": _DPI_MODE, "window": w}, ensure_ascii=False, indent=1))
        if w:
            print("rect=%r target=%r" % (w["rect"], io_.target_rect))
        return 0

    if cmd == "scan":
        if len(argv) > 2 and argv[2].lower().endswith((".png", ".jpg", ".bmp")):
            img = Image.open(argv[2]).convert("RGB")
            # pretend the image size is the window rect for coordinate maths
            io_.rect = (0, 0, img.size[0], img.size[1])
            io_.hwnd = 0
        else:
            io_.ensure_geometry()
            img = io_.capture()
        items, crop, h = io_.scan_message_area(img)
        print(json.dumps({"hash": h, "n": len(items),
                          "items": [{k: v for k, v in it.items() if k != "text_norm"}
                                    for it in items]},
                         ensure_ascii=False, indent=1))
        return 0

    if cmd == "ocr":
        img = Image.open(argv[2]).convert("RGB")
        crop, _ = io_.message_area(img) if img.size[0] >= 500 else (img, (0, 0))
        print(json.dumps(io_.ocr_image(crop), ensure_ascii=False, indent=1))
        return 0

    if cmd == "rect":
        print(json.dumps(io_.ensure_geometry(), ensure_ascii=False))
        return 0

    if cmd == "minimize":
        io_.ensure_geometry()
        user32.ShowWindow(io_.hwnd, 6)  # SW_MINIMIZE
        time.sleep(0.8)
        res = io_.ensure_geometry()
        print(json.dumps(res, ensure_ascii=False))
        return 0 if res["ok"] else 1

    print("usage: python wechat_io.py info|scan [img]|ocr <img>|rect|minimize")
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
