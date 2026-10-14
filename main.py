from __future__ import annotations

import argparse
import ctypes
import json
import os
import queue
import threading
import traceback
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog

from PIL import Image, ImageTk

from engines.capture_app import DragOverlay
from engines.windows_control import enable_dpi_awareness, list_windows
from engines.answer_app import SWITCH_MODES, SWITCH_CLICK
from engines.core import load_question_bank
from engines.recognizer import IMAGE_TYPES, export_all
from task_pipeline import Workflow, RunControl, Cancelled, probe

APP_NAME = '一站式题库助手'
VERSION = '1.0.1'
CONFIG_DIR = Path(os.getenv('APPDATA', str(Path.home()))) / APP_NAME
CONFIG_FILE = CONFIG_DIR / 'settings.json'
MODULES = [('capture', '01', '截图翻页', '连续截图 · 自动翻页 · 按序归档'),
           ('recognize', '02', '截图识别', '离线识别 · 蓝色即答案 · 导出题库'),
           ('answer', '03', '自动答题', '导入题库 · 匹配题目 · 自动选择')]
LIGHT = dict(bg='#F3F6FA', surface='#FFFFFF', text='#18283B', muted='#65768B', border='#DCE4EE',
             accent='#087F8C', selected='#E6F5F4', log='#F8FAFC', danger='#C24147')
DARK = dict(bg='#101A28', surface='#192637', text='#E8EFF7', muted='#9AACBF', border='#35465B',
            accent='#39C8BA', selected='#203E48', log='#142030', danger='#FC8D91')


class App:
    def __init__(self, root, config_file=CONFIG_FILE):
        self.root, self.config_file = root, config_file
        # Tk point fonts follow Windows DPI; scale fixed pixel layouts with them.
        self.scale = min(float(root.tk.call('tk', 'scaling'))/1.333333,
                         root.winfo_screenwidth()/1160, root.winfo_screenheight()/940)
        self.scale = max(1.0, self.scale)
        root.tk.call('tk', 'scaling', self.scale*1.333333)
        window_height = min(round(860*self.scale), root.winfo_screenheight()-round(120*self.scale))
        self.default_geometry = f'{round(1120*self.scale)}x{window_height}+30+30'
        try:
            self.saved = json.loads(config_file.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            self.saved = {}
        self.root.title(f'{APP_NAME}  |  从截图到答题，一站完成')
        self.root.geometry(self.saved.get('geometry', self.default_geometry))
        self.root.minsize(round(1040*self.scale), round(790*self.scale))
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        self.events = queue.Queue()
        self.worker = None
        self.busy = False
        self.loaded_bank_path = ''
        self.control = RunControl()
        self.closing = False
        self.waiting_handoff = False
        self.active_stage = None
        self.results = []
        self.images = [p for p in self.saved.get('images', []) if Path(p).is_file()]
        self.last_dir = self.saved.get('last_dir', '')
        self.region = self.saved.get('region')
        self.next_point = self.saved.get('next_point')
        self.swipe_start = self.saved.get('swipe_start')
        self.swipe_end = self.saved.get('swipe_end')
        self.hotkeys = self.saved.get('hotkeys', ['F8', 'F9', 'Esc'])
        self.key_down = set()
        self.vars = {}
        defaults = dict(module='capture', theme='浅色', cache=str(Path.home()/'Pictures'/APP_NAME),
                        delay='1.2', count='10', first_number='1', turn_mode='点击下一题',
                        capture_method='直接截图', snip_wait='1.0', swipe_duration='0.45',
                        bank='', threshold='80', answer_switch=SWITCH_CLICK, handoff_seconds='10')
        for name, default in defaults.items():
            self.vars[name] = tk.StringVar(value=str(self.saved.get(name, default)))
        for name, default in dict(topmost=False, relay=False, handoff_wait=True).items():
            self.vars[name] = tk.BooleanVar(value=bool(self.saved.get(name, default)))
        self.status = tk.StringVar(value='准备就绪')
        self.image_info = tk.StringVar()
        self.region_info = tk.StringVar()
        self.turn_info = tk.StringVar()
        self.window_var = tk.StringVar()
        self.bank_info = tk.StringVar(value='尚未导入题库，选择文件后会自动读取')
        self.progress = tk.DoubleVar(value=0)
        self.style = ttk.Style(root)
        self.style.theme_use('clam')
        self.build()
        self.apply_theme()
        self.refresh_descriptions()
        self.refresh_windows()
        self.select_module(self.vars['module'].get())
        self.root.attributes('-topmost', self.vars['topmost'].get())
        self.root.after(100, self.poll)
        self.root.report_callback_exception = self.report_ui_error
        self.vars['bank'].trace_add('write', self.bank_path_changed)
        if self.vars['bank'].get():
            self.root.after(250, self.check_bank)
        self.log('欢迎使用。三个模块可独立运行；开启接力可依次完成截图、建库和答题。')

    def build(self):
        self.shell = ttk.Frame(self.root, padding=(24, 12))
        self.shell.pack(fill='both', expand=True)
        self.shell.columnconfigure(0, weight=1)
        self.shell.rowconfigure(3, weight=1)
        header = ttk.Frame(self.shell)
        header.grid(row=0, column=0, sticky='ew', pady=(0, 10))
        title = ttk.Frame(header)
        title.pack(side='left')
        ttk.Label(title, text=APP_NAME, style='Title.TLabel').pack(anchor='w')
        ttk.Label(title, text='截图、识别、答题，在同一个工作台完成。', style='Muted.TLabel').pack(anchor='w', pady=(4, 0))
        for label, command in [('×', self.close), ('□', self.maximize), ('—', self.root.iconify),
                               ('关于', self.about), ('设置', self.settings)]:
            ttk.Button(header, text=label, command=command, width=5).pack(side='right', padx=3)
        ttk.Checkbutton(header, text='置顶', variable=self.vars['topmost'], command=self.set_topmost).pack(side='right', padx=10)

        cards = ttk.Frame(self.shell)
        cards.grid(row=1, column=0, sticky='ew')
        self.cards = {}
        for index, (key, number, name, desc) in enumerate(MODULES):
            cards.columnconfigure(index, weight=1, uniform='cards')
            card = tk.Frame(cards, highlightthickness=2, padx=18, pady=8, cursor='hand2')
            card.grid(row=0, column=index, sticky='nsew', padx=(0 if index == 0 else 7, 0 if index == 2 else 7))
            icon = {'capture': '▣', 'recognize': '▤', 'answer': '✓'}[key]
            a = tk.Label(card, text=f'{icon}   {name}', font=('Microsoft YaHei UI', 14, 'bold'), anchor='w')
            a.pack(fill='x')
            b = tk.Label(card, text=desc, font=('Microsoft YaHei UI', 9), anchor='w')
            b.pack(fill='x', pady=(7, 0))
            self.cards[key] = (card, a, b)
            for widget in (card, a, b):
                widget.bind('<Button-1>', lambda event, k=key: self.select_module(k))
                widget.bind('<Enter>', lambda event, c=card: c.configure(highlightbackground=self.colors['accent']))
                widget.bind('<Leave>', lambda event: self.paint_cards())

        flow = ttk.Frame(self.shell)
        flow.grid(row=2, column=0, sticky='ew', pady=(8, 6))
        self.relay_check = ttk.Checkbutton(flow, text='接力运行', variable=self.vars['relay'], command=self.update_mode)
        self.relay_check.pack(side='left')
        self.flow_label = ttk.Label(flow, text='截图  →  识别建库  →  自动答题', style='Muted.TLabel')
        self.flow_label.pack(side='left', padx=15)
        self.mode_label = ttk.Label(flow, text='独立模式', style='Accent.TLabel')
        self.mode_label.pack(side='right')

        self.pane = ttk.Panedwindow(self.shell, orient='vertical')
        self.pane.grid(row=3, column=0, sticky='nsew')
        work = ttk.Frame(self.pane)
        self.pane.add(work, weight=3)
        toolbar = ttk.Frame(work)
        toolbar.pack(fill='x', pady=(0, 6))
        ttk.Label(toolbar, text='工作记录', style='Section.TLabel').pack(side='left')
        ttk.Button(toolbar, text='导出日志', command=self.export_logs).pack(side='right', padx=(6, 0))
        ttk.Button(toolbar, text='清空日志', command=self.clear_logs).pack(side='right')
        self.tabs = ttk.Notebook(work)
        self.tabs.pack(fill='both', expand=True)
        log_frame = ttk.Frame(self.tabs)
        self.tabs.add(log_frame, text='运行日志')
        self.log_text = tk.Text(log_frame, wrap='word', state='disabled', height=6, relief='flat', padx=12, pady=8,
                                font=('Microsoft YaHei UI', 10))
        scrollbar = ttk.Scrollbar(log_frame, orient='vertical', command=self.log_text.yview)
        scrollbar.pack(side='right', fill='y')
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(fill='both', expand=True)
        result_frame = ttk.Frame(self.tabs)
        self.tabs.add(result_frame, text='题目结果')
        result_bar = ttk.Frame(result_frame)
        result_bar.pack(fill='x', pady=4)
        ttk.Label(result_bar, text='双击题目可校对修改', style='Muted.TLabel').pack(side='left', padx=8)
        self.export_button = ttk.Button(result_bar, text='导出题库', command=self.export_results)
        self.export_button.pack(side='right', padx=6)
        self.tree = ttk.Treeview(result_frame, columns=('no', 'stem', 'options', 'answer'), show='headings', height=5)
        for col, name, width in [('no', '题号', 55), ('stem', '题干', 450), ('options', '选项', 280), ('answer', '答案', 70)]:
            self.tree.heading(col, text=name)
            self.tree.column(col, width=width, minwidth=45, stretch=col in ('stem', 'options'))
        scrollbar = ttk.Scrollbar(result_frame, orient='vertical', command=self.tree.yview)
        scrollbar.pack(side='right', fill='y')
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(fill='both', expand=True)
        self.tree.bind('<Double-1>', self.edit_question)
        self.bank_frame = ttk.Frame(self.tabs)
        self.tabs.add(self.bank_frame, text='题库预览')
        self.bank_tree = ttk.Treeview(self.bank_frame, columns=('no', 'type', 'stem', 'options', 'answer'), show='headings', height=5)
        for col, name, width in [('no', '题号', 60), ('type', '题型', 85), ('stem', '题干', 430), ('options', '选项', 270), ('answer', '正确答案', 80)]:
            self.bank_tree.heading(col, text=name)
            self.bank_tree.column(col, width=round(width*self.scale), minwidth=45, stretch=col in ('stem', 'options'))
        bank_scroll = ttk.Scrollbar(self.bank_frame, orient='vertical', command=self.bank_tree.yview)
        bank_scroll.pack(side='right', fill='y')
        self.bank_tree.configure(yscrollcommand=bank_scroll.set)
        self.bank_tree.pack(fill='both', expand=True)
        self.preview_frame = ttk.Frame(self.tabs)
        self.tabs.add(self.preview_frame, text='图片预览')
        self.preview_label = ttk.Label(self.preview_frame, text='识别时显示当前截图；也可选择已导入的图片预览。', anchor='center')
        self.preview_label.pack(fill='both', expand=True)
        self.preview_path = None
        self.preview_frame.bind('<Configure>', lambda event: self.render_preview())

        params = ttk.Frame(self.pane, padding=(0, 10, 0, 0))
        self.pane.add(params, weight=0)
        ttk.Label(params, text='运行参数', style='Section.TLabel').pack(anchor='w', pady=(0, 7))
        common = ttk.Frame(params)
        common.pack(fill='x')
        common.columnconfigure(1, weight=1)
        common.columnconfigure(5, weight=1)
        ttk.Label(common, text='截图区域').grid(row=0, column=0, sticky='w', padx=(0, 8))
        ttk.Entry(common, textvariable=self.region_info, state='readonly').grid(row=0, column=1, sticky='ew')
        self.region_button = ttk.Button(common, text='框选', command=lambda: self.pick('region'), width=6)
        self.region_button.grid(row=0, column=2, padx=(6, 16))
        ttk.Label(common, text='步骤延时（秒）').grid(row=0, column=3, sticky='w', padx=(0, 8))
        ttk.Entry(common, textvariable=self.vars['delay'], width=7).grid(row=0, column=4, sticky='w')
        ttk.Button(common, text='热键设置', command=self.settings).grid(row=0, column=6, sticky='e', padx=(12, 0))
        ttk.Label(common, text='缓存路径').grid(row=1, column=0, sticky='w', pady=(8, 0))
        ttk.Entry(common, textvariable=self.vars['cache']).grid(row=1, column=1, columnspan=5, sticky='ew', pady=(8, 0))
        ttk.Button(common, text='选择目录', command=self.choose_cache).grid(row=1, column=6, padx=(12, 0), pady=(8, 0))
        ttk.Separator(params).pack(fill='x', pady=7)
        self.module_host = ttk.Frame(params, height=round(140*self.scale))
        self.module_host.pack(fill='x')
        self.module_host.pack_propagate(False)
        self.module_frames = {}
        for key, *_ in MODULES:
            self.module_frames[key] = ttk.Frame(self.module_host)
        self.build_capture(self.module_frames['capture'])
        self.build_recognize(self.module_frames['recognize'])
        self.build_answer(self.module_frames['answer'])

        footer = ttk.Frame(self.shell)
        footer.grid(row=4, column=0, sticky='ew', pady=(12, 0))
        footer.columnconfigure(0, weight=1)
        ttk.Progressbar(footer, variable=self.progress, maximum=100).grid(row=0, column=0, columnspan=5, sticky='ew', pady=(0, 8))
        ttk.Label(footer, textvariable=self.status, style='Muted.TLabel', wraplength=round(470*self.scale)).grid(row=1, column=0, sticky='w')
        self.start_button = ttk.Button(footer, text='开始', style='Primary.TButton', command=self.start, width=11)
        self.start_button.grid(row=1, column=1, padx=5)
        self.pause_button = ttk.Button(footer, text='暂停', command=self.pause, width=9, state='disabled')
        self.pause_button.grid(row=1, column=2, padx=5)
        self.stop_button = ttk.Button(footer, text='停止', command=self.stop, width=9, state='disabled')
        self.stop_button.grid(row=1, column=3, padx=5)
        ttk.Button(footer, text='打开目录', command=self.open_dir, width=11).grid(row=1, column=4, padx=(5, 0))
        self.root.after(200, self.restore_sash)

    def build_capture(self, frame):
        frame.columnconfigure(7, weight=1)
        for col, label, key in [(0, '截图数量', 'count'), (2, '起始编号', 'first_number'), (4, '拖动耗时（秒）', 'swipe_duration')]:
            ttk.Label(frame, text=label).grid(row=0, column=col, sticky='w', padx=(0, 8))
            ttk.Entry(frame, textvariable=self.vars[key], width=7).grid(row=0, column=col+1, sticky='w', padx=(0, 16))
        ttk.Combobox(frame, textvariable=self.vars['capture_method'], values=['直接截图', '系统截图'], state='readonly', width=12).grid(row=0, column=7, sticky='e')
        ttk.Label(frame, text='翻页方式').grid(row=1, column=0, pady=10, sticky='w')
        ttk.Combobox(frame, textvariable=self.vars['turn_mode'], values=['点击下一题', '拖动翻页'], state='readonly', width=13).grid(row=1, column=1, columnspan=2, sticky='w')
        ttk.Button(frame, text='设置下一题位置', command=lambda: self.pick('next')).grid(row=1, column=3, columnspan=2, sticky='w')
        ttk.Button(frame, text='设置拖动手势', command=lambda: self.pick('swipe')).grid(row=1, column=5, columnspan=2, sticky='w')
        ttk.Label(frame, textvariable=self.turn_info, style='Muted.TLabel').grid(row=2, column=0, columnspan=8, sticky='w')

    def build_recognize(self, frame):
        bar = ttk.Frame(frame)
        bar.pack(fill='x')
        ttk.Button(bar, text='选择图片', command=self.choose_images).pack(side='left')
        ttk.Button(bar, text='选择文件夹', command=self.choose_image_folder).pack(side='left', padx=8)
        ttk.Button(bar, text='清空列表', command=self.clear_images).pack(side='left')
        ttk.Label(bar, textvariable=self.image_info, style='Accent.TLabel').pack(side='left', padx=18)
        self.image_combo = ttk.Combobox(frame, state='readonly')
        self.image_combo.pack(fill='x', pady=9)
        self.image_combo.bind('<<ComboboxSelected>>', self.choose_preview)
        ttk.Label(frame, text='本地离线识别；蓝色勾选直接作为正确答案。完成后自动生成 Word / Excel / JSON。', style='Muted.TLabel').pack(anchor='w')

    def build_answer(self, frame):
        frame.columnconfigure(1, weight=1)
        ttk.Label(frame, text='题库文件').grid(row=0, column=0, padx=(0, 8), sticky='w')
        ttk.Entry(frame, textvariable=self.vars['bank']).grid(row=0, column=1, columnspan=3, sticky='ew')
        ttk.Button(frame, text='选择', command=self.choose_bank, width=7).grid(row=0, column=4, padx=6)
        self.load_bank_button = ttk.Button(frame, text='重新导入', command=self.check_bank)
        self.load_bank_button.grid(row=0, column=5)
        ttk.Label(frame, textvariable=self.bank_info, style='Accent.TLabel').grid(row=1, column=0, columnspan=6, sticky='w', pady=(4, 0))
        ttk.Label(frame, text='答题窗口').grid(row=2, column=0, sticky='w', pady=8)
        self.window_combo = ttk.Combobox(frame, textvariable=self.window_var, state='readonly')
        self.window_combo.grid(row=2, column=1, columnspan=3, sticky='ew', pady=8)
        self.window_combo.bind('<<ComboboxSelected>>', lambda event: self.update_mode())
        ttk.Button(frame, text='刷新', command=self.refresh_windows, width=7).grid(row=2, column=4, padx=6)
        self.probe_button = ttk.Button(frame, text='仅识别测试', command=self.probe)
        self.probe_button.grid(row=2, column=5)
        ttk.Label(frame, text='切题方式').grid(row=3, column=0, sticky='w')
        ttk.Combobox(frame, textvariable=self.vars['answer_switch'], values=SWITCH_MODES, state='readonly', width=32).grid(row=3, column=1, sticky='w')
        ttk.Label(frame, text='最低匹配度 %').grid(row=3, column=2, padx=8)
        ttk.Entry(frame, textvariable=self.vars['threshold'], width=6).grid(row=3, column=3, sticky='w')
        ttk.Label(frame, text='支持识别结果 .xls', style='Muted.TLabel').grid(row=3, column=4, columnspan=2, sticky='e')

    def apply_theme(self):
        c = self.colors = DARK if self.vars['theme'].get() == '深色' else LIGHT
        self.root.configure(bg=c['bg'])
        self.style.configure('.', font=('Microsoft YaHei UI', 10), background=c['bg'], foreground=c['text'], bordercolor=c['border'])
        for kind in ('TFrame', 'TLabel', 'TCheckbutton'):
            self.style.configure(kind, background=c['bg'], foreground=c['text'])
        self.style.configure('TCheckbutton', indicatorsize=round(14*self.scale))
        self.style.map('TCheckbutton', background=[('active', c['bg'])])
        self.style.configure('Title.TLabel', font=('Microsoft YaHei UI', 22, 'bold'))
        self.style.configure('Section.TLabel', font=('Microsoft YaHei UI', 11, 'bold'))
        self.style.configure('Muted.TLabel', foreground=c['muted'], font=('Microsoft YaHei UI', 9))
        self.style.configure('Accent.TLabel', foreground=c['accent'])
        self.style.configure('TButton', background=c['surface'], foreground=c['text'], padding=(10, 7), borderwidth=1)
        self.style.map('TButton', background=[('active', c['selected'])], foreground=[('disabled', c['muted'])])
        self.style.configure('Primary.TButton', background=c['accent'], foreground='#FFFFFF')
        self.style.map('Primary.TButton', background=[('disabled', c['border']), ('active', '#116E7C')])
        for kind in ('TEntry', 'TCombobox'):
            self.style.configure(kind, fieldbackground=c['surface'], foreground=c['text'], padding=5)
            self.style.map(kind, fieldbackground=[('readonly', c['surface'])], foreground=[('readonly', c['text'])])
        self.style.configure('Treeview', background=c['surface'], fieldbackground=c['surface'], foreground=c['text'], rowheight=round(29*self.scale))
        self.style.configure('Treeview.Heading', background=c['bg'], foreground=c['muted'], padding=6)
        self.style.map('Treeview', background=[('selected', c['selected'])], foreground=[('selected', c['text'])])
        self.style.configure('TNotebook.Tab', padding=(16, 7))
        self.style.map('TNotebook.Tab', background=[('selected', c['surface'])], foreground=[('selected', c['accent'])])
        self.style.configure('Horizontal.TProgressbar', background=c['accent'], troughcolor=c['border'], borderwidth=0)
        self.log_text.configure(bg=c['log'], fg=c['text'], insertbackground=c['text'])
        self.root.option_add('*TCombobox*Listbox.background', c['surface'])
        self.root.option_add('*TCombobox*Listbox.foreground', c['text'])
        self.paint_cards()

    def paint_cards(self):
        for key, (card, a, b) in self.cards.items():
            selected = self.vars['module'].get() == key
            bg = self.colors['selected'] if selected else self.colors['surface']
            card.configure(bg=bg, highlightbackground=self.colors['accent'] if selected else self.colors['border'])
            a.configure(bg=bg, fg=self.colors['accent'] if selected else self.colors['text'])
            b.configure(bg=bg, fg=self.colors['muted'])

    def select_module(self, key):
        if key not in self.module_frames:
            key = 'capture'
        self.vars['module'].set(key)
        for frame in self.module_frames.values():
            frame.pack_forget()
        self.module_frames[key].pack(fill='both', expand=True)
        self.tabs.tab(self.preview_frame, state='normal' if key == 'recognize' else 'disabled')
        if key != 'recognize' and self.tabs.select() == str(self.preview_frame):
            self.tabs.select(0)
        self.paint_cards()
        self.update_mode()

    def update_mode(self):
        relay = self.vars['relay'].get()
        self.mode_label.configure(text='接力模式 · 可切换卡片配置各步骤' if relay else '独立模式')
        if not self.running:
            label = {'capture': '开始截图', 'recognize': '开始识别', 'answer': '开始答题'}[self.vars['module'].get()]
            self.start_button.configure(text='开始接力' if relay else label)

    @property
    def running(self):
        return self.busy or (self.worker is not None and self.worker.is_alive())

    def refresh_descriptions(self):
        self.region_info.set('未选择' if not self.region else ', '.join(map(str, self.region)))
        self.image_info.set(f'已选择 {len(self.images)} 张图片')
        self.image_combo.configure(values=[Path(p).name for p in self.images])
        if self.images:
            self.image_combo.current(0)
        self.turn_info.set(f'下一题：{self.next_point or "未设置"}    拖动：{self.swipe_start or "未设置"} → {self.swipe_end or "未设置"}')

    def refresh_windows(self):
        previous = self.window_var.get()
        self.windows = [w for w in list_windows() if not w.title.startswith(APP_NAME)]
        self.window_combo.configure(values=[w.display for w in self.windows])
        saved_title = self.saved.get('window_title', '')
        for i, w in enumerate(self.windows):
            if w.display == previous or w.title == saved_title:
                self.window_combo.current(i)
                break
        else:
            self.window_var.set('')

    def selected_window(self):
        i = self.window_combo.current()
        if not 0 <= i < len(self.windows):
            raise ValueError('请在自动答题卡片中选择答题窗口。')
        return self.windows[i]

    def choose_cache(self):
        if self.running:
            return
        value = filedialog.askdirectory(parent=self.root, title='选择缓存目录')
        if value:
            self.vars['cache'].set(value)

    def choose_bank(self):
        if self.running:
            return
        value = filedialog.askopenfilename(parent=self.root, title='选择题库', filetypes=[('题库文件', '*.xls *.xlsx *.xlsm *.csv *.tsv *.doc *.docx *.txt'), ('所有文件', '*.*')])
        if value:
            self.vars['bank'].set(value)
            self.check_bank()

    def choose_images(self):
        if self.running:
            return
        paths = filedialog.askopenfilenames(parent=self.root, title='选择截图', filetypes=[('图片', '*.png *.jpg *.jpeg *.bmp *.tif *.tiff *.webp')])
        self.images = list(dict.fromkeys(self.images + list(paths)))
        self.refresh_descriptions()

    def choose_image_folder(self):
        if self.running:
            return
        folder = filedialog.askdirectory(parent=self.root, title='选择截图文件夹')
        if folder:
            self.images = sorted(str(p) for p in Path(folder).iterdir() if p.suffix.lower() in IMAGE_TYPES)
            self.refresh_descriptions()

    def clear_images(self):
        if not self.running:
            self.images = []
            self.refresh_descriptions()

    def choose_preview(self, event=None):
        i = self.image_combo.current()
        if 0 <= i < len(self.images):
            self.preview_path = self.images[i]
            self.render_preview()
            self.tabs.select(self.preview_frame)

    def render_preview(self):
        if not self.preview_path:
            return
        try:
            with Image.open(self.preview_path) as source:
                image = source.copy()
            image.thumbnail((max(100, self.preview_frame.winfo_width()-16), max(80, self.preview_frame.winfo_height()-12)))
            self.preview_image = ImageTk.PhotoImage(image)
            self.preview_label.configure(image=self.preview_image, text='')
        except OSError as exc:
            self.preview_label.configure(image='', text=f'无法预览：{exc}')

    def pick(self, kind):
        if self.running:
            return
        self.root.withdraw()
        def done(*points):
            if kind == 'region':
                a, b = points
                self.region = [min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])]
            elif kind == 'next':
                self.next_point = list(points[0])
                self.vars['turn_mode'].set('点击下一题')
            else:
                self.swipe_start, self.swipe_end = map(list, points)
                self.vars['turn_mode'].set('拖动翻页')
            self.root.deiconify()
            self.refresh_descriptions()
        text = {'region': '拖框选择截图范围', 'next': '点击下一题按钮的位置（不会实际点击）', 'swipe': '拖动一次，设置翻页方向'}[kind]
        self.root.after(250, lambda: DragOverlay(self.root, text, text, done, require_box=kind=='region', point_mode=kind=='next'))

    def snapshot(self, mode):
        s = {key: var.get() for key, var in self.vars.items()}
        s.update(mode=mode, region=self.region, next_point=self.next_point, swipe_start=self.swipe_start,
                 swipe_end=self.swipe_end, images=list(self.images))
        if not s['cache'].strip():
            raise ValueError('请选择缓存目录。')
        for key, low, high, integer in [('delay', .5, 30, False), ('count', 1, 10000, True),
                                       ('first_number', 1, 999999, True), ('swipe_duration', .18, 3, False),
                                       ('snip_wait', .3, 10, False), ('threshold', 60, 99, False), ('handoff_seconds', 3, 600, True)]:
            needed = {
                'capture': {'delay', 'count', 'first_number', 'swipe_duration', 'snip_wait'},
                'answer': {'delay', 'threshold', 'swipe_duration'},
                'recognize': set(),
                'relay': {'delay', 'count', 'first_number', 'swipe_duration', 'snip_wait', 'threshold', 'handoff_seconds'},
            }[mode]
            if key not in needed:
                continue
            try:
                s[key] = int(s[key]) if integer else float(s[key])
                if not low <= s[key] <= high:
                    raise ValueError()
            except ValueError:
                labels = dict(delay='步骤延时', count='截图数量', first_number='起始编号', swipe_duration='拖动耗时', snip_wait='系统截图等待', threshold='匹配度', handoff_seconds='接力倒计时')
                raise ValueError(f'{labels[key]}应在 {low}—{high} 之间。')
        if mode in ('capture', 'relay'):
            if not self.region:
                raise ValueError('请先框选截图区域。')
            if s['count'] > 1:
                if s['turn_mode'] == '点击下一题' and not self.next_point:
                    raise ValueError('请设置下一题按钮位置。')
                if s['turn_mode'] == '拖动翻页' and (not self.swipe_start or not self.swipe_end):
                    raise ValueError('请设置拖动翻页手势。')
        if mode == 'recognize' and not self.images:
            raise ValueError('请先选择图片或文件夹。')
        if mode in ('answer', 'relay'):
            s['window'] = self.selected_window()
        if mode == 'answer' and not Path(s['bank']).is_file():
            raise ValueError('请先选择有效的题库文件。')
        return s

    def start(self):
        if self.running:
            return
        mode = 'relay' if self.vars['relay'].get() else self.vars['module'].get()
        try:
            settings = self.snapshot(mode)
        except Exception as exc:
            self.notice('检查运行参数', str(exc))
            return
        if mode in ('recognize', 'relay'):
            self.results = []
            self.tree.delete(*self.tree.get_children())
        self.save()
        self.control = RunControl()
        self.waiting_handoff = False
        self.progress.set(0)
        self.status.set('正在准备…')
        self.set_running(True)
        def work():
            try:
                Workflow(settings, self.control, self.emit).run(mode)
                self.control.checkpoint()
                self.emit('done', '本次任务已完成')
            except Cancelled:
                self.emit('done', '已停止，已生成文件保留在任务目录')
            except Exception as exc:
                self.emit('error', str(exc))
                self.emit('log', traceback.format_exc())
            finally:
                self.emit('finished', None)
        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def set_running(self, value):
        self.busy = value
        for button in (self.start_button, self.region_button, self.export_button, self.probe_button, self.load_bank_button, self.relay_check):
            button.configure(state='disabled' if value else 'normal')
        self.pause_button.configure(state='normal' if value else 'disabled', text='暂停')
        self.stop_button.configure(state='normal' if value else 'disabled')
        if not value:
            self.update_mode()

    def pause(self):
        if not self.running or self.control.stop.is_set():
            return
        if self.control.paused.is_set():
            self.pause_button.configure(text='暂停')
            self.status.set('继续运行')
            self.waiting_handoff = False
            if self.active_stage in ('capture', 'answer'):
                self.hide()
            self.control.paused.clear()
        else:
            self.control.paused.set()
            self.pause_button.configure(text='继续')
            self.status.set('已暂停（当前识别或鼠标动作完成后生效）')
            self.show()

    def stop(self):
        if self.running:
            self.control.stop.set()
            self.control.paused.clear()
            self.status.set('正在停止…')
            self.pause_button.configure(state='disabled')
            self.stop_button.configure(state='disabled')

    def emit(self, kind, value):
        self.events.put((kind, value))

    def report_ui_error(self, exc_type, exc, tb):
        detail = ''.join(traceback.format_exception(exc_type, exc, tb))
        self.log(f'界面操作异常：{exc}')
        self.status.set('操作未完成，请查看日志后重试')
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            (CONFIG_DIR/'界面错误.log').write_text(detail, encoding='utf-8')
        except OSError:
            pass
        if self.worker is None or not self.worker.is_alive():
            self.set_running(False)

    def poll(self):
        keep_polling = True
        try:
            keep_polling = self.poll_events()
        except Exception as exc:
            self.report_ui_error(type(exc), exc, exc.__traceback__)
        finally:
            if keep_polling:
                # A UI callback failure must not permanently disable Start.
                if self.busy and self.worker is not None and not self.worker.is_alive() and self.events.empty():
                    self.set_running(False)
                self.root.after(80, self.poll)

    def poll_events(self):
        for _ in range(150):
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == 'log':
                self.log(value)
            elif kind in ('status', 'done'):
                self.status.set(value)
                if kind == 'done':
                    self.log(value)
            elif kind == 'directory':
                self.last_dir = value
            elif kind == 'images':
                self.images = value
                self.refresh_descriptions()
            elif kind == 'bank':
                self.vars['bank'].set(value)
                self.bank_info.set('已生成题库；可点击“重新导入”查看全部题目')
            elif kind == 'bank_loaded':
                path, bank = value
                if path != self.vars['bank'].get():
                    continue
                self.loaded_bank_path = path
                self.bank_info.set(f'✓ 已导入 {len(bank)} 道题，可在“题库预览”查看题干、选项和答案')
                self.bank_tree.delete(*self.bank_tree.get_children())
                for index, question in enumerate(bank):
                    self.bank_tree.insert('', 'end', iid=str(index), values=(question.qid, question.qtype, question.text,
                        '  '.join(f'{k}.{v}' for k,v in question.options.items()), '、'.join(question.answer)))
                self.tabs.select(self.bank_frame)
                self.status.set(f'已导入 {len(bank)} 道题。请选择答题窗口，然后点击“开始答题”。')
                self.log(f'题库导入成功：{len(bank)} 道题；{path}')
            elif kind == 'bank_failed':
                path, error = value
                if path == self.vars['bank'].get():
                    self.bank_info.set('✗ 导入失败，请重新选择题库；详情见运行日志')
                self.log(f'题库导入失败：{error}')
                self.status.set('题库导入失败，请查看日志')
                self.tabs.select(0)
            elif kind == 'preview':
                self.preview_path = value
                self.render_preview()
            elif kind == 'question':
                self.results.append(value)
                self.add_result(len(self.results)-1, value)
            elif kind == 'stage':
                self.active_stage = value
                self.select_module(value)
                self.progress.set(0)
                self.log(f'当前步骤：{dict((k,n) for k,_,n,_ in MODULES)[value]}')
            elif kind == 'progress':
                self.progress.set(value[0])
                self.status.set(value[1])
            elif kind == 'handoff':
                self.waiting_handoff = True
                self.pause_button.configure(text='继续答题')
                self.status.set('等待就绪：切到答题起始页后继续')
                self.log(value)
                self.show()
            elif kind == 'error':
                self.status.set('运行中断，请查看日志')
                self.log(value)
                self.show()
                if not self.closing:
                    self.notice('运行提示', value)
            elif kind == 'hide':
                self.hide()
            elif kind == 'show':
                self.show()
            elif kind == 'finished':
                self.set_running(False)
                self.waiting_handoff = False
                self.show()
                self.save()
        self.poll_hotkeys()
        if self.closing and not self.running:
            self.root.destroy()
            return False
        return True

    def poll_hotkeys(self):
        mapping = {'Esc': 0x1B, **{f'F{i}': 0x70+i-1 for i in range(6, 13)}}
        if self.root.grab_current() is not None:
            return
        for key, action in zip(self.hotkeys, (self.start, self.pause, self.stop)):
            pressed = bool(ctypes.windll.user32.GetAsyncKeyState(mapping[key]) & 0x8000)
            if pressed and key not in self.key_down:
                self.key_down.add(key)
                action()
            elif not pressed:
                self.key_down.discard(key)

    def log(self, message):
        self.log_text.configure(state='normal')
        self.log_text.insert('end', f'[{datetime.now():%H:%M:%S}] {message}\n')
        self.log_text.see('end')
        self.log_text.configure(state='disabled')

    def clear_logs(self):
        self.log_text.configure(state='normal')
        self.log_text.delete('1.0', 'end')
        self.log_text.configure(state='disabled')

    def export_logs(self):
        path = filedialog.asksaveasfilename(parent=self.root, title='导出日志', defaultextension='.txt', initialfile=f'运行日志_{datetime.now():%Y%m%d_%H%M%S}.txt')
        if path:
            try:
                Path(path).write_text(self.log_text.get('1.0', 'end'), encoding='utf-8')
                self.log(f'日志已导出：{path}')
            except OSError as exc:
                self.notice('导出失败', str(exc))

    def add_result(self, index, q):
        values = (q.source_number or index+1, q.stem, '  '.join(f'{k}.{v}' for k,v in q.options.items()), q.answer or '未检出')
        if self.tree.exists(str(index)):
            self.tree.item(str(index), values=values)
        else:
            self.tree.insert('', 'end', iid=str(index), values=values)

    def edit_question(self, event=None):
        if self.running or not self.tree.selection():
            return
        index = int(self.tree.selection()[0])
        q = self.results[index]
        win, frame = self.dialog('校对题目', '720x590')
        ttk.Label(frame, text='题干').pack(anchor='w')
        stem = tk.Text(frame, height=4, font=('Microsoft YaHei UI', 10), bg=self.colors['surface'], fg=self.colors['text'])
        stem.pack(fill='x', pady=6)
        stem.insert('1.0', q.stem)
        entries = {}
        for letter in 'ABCDEFGH':
            row = ttk.Frame(frame)
            row.pack(fill='x', pady=3)
            ttk.Label(row, text=f'选项 {letter}', width=9).pack(side='left')
            variable = tk.StringVar(value=q.options.get(letter, ''))
            ttk.Entry(row, textvariable=variable).pack(side='left', fill='x', expand=True)
            entries[letter] = variable
        row = ttk.Frame(frame)
        row.pack(fill='x', pady=8)
        ttk.Label(row, text='正确答案', width=9).pack(side='left')
        answer = tk.StringVar(value=q.answer)
        ttk.Entry(row, textvariable=answer, width=12).pack(side='left')
        ttk.Label(row, text='多选填写 AB 等字母', style='Muted.TLabel').pack(side='left', padx=10)
        def save():
            options = {k:v.get().strip() for k,v in entries.items() if v.get().strip()}
            value = answer.get().strip().upper()
            if not stem.get('1.0', 'end').strip() or not options or not value or any(c not in options for c in value):
                self.notice('检查题目', '请填写题干、选项和有效的答案字母。')
                return
            q.stem, q.options, q.answer = stem.get('1.0', 'end').strip(), options, ''.join(dict.fromkeys(value))
            self.add_result(index, q)
            self.log('题目已修改，请点击“导出题库”保存到文件。')
            win.destroy()
        ttk.Button(frame, text='保存修改', command=save, style='Primary.TButton').pack(anchor='e')

    def export_results(self):
        if self.running:
            return
        if not self.results:
            self.notice('导出题库', '请先识别截图。')
            return
        folder = filedialog.askdirectory(parent=self.root, title='保存校对后的题库')
        if folder:
            try:
                _, excel, _ = export_all(self.results, folder)
                self.vars['bank'].set(str(excel))
                self.last_dir = folder
                self.log(f'题库已导出：{excel}')
            except Exception as exc:
                self.notice('导出失败', str(exc))

    def background_check(self, function):
        if self.running:
            return
        self.control = RunControl()
        self.set_running(True)
        self.status.set('正在检查…')
        def run():
            try:
                result = function()
                self.emit('log', result)
                self.emit('done', '检查完成')
            except Exception as exc:
                self.emit('error', str(exc))
            finally:
                self.emit('finished', None)
        self.worker = threading.Thread(target=run, daemon=True)
        self.worker.start()

    def bank_path_changed(self, *_):
        self.loaded_bank_path = ''
        self.bank_info.set('文件已选择，等待导入…' if self.vars['bank'].get() else '尚未导入题库，选择文件后会自动读取')
        self.bank_tree.delete(*self.bank_tree.get_children())

    def check_bank(self):
        if self.running:
            return
        path = self.vars['bank'].get().strip()
        self.bank_info.set('正在导入题库，请稍候…')
        self.status.set('正在导入题库…')
        self.set_running(True)
        def run():
            try:
                bank = load_question_bank(path)
                self.emit('bank_loaded', (path, bank))
            except Exception as exc:
                self.emit('bank_failed', (path, str(exc)))
            finally:
                self.emit('finished', None)
        self.worker = threading.Thread(target=run, daemon=True)
        self.worker.start()

    def probe(self):
        try:
            window = self.selected_window()
            path, threshold = self.vars['bank'].get(), float(self.vars['threshold'].get())
        except Exception as exc:
            self.notice('识别测试', str(exc))
            return
        def check():
            self.emit('hide', None)
            self.control.wait(1)
            return probe(path, window, threshold)
        self.background_check(check)

    def dialog(self, title, geometry='520x340'):
        win = tk.Toplevel(self.root)
        win.title(title)
        width, height = map(int, geometry.split('x'))
        win.geometry(f'{round(width*self.scale)}x{round(height*self.scale)}')
        win.configure(bg=self.colors['bg'])
        win.transient(self.root)
        x = max(0, min(self.root.winfo_rootx()+40, self.root.winfo_screenwidth()-round(width*self.scale)))
        y = max(0, min(self.root.winfo_rooty()+50, self.root.winfo_screenheight()-round(height*self.scale)-80))
        win.geometry(f'+{x}+{y}')
        win.grab_set()
        win.lift()
        frame = ttk.Frame(win, padding=20)
        frame.pack(fill='both', expand=True)
        return win, frame

    def notice(self, title, message):
        win, frame = self.dialog(title, '560x280')
        ttk.Label(frame, text=title, style='Section.TLabel').pack(anchor='w', pady=(0, 12))
        ttk.Label(frame, text=message, wraplength=round(510*self.scale), justify='left').pack(fill='both', expand=True)
        ttk.Button(frame, text='知道了', command=win.destroy, style='Primary.TButton').pack(anchor='e')

    def settings(self):
        if self.running:
            self.notice('设置', '运行参数已固定，请停止任务后修改设置。')
            return
        win, frame = self.dialog('工作台设置', '560x510')
        ttk.Label(frame, text='外观与运行习惯', style='Section.TLabel').pack(anchor='w', pady=(0, 12))
        row = ttk.Frame(frame)
        row.pack(fill='x', pady=5)
        ttk.Label(row, text='界面主题', width=18).pack(side='left')
        combo = ttk.Combobox(row, textvariable=self.vars['theme'], values=['浅色', '深色'], state='readonly', width=16)
        combo.pack(side='left')
        combo.bind('<<ComboboxSelected>>', lambda event: self.apply_theme())
        variables = [tk.StringVar(value=key) for key in self.hotkeys]
        for label, var in zip(('开始热键', '暂停 / 继续热键', '停止热键'), variables):
            row = ttk.Frame(frame)
            row.pack(fill='x', pady=5)
            ttk.Label(row, text=label, width=18).pack(side='left')
            ttk.Combobox(row, textvariable=var, values=[f'F{i}' for i in range(6, 13)]+['Esc'], state='readonly', width=16).pack(side='left')
        ttk.Label(frame, text='自动答题时 Esc 始终可紧急停止。', style='Muted.TLabel').pack(anchor='w', pady=6)
        ttk.Separator(frame).pack(fill='x', pady=10)
        ttk.Checkbutton(frame, text='接力建库后，等待我切到答题起始页再继续', variable=self.vars['handoff_wait']).pack(anchor='w')
        for label, key in [('自动接力倒计时（秒）', 'handoff_seconds'), ('系统截图等待（秒）', 'snip_wait')]:
            row = ttk.Frame(frame)
            row.pack(fill='x', pady=7)
            ttk.Label(row, text=label, width=26).pack(side='left')
            ttk.Entry(row, textvariable=self.vars[key], width=10).pack(side='left')
        def save():
            keys = [var.get() for var in variables]
            if len(set(keys)) != 3 or 'Esc' in keys[:2]:
                self.notice('热键冲突', '三个热键不能重复，Esc 只能用于停止。')
                return
            self.hotkeys = keys
            self.save()
            win.destroy()
        ttk.Button(frame, text='保存设置', command=save, style='Primary.TButton').pack(anchor='e', pady=10)

    def about(self):
        self.notice('关于', f'{APP_NAME} {VERSION}\n\n截图翻页、离线识别建库、自动答题三合一。\n蓝色勾选直接作为正确答案。\n支持识别结果的“选择项1—4 / 答案1”表头。')

    def set_topmost(self):
        self.root.attributes('-topmost', self.vars['topmost'].get())

    def maximize(self):
        self.root.state('normal' if self.root.state() == 'zoomed' else 'zoomed')

    def hide(self):
        if not self.closing:
            self.root.attributes('-topmost', False)
            self.root.iconify()

    def show(self):
        if not self.closing:
            self.root.deiconify()
            self.set_topmost()

    def open_dir(self):
        try:
            path = Path(self.last_dir or self.vars['cache'].get())
            path.mkdir(parents=True, exist_ok=True)
            os.startfile(path)
        except OSError as exc:
            self.notice('打开目录失败', str(exc))

    def restore_sash(self):
        if self.saved.get('sash'):
            self.pane.sashpos(0, max(round(150*self.scale), min(self.saved['sash'], self.pane.winfo_height()-round(280*self.scale))))
        if self.saved.get('maximized'):
            self.root.state('zoomed')

    def save(self):
        data = {key:var.get() for key,var in self.vars.items()}
        data.update(region=self.region, next_point=self.next_point, swipe_start=self.swipe_start, swipe_end=self.swipe_end,
                    hotkeys=self.hotkeys, images=self.images, last_dir=self.last_dir, sash=self.pane.sashpos(0))
        data['geometry'] = self.root.geometry() if self.root.state() == 'normal' else self.saved.get('geometry', self.default_geometry)
        data['maximized'] = self.root.state() == 'zoomed'
        try:
            data['window_title'] = self.selected_window().title
        except ValueError:
            data['window_title'] = ''
        try:
            self.config_file.parent.mkdir(parents=True, exist_ok=True)
            temp = self.config_file.with_suffix('.tmp')
            temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
            temp.replace(self.config_file)
        except OSError as exc:
            self.log(f'设置保存失败：{exc}')

    def close(self):
        self.save()
        self.closing = True
        if self.running:
            self.stop()
            self.status.set('正在停止当前任务并退出…')
        else:
            self.root.destroy()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--smoke-test', nargs=3, metavar=('BANK', 'IMAGE', 'REPORT'))
    args = parser.parse_args()
    if args.smoke_test:
        bank_path, image_path, report = args.smoke_test
        from dataclasses import asdict
        from engines.recognizer import QuestionRecognizer
        try:
            bank = load_question_bank(bank_path)
            question = QuestionRecognizer().recognize(image_path)
            folder = Path(report).parent/'导出验证'
            _, excel, _ = export_all([question], folder)
            imported = load_question_bank(excel)
            assert imported[0].answer == tuple(question.answer)
            Path(report).write_text(json.dumps(dict(status='PASS', version=VERSION, bank_count=len(bank), question=asdict(question), roundtrip_count=len(imported)), ensure_ascii=False, indent=2), encoding='utf-8')
        except Exception:
            Path(report).write_text(traceback.format_exc(), encoding='utf-8')
            raise
        return
    enable_dpi_awareness()
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == '__main__':
    main()
