import os
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

from main import App
from engines.windows_control import WindowInfo


class ImportAndStartTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = tk.Tk()
        cls.root.withdraw()
        cls.app = App(cls.root, Path(cls.temp.name)/'settings.json')
        cls.app.show = lambda: None
        cls.app.hide = lambda: None

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()
        cls.temp.cleanup()

    def setUp(self):
        self.app.vars['relay'].set(False)
        self.app.select_module('answer')
        self.app.windows = [WindowInfo(123, '测试窗口', (0,0,1000,1000))]
        self.app.window_combo.configure(values=['测试窗口'])
        self.app.window_combo.current(0)

    def drain(self):
        deadline = time.monotonic()+5
        while time.monotonic() < deadline:
            self.root.update()
            if not self.app.running and self.app.events.empty():
                return
            time.sleep(.02)
        self.fail('UI did not return to idle')

    @unittest.skipUnless(os.getenv('QUIZ_BANK'), 'QUIZ_BANK not set')
    def test_file_selection_imports_and_enables_start(self):
        with patch('main.filedialog.askopenfilename', return_value=os.environ['QUIZ_BANK']):
            self.app.choose_bank()
        self.drain()
        self.assertIn('已导入 40 道题', self.app.bank_info.get())
        self.assertEqual(len(self.app.bank_tree.get_children()), 40)
        self.assertFalse(self.app.start_button.instate(['disabled']))
        with patch('main.Workflow') as workflow:
            self.app.start_button.invoke()
            self.drain()
            workflow.return_value.run.assert_called_once_with('answer')
        self.assertFalse(self.app.start_button.instate(['disabled']))

    def test_failed_import_releases_button(self):
        self.app.vars['bank'].set('file-not-found.xls')
        self.app.check_bank()
        self.drain()
        self.assertIn('导入失败', self.app.bank_info.get())
        self.assertFalse(self.app.start_button.instate(['disabled']))
        self.assertEqual(len(self.app.bank_tree.get_children()), 0)

    def test_preview_clears_when_file_changes(self):
        self.app.bank_tree.insert('', 'end', values=('1','单选题','题干','A.甲','A'))
        self.app.vars['bank'].set('another.xls')
        self.assertEqual(len(self.app.bank_tree.get_children()), 0)
        self.assertEqual(self.app.loaded_bank_path, '')

    @unittest.skipUnless(os.getenv('QUIZ_BANK'), 'QUIZ_BANK not set')
    def test_inactive_capture_settings_do_not_block_answer(self):
        self.app.vars['bank'].set(os.environ['QUIZ_BANK'])
        old = self.app.vars['count'].get()
        self.app.vars['count'].set('')
        try:
            self.assertEqual(self.app.snapshot('answer')['window'].hwnd, 123)
        finally:
            self.app.vars['count'].set(old)

    def test_error_in_completion_does_not_stop_ui_polling(self):
        self.app.set_running(True)
        self.app.emit('finished', None)
        with patch.object(self.app, 'save', side_effect=RuntimeError('simulated failure')), patch('main.CONFIG_DIR', Path(self.temp.name)):
            self.app.poll()
        self.app.emit('status', '轮询仍正常')
        self.drain()
        self.assertEqual(self.app.status.get(), '轮询仍正常')
        self.assertFalse(self.app.start_button.instate(['disabled']))


if __name__ == '__main__':
    unittest.main()
