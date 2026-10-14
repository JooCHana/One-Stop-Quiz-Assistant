from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageGrab

from engines import capture_app, answer_app
from engines.core import OfflineOCR, load_question_bank, analyze_page, capture_screen_region
from engines.recognizer import QuestionRecognizer, export_all
from engines.windows_control import focus_window


class Cancelled(Exception):
    pass


class RunControl:
    def __init__(self):
        self.stop = threading.Event()
        self.paused = threading.Event()

    def checkpoint(self):
        while self.paused.is_set() and not self.stop.is_set():
            self.stop.wait(.05)
        if self.stop.is_set():
            raise Cancelled()

    def wait(self, seconds):
        remaining = seconds
        while remaining > 0:
            self.checkpoint()
            tick = min(.05, remaining)
            start = time.monotonic()
            self.stop.wait(tick)
            remaining -= time.monotonic() - start
        self.checkpoint()


class StopToken:
    """The existing answer engine gets pause-aware cancellation at each boundary."""
    def __init__(self, control):
        self.control = control

    def is_set(self):
        try:
            self.control.checkpoint()
            return False
        except Cancelled:
            return True

    def set(self):
        self.control.stop.set()


class AnswerRunner(answer_app.QuizAssistantApp):
    def __init__(self, bank, control, emit):
        self.bank = bank
        self.stop_event = StopToken(control)
        self.ocr = OfflineOCR()
        self.emit = emit
        self.messages = self
        self.failure = None

    def put(self, event):
        if event[0] == 'status':
            self.emit('status', event[1])

    def log(self, message):
        self.emit('log', message)

    def set_status(self, message):
        self.emit('status', message)

    def _report_worker_error(self, title, exc):
        self.failure = exc


class Workflow:
    def __init__(self, settings, control, emit):
        self.settings = settings
        self.control = control
        self.emit = emit
        self.output = Path(settings['cache']) / datetime.now().strftime('任务_%Y%m%d_%H%M%S_%f')
        self.output.mkdir(parents=True, exist_ok=False)
        self.emit('directory', str(self.output))

    def run(self, mode):
        if mode == 'capture':
            return self.capture()
        if mode == 'recognize':
            return self.recognize(self.settings['images'])
        if mode == 'answer':
            return self.answer(self.settings['bank'])
        images = self.capture()
        _, bank_path = self.recognize(images)
        self.control.checkpoint()
        if self.settings['handoff_wait']:
            self.control.paused.set()
            self.emit('handoff', '题库已生成。请切到答题起始页，按继续或暂停热键接力答题。')
            self.control.checkpoint()
        else:
            self.emit('log', f"题库已生成，{self.settings['handoff_seconds']} 秒后开始答题，请切到答题起始页。")
            self.control.wait(self.settings['handoff_seconds'])
        self.answer(bank_path)

    def capture(self):
        s = self.settings
        self.emit('stage', 'capture')
        self.emit('hide', None)
        self.emit('log', '准备截图，3 秒后开始；暂停和停止热键在其他窗口也有效。')
        self.control.wait(3)
        folder = self.output / '截图'
        folder.mkdir()
        paths = []
        for index in range(s['count']):
            self.control.checkpoint()
            if s['capture_method'] == '系统截图':
                before = capture_app.clipboard_sequence()
                capture_app.press_snipping_shortcut()
                self.control.wait(s['snip_wait'])
                left, top, right, bottom = s['region']
                capture_app.mouse_drag((left, top), (right, bottom), .35, self.control.stop)
                deadline = time.monotonic()+8
                image = None
                while time.monotonic() < deadline:
                    self.control.checkpoint()
                    if capture_app.clipboard_sequence() != before:
                        item = ImageGrab.grabclipboard()
                        if isinstance(item, Image.Image):
                            image = item.copy()
                            break
                    self.control.wait(.1)
                if image is None:
                    raise ValueError('系统截图未返回图片，请增大截图等待时间或切换直接截图。')
            else:
                image = ImageGrab.grab(bbox=tuple(s['region']), all_screens=True)
            self.control.checkpoint()
            path = folder / f"{s['first_number']+index:04d}.png"
            image.save(path)
            paths.append(str(path))
            self.emit('progress', ((index+1)/s['count']*100, f"已截图 {index+1}/{s['count']}"))
            self.emit('log', f'已保存截图：{path.name}')
            if index+1 < s['count']:
                self.control.wait(.2)
                if s['turn_mode'] == '点击下一题':
                    capture_app.mouse_click(tuple(s['next_point']), self.control.stop)
                else:
                    capture_app.mouse_drag(tuple(s['swipe_start']), tuple(s['swipe_end']), s['swipe_duration'], self.control.stop)
                self.control.wait(s['delay'])
        self.emit('images', paths)
        self.emit('log', f'截图完成，共 {len(paths)} 张。')
        return paths

    def recognize(self, paths):
        self.emit('stage', 'recognize')
        self.emit('show', None)
        reader = QuestionRecognizer()
        questions, failures = [], []
        for index, path in enumerate(paths):
            self.control.checkpoint()
            self.emit('preview', path)
            try:
                q = reader.recognize(path)
                questions.append(q)
                self.emit('question', q)
                self.emit('log', f"{Path(path).name}：{q.stem[:48]}；答案 {q.answer or '未检出'}")
            except Exception as exc:
                failures.append({'file': str(path), 'error': str(exc)})
                self.emit('log', f'识别失败：{Path(path).name}：{exc}')
            self.emit('progress', ((index+1)/len(paths)*100, f'已识别 {index+1}/{len(paths)}'))
        self.control.checkpoint()
        if failures:
            (self.output/'识别失败.json').write_text(json.dumps(failures, ensure_ascii=False, indent=2), encoding='utf-8')
        if not questions:
            raise ValueError('没有识别出可导出的题目，请查看运行日志。')
        word, excel, data = export_all(questions, self.output/'题库')
        self.emit('bank', str(excel))
        self.emit('log', f'已生成 Word、Excel 和 JSON 题库：{excel.parent}')
        if failures:
            raise ValueError(f'{len(questions)} 张识别成功并已导出，{len(failures)} 张失败。请补充识别失败的图片后再答题。')
        if any(not q.answer for q in questions):
            self.emit('log', '部分题目未检出答案，可在“题目结果”双击编辑后重新导出。')
            if self.settings.get('mode') == 'relay':
                raise ValueError('题库已保存，但部分题目缺少答案。请在题目结果中补齐并导出，再独立启动答题。')
        return questions, excel

    def answer(self, bank_path):
        s = self.settings
        self.control.checkpoint()
        bank = load_question_bank(bank_path, lambda msg: self.emit('log', msg))
        self.emit('stage', 'answer')
        self.emit('hide', None)
        self.emit('log', f'题库导入成功，共 {len(bank)} 道题；3 秒后开始答题。')
        self.control.wait(3)
        runner = AnswerRunner(bank, self.control, self.emit)
        # Guard every OS action, including actions between the engine's loops.
        originals = {}
        for name in ('focus_window', 'click_relative', 'move_relative', 'scroll_relative', 'drag_relative'):
            original = getattr(answer_app, name)
            originals[name] = original
            def guarded(*args, _original=original, **kwargs):
                self.control.checkpoint()
                return _original(*args, **kwargs)
            setattr(answer_app, name, guarded)
        try:
            runner._auto_worker(s['window'], s['delay'], s['threshold'], s['answer_switch'], s['swipe_duration'])
        finally:
            for name, original in originals.items():
                setattr(answer_app, name, original)
        self.control.checkpoint()
        if runner.failure:
            raise runner.failure


def probe(bank_path, window, threshold):
    bank = load_question_bank(bank_path)
    image = capture_screen_region(focus_window(window.hwnd))
    result = analyze_page(image, bank, OfflineOCR(), threshold)
    return f'导入 {len(bank)} 道题；当前匹配题号 {result.question.qid}，匹配度 {result.score:.1f}%，答案 {"、".join(result.question.answer)}。未执行点击。'
