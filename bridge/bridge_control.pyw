# -*- coding: utf-8 -*-
"""DSH 微信桥接控制台（一键启停：Umi-OCR + 看门狗 + bridge.py）。

用 DSH 自带 pythonw.exe 启动；双击「桥接控制台.vbs」或桌面快捷方式。
"""
import ctypes
import json
import os
import socket
import subprocess
import time
import traceback
import tkinter as tk
from tkinter import ttk, messagebox

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "state")
HEALTH = os.path.join(STATE, "health.json")
WATCHDOG_PID = os.path.join(STATE, "watchdog.pid")
LOG_DIR = os.path.join(STATE, "logs")
ERROR_LOG = os.path.join(STATE, "control-error.log")
NO_WINDOW = 0x08000000
UMI_EXE = r"C:\Program Files\Umi-OCR\Umi-OCR.exe"
UMI_PORT = 1224

BG = "#ffffff"
FG = "#1f2328"
MUTED = "#6b7280"
GREEN = "#0a7d32"
RED = "#b42318"
AMBER = "#b45309"


def process_alive(pid):
    if not pid:
        return False
    try:
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
        if h:
            ctypes.windll.kernel32.CloseHandle(h)
            return True
    except Exception:
        pass
    return False


def port_open(port):
    try:
        s = socket.create_connection(("127.0.0.1", int(port)), 0.4)
        s.close()
        return True
    except Exception:
        return False


def read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def read_pid(path):
    try:
        with open(path, "r", encoding="ascii") as f:
            return int((f.read() or "").strip().splitlines()[0])
    except Exception:
        return 0


def run_hidden(args, timeout=40):
    return subprocess.run(args, cwd=HERE, creationflags=NO_WINDOW,
                          capture_output=True, text=True, timeout=timeout)


def log_line(text):
    try:
        os.makedirs(STATE, exist_ok=True)
        with open(ERROR_LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + text + "\n")
    except Exception:
        pass


class App(object):
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("DSH 微信桥接控制台")
        self.root.geometry("500x390")
        self.root.minsize(470, 370)
        self.root.configure(bg=BG)
        self.root.protocol("WM_DELETE_WINDOW", self.close_ui)
        self._build()
        self.refresh()

    def _build(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("TButton", font=("Microsoft YaHei UI", 9), padding=6)
        style.configure("Big.TButton", font=("Microsoft YaHei UI", 10, "bold"), padding=9)

        tk.Label(self.root, text="DSH ⇄ 微信桥接", bg=BG, fg=FG,
                 font=("Microsoft YaHei UI", 14, "bold")).pack(anchor="w", padx=16, pady=(14, 2))
        tk.Label(self.root, text="一键启动/停止 = Umi-OCR + 看门狗 + bridge.py；停止后不会再自动重启。",
                 bg=BG, fg=MUTED, font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=16)

        self.status_var = tk.StringVar(value="读取状态…")
        self.status = tk.Label(self.root, textvariable=self.status_var, bg=BG, fg=MUTED,
                               font=("Microsoft YaHei UI", 10, "bold"), anchor="w", justify="left")
        self.status.pack(fill="x", padx=16, pady=(12, 2))

        self.umi_var = tk.StringVar(value="")
        self.umi_label = tk.Label(self.root, textvariable=self.umi_var, bg=BG, fg=MUTED,
                                  font=("Microsoft YaHei UI", 9, "bold"), anchor="w")
        self.umi_label.pack(fill="x", padx=16)

        self.detail_var = tk.StringVar(value="")
        tk.Label(self.root, textvariable=self.detail_var, bg=BG, fg=MUTED,
                 font=("Consolas", 8), anchor="w", justify="left").pack(fill="x", padx=16, pady=(4, 0))

        frame = tk.Frame(self.root, bg=BG)
        frame.pack(fill="x", padx=16, pady=(16, 4))
        self.btn_all_stop = ttk.Button(frame, text="一键停止（桥接+OCR）", style="Big.TButton",
                                       command=self.stop_all_click)
        self.btn_all_stop.pack(side="left")
        self.btn_all_start = ttk.Button(frame, text="一键启动（OCR+桥接）", style="Big.TButton",
                                        command=self.start_all)
        self.btn_all_start.pack(side="left", padx=(8, 0))

        frame2 = tk.Frame(self.root, bg=BG)
        frame2.pack(fill="x", padx=16, pady=(0, 4))
        self.btn_stop = ttk.Button(frame2, text="仅停止桥接", command=self.stop_bridge_click)
        self.btn_stop.pack(side="left")
        self.btn_start = ttk.Button(frame2, text="仅启动桥接", command=self.start_bridge_only)
        self.btn_start.pack(side="left", padx=(6, 0))
        ttk.Button(frame2, text="打开日志", command=self.open_logs).pack(side="left", padx=(6, 0))
        ttk.Button(frame2, text="刷新", command=self.refresh).pack(side="left", padx=(6, 0))

        frame3 = tk.Frame(self.root, bg=BG)
        frame3.pack(fill="x", padx=16, pady=(0, 4))
        ttk.Button(frame3, text="退出控制台", command=self.close_ui).pack(side="left")

        tk.Label(self.root,
                 text="直接杀 python 进程会被看门狗拉起；紧急情况用本页停止，或运行 stop-bridge.cmd。\n"
                      "Umi-OCR 启动可能需 5–15 秒；未就绪时桥接会 OCR 失败。",
                 bg=BG, fg=MUTED, wraplength=460, justify="left",
                 font=("Microsoft YaHei UI", 8)).pack(anchor="w", padx=16, pady=(8, 10))

    # ---------------- state ----------------
    def _state(self):
        health = read_json(HEALTH)
        pid = health.get("pid")
        wd = read_pid(WATCHDOG_PID)
        bridge_alive = process_alive(pid)
        wd_alive = process_alive(wd)
        ts = health.get("ts") or 0
        age = time.time() - float(ts) if ts else 9999
        dsh = health.get("dsh") or {}
        umi = port_open(UMI_PORT)
        return health, pid, wd, bridge_alive, wd_alive, age, dsh, umi

    def refresh(self):
        try:
            health, pid, wd, bridge_alive, wd_alive, age, dsh, umi = self._state()
            if bridge_alive and age <= 20:
                self.status_var.set("● 桥接运行中   PID %s   看门狗 %s" % (pid, wd if wd_alive else "未运行"))
                self.status.config(fg=GREEN)
            elif bridge_alive:
                self.status_var.set("● 桥接进程在，但状态 %d 秒未更新（可能卡住）" % int(age))
                self.status.config(fg=AMBER)
            else:
                self.status_var.set("● 桥接已停止" + ("（看门狗仍在）" if wd_alive else ""))
                self.status.config(fg=RED)

            if umi:
                self.umi_var.set("● Umi-OCR 运行中（127.0.0.1:%d）" % UMI_PORT)
                self.umi_label.config(fg=GREEN)
            else:
                self.umi_var.set("● Umi-OCR 未运行（OCR 不可用）")
                self.umi_label.config(fg=RED)

            self.btn_all_stop.config(state="normal" if (bridge_alive or umi) else "disabled")
            self.btn_all_start.config(state="disabled" if (bridge_alive and umi) else "normal")
            self.btn_stop.config(state="normal" if bridge_alive else "disabled")
            self.btn_start.config(state="disabled" if bridge_alive else "normal")

            win = health.get("window") or "-"
            ocr = health.get("ocr") or "-"
            dsh_txt = "DSH:%s/%s" % ("已连" if dsh.get("connected") else "未连",
                                     dsh.get("title") or "-")
            last = time.strftime("%H:%M:%S", time.localtime(age and (time.time() - age) or 0))
            self.detail_var.set("窗口 %s\nOCR %s   %s\n最近扫描 %s" % (win, ocr, dsh_txt, last))
        except Exception as exc:
            log_line("refresh: %r" % exc)
        self.root.after(2000, self.refresh)

    # ---------------- Umi-OCR ----------------
    def start_umi(self):
        if port_open(UMI_PORT):
            return True
        if not os.path.isfile(UMI_EXE):
            log_line("umi missing: %s" % UMI_EXE)
            return False
        try:
            proc = subprocess.Popen([UMI_EXE, "--hide"], cwd=os.path.dirname(UMI_EXE),
                                    creationflags=NO_WINDOW,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            log_line("umi start pid=%s" % proc.pid)
        except Exception as exc:
            log_line("umi start failed: %r" % exc)
            return False
        for _ in range(30):
            time.sleep(0.5)
            if port_open(UMI_PORT):
                return True
        if proc.poll() is not None:
            try:
                subprocess.Popen([UMI_EXE], cwd=os.path.dirname(UMI_EXE),
                                 creationflags=NO_WINDOW,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as exc:
                log_line("umi fallback failed: %r" % exc)
                return False
            for _ in range(30):
                time.sleep(0.5)
                if port_open(UMI_PORT):
                    return True
        return False

    def stop_umi(self):
        try:
            run_hidden(["taskkill", "/IM", "Umi-OCR.exe", "/T", "/F"], timeout=20)
        except Exception as exc:
            log_line("umi stop: %r" % exc)
        for _ in range(20):
            time.sleep(0.3)
            if not port_open(UMI_PORT):
                return True
        return not port_open(UMI_PORT)

    # ---------------- bridge ----------------
    def start_bridge_only(self):
        try:
            run_hidden(["cmd", "/c", os.path.join(HERE, "start-bridge.cmd")], timeout=30)
        except Exception as exc:
            log_line("start bridge: %r" % exc)
            messagebox.showerror("启动失败", "启动命令失败：\n%s" % exc)
            return
        self.status_var.set("正在启动桥接…")
        self.root.after(5000, self.refresh)

    def stop_bridge(self, confirm=True):
        if confirm and not messagebox.askyesno(
                "停止桥接", "确定停止微信桥接（含看门狗）？\n停止后微信消息不会自动处理。"):
            return False
        try:
            r = run_hidden(["cmd", "/c", os.path.join(HERE, "stop-bridge.cmd")], timeout=30)
            log_line("stop bridge rc=%s out=%s err=%s" % (r.returncode, r.stdout, r.stderr))
        except Exception as exc:
            log_line("stop bridge: %r" % exc)
        time.sleep(1.2)
        self.refresh()
        return True

    def stop_bridge_click(self):
        self.stop_bridge(confirm=True)

    # ---------------- all ----------------
    def start_all(self):
        self.btn_all_start.config(state="disabled")
        self.status_var.set("正在启动 Umi-OCR…")
        self.root.update_idletasks()
        if not self.start_umi():
            messagebox.showerror("启动失败",
                                 "Umi-OCR 未能在 30 秒内就绪。\n请手动打开：\n%s\n再点一次一键启动。" % UMI_EXE)
            self.refresh()
            return
        self.status_var.set("Umi-OCR 就绪，正在启动桥接…")
        self.root.update_idletasks()
        self.start_bridge_only()
        self.root.after(6000, self.refresh)

    def stop_all(self, confirm=True):
        if confirm and not messagebox.askyesno(
                "一键停止", "确定停止桥接（含看门狗）并关闭 Umi-OCR？\n停止后微信消息不会自动处理。"):
            return False
        self.stop_bridge(confirm=False)
        self.status_var.set("正在关闭 Umi-OCR…")
        self.root.update_idletasks()
        self.stop_umi()
        self.refresh()
        return True

    def stop_all_click(self):
        self.stop_all(confirm=True)

    def open_logs(self):
        try:
            os.startfile(LOG_DIR)
        except Exception as exc:
            log_line("open logs: %r" % exc)

    def close_ui(self):
        try:
            _, _, _, bridge_alive, wd_alive, _, _, umi = self._state()
        except Exception:
            bridge_alive = wd_alive = umi = False
        if bridge_alive or wd_alive or umi:
            ans = messagebox.askyesnocancel(
                "退出控制台",
                "桥接/Umi-OCR 仍在运行。\n\n"
                "是：停止桥接（含看门狗）和 Umi-OCR 后退出\n"
                "否：只退出控制台，它们继续运行\n取消：留在控制台")
            if ans is None:
                return
            if ans:
                self.stop_all(confirm=False)
                self.root.after(300, self.root.destroy)
                return
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    try:
        App().run()
    except Exception:
        log_line(traceback.format_exc())
        try:
            messagebox.showerror("桥接控制台", "控制台启动失败，见 state\\control-error.log")
        except Exception:
            pass
