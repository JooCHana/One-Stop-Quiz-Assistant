from __future__ import annotations

import ctypes
import json
import os
import queue
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageGrab


APP_NAME = "题库自动截图翻页"
BASE_DIR = Path(os.getenv("APPDATA", str(Path.home()))) / APP_NAME
CONFIG_FILE = BASE_DIR / "config.json"

VK_LWIN = 0x5B
VK_SHIFT = 0x10
VK_S = 0x53
VK_F8 = 0x77
VK_F9 = 0x78
VK_ESCAPE = 0x1B
KEYUP = 0x0002
MOUSE_LEFTDOWN = 0x0002
MOUSE_LEFTUP = 0x0004

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


def enable_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass


def key_down(vk: int) -> None:
    user32.keybd_event(vk, 0, 0, 0)


def key_up(vk: int) -> None:
    user32.keybd_event(vk, 0, KEYUP, 0)


def press_snipping_shortcut() -> None:
    key_down(VK_LWIN)
    key_down(VK_SHIFT)
    key_down(VK_S)
    time.sleep(0.08)
    key_up(VK_S)
    key_up(VK_SHIFT)
    key_up(VK_LWIN)


def mouse_drag(start: tuple[int, int], end: tuple[int, int], duration: float, stop_event: threading.Event) -> None:
    x1, y1 = start
    x2, y2 = end
    user32.SetCursorPos(x1, y1)
    time.sleep(0.08)
    user32.mouse_event(MOUSE_LEFTDOWN, 0, 0, 0, 0)
    steps = max(8, int(duration * 60))
    try:
        for index in range(1, steps + 1):
            if stop_event.is_set():
                break
            ratio = index / steps
            x = round(x1 + (x2 - x1) * ratio)
            y = round(y1 + (y2 - y1) * ratio)
            user32.SetCursorPos(x, y)
            time.sleep(duration / steps)
    finally:
        user32.mouse_event(MOUSE_LEFTUP, 0, 0, 0, 0)


def clipboard_sequence() -> int:
    return int(user32.GetClipboardSequenceNumber())


def mouse_click(position: tuple[int, int], stop_event: threading.Event) -> None:
    if stop_event.is_set():
        return
    user32.SetCursorPos(*position)
    if stop_event.wait(0.08):
        return
    user32.mouse_event(MOUSE_LEFTDOWN, 0, 0, 0, 0)
    try:
        stop_event.wait(0.05)
    finally:
        user32.mouse_event(MOUSE_LEFTUP, 0, 0, 0, 0)


@dataclass
class Settings:
    region: list[int] | None = None
    swipe_start: list[int] | None = None
    swipe_end: list[int] | None = None
    turn_mode: str = "swipe"
    next_position: list[int] | None = None
    page_count: int = 10
    first_number: int = 1
    snip_wait: float = 1.0
    page_wait: float = 1.2
    swipe_duration: float = 0.45
    output_dir: str = str(Path.home() / "Pictures" / "题库截图")

    @classmethod
    def load(cls) -> "Settings":
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            allowed = cls.__dataclass_fields__.keys()
            return cls(**{key: value for key, value in data.items() if key in allowed})
        except Exception:
            return cls()

    def save(self) -> None:
        BASE_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")


class DragOverlay:
    def __init__(self, parent: tk.Tk, title: str, help_text: str, callback, require_box: bool = True, point_mode: bool = False) -> None:
        self.parent = parent
        self.callback = callback
        self.require_box = require_box
        self.point_mode = point_mode
        self.start: tuple[int, int] | None = None
        self.rect = None

        self.window = tk.Toplevel(parent)
        self.window.attributes("-fullscreen", True)
        self.window.attributes("-topmost", True)
        self.window.attributes("-alpha", 0.28)
        self.window.configure(bg="black", cursor="crosshair")
        self.window.title(title)

        self.canvas = tk.Canvas(self.window, bg="black", highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.create_text(
            self.window.winfo_screenwidth() // 2,
            45,
            text=help_text + "\n按 Esc 取消",
            fill="white",
            font=("Microsoft YaHei UI", 18, "bold"),
            justify="center",
        )
        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_motion)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.window.bind("<Escape>", lambda _event: self.cancel())
        self.window.focus_force()

    def on_press(self, event) -> None:
        self.start = (event.x_root, event.y_root)
        if self.point_mode:
            return
        self.rect = self.canvas.create_rectangle(
            event.x, event.y, event.x, event.y, outline="#00e5ff", width=4
        )

    def on_motion(self, event) -> None:
        if self.start is None or self.rect is None:
            return
        x1 = self.start[0] - self.window.winfo_rootx()
        y1 = self.start[1] - self.window.winfo_rooty()
        self.canvas.coords(self.rect, x1, y1, event.x, event.y)

    def on_release(self, event) -> None:
        if self.start is None:
            return
        end = (event.x_root, event.y_root)
        if self.point_mode:
            self.window.destroy()
            self.parent.after(100, lambda: self.callback(end))
            return
        dx = abs(end[0] - self.start[0])
        dy = abs(end[1] - self.start[1])
        too_small = (dx < 20 or dy < 20) if self.require_box else (dx * dx + dy * dy < 80 * 80)
        if too_small:
            self.canvas.delete(self.rect)
            self.rect = None
            self.start = None
            return
        start = self.start
        self.window.destroy()
        self.parent.after(100, lambda: self.callback(start, end))

    def cancel(self) -> None:
        self.window.destroy()
        self.parent.deiconify()


class ScreenshotApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.settings = Settings.load()
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.key_state = {VK_F8: False, VK_F9: False, VK_ESCAPE: False}
        self.last_run_dir: Path | None = None

        self.root.title(APP_NAME)
        self.root.geometry("680x750")
        self.root.minsize(660, 720)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.option_add("*Font", ("Microsoft YaHei UI", 10))

        style = ttk.Style()
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass

        self.page_count = tk.StringVar(value=str(self.settings.page_count))
        self.first_number = tk.StringVar(value=str(self.settings.first_number))
        self.snip_wait = tk.StringVar(value=str(self.settings.snip_wait))
        self.page_wait = tk.StringVar(value=str(self.settings.page_wait))
        self.swipe_duration = tk.StringVar(value=str(self.settings.swipe_duration))
        self.output_dir = tk.StringVar(value=self.settings.output_dir)
        self.region_text = tk.StringVar()
        self.swipe_text = tk.StringVar()
        self.turn_mode = tk.StringVar(value=self.settings.turn_mode)
        self.next_text = tk.StringVar()
        self.status_text = tk.StringVar(value="准备就绪")
        self.progress_text = tk.StringVar(value="0 / 0")

        self.build_ui()
        self.refresh_descriptions()
        self.root.after(80, self.poll_events)
        self.root.after(80, self.poll_hotkeys)

    def build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=18)
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text=APP_NAME, font=("Microsoft YaHei UI", 20, "bold")).pack(anchor="w")
        ttk.Label(
            outer,
            text="用 Win + Shift + S 逐页截图，支持拖动翻页或点击下一题。",
            foreground="#555555",
        ).pack(anchor="w", pady=(3, 14))

        setup = ttk.LabelFrame(outer, text="一、设置位置", padding=12)
        setup.pack(fill="x")
        ttk.Button(setup, text="框选截图范围", command=self.select_region).grid(row=0, column=0, sticky="ew", padx=(0, 10))
        ttk.Label(setup, textvariable=self.region_text).grid(row=0, column=1, sticky="w")
        ttk.Button(setup, text="拖动设置翻页手势", command=self.select_swipe).grid(row=1, column=0, sticky="ew", padx=(0, 10), pady=(10, 0))
        ttk.Label(setup, textvariable=self.swipe_text).grid(row=1, column=1, sticky="w", pady=(10, 0))
        ttk.Button(setup, text="设置“下一题”位置", command=self.select_next).grid(row=2, column=0, sticky="ew", padx=(0, 10), pady=(10, 0))
        ttk.Label(setup, textvariable=self.next_text).grid(row=2, column=1, sticky="w", pady=(10, 0))
        modes = ttk.Frame(setup)
        modes.grid(row=3, column=0, columnspan=2, sticky="w", pady=(10, 0))
        ttk.Label(modes, text="翻页方式：").pack(side="left")
        self.mode_buttons = []
        for label, value in (("拖动翻页", "swipe"), ("点击下一题", "click")):
            button = ttk.Radiobutton(modes, text=label, value=value, variable=self.turn_mode, command=self.save_turn_mode)
            button.pack(side="left", padx=(0, 16))
            self.mode_buttons.append(button)
        setup.columnconfigure(1, weight=1)

        options = ttk.LabelFrame(outer, text="二、设置任务", padding=12)
        options.pack(fill="x", pady=(12, 0))
        self.add_field(options, 0, "截图数量", self.page_count, "张")
        self.add_field(options, 1, "起始编号", self.first_number, "（文件名从此编号开始）")
        self.add_field(options, 2, "截图界面等待", self.snip_wait, "秒")
        self.add_field(options, 3, "翻页后等待", self.page_wait, "秒")
        self.add_field(options, 4, "拖动耗时", self.swipe_duration, "秒")

        output_row = ttk.Frame(options)
        output_row.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(9, 0))
        ttk.Label(output_row, text="保存到", width=14).pack(side="left")
        ttk.Entry(output_row, textvariable=self.output_dir).pack(side="left", fill="x", expand=True)
        ttk.Button(output_row, text="选择…", command=self.choose_output).pack(side="left", padx=(8, 0))
        options.columnconfigure(1, weight=1)

        run = ttk.LabelFrame(outer, text="三、开始扫描", padding=12)
        run.pack(fill="both", expand=True, pady=(12, 0))
        ttk.Label(
            run,
            text="开始前请把小程序翻到第一题并保持窗口可见。启动后有 3 秒准备时间。",
            wraplength=590,
        ).pack(anchor="w")
        buttons = ttk.Frame(run)
        buttons.pack(fill="x", pady=12)
        self.start_button = ttk.Button(buttons, text="开始（F8）", command=self.start)
        self.start_button.pack(side="left", fill="x", expand=True)
        self.pause_button = ttk.Button(buttons, text="暂停（F9）", command=self.toggle_pause, state="disabled")
        self.pause_button.pack(side="left", fill="x", expand=True, padx=8)
        self.stop_button = ttk.Button(buttons, text="停止（Esc）", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", fill="x", expand=True)

        self.progress = ttk.Progressbar(run, maximum=100, mode="determinate")
        self.progress.pack(fill="x")
        ttk.Label(run, textvariable=self.progress_text, font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="e", pady=(4, 0))
        ttk.Label(run, textvariable=self.status_text, foreground="#0066aa", wraplength=590).pack(anchor="w", pady=(8, 0))

    @staticmethod
    def add_field(parent, row: int, label: str, variable: tk.StringVar, suffix: str) -> None:
        ttk.Label(parent, text=label, width=14).grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(parent, textvariable=variable, width=12).grid(row=row, column=1, sticky="w", pady=4)
        ttk.Label(parent, text=suffix, foreground="#666666").grid(row=row, column=2, sticky="w", padx=(8, 0), pady=4)

    def select_region(self) -> None:
        if self.is_running:
            return
        self.root.withdraw()
        self.root.after(
            250,
            lambda: DragOverlay(
                self.root,
                "设置截图范围",
                "按住鼠标左键，框住每一页要保存的题目区域",
                self.region_selected,
            ),
        )

    def region_selected(self, start: tuple[int, int], end: tuple[int, int]) -> None:
        left, right = sorted((start[0], end[0]))
        top, bottom = sorted((start[1], end[1]))
        self.settings.region = [left, top, right, bottom]
        self.settings.save()
        self.root.deiconify()
        self.refresh_descriptions()

    def select_swipe(self) -> None:
        if self.is_running:
            return
        self.root.withdraw()
        self.root.after(
            250,
            lambda: DragOverlay(
                self.root,
                "设置翻页手势",
                "像平时翻页一样，按住鼠标左键拖动一次",
                self.swipe_selected,
                require_box=False,
            ),
        )

    def swipe_selected(self, start: tuple[int, int], end: tuple[int, int]) -> None:
        self.settings.swipe_start = list(start)
        self.settings.swipe_end = list(end)
        self.settings.save()
        self.root.deiconify()
        self.refresh_descriptions()

    def refresh_descriptions(self) -> None:
        if self.settings.next_position:
            x, y = self.settings.next_position
            self.next_text.set(f"已设置：点击（{x}, {y}）")
        else:
            self.next_text.set("尚未设置")
        if self.settings.region:
            left, top, right, bottom = self.settings.region
            self.region_text.set(f"已设置：{right-left} × {bottom-top} 像素")
        else:
            self.region_text.set("尚未设置")
        if self.settings.swipe_start and self.settings.swipe_end:
            sx, sy = self.settings.swipe_start
            ex, ey = self.settings.swipe_end
            direction = "向右" if ex > sx else "向左"
            self.swipe_text.set(f"已设置：{direction}拖动（{sx}, {sy} → {ex}, {ey}）")
        else:
            self.swipe_text.set("尚未设置")

    def choose_output(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.output_dir.get() or str(Path.home()))
        if selected:
            self.output_dir.set(selected)

    def save_turn_mode(self) -> None:
        self.settings.turn_mode = self.turn_mode.get()
        self.settings.save()

    def select_next(self) -> None:
        if self.is_running:
            return
        self.root.withdraw()
        self.root.after(250, lambda: DragOverlay(
            self.root, "设置下一题位置",
            "点击题库的“下一题”按钮中心，记录位置（本次不会实际翻页）",
            self.next_selected, point_mode=True,
        ))

    def next_selected(self, position: tuple[int, int]) -> None:
        self.settings.next_position = list(position)
        self.turn_mode.set("click")
        self.save_turn_mode()
        self.root.deiconify()
        self.refresh_descriptions()

    @property
    def is_running(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    def read_form(self) -> bool:
        try:
            page_count = int(self.page_count.get())
            first_number = int(self.first_number.get())
            snip_wait = float(self.snip_wait.get())
            page_wait = float(self.page_wait.get())
            turn_mode = self.turn_mode.get()
            if turn_mode not in ("swipe", "click"):
                raise ValueError("请选择翻页方式")
            swipe_duration = float(self.swipe_duration.get()) if turn_mode == "swipe" else self.settings.swipe_duration
            if page_count < 1 or page_count > 10000:
                raise ValueError("截图数量需为 1～10000")
            if first_number < 0:
                raise ValueError("起始编号不能小于 0")
            if not 0.3 <= snip_wait <= 10:
                raise ValueError("截图界面等待需为 0.3～10 秒")
            if not 0.2 <= page_wait <= 30:
                raise ValueError("翻页后等待需为 0.2～30 秒")
            if turn_mode == "swipe" and not 0.1 <= swipe_duration <= 5:
                raise ValueError("拖动耗时需为 0.1～5 秒")
            output = self.output_dir.get().strip()
            if not output:
                raise ValueError("请选择保存位置")
        except ValueError as exc:
            messagebox.showerror("设置有误", str(exc))
            return False

        self.settings.page_count = page_count
        self.settings.turn_mode = turn_mode
        self.settings.first_number = first_number
        self.settings.snip_wait = snip_wait
        self.settings.page_wait = page_wait
        self.settings.swipe_duration = swipe_duration
        self.settings.output_dir = output
        self.settings.save()
        return True

    def start(self) -> None:
        if self.is_running:
            return
        if not self.settings.region:
            messagebox.showinfo("还差一步", "请先框选截图范围。")
            return
        if self.turn_mode.get() == "click" and not self.settings.next_position:
            messagebox.showinfo("还差一步", "请先设置“下一题”按钮的位置。")
            return
        if self.turn_mode.get() == "swipe" and (not self.settings.swipe_start or not self.settings.swipe_end):
            messagebox.showinfo("还差一步", "请先设置翻页手势。")
            return
        if not self.read_form():
            return

        output_root = Path(self.settings.output_dir).expanduser()
        run_name = "扫描_" + datetime.now().strftime("%Y%m%d_%H%M%S")
        self.last_run_dir = output_root / run_name
        try:
            self.last_run_dir.mkdir(parents=True, exist_ok=False)
        except OSError as exc:
            messagebox.showerror("无法创建保存文件夹", str(exc))
            return

        self.stop_event.clear()
        self.pause_event.clear()
        self.progress["value"] = 0
        self.progress_text.set(f"0 / {self.settings.page_count}")
        self.set_running_ui(True)
        self.root.withdraw()
        self.worker = threading.Thread(target=self.capture_loop, daemon=True)
        self.worker.start()

    def capture_loop(self) -> None:
        try:
            for remaining in (3, 2, 1):
                self.events.put(("status", f"{remaining} 秒后开始，请切换到小程序…"))
                if not self.interruptible_wait(1.0):
                    return

            total = self.settings.page_count
            digits = max(4, len(str(self.settings.first_number + total - 1)))
            for index in range(total):
                if not self.wait_while_paused():
                    return
                number = self.settings.first_number + index
                self.events.put(("status", f"正在截取第 {index + 1} / {total} 页…"))

                before = clipboard_sequence()
                press_snipping_shortcut()
                if not self.interruptible_wait(self.settings.snip_wait):
                    return

                left, top, right, bottom = self.settings.region
                mouse_drag((left, top), (right, bottom), 0.35, self.stop_event)
                if self.stop_event.is_set():
                    return

                image = self.wait_for_clipboard_image(before, timeout=8.0)
                if image is None:
                    raise RuntimeError(
                        "没有从剪贴板收到截图。请适当调大“截图界面等待”，并确认 Win+Shift+S 可以正常使用。"
                    )
                filename = self.last_run_dir / f"{number:0{digits}d}.png"
                image.save(filename, "PNG")
                self.events.put(("progress", (index + 1, total)))

                if index < total - 1:
                    if not self.interruptible_wait(0.25):
                        return
                    if self.settings.turn_mode == "click":
                        mouse_click(tuple(self.settings.next_position), self.stop_event)
                    else:
                        mouse_drag(
                            tuple(self.settings.swipe_start),
                            tuple(self.settings.swipe_end),
                            self.settings.swipe_duration,
                            self.stop_event,
                        )
                    if not self.interruptible_wait(self.settings.page_wait):
                        return

            self.events.put(("done", str(self.last_run_dir)))
        except Exception as exc:
            self.events.put(("error", str(exc)))
        finally:
            if self.stop_event.is_set():
                self.events.put(("stopped", None))

    def wait_for_clipboard_image(self, old_sequence: int, timeout: float) -> Image.Image | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not self.stop_event.is_set():
            if clipboard_sequence() != old_sequence:
                try:
                    item = ImageGrab.grabclipboard()
                    if isinstance(item, Image.Image):
                        return item.copy()
                except OSError:
                    pass
            time.sleep(0.12)
        return None

    def interruptible_wait(self, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.stop_event.is_set():
                return False
            if not self.wait_while_paused():
                return False
            time.sleep(min(0.08, max(0.01, deadline - time.monotonic())))
        return True

    def wait_while_paused(self) -> bool:
        while self.pause_event.is_set():
            if self.stop_event.wait(0.08):
                return False
        return not self.stop_event.is_set()

    def toggle_pause(self) -> None:
        if not self.is_running:
            return
        if self.pause_event.is_set():
            self.pause_event.clear()
            self.pause_button.configure(text="暂停（F9）")
            self.status_text.set("已继续")
        else:
            self.pause_event.set()
            self.pause_button.configure(text="继续（F9）")
            self.status_text.set("已暂停；再次按 F9 继续")

    def stop(self) -> None:
        if self.is_running:
            self.stop_event.set()
            self.pause_event.clear()
            self.status_text.set("正在停止…")

    def poll_hotkeys(self) -> None:
        for vk in self.key_state:
            pressed = bool(user32.GetAsyncKeyState(vk) & 0x8000)
            if pressed and not self.key_state[vk]:
                if vk == VK_F8 and not self.is_running and self.root.state() != "withdrawn":
                    self.start()
                elif vk == VK_F9 and self.is_running:
                    self.toggle_pause()
                elif vk == VK_ESCAPE and self.is_running:
                    self.stop()
            self.key_state[vk] = pressed
        self.root.after(80, self.poll_hotkeys)

    def poll_events(self) -> None:
        try:
            while True:
                name, payload = self.events.get_nowait()
                if name == "status":
                    self.status_text.set(str(payload))
                elif name == "progress":
                    current, total = payload
                    self.progress["value"] = current * 100 / total
                    self.progress_text.set(f"{current} / {total}")
                elif name == "done":
                    self.finish_ui("扫描完成")
                    messagebox.showinfo("扫描完成", f"全部截图已保存到：\n{payload}")
                elif name == "error":
                    self.finish_ui("扫描失败")
                    messagebox.showerror("扫描失败", str(payload))
                elif name == "stopped":
                    self.finish_ui("已停止；已完成的截图会保留")
        except queue.Empty:
            pass
        self.root.after(80, self.poll_events)

    def finish_ui(self, status: str) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        self.status_text.set(status)
        self.set_running_ui(False)

    def set_running_ui(self, running: bool) -> None:
        for button in self.mode_buttons:
            button.configure(state="disabled" if running else "normal")
        self.start_button.configure(state="disabled" if running else "normal")
        self.pause_button.configure(state="normal" if running else "disabled", text="暂停（F9）")
        self.stop_button.configure(state="normal" if running else "disabled")

    def on_close(self) -> None:
        if self.is_running:
            if not messagebox.askyesno("确认退出", "任务正在运行，确定停止并退出吗？"):
                return
            self.stop_event.set()
        self.root.destroy()


def main() -> None:
    enable_dpi_awareness()
    root = tk.Tk()
    ScreenshotApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
