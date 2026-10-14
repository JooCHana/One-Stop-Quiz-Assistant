from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from dataclasses import dataclass


user32 = ctypes.windll.user32


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    title: str
    rect: tuple[int, int, int, int]

    @property
    def width(self) -> int:
        return self.rect[2] - self.rect[0]

    @property
    def height(self) -> int:
        return self.rect[3] - self.rect[1]

    @property
    def display(self) -> str:
        shortened = self.title if len(self.title) <= 52 else self.title[:49] + "..."
        return f"{shortened}  ({self.width}×{self.height})"


def enable_dpi_awareness() -> None:
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass


def list_windows(exclude_hwnd: int | None = None) -> list[WindowInfo]:
    windows: list[WindowInfo] = []
    enum_proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd, _lparam):
        if exclude_hwnd and int(hwnd) == int(exclude_hwnd):
            return True
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        title = buffer.value.strip()
        rect = wintypes.RECT()
        if not title or not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return True
        bounds = (rect.left, rect.top, rect.right, rect.bottom)
        if bounds[2] - bounds[0] >= 500 and bounds[3] - bounds[1] >= 350:
            windows.append(WindowInfo(int(hwnd), title, bounds))
        return True

    proc = enum_proc_type(callback)
    user32.EnumWindows(proc, 0)
    return sorted(windows, key=lambda item: ("教育" not in item.title, -item.width * item.height, item.title))


def get_window_rect(hwnd: int) -> tuple[int, int, int, int]:
    rect = wintypes.RECT()
    if not user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
        raise RuntimeError("无法获取目标窗口位置。")
    return rect.left, rect.top, rect.right, rect.bottom


def focus_window(hwnd: int) -> tuple[int, int, int, int]:
    if not user32.IsWindow(hwnd):
        raise RuntimeError("目标窗口已经关闭。")
    user32.ShowWindow(wintypes.HWND(hwnd), 9)
    user32.SetForegroundWindow(wintypes.HWND(hwnd))
    time.sleep(0.35)
    return get_window_rect(hwnd)


def _map_point(
    rect: tuple[int, int, int, int],
    point: tuple[int, int],
    source_size: tuple[int, int] | None,
) -> tuple[int, int]:
    left, top, right, bottom = rect
    if source_size and source_size[0] > 0 and source_size[1] > 0:
        x = left + round(point[0] * (right - left) / source_size[0])
        y = top + round(point[1] * (bottom - top) / source_size[1])
    else:
        x, y = left + int(point[0]), top + int(point[1])
    return x, y


def click_relative(
    hwnd: int,
    point: tuple[int, int],
    source_size: tuple[int, int] | None = None,
) -> None:
    x, y = move_relative(hwnd, point, source_size)
    user32.mouse_event(0x0002, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTDOWN
    time.sleep(0.075)
    user32.mouse_event(0x0004, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTUP


def move_relative(
    hwnd: int,
    point: tuple[int, int],
    source_size: tuple[int, int] | None = None,
) -> tuple[int, int]:
    """Move the pointer to an image-relative point and return screen coordinates."""
    rect = get_window_rect(hwnd)
    x, y = _map_point(rect, point, source_size)
    if not user32.SetCursorPos(x, y):
        raise RuntimeError("无法把鼠标移动到目标选项。")
    return x, y


def drag_relative(
    hwnd: int,
    start: tuple[int, int],
    end: tuple[int, int],
    duration: float = 0.45,
    source_size: tuple[int, int] | None = None,
) -> None:
    """Perform a smooth left-button drag inside the target window."""
    rect = get_window_rect(hwnd)
    start_x, start_y = _map_point(rect, start, source_size)
    end_x, end_y = _map_point(rect, end, source_size)
    duration = max(0.18, duration)
    steps = max(12, min(48, round(duration * 60)))
    user32.SetCursorPos(start_x, start_y)
    time.sleep(0.06)
    user32.mouse_event(0x0002, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTDOWN
    try:
        for index in range(1, steps + 1):
            value = index / steps
            eased = value * value * (3.0 - 2.0 * value)
            x = round(start_x + (end_x - start_x) * eased)
            y = round(start_y + (end_y - start_y) * eased)
            user32.SetCursorPos(x, y)
            time.sleep(duration / steps)
    finally:
        user32.mouse_event(0x0004, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTUP
    time.sleep(0.08)


def scroll_relative(
    hwnd: int,
    point: tuple[int, int],
    notches: int,
    source_size: tuple[int, int] | None = None,
) -> None:
    """Scroll inside the target window. Positive goes up; negative goes down."""
    rect = get_window_rect(hwnd)
    x, y = _map_point(rect, point, source_size)
    user32.SetCursorPos(x, y)
    wheel_delta = int(notches) * 120
    user32.mouse_event(0x0800, 0, 0, ctypes.c_ulong(wheel_delta & 0xFFFFFFFF).value, 0)
    time.sleep(0.08)


def escape_pressed() -> bool:
    return bool(user32.GetAsyncKeyState(0x1B) & 0x8000)
