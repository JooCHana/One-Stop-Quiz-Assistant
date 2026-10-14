from __future__ import annotations

import argparse
import copy
import ctypes
import json
import os
import re
import sys
import threading
import traceback
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

import cv2
import numpy as np
import xlrd
import xlwt
from docx import Document
from docx.shared import Pt
from rapidocr_onnxruntime import RapidOCR


APP_NAME = "题库截图识别助手"
VERSION = "1.2.1"
IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}


def resource_path(name: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    bundled = base / "resources" / name
    if bundled.exists():
        return bundled
    return Path(__file__).resolve().parent.parent / "resources" / name


@dataclass
class OCRLine:
    text: str
    score: float
    x1: float
    y1: float
    x2: float
    y2: float

    @property
    def x(self) -> float:
        return (self.x1 + self.x2) / 2

    @property
    def y(self) -> float:
        return (self.y1 + self.y2) / 2


@dataclass
class Question:
    source_file: str
    source_number: str = ""
    question_type: str = "单选题"
    stem: str = ""
    options: dict[str, str] = field(default_factory=dict)
    answer: str = ""
    selected_answer: str = ""
    analysis: str = "暂无"
    difficulty: int = 0
    confidence: float = 0.0

    def display_answer(self) -> str:
        return self.answer or "待确认"


def read_image(path: str | Path) -> np.ndarray:
    data = np.fromfile(str(path), dtype=np.uint8)
    image = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"无法读取图片：{path}")
    return image


def enhance_for_ocr(image: np.ndarray) -> np.ndarray:
    """Upscale and locally enhance small/soft screenshots while retaining answer colors."""
    height, width = image.shape[:2]
    scale = min(2.5, max(1.0, 1600.0 / max(width, 1)))
    if max(height, width) * scale > 4600:
        scale = 4600.0 / max(height, width)
    if scale > 1.05:
        image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB)
    lightness, channel_a, channel_b = cv2.split(lab)
    lightness = cv2.createCLAHE(clipLimit=1.6, tileGridSize=(8, 8)).apply(lightness)
    enhanced = cv2.cvtColor(cv2.merge((lightness, channel_a, channel_b)), cv2.COLOR_LAB2BGR)
    blurred = cv2.GaussianBlur(enhanced, (0, 0), 1.0)
    return cv2.addWeighted(enhanced, 1.35, blurred, -0.35, 0)


def enable_high_dpi() -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def clean_text(text: str) -> str:
    text = text.replace("﹕", "：").replace("︰", "：")
    text = re.sub(r"\s+", "", text)
    return text.strip()


def normalize_blank_placeholder(stem: str) -> str:
    """Repair a commonly missed closing bracket without altering valid parenthetical text."""
    opens = stem.count("（") + stem.count("(")
    closes = stem.count("）") + stem.count(")")
    if opens <= closes:
        return stem
    positions = [pos for pos in (stem.find("（"), stem.find("(")) if pos >= 0]
    if not positions:
        return stem
    pos = min(positions)
    opening = stem[pos]
    closing = "）" if opening == "（" else ")"
    return stem[:pos + 1] + closing + stem[pos + 1:]


class QuestionRecognizer:
    def __init__(self) -> None:
        self._engine: RapidOCR | None = None

    @property
    def engine(self) -> RapidOCR:
        if self._engine is None:
            self._engine = RapidOCR()
        return self._engine

    def recognize(self, path: str | Path) -> Question:
        image = enhance_for_ocr(read_image(path))
        raw, _ = self.engine(image)
        if not raw:
            raise ValueError("图片中未识别到文字")
        lines = self._ocr_lines(raw)
        circles = self._option_circles(image, lines)
        if circles:
            lines = self._read_circle_layout(image, lines, circles)
        return self._parse(lines, image, Path(path).name)

    @staticmethod
    def _ocr_lines(raw) -> list[OCRLine]:
        lines: list[OCRLine] = []
        for box, text, score in raw or []:
            xs = [float(p[0]) for p in box]
            ys = [float(p[1]) for p in box]
            value = clean_text(str(text))
            if value:
                lines.append(OCRLine(value, float(score), min(xs), min(ys), max(xs), max(ys)))
        lines.sort(key=lambda item: (item.y, item.x))
        return lines

    @staticmethod
    def _option_circles(image: np.ndarray, lines: list[OCRLine]) -> list[tuple[int, int, int, int]]:
        """Locate an aligned column of round option controls, including checked ones."""
        height, width = image.shape[:2]
        title = next((line for line in lines if any(t in line.text for t in ("单选题", "多选题", "判断题"))), None)
        if title is None:
            return []
        end = min((line.y1 for line in lines if "正确答案" in line.text), default=height)
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        mask = cv2.threshold(gray, 180, 255, cv2.THRESH_BINARY_INV)[1]
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        candidates = []
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            if not (width * .015 < w < width * .065 and .85 < w / h < 1.15
                    and x < width * .3 and title.y2 + h < y < end - h):
                continue
            perimeter = cv2.arcLength(contour, True)
            area = cv2.contourArea(contour)
            if perimeter and 4 * np.pi * area / perimeter ** 2 > .72 and .65 < area / (w * h) < .85:
                candidates.append((x, y, w, h))
        groups = []
        for anchor in candidates:
            group = sorted([c for c in candidates if abs(c[0] + c[2]/2 - anchor[0] - anchor[2]/2) < anchor[2]*.3
                            and .7 < c[2]/anchor[2] < 1.4], key=lambda c: c[1])
            if 2 <= len(group) <= 8 and all(b[1] - a[1] > max(a[3], b[3])*1.5 for a, b in zip(group, group[1:])):
                groups.append(group)
        return max(groups, key=len) if groups else []

    def _read_circle_layout(self, image, original_lines, circles) -> list[OCRLine]:
        # Pale watermarks are discarded only for this layout. Keep the color image
        # unchanged for selection/correct-answer detection.
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        clean = cv2.threshold(gray, 180, 255, cv2.THRESH_BINARY)[1]
        raw, _ = self.engine(clean)
        clean_lines = self._ocr_lines(raw)
        b, g, r = cv2.split(image.astype(np.int16))
        header = clean.copy()
        header[(b > r + 35) & (b > g + 15)] = 255
        first_y = circles[0][1]
        raw, _ = self.engine(header[:max(1, int(first_y-circles[0][3]*.6))])
        lines = self._ocr_lines(raw)
        title = next(line for line in original_lines if any(t in line.text for t in ("单选题", "多选题", "判断题")))
        if not any(any(t in line.text for t in ("单选题", "多选题", "判断题")) for line in lines):
            kind = next(t for t in ("单选题", "多选题", "判断题") if t in title.text)
            lines.append(OCRLine(kind, title.score, title.x1, title.y1, title.x2, title.y2))
        footer_y = min((line.y1 for line in original_lines if "正确答案" in line.text or "参考解析" in line.text), default=image.shape[0])
        for index, (x, y, w, h) in enumerate(circles):
            top = max(0, int(y - h*.6)) if index == 0 else int((circles[index-1][1] + circles[index-1][3] + y)/2)
            bottom = int((y+h+circles[index+1][1])/2) if index+1 < len(circles) else min(int(footer_y), int(y+h*3))
            left = x+w+max(2, int(w*.08))
            row = [line for line in clean_lines if top < line.y < bottom and line.x1 >= x+w]
            if not row:
                raw, _ = self.engine(clean[top:bottom, left:])
                row = self._ocr_lines(raw)
            if not row:
                raise ValueError(f"选项 {chr(65+index)} 文字未识别，请使用更清晰的截图")
            value = "".join(line.text for line in row)
            lines.append(OCRLine(chr(65+index)+"、"+value, float(np.mean([line.score for line in row])),
                                 x, y, image.shape[1], y+h))
        lines.extend(line for line in original_lines if line.y1 >= footer_y)
        return sorted(lines, key=lambda line: (line.y, line.x))

    def _parse(self, lines: list[OCRLine], image: np.ndarray, source_file: str) -> Question:
        height, width = image.shape[:2]
        qtype = "单选题"
        type_line: OCRLine | None = None
        for line in lines:
            for kind in ("单选题", "多选题", "判断题", "简答题"):
                if kind in line.text:
                    qtype, type_line = kind, line
                    break
            if type_line:
                break

        source_number = ""
        for line in lines:
            match = re.search(r"(\d{1,5})\s*[/／]\s*\d{1,6}", line.text)
            if match:
                source_number = match.group(1)
                break

        answer_markers = [line for line in lines if "正确答案" in line.text]
        analysis_markers = [line for line in lines if "参考解析" in line.text or line.text.startswith("解析：")]
        answer_y = answer_markers[0].y if answer_markers else height

        option_re = re.compile(r"^([A-HＡ-Ｈ])[\.、:：\)）\s]*(.+)$", re.I)
        explicit: list[tuple[OCRLine, str, str]] = []
        for line in lines:
            if line.y >= answer_y or (type_line and line.y <= type_line.y + 40):
                continue
            match = option_re.match(line.text)
            if match:
                letter = match.group(1).upper().translate(str.maketrans("ＡＢＣＤＥＦＧＨ", "ABCDEFGH"))
                explicit.append((line, letter, match.group(2).strip()))

        if not explicit:
            raise ValueError("未找到选项，请选择包含完整题目和答案区域的截图")

        first_option_y = min(item[0].y for item in explicit)
        question_start_y = (type_line.y1 - 20) if type_line else max(0, first_option_y - height * 0.25)
        ignored = ("顺序练习", "答题错误", "答题正确", "收藏", "难度", "正确答案", "您选择的答案", "参考解析")
        stem_lines = [
            line for line in lines
            if question_start_y < line.y < first_option_y - 20
            and not any(token in line.text for token in ignored)
            and not re.fullmatch(r"\d+[/／]\d+", line.text)
        ]
        stem = "".join(line.text for line in stem_lines)
        stem = re.sub(r"(?:单选题|多选题|判断题|简答题)", "", stem)
        number = re.match(r"^(\d{1,5})[、.．]", stem)
        if number:
            source_number = source_number or number.group(1)
            stem = stem[number.end():]
        stem = re.sub(r"[（(]\d+(?:\.\d+)?分[）)]$", "", stem)
        stem = normalize_blank_placeholder(re.sub(r"^[题干：:]+", "", stem).strip())

        option_area = [line for line in lines if first_option_y - 5 <= line.y < answer_y - 20]
        option_area.sort(key=lambda item: (item.y, item.x))
        options: dict[str, str] = {}
        option_lines: dict[str, OCRLine] = {}
        last_letter = ""
        explicit_gaps = [explicit[i][0].y - explicit[i - 1][0].y for i in range(1, len(explicit))]
        typical_gap = float(np.median(explicit_gaps)) if explicit_gaps else height * 0.075
        last_y = 0.0
        for line in option_area:
            match = option_re.match(line.text)
            if match:
                letter = match.group(1).upper().translate(str.maketrans("ＡＢＣＤＥＦＧＨ", "ABCDEFGH"))
                value = match.group(2).strip()
                options[letter] = value
                option_lines[letter] = line
                last_letter, last_y = letter, line.y
                continue
            if not last_letter or any(token in line.text for token in ignored):
                continue
            expected = chr(ord(last_letter) + 1)
            new_row = (line.y - last_y) > max(45, typical_gap * 0.55)
            if new_row and expected <= "H" and len(clean_text(line.text)) >= 2:
                options[expected] = line.text
                option_lines[expected] = line
                last_letter, last_y = expected, line.y
            elif line.y - last_y < max(75, typical_gap * 0.45):
                options[last_letter] = options[last_letter] + line.text
                last_y = line.y

        answer = self._answer_near_marker(lines, answer_markers[0] if answer_markers else None, width)
        selected_marker = next((line for line in lines if "您选择的答案" in line.text), None)
        selected = self._answer_near_marker(lines, selected_marker, width)
        blue_answer = self._blue_options(image, option_lines)
        if not selected:
            selected = blue_answer
        if blue_answer:
            answer = blue_answer
        if not answer:
            answer = self._green_option(image, option_lines)

        analysis = "暂无"
        if analysis_markers:
            marker = analysis_markers[0]
            analysis_lines = [
                line for line in lines
                if marker.y + 25 < line.y < height * 0.94
                and "难度" not in line.text
                and "收藏" not in line.text
                and not re.fullmatch(r"\d+", line.text)
            ]
            if analysis_lines:
                analysis = "".join(line.text for line in analysis_lines).strip("：:") or "暂无"

        difficulty = 0
        for line in lines:
            if "难度" in line.text or "★" in line.text:
                difficulty = line.text.count("★")
                if difficulty:
                    break

        useful = stem_lines + list(option_lines.values())
        confidence = sum(line.score for line in useful) / len(useful) if useful else 0.0
        for letter in answer:
            options.setdefault(letter, "")
        return Question(
            source_file=source_file,
            source_number=source_number,
            question_type=qtype,
            stem=stem,
            options=dict(sorted(options.items())),
            answer=answer,
            selected_answer=selected,
            analysis=analysis,
            difficulty=difficulty,
            confidence=confidence,
        )

    @staticmethod
    def _blue_options(image: np.ndarray, option_lines: dict[str, OCRLine]) -> str:
        selected = []
        for letter, line in option_lines.items():
            h = max(1, int(line.y2-line.y1))
            x, y = max(0, int(line.x1)), max(0, int(line.y1))
            crop = image[y:min(image.shape[0], y+h), x:min(image.shape[1], x+h)]
            if not crop.size:
                continue
            b, g, r = cv2.split(crop.astype(np.int16))
            if ((b > r+45) & (g > r+25) & (b > g+15)).mean() > .25:
                selected.append(letter)
        return "".join(selected)

    @staticmethod
    def _answer_near_marker(lines: list[OCRLine], marker: OCRLine | None, width: int) -> str:
        if marker is None:
            return ""
        inline = re.search(r"(?:正确答案|您选择的答案)[:：]?([A-H]+)", marker.text, re.I)
        if inline:
            return inline.group(1).upper()
        candidates: list[tuple[float, OCRLine]] = []
        for line in lines:
            value = line.text.upper()
            if re.fullmatch(r"[A-H]+", value) and marker.y + 15 < line.y < marker.y + 180:
                distance = abs(line.x - marker.x) + (line.y - marker.y) * 0.25
                if abs(line.x - marker.x) < width * 0.28:
                    candidates.append((distance, line))
        return min(candidates, key=lambda item: item[0])[1].text.upper() if candidates else ""

    @staticmethod
    def _green_option(image: np.ndarray, option_lines: dict[str, OCRLine]) -> str:
        if not option_lines:
            return ""
        height, width = image.shape[:2]
        scores: list[tuple[float, str]] = []
        for letter, line in option_lines.items():
            y1, y2 = max(0, int(line.y - 60)), min(height, int(line.y + 60))
            x1, x2 = max(0, int(width * 0.04)), min(width, int(width * 0.96))
            crop = image[y1:y2, x1:x2]
            if crop.size == 0:
                continue
            b, g, r = cv2.split(crop.astype(np.int16))
            mask = (g > r + 8) & (g > b + 8)
            scores.append((float(mask.mean()), letter))
        if not scores:
            return ""
        score, letter = max(scores)
        return letter if score > 0.015 else ""


def split_stem_for_answer(stem: str, answer: str) -> tuple[str, str, str]:
    if not answer:
        return stem, "", ""
    for pattern in (r"[（(]\s*[）)]", r"_{2,}", r"＿{2,}"):
        match = re.search(pattern, stem)
        if match:
            return stem[:match.start()] + "（", answer, "）" + stem[match.end():]
    match = re.search(r"[（(](?=[\u4e00-\u9fff])", stem)
    if match:
        return stem[:match.start()] + "（", answer, "）" + stem[match.end():]
    return stem + "（", answer, "）"


def copy_paragraph_format(source, target) -> None:
    if source._p.pPr is not None:
        target._p.insert(0, copy.deepcopy(source._p.pPr))


def style_run_from_template(run, template_run, bold: bool | None = None) -> None:
    if template_run is not None and template_run._r.rPr is not None:
        run._r.insert(0, copy.deepcopy(template_run._r.rPr))
    if run.font.size is None:
        run.font.size = Pt(10.5)
    if bold is not None:
        run.bold = bold


def export_word(questions: Iterable[Question], output_path: str | Path, template: str | Path | None = None) -> Path:
    questions = list(questions)
    if not questions:
        raise ValueError("没有可导出的题目")
    template_path = Path(template) if template else resource_path("文档模版.docx")
    doc = Document(str(template_path))
    question_template = doc.paragraphs[0] if doc.paragraphs else None
    option_template = doc.paragraphs[1] if len(doc.paragraphs) > 1 else question_template
    q_run_template = question_template.runs[0] if question_template and question_template.runs else None
    o_run_template = option_template.runs[0] if option_template and option_template.runs else q_run_template
    body = doc._element.body
    for child in list(body):
        if child.tag.endswith("sectPr"):
            continue
        body.remove(child)

    for index, question in enumerate(questions, 1):
        paragraph = doc.add_paragraph()
        if question_template:
            copy_paragraph_format(question_template, paragraph)
        before, answer, after = split_stem_for_answer(question.stem, question.answer)
        run = paragraph.add_run(f"{index}.{before}")
        style_run_from_template(run, q_run_template)
        if answer:
            answer_run = paragraph.add_run(answer)
            style_run_from_template(answer_run, q_run_template, bold=True)
            tail_run = paragraph.add_run(after)
            style_run_from_template(tail_run, q_run_template)

        options_paragraph = doc.add_paragraph()
        if option_template:
            copy_paragraph_format(option_template, options_paragraph)
        parts = [f"{letter} {text}" for letter, text in sorted(question.options.items()) if text]
        option_run = options_paragraph.add_run("      ".join(parts))
        style_run_from_template(option_run, o_run_template)

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output))
    return output


def _xlwt_style(font_name: str = "宋体", bold: bool = False, center: bool = False, border: bool = False) -> xlwt.XFStyle:
    style = xlwt.XFStyle()
    style.font = xlwt.Font()
    style.font.name = font_name
    style.font.height = 200
    style.font.bold = bold
    style.alignment = xlwt.Alignment()
    style.alignment.vert = xlwt.Alignment.VERT_CENTER
    style.alignment.horz = xlwt.Alignment.HORZ_CENTER if center else xlwt.Alignment.HORZ_LEFT
    style.alignment.wrap = 1
    if border:
        style.borders = xlwt.Borders()
        style.borders.left = style.borders.right = xlwt.Borders.THIN
        style.borders.top = style.borders.bottom = xlwt.Borders.THIN
    return style


def export_excel(questions: Iterable[Question], output_path: str | Path, template: str | Path | None = None) -> Path:
    questions = list(questions)
    if not questions:
        raise ValueError("没有可导出的题目")
    template_path = Path(template) if template else resource_path("2026 必知必会.xls")
    source_book = xlrd.open_workbook(str(template_path), formatting_info=True)
    source_sheet = source_book.sheet_by_index(0)
    book = xlwt.Workbook(encoding="utf-8")
    sheet = book.add_sheet(source_sheet.name or "Sheet1", cell_overwrite_ok=True)

    normal = _xlwt_style()
    meta_label = _xlwt_style(bold=False)
    header = _xlwt_style(bold=True, center=True, border=True)
    data = _xlwt_style(border=True)
    data_center = _xlwt_style(center=True, border=True)
    for row in range(5):
        for col in range(14):
            value = source_sheet.cell_value(row, col)
            style = header if row == 4 else (meta_label if col == 0 else normal)
            sheet.write(row, col, value, style)
    for col in range(14):
        info = source_sheet.colinfo_map.get(col)
        sheet.col(col).width = info.width if info and info.width else (4200 if col in (2, 9) else 2800)
    sheet.col(2).width = max(sheet.col(2).width, 13000)
    sheet.col(9).width = max(sheet.col(9).width, 6500)
    for col in range(5, 9):
        sheet.col(col).width = max(sheet.col(col).width, 4200)

    type_map = {"单选题": "", "多选题": "不定项选择题", "判断题": "判断", "简答题": "简答"}
    for offset, question in enumerate(questions, start=5):
        values = [
            "", "", question.stem, question.source_number,
            type_map.get(question.question_type, question.question_type),
            question.options.get("A", ""), question.options.get("B", ""),
            question.options.get("C", ""), question.options.get("D", ""),
            question.analysis or "暂无", question.answer, question.selected_answer,
            2.0 if question.options else 1.0, "",
        ]
        for col, value in enumerate(values):
            sheet.write(offset, col, value, data_center if col in (3, 4, 10, 11, 12, 13) else data)
        sheet.row(offset).height_mismatch = True
        sheet.row(offset).height = 900

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    book.save(str(output))
    return output


def export_all(questions: list[Question], output_dir: str | Path) -> tuple[Path, Path, Path]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    word = export_word(questions, output_dir / f"题库识别结果_{stamp}.docx")
    excel = export_excel(questions, output_dir / f"题库识别结果_{stamp}.xls")
    json_path = output_dir / f"题库识别结果_{stamp}.json"
    json_path.write_text(json.dumps([asdict(q) for q in questions], ensure_ascii=False, indent=2), encoding="utf-8")
    return word, excel, json_path


class App:
    def __init__(self) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.root = tk.Tk()
        self.root.title(f"{APP_NAME}  {VERSION}")
        self.root.geometry("1180x760")
        self.root.minsize(980, 650)
        self.root.configure(bg="#f3f6fa")
        try:
            dpi = self.root.winfo_fpixels("1i")
            self.root.tk.call("tk", "scaling", max(1.0, dpi / 72.0))
        except Exception:
            pass
        self.images: list[str] = []
        self.questions: list[Question] = []
        self.recognizer = QuestionRecognizer()
        self.output_dir = tk.StringVar(value=str(Path.home() / "Desktop" / "题库识别结果"))
        self.status = tk.StringVar(value="请选择一张或多张题目截图")
        self._build_ui()

    def _build_ui(self) -> None:
        tk, ttk = self.tk, self.ttk
        style = ttk.Style()
        style.theme_use("vista")
        style.configure("Treeview", rowheight=30, font=("Microsoft YaHei UI", 10))
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Accent.TButton", font=("Microsoft YaHei UI", 10, "bold"), padding=(14, 8))

        header = tk.Frame(self.root, bg="#2457d6", height=82)
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(header, text="题库截图识别助手", bg="#2457d6", fg="white", font=("Microsoft YaHei UI", 20, "bold")).pack(anchor="w", padx=28, pady=(14, 0))
        tk.Label(header, text="本地离线识别 · 支持批量图片 · 可校对后导出 Word 和 Excel", bg="#2457d6", fg="#dce6ff", font=("Microsoft YaHei UI", 10)).pack(anchor="w", padx=30)

        toolbar = tk.Frame(self.root, bg="#ffffff", pady=10)
        toolbar.pack(fill="x", padx=16, pady=(14, 8))
        ttk.Button(toolbar, text="选择图片", command=self.choose_images).pack(side="left", padx=6)
        ttk.Button(toolbar, text="选择文件夹", command=self.choose_folder).pack(side="left", padx=6)
        ttk.Button(toolbar, text="开始识别", style="Accent.TButton", command=self.start_recognition).pack(side="left", padx=14)
        ttk.Button(toolbar, text="编辑选中题目", command=self.edit_selected).pack(side="left", padx=6)
        ttk.Button(toolbar, text="删除选中题目", command=self.delete_selected).pack(side="left", padx=6)
        ttk.Button(toolbar, text="一键导出", style="Accent.TButton", command=self.do_export).pack(side="right", padx=8)

        main = tk.PanedWindow(self.root, orient="horizontal", sashwidth=6, bg="#f3f6fa")
        main.pack(fill="both", expand=True, padx=16, pady=4)
        left = tk.Frame(main, bg="white", bd=1, relief="solid")
        right = tk.Frame(main, bg="white", bd=1, relief="solid")
        main.add(left, minsize=250, width=320)
        main.add(right, minsize=620)

        tk.Label(left, text="待识别图片", bg="white", fg="#1e293b", font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w", padx=14, pady=(12, 6))
        self.image_list = tk.Listbox(left, borderwidth=0, highlightthickness=0, font=("Microsoft YaHei UI", 10), selectbackground="#dbe7ff")
        self.image_list.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        tk.Label(right, text="识别结果（双击可编辑）", bg="white", fg="#1e293b", font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w", padx=14, pady=(12, 6))
        columns = ("index", "type", "stem", "answer", "confidence")
        self.tree = ttk.Treeview(right, columns=columns, show="headings")
        headings = {"index": "序号", "type": "题型", "stem": "题干", "answer": "答案", "confidence": "识别率"}
        widths = {"index": 55, "type": 85, "stem": 560, "answer": 70, "confidence": 80}
        for col in columns:
            self.tree.heading(col, text=headings[col])
            self.tree.column(col, width=widths[col], anchor="center" if col != "stem" else "w", stretch=(col == "stem"))
        self.tree.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.tree.bind("<Double-1>", lambda _event: self.edit_selected())

        bottom = tk.Frame(self.root, bg="#f3f6fa")
        bottom.pack(fill="x", padx=20, pady=(5, 14))
        tk.Label(bottom, text="输出位置：", bg="#f3f6fa", font=("Microsoft YaHei UI", 9)).pack(side="left")
        ttk.Entry(bottom, textvariable=self.output_dir).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(bottom, text="更改", command=self.choose_output).pack(side="left", padx=4)
        self.progress = ttk.Progressbar(bottom, length=160, mode="determinate")
        self.progress.pack(side="left", padx=12)
        tk.Label(bottom, textvariable=self.status, bg="#f3f6fa", fg="#475569", font=("Microsoft YaHei UI", 9)).pack(side="right")

    def choose_images(self) -> None:
        from tkinter import filedialog
        files = filedialog.askopenfilenames(title="选择题目截图", filetypes=[("图片文件", "*.png *.jpg *.jpeg *.bmp *.tif *.tiff *.webp")])
        self._add_images(files)

    def choose_folder(self) -> None:
        from tkinter import filedialog
        folder = filedialog.askdirectory(title="选择图片文件夹")
        if folder:
            files = [str(path) for path in sorted(Path(folder).iterdir()) if path.suffix.lower() in IMAGE_TYPES]
            self._add_images(files)

    def _add_images(self, files: Iterable[str]) -> None:
        for file in files:
            if file not in self.images:
                self.images.append(file)
                self.image_list.insert("end", Path(file).name)
        self.status.set(f"已选择 {len(self.images)} 张图片")

    def choose_output(self) -> None:
        from tkinter import filedialog
        folder = filedialog.askdirectory(title="选择输出文件夹", initialdir=self.output_dir.get())
        if folder:
            self.output_dir.set(folder)

    def start_recognition(self) -> None:
        from tkinter import messagebox
        if not self.images:
            messagebox.showinfo("提示", "请先选择题目截图。")
            return
        self.questions.clear()
        self._refresh_tree()
        self.progress["maximum"] = len(self.images)
        self.progress["value"] = 0
        threading.Thread(target=self._recognition_worker, daemon=True).start()

    def _recognition_worker(self) -> None:
        errors: list[str] = []
        for index, path in enumerate(self.images, 1):
            self.root.after(0, self.status.set, f"正在识别 {index}/{len(self.images)}：{Path(path).name}")
            try:
                question = self.recognizer.recognize(path)
                self.questions.append(question)
            except Exception as exc:
                errors.append(f"{Path(path).name}：{exc}")
            self.root.after(0, self._after_one_recognized, index)
        self.root.after(0, self._recognition_done, errors)

    def _after_one_recognized(self, index: int) -> None:
        self.progress["value"] = index
        self._refresh_tree()

    def _recognition_done(self, errors: list[str]) -> None:
        from tkinter import messagebox
        self.status.set(f"识别完成：成功 {len(self.questions)} 题，失败 {len(errors)} 张")
        if errors:
            messagebox.showwarning("部分图片未识别", "\n".join(errors[:10]))
        elif self.questions:
            messagebox.showinfo("识别完成", "题目和答案已提取。请双击题目校对，确认后点击“一键导出”。")

    def _refresh_tree(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        for index, question in enumerate(self.questions, 1):
            stem = question.stem if len(question.stem) <= 58 else question.stem[:58] + "…"
            self.tree.insert("", "end", iid=str(index - 1), values=(index, question.question_type, stem, question.display_answer(), f"{question.confidence:.0%}"))

    def _selected_index(self) -> int | None:
        selected = self.tree.selection()
        return int(selected[0]) if selected else None

    def delete_selected(self) -> None:
        index = self._selected_index()
        if index is not None:
            del self.questions[index]
            self._refresh_tree()

    def edit_selected(self) -> None:
        from tkinter import messagebox, ttk
        index = self._selected_index()
        if index is None:
            messagebox.showinfo("提示", "请先选中一条题目。")
            return
        q = self.questions[index]
        win = self.tk.Toplevel(self.root)
        win.title(f"编辑第 {index + 1} 题")
        win.geometry("760x660")
        win.transient(self.root)
        win.grab_set()
        frame = self.tk.Frame(win, padx=18, pady=14)
        frame.pack(fill="both", expand=True)

        type_var = self.tk.StringVar(value=q.question_type)
        answer_var = self.tk.StringVar(value=q.answer)
        selected_var = self.tk.StringVar(value=q.selected_answer)
        ttk.Label(frame, text="题型").grid(row=0, column=0, sticky="w", pady=5)
        ttk.Combobox(frame, textvariable=type_var, values=("单选题", "多选题", "判断题", "简答题"), state="readonly", width=18).grid(row=0, column=1, sticky="w")
        ttk.Label(frame, text="正确答案").grid(row=0, column=2, sticky="e", padx=(24, 5))
        ttk.Entry(frame, textvariable=answer_var, width=12).grid(row=0, column=3, sticky="w")
        ttk.Label(frame, text="题干").grid(row=1, column=0, sticky="nw", pady=8)
        stem_text = self.tk.Text(frame, height=7, wrap="word", font=("Microsoft YaHei UI", 10))
        stem_text.grid(row=1, column=1, columnspan=3, sticky="nsew", pady=5)
        stem_text.insert("1.0", q.stem)
        option_boxes: dict[str, object] = {}
        for row, letter in enumerate("ABCD", start=2):
            ttk.Label(frame, text=f"选项 {letter}").grid(row=row, column=0, sticky="nw", pady=6)
            box = self.tk.Text(frame, height=2, wrap="word", font=("Microsoft YaHei UI", 10))
            box.grid(row=row, column=1, columnspan=3, sticky="ew", pady=3)
            box.insert("1.0", q.options.get(letter, ""))
            option_boxes[letter] = box
        ttk.Label(frame, text="您选择的答案").grid(row=6, column=0, sticky="w", pady=6)
        ttk.Entry(frame, textvariable=selected_var, width=12).grid(row=6, column=1, sticky="w")
        ttk.Label(frame, text="解析").grid(row=7, column=0, sticky="nw", pady=8)
        analysis_text = self.tk.Text(frame, height=5, wrap="word", font=("Microsoft YaHei UI", 10))
        analysis_text.grid(row=7, column=1, columnspan=3, sticky="nsew", pady=5)
        analysis_text.insert("1.0", q.analysis)
        frame.columnconfigure(1, weight=1)
        frame.columnconfigure(3, weight=1)
        frame.rowconfigure(1, weight=1)
        frame.rowconfigure(7, weight=1)

        def save() -> None:
            q.question_type = type_var.get()
            q.answer = clean_text(answer_var.get()).upper()
            q.selected_answer = clean_text(selected_var.get()).upper()
            q.stem = stem_text.get("1.0", "end").strip()
            q.options = {letter: box.get("1.0", "end").strip() for letter, box in option_boxes.items() if box.get("1.0", "end").strip()}
            q.analysis = analysis_text.get("1.0", "end").strip() or "暂无"
            self._refresh_tree()
            win.destroy()

        ttk.Button(frame, text="保存修改", style="Accent.TButton", command=save).grid(row=8, column=3, sticky="e", pady=12)
        ttk.Button(frame, text="取消", command=win.destroy).grid(row=8, column=2, sticky="e", padx=8, pady=12)

    def do_export(self) -> None:
        from tkinter import filedialog, messagebox
        if not self.questions:
            messagebox.showinfo("提示", "请先识别题目。")
            return
        folder = filedialog.askdirectory(
            title="选择 Word 和 Excel 的导出文件夹",
            initialdir=self.output_dir.get(),
            mustexist=False,
        )
        if not folder:
            self.status.set("已取消导出")
            return
        self.output_dir.set(folder)
        try:
            word, excel, _json = export_all(self.questions, folder)
        except Exception as exc:
            messagebox.showerror("导出失败", f"{exc}\n\n{traceback.format_exc(limit=2)}")
            return
        self.status.set(f"已导出：{word.name}、{excel.name}")
        if messagebox.askyesno("导出完成", f"Word 和 Excel 已生成：\n{self.output_dir.get()}\n\n是否打开输出文件夹？"):
            os.startfile(self.output_dir.get())

    def run(self) -> None:
        self.root.mainloop()


def cli_main(args: argparse.Namespace) -> int:
    paths: list[Path] = []
    for value in args.image:
        path = Path(value)
        if path.is_dir():
            paths.extend(sorted(item for item in path.iterdir() if item.suffix.lower() in IMAGE_TYPES))
        else:
            paths.append(path)
    if not paths:
        raise SystemExit("请使用 --image 指定图片或图片文件夹")
    recognizer = QuestionRecognizer()
    questions = [recognizer.recognize(path) for path in paths]
    word, excel, json_path = export_all(questions, args.output)
    print(json.dumps({"word": str(word), "excel": str(excel), "json": str(json_path), "questions": [asdict(q) for q in questions]}, ensure_ascii=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--cli", action="store_true", help="命令行批量识别")
    parser.add_argument("--image", action="append", default=[], help="图片或图片文件夹，可重复使用")
    parser.add_argument("--output", default=str(Path.cwd() / "output"), help="输出文件夹")
    parser.add_argument("--version", action="store_true")
    args = parser.parse_args()
    if args.version:
        print(VERSION)
        return 0
    if args.cli:
        return cli_main(args)
    enable_high_dpi()
    App().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
