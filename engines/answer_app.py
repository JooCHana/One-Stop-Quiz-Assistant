from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
import traceback
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageChops, ImageStat

from .core import (
    OfflineOCR,
    Question,
    QuizError,
    analyze_page,
    capture_screen_region,
    find_visible_answer_points,
    load_question_bank,
    scan_answer_view,
    wait_for_escape,
)
from .windows_control import (
    WindowInfo,
    click_relative,
    drag_relative,
    enable_dpi_awareness,
    escape_pressed,
    focus_window,
    get_window_rect,
    list_windows,
    move_relative,
    scroll_relative,
)


APP_NAME = "题库自动答题助手"
VERSION = "1.7.0"
SWITCH_CLICK = "点击“下一题”按钮"
SWITCH_SWIPE_LEFT = "向左滑动（下一题）"
SWITCH_SWIPE_RIGHT = "向右滑动（下一题）"
SWITCH_AUTO = "选择答案后自动切题（只等待）"
SWITCH_MODES = (
    SWITCH_CLICK,
    SWITCH_SWIPE_LEFT,
    SWITCH_SWIPE_RIGHT,
    SWITCH_AUTO,
)


def question_switch_drag(
    image_size: tuple[int, int], switch_mode: str
) -> tuple[tuple[int, int], tuple[int, int]]:
    """Return stable in-window drag points for the configured next-question gesture."""
    width, height = image_size
    if width <= 0 or height <= 0:
        raise ValueError("窗口尺寸无效")
    y = round(height * 0.48)
    left = (round(width * 0.22), y)
    right = (round(width * 0.78), y)
    if switch_mode == SWITCH_SWIPE_LEFT:
        return right, left
    if switch_mode == SWITCH_SWIPE_RIGHT:
        return left, right
    raise ValueError("当前切题方式不是滑动模式")


def is_last_question(
    progress: tuple[int, int] | None,
    next_point: tuple[int, int] | None,
    finish_button_detected: bool,
    switch_mode: str,
) -> bool:
    """Decide whether to stop without mistaking swipe-only pages for the last one."""
    if progress:
        return progress[0] >= progress[1]
    return switch_mode == SWITCH_CLICK and next_point is None and finish_button_detected


def unchanged_page_limit(switch_mode: str) -> int:
    """Allow extra time for pages that animate and advance by themselves."""
    return 24 if switch_mode == SWITCH_AUTO else 8


def option_click_candidates(
    point: tuple[int, int], image_size: tuple[int, int]
) -> tuple[tuple[int, int], ...]:
    """Return safe alternatives on the same option row."""
    width, height = image_size
    x, y = point
    raw = (
        (x, y),
        (round(width * 0.11), y),
        (round(width * 0.30), y),
    )
    result: list[tuple[int, int]] = []
    for candidate_x, candidate_y in raw:
        candidate = (
            max(1, min(width - 2, candidate_x)),
            max(1, min(height - 2, candidate_y)),
        )
        if all(abs(candidate[0] - old[0]) > 14 for old in result):
            result.append(candidate)
    return tuple(result)


def option_visual_change_score(
    before: Image.Image,
    after: Image.Image,
    point: tuple[int, int],
) -> float:
    """Measure local change around a clicked row and its option icon."""
    if before.size != after.size:
        return 100.0
    width, height = before.size
    radius_x = max(30, round(width * 0.035))
    radius_y = max(22, round(height * 0.022))
    centers = (point, (round(width * 0.11), point[1]))
    best = 0.0
    for center_x, center_y in centers:
        box = (
            max(0, center_x - radius_x),
            max(0, center_y - radius_y),
            min(width, center_x + radius_x + 1),
            min(height, center_y + radius_y + 1),
        )
        difference = ImageChops.difference(before.crop(box), after.crop(box)).convert("L")
        histogram = difference.histogram()
        pixels = max(1, sum(histogram))
        changed_ratio = 100.0 * sum(histogram[18:]) / pixels
        mean_change = float(ImageStat.Stat(difference).mean[0])
        best = max(best, mean_change, changed_ratio * 1.5)
    return best


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def config_path() -> Path:
    base = Path(os.environ.get("APPDATA", str(Path.home()))) / "QuizAutoAssistant"
    base.mkdir(parents=True, exist_ok=True)
    return base / "config.json"


class QuizAssistantApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(f"{APP_NAME} {VERSION}")
        self.root.geometry("860x760")
        self.root.minsize(720, 640)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.messages: queue.Queue[tuple[str, object]] = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.bank = []
        self.bank_path_loaded = ""
        self.ocr = OfflineOCR()
        self.windows: list[WindowInfo] = []

        self.file_var = tk.StringVar()
        self.window_var = tk.StringVar()
        self.delay_var = tk.DoubleVar(value=1.25)
        self.threshold_var = tk.DoubleVar(value=80.0)
        self.switch_mode_var = tk.StringVar(value=SWITCH_CLICK)
        self.swipe_duration_var = tk.DoubleVar(value=0.45)
        self.status_var = tk.StringVar(value="请先选择题库和答题窗口")

        self._load_config()
        self._build_ui()
        self.refresh_windows()
        self.root.after(100, self._drain_messages)

    def _build_ui(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 18, "bold"))
        style.configure("Step.TLabel", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Accent.TButton", font=("Microsoft YaHei UI", 10, "bold"), padding=(16, 8))

        main = ttk.Frame(self.root, padding=20)
        main.pack(fill="both", expand=True)

        ttk.Label(main, text=APP_NAME, style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            main,
            text="离线识别题目并从题库匹配答案。识别不可靠时会自动停止。按 Esc 可随时急停。",
            foreground="#555555",
        ).pack(anchor="w", pady=(4, 16))

        file_frame = ttk.LabelFrame(main, text="1  选择题库或考试文档", padding=12)
        file_frame.pack(fill="x", pady=(0, 10))
        ttk.Entry(file_frame, textvariable=self.file_var).pack(side="left", fill="x", expand=True)
        ttk.Button(file_frame, text="浏览…", command=self.browse_file).pack(side="left", padx=(8, 0))
        ttk.Button(file_frame, text="读取", command=self.load_bank_clicked).pack(side="left", padx=(8, 0))

        window_frame = ttk.LabelFrame(main, text="2  选择正在答题的窗口", padding=12)
        window_frame.pack(fill="x", pady=(0, 10))
        self.window_combo = ttk.Combobox(window_frame, textvariable=self.window_var, state="readonly")
        self.window_combo.pack(side="left", fill="x", expand=True)
        ttk.Button(window_frame, text="刷新窗口", command=self.refresh_windows).pack(side="left", padx=(8, 0))

        settings = ttk.LabelFrame(main, text="3  设置", padding=12)
        settings.pack(fill="x", pady=(0, 10))
        ttk.Label(settings, text="每题等待").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(settings, from_=0.7, to=5.0, increment=0.1, textvariable=self.delay_var, width=7).grid(
            row=0, column=1, padx=(8, 4)
        )
        ttk.Label(settings, text="秒").grid(row=0, column=2, sticky="w")
        ttk.Label(settings, text="最低匹配度").grid(row=0, column=3, padx=(28, 0), sticky="w")
        ttk.Spinbox(settings, from_=65, to=98, increment=1, textvariable=self.threshold_var, width=7).grid(
            row=0, column=4, padx=(8, 4)
        )
        ttk.Label(settings, text="%").grid(row=0, column=5, sticky="w")
        ttk.Label(settings, text="最后一题完成后停止运行，助手保持打开").grid(
            row=0, column=6, padx=(28, 0), sticky="w"
        )
        ttk.Label(settings, text="题目切换方式").grid(row=1, column=0, pady=(12, 0), sticky="w")
        self.switch_mode_combo = ttk.Combobox(
            settings,
            textvariable=self.switch_mode_var,
            values=SWITCH_MODES,
            state="readonly",
            width=30,
        )
        self.switch_mode_combo.grid(
            row=1, column=1, columnspan=3, padx=(8, 12), pady=(12, 0), sticky="w"
        )
        ttk.Label(settings, text="滑动时长").grid(row=1, column=4, pady=(12, 0), sticky="e")
        ttk.Spinbox(
            settings,
            from_=0.20,
            to=1.50,
            increment=0.05,
            textvariable=self.swipe_duration_var,
            width=7,
        ).grid(row=1, column=5, padx=(8, 4), pady=(12, 0), sticky="w")
        ttk.Label(settings, text="秒（滑动模式生效）").grid(
            row=1, column=6, pady=(12, 0), sticky="w"
        )

        buttons = ttk.Frame(main)
        buttons.pack(fill="x", pady=(2, 12))
        self.test_button = ttk.Button(buttons, text="测试识别（不会点击）", command=self.test_recognition)
        self.test_button.pack(side="left")
        self.start_button = ttk.Button(buttons, text="开始自动答题", style="Accent.TButton", command=self.start_auto)
        self.start_button.pack(side="left", padx=(10, 0))
        self.stop_button = ttk.Button(buttons, text="停止", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=(10, 0))

        ttk.Label(main, textvariable=self.status_var, foreground="#0B57D0").pack(anchor="w", pady=(0, 6))
        log_frame = ttk.LabelFrame(main, text="运行记录", padding=6)
        log_frame.pack(fill="both", expand=True)
        self.log_text = tk.Text(
            log_frame,
            height=13,
            wrap="word",
            state="disabled",
            font=("Microsoft YaHei UI", 9),
            background="#FAFAFA",
        )
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        ttk.Label(
            main,
            text="建议先点“测试识别”。自动运行时不要遮挡或移动答题窗口，也不要操作鼠标。",
            foreground="#7A4B00",
        ).pack(anchor="w", pady=(10, 0))

    def _load_config(self) -> None:
        config = {}
        try:
            config = json.loads(config_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        candidate = config.get("bank_path", "")
        if candidate and Path(candidate).exists():
            self.file_var.set(candidate)
        else:
            nearby = [
                path
                for pattern in ("*.xls*", "*.doc", "*.docx", "*.txt", "*.csv", "*.tsv")
                for path in app_dir().glob(pattern)
            ]
            if nearby:
                self.file_var.set(str(nearby[0]))
        self.delay_var.set(float(config.get("delay", 1.25)))
        self.threshold_var.set(float(config.get("threshold", 80.0)))
        switch_mode = str(config.get("switch_mode", SWITCH_CLICK))
        self.switch_mode_var.set(switch_mode if switch_mode in SWITCH_MODES else SWITCH_CLICK)
        self.swipe_duration_var.set(float(config.get("swipe_duration", 0.45)))
        self.saved_window_title = str(config.get("window_title", ""))

    def _save_config(self) -> None:
        selected = self._selected_window(silent=True)
        data = {
            "bank_path": self.file_var.get().strip(),
            "delay": float(self.delay_var.get()),
            "threshold": float(self.threshold_var.get()),
            "switch_mode": self.switch_mode_var.get(),
            "swipe_duration": float(self.swipe_duration_var.get()),
            "window_title": selected.title if selected else self.saved_window_title,
        }
        try:
            config_path().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    def log(self, message: str) -> None:
        self.messages.put(("log", message))

    def set_status(self, message: str) -> None:
        self.messages.put(("status", message))

    def _drain_messages(self) -> None:
        try:
            while True:
                kind, payload = self.messages.get_nowait()
                if kind == "log":
                    stamp = time.strftime("%H:%M:%S")
                    self.log_text.configure(state="normal")
                    self.log_text.insert("end", f"[{stamp}] {payload}\n")
                    self.log_text.see("end")
                    self.log_text.configure(state="disabled")
                elif kind == "status":
                    self.status_var.set(str(payload))
                elif kind == "working":
                    self._set_working(bool(payload))
                elif kind == "show":
                    self.root.deiconify()
                    self.root.lift()
                    self.root.focus_force()
                elif kind == "info":
                    title, body = payload
                    messagebox.showinfo(title, body, parent=self.root)
                elif kind == "error":
                    title, body = payload
                    messagebox.showerror(title, body, parent=self.root)
        except queue.Empty:
            pass
        self.root.after(100, self._drain_messages)

    def _set_working(self, working: bool) -> None:
        self.start_button.configure(state="disabled" if working else "normal")
        self.test_button.configure(state="disabled" if working else "normal")
        self.stop_button.configure(state="normal" if working else "disabled")

    def browse_file(self) -> None:
        filename = filedialog.askopenfilename(
            parent=self.root,
            title="选择题库或考试文档",
            filetypes=[
                ("题库和考试文档", "*.xls *.xlsx *.xlsm *.csv *.tsv *.doc *.docx *.txt"),
                ("Word 文档", "*.doc *.docx"),
                ("所有文件", "*.*"),
            ],
        )
        if filename:
            self.file_var.set(filename)
            self.bank = []
            self.bank_path_loaded = ""
            self.load_bank_clicked()

    def _ensure_bank(self) -> None:
        path = self.file_var.get().strip().strip('"')
        if not path:
            raise QuizError("请先选择题库文件。")
        if self.bank and self.bank_path_loaded == str(Path(path).resolve()):
            return
        bank = load_question_bank(path, self.log)
        self.bank = bank
        self.bank_path_loaded = str(Path(path).resolve())
        types: dict[str, int] = {}
        for item in bank:
            label = item.qtype or "选择题"
            types[label] = types.get(label, 0) + 1
        detail = "，".join(f"{key} {value}" for key, value in sorted(types.items()))
        self.log(f"题库读取完成：共 {len(bank)} 道（{detail}）")
        self.set_status(f"已读取 {len(bank)} 道题")

    def load_bank_clicked(self) -> None:
        try:
            self._ensure_bank()
            self._save_config()
        except Exception as exc:
            messagebox.showerror("题库读取失败", str(exc), parent=self.root)

    def refresh_windows(self) -> None:
        own_hwnd = self.root.winfo_id() if self.root.winfo_exists() else None
        self.windows = list_windows(exclude_hwnd=own_hwnd)
        values = [item.display for item in self.windows]
        self.window_combo.configure(values=values)
        selected_index = -1
        if self.saved_window_title:
            selected_index = next(
                (i for i, item in enumerate(self.windows) if item.title == self.saved_window_title), -1
            )
        if selected_index < 0:
            selected_index = next(
                (
                    i
                    for i, item in enumerate(self.windows)
                    if any(word in item.title for word in ("教育培训", "小程序", "微信"))
                ),
                -1,
            )
        if selected_index < 0 and self.windows:
            selected_index = 0
        if selected_index >= 0:
            self.window_combo.current(selected_index)
        self.log(f"已刷新窗口列表：找到 {len(self.windows)} 个可选窗口")

    def _selected_window(self, silent: bool = False) -> WindowInfo | None:
        index = self.window_combo.current()
        if 0 <= index < len(self.windows):
            return self.windows[index]
        if silent:
            return None
        raise QuizError("请选择正在显示题目的答题窗口。")

    def _validate_settings(self) -> tuple[WindowInfo, float, float, str, float]:
        self._ensure_bank()
        window = self._selected_window()
        delay = float(self.delay_var.get())
        threshold = float(self.threshold_var.get())
        switch_mode = self.switch_mode_var.get()
        swipe_duration = float(self.swipe_duration_var.get())
        if not 0.5 <= delay <= 10:
            raise QuizError("每题等待时间应在 0.5–10 秒之间。")
        if not 60 <= threshold <= 99:
            raise QuizError("最低匹配度应在 60%–99% 之间。")
        if switch_mode not in SWITCH_MODES:
            raise QuizError("请选择有效的题目切换方式。")
        if not 0.18 <= swipe_duration <= 2.0:
            raise QuizError("滑动时长应在 0.18–2 秒之间。")
        self._save_config()
        return window, delay, threshold, switch_mode, swipe_duration

    def test_recognition(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        try:
            window, _, threshold, _, _ = self._validate_settings()
        except Exception as exc:
            messagebox.showerror("无法测试", str(exc), parent=self.root)
            return
        self.stop_event.clear()
        self._set_working(True)
        self.set_status("正在测试识别，首次启动 OCR 可能需要几秒…")
        self.worker = threading.Thread(target=self._test_worker, args=(window, threshold), daemon=True)
        self.worker.start()

    def _test_worker(self, window: WindowInfo, threshold: float) -> None:
        try:
            rect = focus_window(window.hwnd)
            image = capture_screen_region(rect)
            analysis = analyze_page(image, self.bank, self.ocr, threshold)
            answer = "、".join(analysis.question.answer)
            progress = f"，页码 {analysis.progress[0]}/{analysis.progress[1]}" if analysis.progress else ""
            self.log(
                f"测试成功：题号 {analysis.question.qid}，答案 {answer}，匹配度 {analysis.score:.1f}%{progress}"
            )
            self.log(f"识别题干：{analysis.recognized_question}")
            self.set_status("测试通过，可以开始自动答题")
            self.messages.put(("show", None))
            self.messages.put(
                (
                    "info",
                    (
                        "测试识别成功",
                        f"匹配题目：{analysis.question.text}\n\n答案：{answer}\n匹配度：{analysis.score:.1f}%\n\n本次测试没有点击页面。",
                    ),
                )
            )
        except Exception as exc:
            self.messages.put(("show", None))
            self._report_worker_error("测试识别失败", exc)
        finally:
            self.messages.put(("working", False))

    def start_auto(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        try:
            window, delay, threshold, switch_mode, swipe_duration = self._validate_settings()
        except Exception as exc:
            messagebox.showerror("无法开始", str(exc), parent=self.root)
            return
        self.stop_event.clear()
        self._set_working(True)
        self.log("自动答题开始。按 Esc 或点击“停止”可随时终止。")
        self.log(f"题目切换方式：{switch_mode}")
        self.set_status("自动答题运行中…")
        self.root.iconify()
        self.worker = threading.Thread(
            target=self._auto_worker,
            args=(window, delay, threshold, switch_mode, swipe_duration),
            daemon=True,
        )
        self.worker.start()

    def _click_answer_verified(
        self,
        window: WindowInfo,
        letter: str,
        point: tuple[int, int],
        image_size: tuple[int, int],
        progress_text: str,
    ) -> None:
        candidates = option_click_candidates(point, image_size)

        def description(candidate: tuple[int, int]) -> str:
            if candidate == candidates[0]:
                return "识别位置"
            return "选项图标区域" if candidate[0] <= image_size[0] * 0.18 else "同一选项行文字区域"

        scores: list[float] = []
        for attempt, candidate in enumerate(candidates):
            if self.stop_event.is_set() or escape_pressed():
                self.stop_event.set()
                return
            move_relative(window.hwnd, candidate, image_size)
            if wait_for_escape(self.stop_event, 0.12, escape_pressed):
                return
            before = capture_screen_region(get_window_rect(window.hwnd))
            click_relative(window.hwnd, candidate, image_size)
            if wait_for_escape(self.stop_event, 0.42, escape_pressed):
                return
            after = capture_screen_region(get_window_rect(window.hwnd))
            score = option_visual_change_score(before, after, candidate)
            scores.append(score)
            if score >= 3.0:
                if attempt:
                    self.log(
                        f"{progress_text}：答案 {letter} 第 {attempt + 1} 次点击成功"
                        f"（{description(candidate)}）"
                    )
                else:
                    self.log(f"{progress_text}：答案 {letter} 已确认选中")
                return
            if attempt + 1 < len(candidates):
                next_description = description(candidates[attempt + 1])
                self.log(
                    f"{progress_text}：答案 {letter} 点击后未检测到选中变化，"
                    f"改用{next_description}重试"
                )
        detail = "、".join(f"{value:.1f}" for value in scores)
        raise QuizError(
            f"已找到答案 {letter}，但多次点击后都未检测到选中状态（变化值 {detail}）。"
            "程序已停止，请确认答题页面没有提示遮罩，并保持窗口完整显示。"
        )

    def _auto_worker(
        self,
        window: WindowInfo,
        delay: float,
        threshold: float,
        switch_mode: str,
        swipe_duration: float,
    ) -> None:
        answered = 0
        last_page_key = ""
        unchanged_count = 0
        try:
            while not self.stop_event.is_set():
                if escape_pressed():
                    self.stop_event.set()
                    break
                rect = focus_window(window.hwnd)
                image = capture_screen_region(rect)
                analysis = analyze_page(image, self.bank, self.ocr, threshold)

                if analysis.page_key == last_page_key:
                    unchanged_count += 1
                    if unchanged_count >= unchanged_page_limit(switch_mode):
                        if switch_mode == SWITCH_AUTO:
                            raise QuizError(
                                "选择答案后等待自动切题超时，页面仍未显示新题，"
                                "已停止以避免重复选择。"
                            )
                        raise QuizError("执行切题后页面长时间没有变化，已停止以避免重复选择。")
                    if wait_for_escape(self.stop_event, 0.45, escape_pressed):
                        break
                    continue
                unchanged_count = 0

                progress_text = (
                    f"{analysis.progress[0]}/{analysis.progress[1]}"
                    if analysis.progress
                    else f"第 {answered + 1} 题"
                )
                answer_text = "、".join(analysis.question.answer)
                self.log(
                    f"{progress_text}：匹配题号 {analysis.question.qid}，选择 {answer_text}，匹配度 {analysis.score:.1f}%"
                )

                current_view_size = analysis.image_size
                selected_letters: set[str] = set()
                for letter in analysis.question.answer:
                    point = analysis.answer_points.get(letter)
                    if point is None:
                        continue
                    if self.stop_event.is_set() or escape_pressed():
                        self.stop_event.set()
                        break
                    self._click_answer_verified(
                        window,
                        letter,
                        point,
                        current_view_size,
                        progress_text,
                    )
                    selected_letters.add(letter)
                if self.stop_event.is_set():
                    break

                current_next = analysis.next_point
                current_progress = analysis.progress
                current_finish = analysis.finish_button_detected
                width, height = analysis.image_size
                scroll_point = (round(width * 0.72), round(height * 0.58))
                pending = [letter for letter in analysis.question.answer if letter not in selected_letters]
                scroll_count = 0
                stationary_scans = 0

                while pending and scroll_count < 12:
                    if stationary_scans < 2:
                        stationary_scans += 1
                        self.log(
                            f"{progress_text}：原位置重新识别尚未找到的选项 "
                            f"{'、'.join(pending)}（{stationary_scans}/2）"
                        )
                        if wait_for_escape(self.stop_event, 0.38, escape_pressed):
                            break
                    else:
                        self.log(f"{progress_text}：下滑寻找尚未显示的选项 {'、'.join(pending)}")
                        scroll_relative(window.hwnd, scroll_point, -3, current_view_size)
                        if wait_for_escape(self.stop_event, 0.48, escape_pressed):
                            break
                        scroll_count += 1
                    scrolled_image = capture_screen_region(focus_window(window.hwnd))
                    current_view_size = scrolled_image.size
                    view = scan_answer_view(scrolled_image, analysis.question, self.ocr, pending)
                    current_next = view.next_point
                    current_progress = view.progress or current_progress
                    current_finish = view.finish_button_detected
                    for letter in tuple(pending):
                        point = view.answer_points.get(letter)
                        if point is None:
                            continue
                        self._click_answer_verified(
                            window,
                            letter,
                            point,
                            current_view_size,
                            progress_text,
                        )
                        selected_letters.add(letter)
                        pending.remove(letter)
                        location = "重新识别后" if stationary_scans <= 2 and scroll_count == 0 else "下滑后"
                        self.log(f"{progress_text}：{location}找到并选择 {letter}")
                    if self.stop_event.is_set():
                        break

                if self.stop_event.is_set():
                    break
                if pending:
                    raise QuizError(
                        f"已连续下滑查找，但仍无法可靠定位选项 {'、'.join(pending)}。"
                        "程序已停止，已选选项不会再次点击。"
                    )

                answered += 1
                last_page_key = analysis.page_key
                is_last = is_last_question(
                    current_progress,
                    current_next,
                    current_finish,
                    switch_mode,
                )
                if is_last:
                    self.log(f"最后一题已完成，本次共自动作答 {answered} 题。")
                    self.set_status("全部题目已完成")
                    if wait_for_escape(self.stop_event, 0.8, escape_pressed):
                        break
                    self.messages.put(("show", None))
                    self.messages.put(
                        (
                            "info",
                            (
                                "已停止运行",
                                f"最后一题已完成，共自动作答 {answered} 题。\n\n助手将保持打开，不再执行下一题。",
                            ),
                        )
                    )
                    return

                if switch_mode == SWITCH_CLICK:
                    while current_next is None and scroll_count < 16:
                        scroll_relative(window.hwnd, scroll_point, -3, current_view_size)
                        if wait_for_escape(self.stop_event, 0.42, escape_pressed):
                            break
                        scrolled_image = capture_screen_region(focus_window(window.hwnd))
                        current_view_size = scrolled_image.size
                        view = scan_answer_view(scrolled_image, analysis.question, self.ocr, ())
                        current_next = view.next_point
                        current_finish = view.finish_button_detected
                        scroll_count += 1
                    if self.stop_event.is_set():
                        break
                    if current_next is None:
                        raise QuizError("未识别到页面底部的“下一题”按钮，已停止。")
                    click_relative(window.hwnd, current_next, current_view_size)
                elif switch_mode in (SWITCH_SWIPE_LEFT, SWITCH_SWIPE_RIGHT):
                    start, end = question_switch_drag(current_view_size, switch_mode)
                    self.log(f"{progress_text}：{switch_mode}")
                    drag_relative(
                        window.hwnd,
                        start,
                        end,
                        duration=swipe_duration,
                        source_size=current_view_size,
                    )
                else:
                    self.log(f"{progress_text}：答案已选择，等待页面自动显示下一题")
                    if wait_for_escape(self.stop_event, delay, escape_pressed):
                        break
                    # The target page advances and scrolls by itself. Do not click,
                    # swipe, or restore the vertical position in this mode.
                    continue
                first_wait = min(0.75, delay)
                if wait_for_escape(self.stop_event, first_wait, escape_pressed):
                    break
                # A long question may leave the view scrolled down. Return to the
                # top so the next question's stem is visible before OCR matching.
                scroll_relative(window.hwnd, scroll_point, 18, current_view_size)
                if wait_for_escape(self.stop_event, max(0.25, delay - first_wait), escape_pressed):
                    break

            self.log(f"自动答题已停止，本次完成 {answered} 题。")
            self.set_status("已停止")
            self.messages.put(("show", None))
        except Exception as exc:
            self.messages.put(("show", None))
            self._report_worker_error("自动答题已停止", exc)
        finally:
            self.messages.put(("working", False))

    def _report_worker_error(self, title: str, exc: Exception) -> None:
        if isinstance(exc, QuizError):
            detail = str(exc)
        else:
            detail = f"{exc}\n\n请保留运行记录并重试。"
            debug_file = app_dir() / "error.log"
            try:
                debug_file.write_text(traceback.format_exc(), encoding="utf-8")
                detail += f"\n错误详情已保存到：{debug_file}"
            except OSError:
                pass
        self.log(detail)
        self.set_status(title)
        self.messages.put(("error", (title, detail)))

    def stop(self) -> None:
        self.stop_event.set()
        self.set_status("正在停止…")

    def on_close(self) -> None:
        self.stop_event.set()
        self._save_config()
        self.root.destroy()


def main() -> None:
    if "--smoke-test" in sys.argv:
        _run_smoke_test()
        return
    if "--self-test" in sys.argv:
        _run_self_test()
        return
    enable_dpi_awareness()
    root = tk.Tk()
    QuizAssistantApp(root)
    root.mainloop()


def _run_smoke_test() -> None:
    """Verify packaged OCR assets and bank readers without clicking the screen."""
    flag = sys.argv.index("--smoke-test")
    args = sys.argv[flag + 1 :]
    if len(args) != 3:
        raise SystemExit(2)
    bank_path, screenshot_path, report_path = args
    report = Path(report_path)
    try:
        from PIL import Image

        bank = load_question_bank(bank_path)
        items = OfflineOCR().recognize(Image.open(screenshot_path))
        left_drag = question_switch_drag((1000, 1600), SWITCH_SWIPE_LEFT)
        right_drag = question_switch_drag((1000, 1600), SWITCH_SWIPE_RIGHT)
        candidates = option_click_candidates((88, 260), (1000, 700))
        before = Image.new("RGB", (1000, 700), "white")
        after = before.copy()
        from PIL import ImageDraw

        ImageDraw.Draw(after).ellipse((98, 248, 122, 272), fill="#1688ff")
        click_change = option_visual_change_score(before, after, candidates[0])
        if not bank or len(items) < 5 or len(candidates) < 2 or click_change < 3.0:
            raise RuntimeError("题库或 OCR 识别结果为空")
        report.write_text(
            f"PASS\n版本={VERSION}\n题库题数={len(bank)}\nOCR文本块={len(items)}\n"
            f"向左滑={left_drag}\n向右滑={right_drag}\n"
            f"点击校验=PASS（变化值 {click_change:.1f}）\n",
            encoding="utf-8",
        )
    except Exception:
        report.write_text("FAIL\n" + traceback.format_exc(), encoding="utf-8")
        raise SystemExit(1)


def _run_self_test() -> None:
    """Package-level verification used by the build process; it never clicks."""
    flag = sys.argv.index("--self-test")
    args = sys.argv[flag + 1 :]
    if len(args) != 3:
        raise SystemExit(2)
    bank_path, screenshot_path, report_path = args
    report = Path(report_path)
    try:
        from PIL import Image

        bank = load_question_bank(bank_path)
        dummy = Question(
            qid="屏幕测试",
            text="电子运统46系统存储的“运统46”信息须留有备份，信息至少保存（）。",
            qtype="单选题",
            answer=("B",),
            options={"A": "3个月", "B": "六个月", "C": "一年", "D": "两年"},
        )
        image = Image.open(screenshot_path)
        ocr = OfflineOCR()
        result = analyze_page(image, [dummy], ocr, 80)
        if len(bank) < 1 or result.score < 90 or result.progress != (1, 40):
            raise RuntimeError("自检结果不符合预期")
        scroll_dummy = Question(
            qid="下滑测试",
            text=dummy.text,
            qtype="多选题",
            answer=("B", "D"),
            options=dummy.options,
        )
        top = analyze_page(image.crop((0, 0, image.width, 620)), [scroll_dummy], ocr, 80)
        lower = scan_answer_view(
            image.crop((0, 350, image.width, image.height)), scroll_dummy, ocr, ("D",)
        )
        if "B" not in top.answer_points or "D" in top.answer_points or "D" not in lower.answer_points:
            raise RuntimeError("下滑选项自检失败")
        icon_question = Question(
            qid="图标测试",
            text=dummy.text,
            qtype="多选题",
            answer=("C", "D"),
            options=dummy.options,
        )
        icon_items = [
            item
            for item in ocr.recognize(image)
            if item.text.strip() not in {"C", "D", "一年", "两年"}
        ]
        icon_points = find_visible_answer_points(
            icon_question, icon_items, 318, image.size, image=image
        )
        if set(icon_points) != {"C", "D"}:
            raise RuntimeError("选项图标自检失败")
        report.write_text(
            f"PASS\n题库题数={len(bank)}\n截图匹配度={result.score:.1f}\n"
            f"答案位置={result.answer_points['B']}\n下一题位置={result.next_point}\n"
            f"下滑测试=上屏B，下滑后D\n"
            f"图标测试=C{icon_points['C']}，D{icon_points['D']}\n",
            encoding="utf-8",
        )
    except Exception:
        report.write_text("FAIL\n" + traceback.format_exc(), encoding="utf-8")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
