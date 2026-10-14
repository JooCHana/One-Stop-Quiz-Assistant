import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from PIL import Image
from engines.core import _questions_from_rows, load_question_bank, analyze_page, OfflineOCR
from engines.recognizer import QuestionRecognizer, export_all
from task_pipeline import Workflow, RunControl, Cancelled
from engines import answer_app
from engines.core import Question, PageAnalysis


class ImportTests(unittest.TestCase):
    def test_recognizer_headers_and_answer_priority(self):
        rows = [['题库说明'], ['题干', '题号', '题型', '选择项1', '选择项2', '答案1', '答案2'],
                ['测试一', 1, '', '甲', '乙', 'B', 'A'], ['测试二', 2, '判断', '正确', '错误', 'A', 'B']]
        bank = _questions_from_rows(rows, 'Sheet1')
        self.assertEqual(len(bank), 2)
        self.assertEqual(bank[0].answer, ('B',))
        self.assertEqual(bank[0].options, {'A': '甲', 'B': '乙'})
        self.assertEqual(bank[1].qtype, '判断题')

    def test_existing_headers_and_multiple_answers(self):
        bank = _questions_from_rows([['试题内容', '正确选项', 'A', 'B'], ['题目', 'AB', '一', '二']], 'old')
        self.assertEqual(bank[0].answer, ('A', 'B'))

    @unittest.skipUnless(os.getenv('QUIZ_BANK'), 'QUIZ_BANK not set')
    def test_real_bank(self):
        bank = load_question_bank(os.environ['QUIZ_BANK'])
        self.assertEqual(len(bank), 40)
        self.assertEqual(bank[1].answer, ('A',))
        self.assertEqual(bank[1].options['A'], '“高”')

    @unittest.skipUnless(os.getenv('OCR_SAMPLE'), 'OCR_SAMPLE not set')
    def test_ocr_export_import_and_answer_match(self):
        q = QuestionRecognizer().recognize(os.environ['OCR_SAMPLE'])
        self.assertEqual(q.answer, 'A')
        with tempfile.TemporaryDirectory() as folder:
            _, excel, _ = export_all([q], folder)
            bank = load_question_bank(excel)
            self.assertEqual(bank[0].answer, ('A',))
            with Image.open(os.environ['OCR_SAMPLE']) as image:
                result = analyze_page(image, bank, OfflineOCR(), 80)
            self.assertGreaterEqual(result.score, 80)
            self.assertIn('A', result.answer_points)


class WorkflowTests(unittest.TestCase):
    def test_pause_and_stop_interrupts_wait(self):
        control = RunControl()
        control.paused.set()
        finished = threading.Event()
        def wait():
            with self.assertRaises(Cancelled):
                control.wait(10)
            finished.set()
        thread = threading.Thread(target=wait)
        thread.start()
        self.assertFalse(finished.wait(.1))
        control.stop.set()
        self.assertTrue(finished.wait(1))
        thread.join()

    def test_relay_propagates_generated_files(self):
        with tempfile.TemporaryDirectory() as folder:
            calls = []
            workflow = Workflow(dict(cache=folder, handoff_wait=False, handoff_seconds=0), RunControl(), lambda *args: None)
            workflow.capture = lambda: ['a.png', 'b.png']
            workflow.recognize = lambda images: (calls.append(images), 'generated.xls')
            workflow.answer = lambda path: calls.append(path)
            workflow.run('relay')
            self.assertEqual(calls, [['a.png', 'b.png'], 'generated.xls'])

    def test_handoff_does_not_answer_until_resumed(self):
        with tempfile.TemporaryDirectory() as folder:
            handoff, answered = threading.Event(), threading.Event()
            control = RunControl()
            workflow = Workflow(dict(cache=folder, handoff_wait=True), control,
                                lambda kind, value: handoff.set() if kind == 'handoff' else None)
            workflow.capture = lambda: ['image.png']
            workflow.recognize = lambda images: ([], 'bank.xls')
            workflow.answer = lambda path: answered.set()
            thread = threading.Thread(target=lambda: workflow.run('relay'))
            thread.start()
            self.assertTrue(handoff.wait(1))
            self.assertFalse(answered.wait(.1))
            control.paused.clear()
            self.assertTrue(answered.wait(1))
            thread.join()

    def test_capture_saves_exact_count_and_does_not_turn_after_last(self):
        with tempfile.TemporaryDirectory() as folder:
            control = RunControl()
            control.wait = lambda seconds: control.checkpoint()
            settings = dict(cache=folder, count=3, first_number=8, capture_method='直接截图', region=[0,0,400,400],
                            turn_mode='点击下一题', next_point=[30,40], delay=1)
            workflow = Workflow(settings, control, lambda *args: None)
            with patch('task_pipeline.ImageGrab.grab', return_value=Image.new('RGB',(400,400),'white')), patch('task_pipeline.capture_app.mouse_click') as click:
                paths = workflow.capture()
            self.assertEqual([Path(p).name for p in paths], ['0008.png', '0009.png', '0010.png'])
            self.assertTrue(all(Path(p).is_file() for p in paths))
            self.assertEqual(click.call_count, 2)

    def test_answer_worker_selects_answer_and_stops_on_last_question(self):
        with tempfile.TemporaryDirectory() as folder:
            control = RunControl()
            control.wait = lambda seconds: control.checkpoint()
            settings = dict(cache=folder, window=SimpleNamespace(hwnd=123), delay=1,
                            threshold=80, answer_switch=answer_app.SWITCH_CLICK, swipe_duration=.45)
            workflow = Workflow(settings, control, lambda *args: None)
            question = Question('1', '测试题', '单选题', ('B',), {'A':'甲','B':'乙'})
            image = Image.new('RGB', (700,900), 'white')
            analysis = PageAnalysis(question, 99, 60, '测试题', [], image.size, 100, (1,1), {'B':(100,400)}, None, True)
            with patch('task_pipeline.load_question_bank', return_value=[question]), \
                 patch.object(answer_app, 'focus_window', return_value=(0,0,700,900)), \
                 patch.object(answer_app, 'get_window_rect', return_value=(0,0,700,900)), \
                 patch.object(answer_app, 'capture_screen_region', return_value=image), \
                 patch.object(answer_app, 'analyze_page', return_value=analysis), \
                 patch.object(answer_app, 'move_relative'), \
                 patch.object(answer_app, 'click_relative') as click, \
                 patch.object(answer_app, 'scroll_relative') as scroll, \
                 patch.object(answer_app, 'drag_relative') as drag, \
                 patch.object(answer_app, 'option_visual_change_score', return_value=10), \
                 patch.object(answer_app, 'wait_for_escape', return_value=False), \
                 patch.object(answer_app, 'escape_pressed', return_value=False):
                workflow.answer('bank.xls')
            click.assert_called_once_with(123, (100,400), (700,900))
            scroll.assert_not_called()
            drag.assert_not_called()


if __name__ == '__main__':
    unittest.main()
