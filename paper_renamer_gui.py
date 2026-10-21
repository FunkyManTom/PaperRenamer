#!/usr/bin/env python3
"""Paper Renamer: a small window around rename_papers.py.

    pip install pyside6 pymupdf requests
    python paper_renamer_gui.py

Keep this file next to rename_papers.py.
"""

import html
import re
import sys
import tempfile
import time
from pathlib import Path

from PySide6.QtCore import QFile, QSettings, QSize, Qt, QThread, QUrl, Signal
from PySide6.QtGui import (
    QColor, QDesktopServices, QFont, QFontMetrics, QIcon, QPainter, QPalette, QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QFileDialog, QFrame,
    QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMainWindow, QMenu, QProgressBar, QPushButton,
    QProxyStyle, QRadioButton, QScrollArea, QSizePolicy, QStackedWidget, QStyle, QStyledItemDelegate, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

import rename_papers as rp

# ---------------------------------------------------------------- theme

# Each theme: colour tokens, whether it's dark, and optionally its own colours for the status pills
# ("pills": status -> colour; the pill background is that colour faded into the surface).
THEMES = {
    "light": dict(
        name="Light", dark=False,
        bg="#f5f6f8", surface="#ffffff", border="#e2e5ea", text="#1d2430", muted="#6b7280",
        accent="#3b5bdb", accent_hover="#364fc7", accent_text="#ffffff", selection="#e8edff",
        alt="#fafbfc", field="#f5f6f8",
    ),
    "dark": dict(
        name="Dark", dark=True,
        bg="#15171c", surface="#1e2128", border="#2f333c", text="#e6e8eb", muted="#9aa1ad",
        accent="#5c7cfa", accent_hover="#748ffc", accent_text="#ffffff", selection="#28304a",
        alt="#22252d", field="#191b21",
    ),
    "oled": dict(
        name="OLED dark", dark=True,  # true black: OLED pixels switch off
        bg="#000000", surface="#000000", border="#262626", text="#e8e8e8", muted="#8b8b8b",
        accent="#6e8bff", accent_hover="#8aa1ff", accent_text="#ffffff", selection="#16213d",
        alt="#0b0b0b", field="#0a0a0a",
    ),
    "latte": dict(
        name="Catppuccin Latte", dark=False,  # https://catppuccin.com/palette
        bg="#e6e9ef", surface="#eff1f5", border="#ccd0da", text="#4c4f69", muted="#6c6f85",
        accent="#8839ef", accent_hover="#7529d6", accent_text="#eff1f5", selection="#e3d5fb",
        alt="#e9ecf2", field="#e6e9ef",
        pills=dict(rename="#1e66f5", check="#df8e1d", manual="#8839ef", ok="#7c7f93", skip="#d20f39",
                   renamed="#40a02b", error="#e64553", duplicate="#179299"),
    ),
    "mocha": dict(
        name="Catppuccin Mocha", dark=True,
        bg="#181825", surface="#1e1e2e", border="#313244", text="#cdd6f4", muted="#a6adc8",
        accent="#cba6f7", accent_hover="#b4befe", accent_text="#11111b", selection="#353648",
        alt="#232334", field="#181825",
        pills=dict(rename="#89b4fa", check="#f9e2af", manual="#cba6f7", ok="#9399b2", skip="#f38ba8",
                   renamed="#a6e3a1", error="#eba0ac", duplicate="#94e2d5"),
    ),
}
THEME_CHOICES = ["system", "light", "dark", "oled", "latte", "mocha"]  # "system" follows the OS
CURRENT = {"theme": "light"}  # the theme in use (set by apply_theme)


def theme():
    return THEMES[CURRENT["theme"]]


def blend(color, other, amount):
    """`amount` of `color` mixed into `other` (hex colours)."""
    a, b = QColor(color), QColor(other)
    mix = [round(x * amount + y * (1 - amount)) for x, y in zip(a.getRgb()[:3], b.getRgb()[:3])]
    return "#{:02x}{:02x}{:02x}".format(*mix)


def pill_colors(status):
    """(background, text) of a status pill ("duplicate" too) in the current theme."""
    t = theme()
    if status in t.get("pills", {}):
        color = t["pills"][status]
        fg = color if t["dark"] else blend(color, t["text"], 0.75)  # a bit darker, to read on light
        return blend(color, t["surface"], 0.2 if t["dark"] else 0.14), fg
    colors = DUPLICATE_PILL if status == "duplicate" else STATUSES[status]
    return colors[2 if t["dark"] else 1]

# status key -> (label, light (bg, fg), dark (bg, fg))
STATUSES = {
    "rename":  ("To rename",     ("#e7efff", "#1c4fd6"), ("#1f2b4d", "#9db8ff")),
    "check":   ("Check",         ("#fff3dc", "#9a5800"), ("#3b2e15", "#f5c56f")),
    "manual":  ("Manual",        ("#f0eaff", "#6a3fd4"), ("#2e2548", "#c7b4ff")),
    "ok":      ("Already named", ("#eef0f3", "#4b5563"), ("#2a2d34", "#b6bcc6")),
    "skip":    ("Not found",     ("#fdecec", "#b42318"), ("#3c2020", "#f3a3a3")),
    "renamed": ("Renamed",       ("#e4f5ea", "#18793a"), ("#16331f", "#82d9a0")),
    "error":   ("Failed",        ("#fbdcdc", "#991b1b"), ("#4a1d1d", "#ff9e9e")),
}
DUPLICATE_PILL = ("Duplicate", ("#dff3f2", "#0d6b66"), ("#14322f", "#7fd8cf"))
ACTIONABLE = ("rename", "check", "manual")
FILTERS = [  # (key, label, which rows it shows)
    ("all", "All", lambda r: True),
    ("rename", "To rename", lambda r: r.state in ("rename", "manual")),
    ("check", "Check", lambda r: r.state == "check"),
    ("skip", "Not found", lambda r: r.state in ("skip", "error")),
    ("done", "Renamed", lambda r: r.state == "renamed"),
    ("dups", "Duplicates", lambda r: r.dup is not None or bool(r.dup_of)),
]
TRASH = "Recycle Bin" if sys.platform == "win32" else "Trash"
PATTERN_PRESETS = [
    "{author}{year}", "{author}_{year}", "{authors}{year}", "{author}{year}_{title}",
    "{author}_{year}_{title}", "{year}_{author}", "{author}_{year}_{journal}",
]
# Shown in the pattern preview before any papers are scanned.
SAMPLE_PAPERS = [
    rp.Meta([["Enrico", "Fermi"]], 1926, "Zur Quantelung des idealen einatomigen Gases", "", "Z. Physik"),
    rp.Meta([["Ashish", "Vaswani"], ["Noam", "Shazeer"], ["Niki", "Parmar"]], 2017,
            "Attention Is All You Need", "", "arXiv"),
]


def is_dark(app):
    try:
        return app.styleHints().colorScheme() == Qt.ColorScheme.Dark
    except AttributeError:  # Qt < 6.5
        return app.palette().color(QPalette.ColorRole.Window).lightness() < 128


TOOLTIP_DELAY_MS = 250  # Fusion waits 700 ms


class QuickTooltipStyle(QProxyStyle):
    def styleHint(self, hint, option=None, widget=None, return_data=None):
        if hint == QStyle.StyleHint.SH_ToolTip_WakeUpDelay:
            return TOOLTIP_DELAY_MS
        return super().styleHint(hint, option, widget, return_data)


def apply_theme(app, choice):
    """Style the app with a theme from THEME_CHOICES (or True/False for dark/light).
    Returns the theme used ("system" resolves to light or dark)."""
    if isinstance(choice, bool):
        choice = "dark" if choice else "light"
    key = ("dark" if is_dark(app) else "light") if choice not in THEMES else choice
    CURRENT["theme"] = key
    t = THEMES[key]
    app.setStyle(QuickTooltipStyle("Fusion"))
    pal = QPalette()
    for role, key in [
        (QPalette.ColorRole.Window, "bg"), (QPalette.ColorRole.Base, "surface"),
        (QPalette.ColorRole.AlternateBase, "alt"), (QPalette.ColorRole.Text, "text"),
        (QPalette.ColorRole.WindowText, "text"), (QPalette.ColorRole.Button, "surface"),
        (QPalette.ColorRole.ButtonText, "text"), (QPalette.ColorRole.Highlight, "accent"),
        (QPalette.ColorRole.HighlightedText, "accent_text"), (QPalette.ColorRole.ToolTipBase, "surface"),
        (QPalette.ColorRole.ToolTipText, "text"), (QPalette.ColorRole.PlaceholderText, "muted"),
        (QPalette.ColorRole.Link, "accent"),
    ]:
        pal.setColor(role, QColor(t[key]))
    app.setPalette(pal)
    warn_fg = pill_colors("skip")[1]
    warn, good = pill_colors("check"), pill_colors("renamed")
    app.setStyleSheet(f"""
        QWidget {{ color: {t['text']}; }}
        QMainWindow, #root {{ background: {t['bg']}; }}
        #title {{ font-size: 20pt; font-weight: 600; }}
        #subtitle, #muted {{ color: {t['muted']}; }}
        #warnText {{ color: {warn_fg}; }}
        #card {{ background: {t['surface']}; border: 1px solid {t['border']}; border-radius: 12px; }}
        QLineEdit {{
            background: {t['field']}; border: 1px solid {t['border']}; border-radius: 8px;
            padding: 7px 10px; selection-background-color: {t['accent']};
        }}
        QComboBox {{
            background: {t['field']}; border: 1px solid {t['border']}; border-radius: 8px;
            padding: 5px 10px; min-width: 210px;
        }}
        QComboBox:focus, QLineEdit:focus {{ border-color: {t['accent']}; }}
        QComboBox QLineEdit {{ border: none; padding: 0; background: transparent; }}
        QComboBox::drop-down {{ border: none; width: 24px; }}
        QComboBox::down-arrow {{
            width: 10px; height: 10px;
            image: url("{svg_file(f"chevron_{t['muted'][1:]}", CHEVRON_SVG.format(t['muted']))}");
        }}
        QComboBox QAbstractItemView {{
            background: {t['surface']}; border: 1px solid {t['border']}; padding: 4px;
            selection-background-color: {t['selection']}; selection-color: {t['text']}; outline: none;
        }}
        QPushButton {{
            background: {t['surface']}; border: 1px solid {t['border']}; border-radius: 8px;
            padding: 7px 16px;
        }}
        QPushButton:hover {{ border-color: {t['accent']}; }}
        QPushButton:disabled {{ color: {t['muted']}; }}
        QPushButton::menu-indicator {{ subcontrol-position: right center; right: 8px; }}
        QPushButton#menuButton {{ padding-right: 26px; }}
        QPushButton#primary {{
            background: {t['accent']}; color: {t['accent_text']}; border: none; font-weight: 600;
            padding: 8px 20px;
        }}
        QPushButton#primary:hover {{ background: {t['accent_hover']}; }}
        QPushButton#primary:disabled {{ background: {t['border']}; color: {t['muted']}; }}
        QPushButton#filter {{
            border-radius: 14px; padding: 4px 12px; background: transparent; color: {t['muted']};
        }}
        QPushButton#filter:checked {{
            background: {t['selection']}; color: {t['accent']}; border-color: {t['selection']};
        }}
        QPushButton#link {{ border: none; background: transparent; color: {t['muted']}; padding: 4px 6px; }}
        QPushButton#link:hover {{ color: {t['accent']}; }}
        QPushButton#link::menu-indicator {{ image: none; width: 0; }}
        QProgressBar {{ background: {t['border']}; border: none; border-radius: 3px; max-height: 6px; }}
        QProgressBar::chunk {{ background: {t['accent']}; border-radius: 3px; }}
        QTableWidget {{
            background: {t['surface']}; alternate-background-color: {t['alt']};
            border: 1px solid {t['border']}; border-radius: 12px; gridline-color: transparent;
            selection-background-color: {t['selection']}; selection-color: {t['text']};
        }}
        QTableWidget::item {{ padding: 0 8px; border: none; }}
        QHeaderView::section {{
            background: {t['surface']}; color: {t['muted']}; border: none;
            border-bottom: 1px solid {t['border']}; padding: 8px; font-weight: 600;
        }}
        QTableCornerButton::section {{ background: {t['surface']}; border: none; }}
        #empty {{ background: {t['surface']}; border: 2px dashed {t['border']}; border-radius: 12px; }}
        #emptyTitle {{ font-size: 14pt; font-weight: 600; }}
        #banner {{ border-radius: 8px; padding: 9px 12px; }}
        #banner[kind="warn"] {{ background: {warn[0]}; color: {warn[1]}; }}
        #banner[kind="good"] {{ background: {good[0]}; color: {good[1]}; }}
        #banner[kind="info"] {{ background: {t['selection']}; color: {t['accent']}; }}
        #dialogTitle {{ font-size: 15pt; font-weight: 600; }}
        #groupTitle {{ font-weight: 600; }}
        QPushButton#small {{ padding: 3px 12px; }}
        QScrollArea, #scrollInner {{ background: transparent; border: none; }}
        QRadioButton::indicator {{
            width: 14px; height: 14px; border-radius: 8px;
            border: 1.5px solid {t['muted']}; background: {t['surface']};
        }}
        QRadioButton::indicator:checked {{
            border: none; width: 17px; height: 17px; background: transparent;
            image: url("{svg_file(f"radio_{t['accent'][1:]}_{t['accent_text'][1:]}",
                                  RADIO_SVG.format(t['accent'], t['accent_text']))}");
        }}
        QRadioButton:disabled, QLabel:disabled, QCheckBox:disabled {{ color: {t['muted']}; }}
        QMenu {{ background: {t['surface']}; border: 1px solid {t['border']}; padding: 4px; }}
        QMenu::item {{ padding: 6px 18px; border-radius: 4px; }}
        QMenu::item:selected {{ background: {t['selection']}; }}
        QMenu::item:disabled {{ color: {t['muted']}; }}
        QToolTip {{ background: {t['surface']}; color: {t['text']}; border: 1px solid {t['border']}; padding: 4px; }}
        QCheckBox::indicator, QTableWidget::indicator {{
            width: 15px; height: 15px; border-radius: 4px;
            border: 1.5px solid {t['muted']}; background: {t['surface']};
        }}
        QCheckBox::indicator:checked, QTableWidget::indicator:checked {{
            background: {t['accent']}; border-color: {t['accent']}; image: url("{svg_file(f"check_{t['accent_text'][1:]}", CHECK_SVG.format(t['accent_text']))}");
        }}
    """)
    return key


CHECK_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
             '<path d="M3.5 8.5l3 3 6-7" fill="none" stroke="{}" stroke-width="2.2" '
             'stroke-linecap="round" stroke-linejoin="round"/></svg>')
CHEVRON_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
               '<path d="M1.5 3.5l3.5 3.5 3.5-3.5" fill="none" stroke="{}" stroke-width="1.6" '
               'stroke-linecap="round" stroke-linejoin="round"/></svg>')
RADIO_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
             '<circle cx="8" cy="8" r="8" fill="{}"/><circle cx="8" cy="8" r="3" fill="{}"/></svg>')


def svg_file(name, svg):
    """Stylesheets can only load images from files, so write tiny ones."""
    path = Path(tempfile.gettempdir(), f"paper_renamer_{name}.svg")
    if not path.exists():
        path.write_text(svg)
    return path.as_posix()


def icon_path():
    """icon.png next to this script, or inside the PyInstaller bundle (--add-data icon.png:.)."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / "icon.png"


def app_icon(color):
    """The program's icon: icon.png, or if that's missing a little document drawn in `color`."""
    if icon_path().is_file():
        icon = QIcon(str(icon_path()))
        if not icon.isNull():
            return icon
    pm = QPixmap(64, 64)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(color))
    p.drawRoundedRect(4, 4, 56, 56, 14, 14)
    p.setBrush(QColor("white"))
    p.drawRoundedRect(19, 13, 26, 38, 4, 4)
    p.setBrush(QColor(color))
    for y, w in ((22, 16), (29, 16), (36, 10)):
        p.drawRoundedRect(24, y, w, 3, 1.5, 1.5)
    p.end()
    return QIcon(pm)


DUPLICATE_ROLE = Qt.ItemDataRole.UserRole + 1


class StatusDelegate(QStyledItemDelegate):
    """Draws the status column as a coloured pill, plus a second one for duplicates."""

    def paint(self, painter, option, index):
        self.initStyleOption(option, index)
        option.text = ""
        option.widget.style().drawControl(QStyle.ControlElement.CE_ItemViewItem, option, painter, option.widget)
        key = index.data(Qt.ItemDataRole.UserRole)
        if key not in STATUSES:
            return
        font = QFont(option.font)
        font.setPointSizeF(font.pointSizeF() * 0.9)
        font.setWeight(QFont.Weight.DemiBold)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setFont(font)
        x = option.rect.x() + 8
        x += self.pill(painter, option.rect, x, font, STATUSES[key][0], key) + 6
        if index.data(DUPLICATE_ROLE):
            self.pill(painter, option.rect, x, font, DUPLICATE_PILL[0], "duplicate")
        painter.restore()

    def pill(self, painter, rect, x, font, label, status):
        bg, fg = pill_colors(status)
        fm = QFontMetrics(font)
        w, h = fm.horizontalAdvance(label) + 20, fm.height() + 6
        y = rect.y() + (rect.height() - h) // 2
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(bg))
        painter.drawRoundedRect(x, y, w, h, h / 2, h / 2)
        painter.setPen(QColor(fg))
        painter.drawText(x, y, w, h, Qt.AlignmentFlag.AlignCenter, label)
        return w

    def sizeHint(self, option, index):
        return QSize(130, 36)


# ---------------------------------------------------------------- background work

class ScanWorker(QThread):
    progress = Signal(int, str)   # files done, file being read
    planned = Signal(object)      # rp.Plan
    finished_scan = Signal(bool)  # cancelled?

    def __init__(self, pdfs, use_ocr, pattern, cache, deep_ocr=False):
        super().__init__()
        self.pdfs, self.use_ocr, self.pattern, self.cache = pdfs, use_ocr, pattern, cache
        self.deep_ocr = deep_ocr
        self.cancelled = False

    def run(self):
        done = 0
        self.progress.emit(0, "")
        try:
            # Remembered papers come first (they're instant); the slow lookups follow.
            for plan in rp.plan_renames(self.pdfs, self.use_ocr, self.pattern, self.cache, cached_first=True,
                                        started=lambda pdf: self.progress.emit(done, pdf.name),
                                        deep_ocr=self.deep_ocr):
                done += 1
                self.planned.emit(plan)
                self.progress.emit(done, "")
                if self.cancelled:
                    break
        except Exception as e:  # stop the scan, but never leave the window stuck in "scanning"
            print(f"scan stopped: {e!r}", file=sys.stderr)
            self.cancelled = True
        finally:
            self.cache.save()
            self.finished_scan.emit(self.cancelled)


class ExportWorker(QThread):
    progress = Signal(int, int, str)
    finished_export = Signal(int, list, str)  # written, DOIs that failed, error message

    def __init__(self, entries, fmt, out_path, cache):
        super().__init__()
        self.entries, self.fmt, self.out_path, self.cache = entries, fmt, out_path, cache
        self.cancelled = False

    def run(self):
        try:
            written, failed = rp.export_citations(
                self.entries, self.fmt, self.out_path, self.cache,
                progress=lambda n, total, key: self.progress.emit(n, total, key),
                cancelled=lambda: self.cancelled)
            self.finished_export.emit(written, failed, "")
        except Exception as e:  # e.g. the file can't be written; always report back
            self.finished_export.emit(0, [], str(e))


class TaskWorker(QThread):
    """Runs job(worker) off the main thread: manual lookups, loading alternatives."""
    progress = Signal(int, int, str)  # done, total, what
    finished_task = Signal(object, str)  # result, error message

    def __init__(self, job):
        super().__init__()
        self.job = job
        self.cancelled = False

    def run(self):
        try:
            self.finished_task.emit(self.job(self), "")
        except ValueError as e:
            self.finished_task.emit(None, str(e))
        except rp.requests.RequestException:
            self.finished_task.emit(None, "Couldn't reach the online databases. Check your internet connection.")
        except Exception as e:
            self.finished_task.emit(None, f"Something went wrong: {e}")


class WatchWorker(QThread):
    """Polls the watched folder every rp.WATCH_INTERVAL seconds until stopped."""
    event = Signal(object)  # rp.WatchEvent

    def __init__(self, watcher):
        super().__init__()
        self.watcher = watcher
        self.cancelled = False

    def run(self):
        while not self.cancelled:
            try:
                events = self.watcher.poll(lambda: self.cancelled)
            except Exception as e:  # keep watching; report it
                events = [rp.WatchEvent(time.time(), "", "error", detail=str(e))]
            for e in events:
                self.event.emit(e)
            for _ in range(rp.WATCH_INTERVAL * 10):
                if self.cancelled:
                    break
                self.msleep(100)


USER_PREFIXES = ("entered ", "chosen ")  # how strings of identifications you made yourself
ID_NAMES = {"doi": "DOI", "isbn": "ISBN", "arXiv": "arXiv ID", "bibcode": "ADS bibcode"}


class Row:
    def __init__(self, plan):
        self.plan = plan
        self.path = plan.pdf
        self.detail = plan.detail
        self.doi = plan.doi
        self.hash = plan.hash
        self.dup = None  # index of its duplicate group, if any
        self.dup_of = ""  # a file elsewhere it duplicates (found by the folder watcher)
        self.order = 0  # position in the folder listing
        # Title/text/file name searches are fuzzy matches: flag them for a second look.
        self.base_state = ("check" if plan.detail.startswith(("title search", "text search", "file name search"))
                           else "rename")
        if plan.status == "skip":
            self.state, self.new_name, self.checked = "skip", "", False
        elif plan.status == "ok":
            self.state, self.new_name, self.checked = "ok", plan.pdf.name, False
        else:
            self.state, self.new_name, self.checked = self.base_state, plan.target.name, True

    @property
    def method(self):
        d = self.plan.detail
        if self.plan.status == "skip":
            return ""
        by_you = d.startswith(USER_PREFIXES)
        d = d.split(" ", 1)[1] if by_you else d
        base = ("DOI" if d.startswith("doi") else "arXiv" if d.startswith("arXiv")
                else "ISBN" if d.startswith("isbn") else "Bibcode" if d.startswith("bibcode")
                else "Edition" if d.startswith("edition") else "File name" if d.startswith("file name")
                else "Title search" if d.startswith("title") else "Text search")
        return base + (" + OCR" if "(OCR)" in d else "") + (", by you" if by_you else "")

    @property
    def method_tip(self):
        """A sentence on how this paper was identified, for the "Found via" tooltip."""
        d, meta = self.plan.detail, self.plan.meta
        if self.plan.status == "skip" or not meta:
            return ""
        key = d.replace(" (OCR)", "").split(" ", 1)[-1]
        where = "Crossref" if meta.doi else "Open Library"
        kind, _, value = key.partition(" ")
        named = f"{ID_NAMES.get(kind, kind)} {value}".strip()
        if d.startswith("entered"):
            tip = f"You entered the {named} and it was looked up."
        elif d.startswith("chosen"):
            tip = ("You chose this edition or match from the alternatives that were found"
                   + (f" ({named})." if value else "."))
        elif d.startswith("doi"):
            tip = (f"The DOI {key} was found in the file name, the PDF's metadata or its text and looked up; "
                   "the record's title appears in the PDF.")
        elif d.startswith("arXiv"):
            tip = f"The arXiv ID {key} was found in the text and looked up on arXiv; its title appears in the PDF."
        elif d.startswith("bibcode"):
            tip = (f"The ADS bibcode {key} on the pages was looked up on ADS (with an API token) or by its year, "
                   "volume and first page on OpenAlex; the title and first author appear in the PDF.")
        elif d.startswith("isbn"):
            tip = (f"The ISBN {key} from the copyright page or file name was looked up on {where}; "
                   "the book's title and author appear in the PDF.")
        elif d.startswith("file name"):
            tip = (f"A book with the title and author in the file name was found on {where}. "
                   "Only the file name was compared, not the PDF's text, so please check it.")
        elif d.startswith("title"):
            tip = ("The title stored in the PDF's metadata was searched on Crossref and closely matches a record. "
                   "A fuzzy match, so please check it.")
        else:
            tip = ("The start of page 1 was searched on Crossref; the hit's title and first author appear in "
                   "the PDF. A fuzzy match, so please check it.")
        if meta.part:
            tip += (" The PDF is only part of the book, so it's named after the chapter"
                    + (" and linked to the chapter's own record." if meta.kind == "chapter" else "."))
        if "(OCR)" in d:
            tip += " The PDF has no usable text, so it was read with OCR."
        if self.plan.cached:
            tip += " Remembered from an earlier scan."
        return tip

    @property
    def details_tip(self):
        return details_html(self.plan.meta)

    @property
    def search_text(self):
        """Everything the search box looks through, folded to lowercase ASCII."""
        parts = [self.path.name, self.new_name, self.detail]
        meta = self.plan.meta
        if meta:
            parts += [meta.title, meta.journal, meta.publisher, meta.abstract, meta.doi, meta.isbn, str(meta.year)]
            parts += [" ".join(a) for a in meta.authors] + list(meta.tags)
        return rp.fold(" ".join(p for p in parts if p))


def details_html(meta):
    """Title, authors, journal, keywords and abstract, as rich text so Qt wraps it."""
    authors = ", ".join(" ".join(p for p in a if p) for a in meta.authors[:3])
    authors += " et al." if len(meta.authors) > 3 else ""
    source = meta.journal or meta.publisher
    lines = [f"<b>{html.escape(re.sub(r'<[^>]+>', '', meta.title))}</b>",
             html.escape(f"{authors} ({meta.year})" + (f", {source}" if source else "")
                         + (f"  ·  ISBN {meta.isbn}" if meta.isbn else ""))]
    if meta.tags:
        lines.append(f"<i>Tags:</i> {html.escape(', '.join(meta.tags))}")
    if meta.abstract:
        short = meta.abstract
        if len(short) > ABSTRACT_CHARS:
            short = short[:ABSTRACT_CHARS].rsplit(" ", 1)[0] + " …"
        lines.append(html.escape(short))
    return tip_html(lines, 520)


def tip_html(paragraphs, width):
    """Rich-text tooltip of a set width: Qt otherwise wraps rich text into a narrow column."""
    return f'<table width="{width}"><tr><td><p>' + "</p><p>".join(paragraphs) + "</p></td></tr></table>"


def clean_filename(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip().strip(".")
    if name and not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name


# ---------------------------------------------------------------- duplicates

def pdf_facts(path):
    """What helps decide which copy to keep: annotations, a text layer, pages, size."""
    facts = {"size": 0, "pages": None, "annots": 0, "text": None}
    try:
        facts["size"] = path.stat().st_size
        if rp.pymupdf:
            with rp.pymupdf.open(path) as doc:
                facts["pages"] = doc.page_count
                facts["annots"] = sum(1 for page in doc for _ in page.annots())
                facts["text"] = any(doc[i].get_text().strip() for i in range(min(3, doc.page_count)))
        else:
            facts["pages"] = len(rp.PdfReader(path).pages)
    except Exception:
        pass
    return facts


def describe(facts):
    parts = [f"{facts['size'] / 1e6:.1f} MB" if facts["size"] >= 1e5 else f"{facts['size'] / 1e3:.0f} kB"]
    if facts["pages"] is not None:
        parts.append(f"{facts['pages']} page{'s' if facts['pages'] != 1 else ''}")
    if facts["annots"]:
        parts.append(f"{facts['annots']} annotation{'s' if facts['annots'] != 1 else ''}")
    if facts["text"] is False:
        parts.append("scan without text")
    return "  ·  ".join(parts)


def keep_score(facts, name):
    # Your own notes matter most, then a searchable text layer, then completeness;
    # for identical files, prefer "paper.pdf" over "paper (1).pdf" / "paper - Copy.pdf".
    looks_like_copy = bool(re.search(r"\(\d+\)|\bcopy\b|\bkopie\b", name, re.I))
    return (facts["annots"] > 0, facts["text"] is not False, facts["pages"] or 0, facts["size"],
            not looks_like_copy)


class DuplicatesDialog(QDialog):
    """One card per paper with 2+ copies: pick the copy to keep, the rest go to the trash."""

    def __init__(self, parent, groups, display_name=lambda path: path.name):
        super().__init__(parent)
        self.display_name = display_name
        self.setWindowTitle("Duplicates")
        self.resize(780, 580)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 18)
        lay.setSpacing(10)
        lay.addWidget(QLabel("Duplicate copies", objectName="dialogTitle"))
        sub = QLabel(f"These files are the same paper (same DOI, or same author, year and title) or identical copies. Choose the copy "
                     f"to keep; the others are moved to the {TRASH}, so you can still restore them.",
                     objectName="muted", wordWrap=True)
        lay.addWidget(sub)

        inner = QWidget(objectName="scrollInner")
        il = QVBoxLayout(inner)
        il.setContentsMargins(0, 4, 0, 4)
        il.setSpacing(10)
        self.choices = []  # (clean-up checkbox, radio group, rows)
        for group in groups:
            il.addWidget(self.group_card(group))
        il.addStretch(1)
        scroll = QScrollArea(widgetResizable=True)
        scroll.setWidget(inner)
        lay.addWidget(scroll, 1)

        foot = QHBoxLayout()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        self.ok = QPushButton(objectName="primary")
        self.ok.clicked.connect(self.accept)
        foot.addStretch(1)
        foot.addWidget(cancel)
        foot.addWidget(self.ok)
        lay.addLayout(foot)
        self.update_summary()

    def group_card(self, group):
        card = QFrame(objectName="card")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(16, 12, 16, 12)
        cl.setSpacing(6)
        head = QHBoxLayout()
        title = next((r.plan.title for r in group if r.plan.title), "") or "Unidentified paper"
        head.addWidget(QLabel(title, objectName="groupTitle", wordWrap=True), 1)
        clean = QCheckBox("Remove extra copies", checked=True)
        head.addWidget(clean, 0, Qt.AlignmentFlag.AlignTop)
        cl.addLayout(head)
        doi = next((r.doi for r in group if r.doi), "")
        same_bytes = len({r.hash for r in group}) == 1
        cl.addWidget(QLabel(("Identical files" if same_bytes else "Same paper, different files")
                            + (f"  ·  DOI {doi}" if doi else ""), objectName="muted"))

        radios = QButtonGroup(card)
        facts = [pdf_facts(r.path) for r in group]
        best = max(range(len(group)), key=lambda k: keep_score(facts[k], group[k].path.name))
        widgets = []
        for k, (r, f) in enumerate(zip(group, facts)):
            line = QHBoxLayout()
            rb = QRadioButton(self.display_name(r.path), checked=(k == best))
            rb.setToolTip("Keep this copy")
            rb.setMinimumWidth(max(240, rb.sizeHint().width()))  # line the details up
            radios.addButton(rb, k)
            info = QLabel(describe(f) + ("  ·  suggested" if k == best else ""), objectName="muted")
            open_btn = QPushButton("Open", objectName="small")
            open_btn.clicked.connect(lambda _=False, p=r.path: QDesktopServices.openUrl(QUrl.fromLocalFile(str(p))))
            line.addWidget(rb)
            line.addSpacing(8)
            line.addWidget(info, 1)
            line.addWidget(open_btn)
            cl.addLayout(line)
            widgets += [rb, info]
        clean.toggled.connect(lambda on: [w.setEnabled(on) for w in widgets])
        clean.toggled.connect(self.update_summary)
        radios.idClicked.connect(self.update_summary)
        self.choices.append((clean, radios, group))
        return card

    def to_trash(self):
        return [r for clean, radios, group in self.choices if clean.isChecked()
                for k, r in enumerate(group) if k != radios.checkedId()]

    def update_summary(self, *_):
        n = len(self.to_trash())
        self.ok.setText(f"Move {n} file{'s' if n != 1 else ''} to {TRASH}" if n else "Nothing to remove")
        self.ok.setEnabled(n > 0)


# ---------------------------------------------------------------- matches & editions

def short_authors(meta):
    names = [a[1] for a in meta.authors[:2]]
    return " & ".join(names) + (" et al." if len(meta.authors) > 2 else "")


def option_line(meta, note=""):
    """(radio text, detail text) for one record in the review dialog."""
    head = f"{meta.year or '?'}  ·  {meta.publisher or meta.journal or 'publisher unknown'}"
    title = re.sub(r"<[^>]+>", "", meta.title)
    bits = [title if len(title) <= 70 else title[:68] + "…", short_authors(meta),
            f"ISBN {meta.isbn}" if meta.isbn else "", f"DOI {meta.doi}" if meta.doi else "", note]
    return head, "  ·  ".join(b for b in bits if b)


class ReviewDialog(QDialog):
    """One card per file: keep its current record or pick another edition or match."""

    def __init__(self, parent, items, display_name=lambda path: path.name):
        super().__init__(parent)
        self.setWindowTitle("Matches and editions")
        self.resize(900, 620)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 18)
        lay.setSpacing(10)
        lay.addWidget(QLabel("Matches and editions", objectName="dialogTitle"))
        lay.addWidget(QLabel("Other editions of these books, and other records the fuzzy searches found. "
                             "Pick the one that fits each PDF; it's renamed after that one. Nothing changes "
                             "until you click Rename.", objectName="muted", wordWrap=True))
        inner = QWidget(objectName="scrollInner")
        il = QVBoxLayout(inner)
        il.setContentsMargins(0, 4, 0, 4)
        il.setSpacing(10)
        self.choices = []  # (radio group, row, options)
        for row, options in items:
            il.addWidget(self.card(row, options, display_name))
        il.addStretch(1)
        scroll = QScrollArea(widgetResizable=True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)  # details wrap instead
        scroll.setWidget(inner)
        lay.addWidget(scroll, 1)
        foot = QHBoxLayout()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        self.ok = QPushButton(objectName="primary")
        self.ok.clicked.connect(self.accept)
        foot.addStretch(1)
        foot.addWidget(cancel)
        foot.addWidget(self.ok)
        lay.addLayout(foot)
        self.update_summary()

    def card(self, row, options, display_name):
        card = QFrame(objectName="card")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(16, 12, 16, 12)
        cl.setSpacing(6)
        head = QHBoxLayout()
        head.addWidget(QLabel(display_name(row.path), objectName="groupTitle", wordWrap=True), 1)
        open_btn = QPushButton("Open", objectName="small")
        open_btn.clicked.connect(lambda _=False, p=row.path: QDesktopServices.openUrl(QUrl.fromLocalFile(str(p))))
        head.addWidget(open_btn, 0, Qt.AlignmentFlag.AlignTop)
        cl.addLayout(head)
        cl.addWidget(QLabel(f"Now: {row.new_name or row.path.name}  ·  found via {row.method}", objectName="muted"))
        radios = QButtonGroup(card)
        for k, (meta, note) in enumerate([(row.plan.meta, "current")] + [(m, n) for m, _, n in options]):
            text, detail = option_line(meta, note)
            line = QHBoxLayout()
            rb = QRadioButton(text.replace("&", "&&"), checked=(k == 0))  # a single & would be a shortcut
            rb.setMinimumWidth(max(300, rb.sizeHint().width()))
            radios.addButton(rb, k)
            info = QLabel(detail, objectName="muted", wordWrap=True)
            info.setToolTip(details_html(meta))
            line.addWidget(rb, 0, Qt.AlignmentFlag.AlignTop)
            line.addSpacing(8)
            line.addWidget(info, 1)
            cl.addLayout(line)
        radios.idClicked.connect(self.update_summary)
        self.choices.append((radios, row, options))
        return card

    def changes(self):
        """[(row, (Meta, how, note))] for each file where another record was picked."""
        return [(row, options[radios.checkedId() - 1]) for radios, row, options in self.choices
                if radios.checkedId() > 0]

    def update_summary(self, *_):
        n = len(self.changes())
        self.ok.setText(f"Use {n} new record{'s' if n != 1 else ''}" if n else "No changes")
        self.ok.setEnabled(n > 0)


# ---------------------------------------------------------------- watching a folder

WATCH_RESULTS = {  # result -> (label, status whose colours it uses)
    "filed": ("Filed", "renamed"), "duplicate": ("Duplicate", "check"), "check": ("Check", "check"),
    "not found": ("Not found", "skip"), "error": ("Failed", "error"), "trashed": ("In the Trash", "ok"),
    "dismissed": ("Dismissed", "ok"),
}
UNDECIDED = ("check", "duplicate", "not found")  # results that leave the PDF waiting for you


def waiting_for_you(entry, watch_dir):
    """Is this activity entry a PDF still in the watched folder that needs your decision?"""
    return (entry["result"] in UNDECIDED and not entry.get("by_you")
            and Path(watch_dir, entry["source"]).exists())


class WatchDialog(QDialog):
    """Settings, start/stop and recent activity of the folder watcher."""

    def __init__(self, win):
        super().__init__(win)
        self.win, settings = win, win.settings
        self.setWindowTitle("Watch a folder")
        self.resize(860, 640)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 18)
        lay.setSpacing(8)
        lay.addWidget(QLabel("Watch a folder", objectName="dialogTitle"))
        lay.addWidget(QLabel(
            "New PDFs in the watched folder (e.g. Downloads) are identified, renamed with your naming pattern and "
            "moved to the output folder, every few seconds while Paper Renamer is open. Each PDF is looked at "
            "once. Duplicates of papers already in the output folder, PDFs that couldn't be identified, and "
            "fuzzy matches stay where they are and are listed below.", objectName="muted", wordWrap=True))
        lay.addSpacing(6)
        self.edits = {}
        for key, label, hint in (("watch_dir", "Watch", "Choose the folder to watch"),
                                 ("output_dir", "Move to", "Choose where identified PDFs go")):
            row = QHBoxLayout()
            name = QLabel(label)
            name.setMinimumWidth(70)
            edit = QLineEdit(settings.value(key, ""), readOnly=True, placeholderText=hint)
            edit.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            choose = QPushButton("Choose…")
            choose.clicked.connect(lambda _=False, k=key: self.choose(k))
            row.addWidget(name)
            row.addWidget(edit, 1)
            row.addWidget(choose)
            lay.addLayout(row)
            self.edits[key] = edit
        self.fuzzy_box = QCheckBox("Also file fuzzy matches (title, text and file name searches, marked Check)")
        self.fuzzy_box.setChecked(settings.value("watch_fuzzy", False, type=bool))
        self.fuzzy_box.setToolTip("Off: they stay in the watched folder for you to check (scan that folder here, "
                                  "or right-click them to identify them).")
        self.fuzzy_box.toggled.connect(self.fuzzy_toggled)
        self.auto_box = QCheckBox("Start watching when Paper Renamer opens")
        self.auto_box.setChecked(settings.value("watch_autostart", False, type=bool))
        self.auto_box.toggled.connect(lambda on: self.win.settings.setValue("watch_autostart", on))
        lay.addWidget(self.fuzzy_box)
        lay.addWidget(self.auto_box)
        status_row = QHBoxLayout()
        self.status = QLabel(objectName="muted", wordWrap=True)
        self.toggle = QPushButton(objectName="primary")
        self.toggle.setMinimumWidth(160)
        self.toggle.clicked.connect(self.start_or_stop)
        status_row.addWidget(self.status, 1)
        status_row.addWidget(self.toggle)
        lay.addSpacing(4)
        lay.addLayout(status_row)

        lay.addSpacing(8)
        lay.addWidget(QLabel("Recent activity", objectName="groupTitle"))
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["When", "File", "Result", "Filed as / duplicate of / details", ""])
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setDefaultSectionSize(32)
        hh = self.table.horizontalHeader()
        hh.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        for col, width in ((0, 110), (1, 240), (2, 130), (4, 124)):
            self.table.setColumnWidth(col, width)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self.table.cellDoubleClicked.connect(self.open_entry)
        lay.addWidget(self.table, 1)
        lay.addWidget(QLabel("Double-click an entry to open the file. Use “Decide…” for PDFs that are waiting "
                             "for you.", objectName="muted"))

        foot = QHBoxLayout()
        again = QPushButton("Look at every PDF again", objectName="link")
        again.setToolTip("Forget which PDFs in the watched folder were already looked at, e.g. after\n"
                         "turning on “Also file fuzzy matches”. Identified papers still come from the cache.")
        again.clicked.connect(self.forget)
        self.dismiss_all = QPushButton("Dismiss all not found", objectName="link", visible=False)
        self.dismiss_all.setToolTip("Leave every PDF that couldn't be identified as it is and stop asking about "
                                    "them, e.g. when\nthe watched folder holds other PDFs than papers.")
        self.dismiss_all.clicked.connect(lambda: self.win.watch_dismiss(
            [e.get("id") or f"{e['when']:.6f}:{e['source']}" for e in self.log_shown
             if e["result"] == "not found" and waiting_for_you(e, self.edits["watch_dir"].text())], self))
        open_out = QPushButton("Open output folder")
        open_out.clicked.connect(lambda: self.edits["output_dir"].text() and QDesktopServices.openUrl(
            QUrl.fromLocalFile(self.edits["output_dir"].text())))
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        foot.addWidget(again)
        foot.addWidget(self.dismiss_all)
        foot.addStretch(1)
        foot.addWidget(open_out)
        foot.addWidget(close)
        lay.addLayout(foot)
        self.refresh()

    def fuzzy_toggled(self, on):
        self.win.settings.setValue("watch_fuzzy", on)
        if self.win.watcher:  # applies to PDFs looked at from now on
            self.win.watcher.file_fuzzy = on

    def choose(self, key):
        start = self.edits[key].text() or str(Path.home())
        folder = QFileDialog.getExistingDirectory(self, "Choose a folder", start)
        if folder:
            self.edits[key].setText(str(Path(folder)))
            self.win.settings.setValue(key, str(Path(folder)))
            if self.win.watch_thread:  # use the new folders right away
                self.win.stop_watching()
                self.win.start_watching()
            self.refresh()

    def start_or_stop(self):
        if self.win.watch_thread:
            self.win.stop_watching()
        else:
            self.win.start_watching()
        self.refresh()

    def forget(self):
        if self.win.watcher:
            self.win.watcher.forget()
        else:
            rp.forget_watched()
        self.status.setText("Every PDF in the watched folder will be looked at again.")

    def refresh(self):
        watching = self.win.watch_thread is not None
        ready = bool(self.edits["watch_dir"].text() and self.edits["output_dir"].text())
        self.toggle.setText("Stop watching" if watching else "Start watching")
        self.toggle.setEnabled(watching or ready)
        if watching:
            self.status.setText(f"Watching {self.edits['watch_dir'].text()}  →  {self.edits['output_dir'].text()}")
        else:
            self.status.setText("Not watching." if ready else "Choose both folders to start.")
        log = list(reversed(self.win.watch_log()))
        watch_dir = self.edits["watch_dir"].text()
        self.table.setRowCount(len(log))
        for i, e in enumerate(log):
            label, status = WATCH_RESULTS.get(e["result"], (e["result"], "error"))
            label += ", by you" if e.get("by_you") else ""
            where = Path(e["target"]).name if e.get("target") else e.get("detail", "")
            if e["result"] == "duplicate":
                where = f"duplicate of {where}"
            cells = [time.strftime("%d %b %H:%M", time.localtime(e["when"])), e["source"], label, where]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setToolTip(e.get("target") or e.get("detail") or text)
                if col == 2:
                    item.setForeground(QColor(self.win.status_colors(status)[1]))
                self.table.setItem(i, col, item)
            self.table.removeCellWidget(i, 4)
            dismissed = e["result"] == "dismissed" and watch_dir and Path(watch_dir, e["source"]).exists()
            if watch_dir and (waiting_for_you(e, watch_dir) or dismissed):
                button = QPushButton("Dismissed" if dismissed else "Decide…", objectName="small")
                button.setMenu(self.decide_menu(e, button))
                self.table.setCellWidget(i, 4, button)
        self.log_shown = log
        self.table.doItemsLayout()  # place new "Decide…" buttons now, not at the next paint
        self.dismiss_all.setVisible(any(e["result"] == "not found" and waiting_for_you(e, watch_dir)
                                        for e in log) if watch_dir else False)
        self.win.update_watch_button()

    def decide_menu(self, e, parent):
        """What you can do with a PDF the watcher left in the watched folder."""
        menu, win = QMenu(parent), self.win
        pdf = Path(self.edits["watch_dir"].text(), e["source"])
        entry_id = e.get("id") or f"{e['when']:.6f}:{e['source']}"
        if e["result"] == "dismissed":
            menu.addAction("Undo dismiss (ask about it again)", lambda: win.watch_dismiss([entry_id], self, undo=True))
            menu.addAction("Open PDF", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(pdf))))
            return menu
        if e["result"] == "check":
            suggestion = re.sub(r"^maybe (\S+\.pdf).*", r"\1", e.get("detail", ""))
            menu.addAction(f"File it as {suggestion}" if suggestion.endswith(".pdf") else "File it as found",
                           lambda: win.watch_decide(lambda w: w.resolve(entry_id), self))
            menu.addAction("Choose another match…", lambda: win.watch_choose(entry_id, pdf, self))
        if e["result"] in ("check", "not found"):
            menu.addAction("Identify by DOI, ISBN, arXiv ID or bibcode…", lambda: win.watch_identify(entry_id, pdf, self))
        if e["result"] == "duplicate":
            menu.addAction(f"Move it to the {TRASH}", lambda: win.watch_trash(entry_id, pdf, e.get("target", ""), self))
            menu.addAction("File it anyway (keep both)",
                           lambda: win.watch_decide(lambda w: w.resolve(entry_id, force=True), self))
            menu.addAction("Open both", lambda: [QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))
                                                 for p in (pdf, Path(e.get("target", ""))) if p.exists()])
        menu.addSeparator()
        dismiss = menu.addAction("Dismiss (leave the PDF as it is)", lambda: win.watch_dismiss([entry_id], self))
        dismiss.setToolTip("For files that aren't papers, or that you want to keep where they are: "
                           "stop asking about it.")
        menu.addAction("Open PDF", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(pdf))))
        return menu

    def open_entry(self, i, _col):
        e = self.log_shown[i]
        path = Path(e["target"]) if e.get("target") else Path(self.edits["watch_dir"].text(), e["source"])
        if path.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


# ---------------------------------------------------------------- API settings

ADS_TOKEN_URL = "https://ui.adsabs.harvard.edu/user/settings/token"


def apply_api_settings(settings):
    rp.set_contact(settings.value("email", ""))
    rp.ADS_TOKEN = settings.value("ads_token", "") or rp.os.environ.get("ADS_TOKEN", "")


class ApiSettingsDialog(QDialog):
    """Optional details for the online services: a contact email and an ADS token."""

    def __init__(self, parent, settings):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("API settings")
        self.setMinimumWidth(580)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 18)
        lay.setSpacing(8)
        lay.addWidget(QLabel("API settings", objectName="dialogTitle"))
        lay.addWidget(QLabel("Both are optional and stay on this computer, in the app's settings.",
                             objectName="muted", wordWrap=True))
        lay.addSpacing(8)

        lay.addWidget(QLabel("Contact email for Crossref and OpenAlex", objectName="groupTitle"))
        lay.addWidget(QLabel("Requests that include an email are served by faster servers (their “polite pool”). "
                             "It's only sent to these two services.", objectName="muted", wordWrap=True))
        self.email = QLineEdit(settings.value("email", ""), placeholderText="you@example.com")
        lay.addWidget(self.email)
        lay.addSpacing(10)

        lay.addWidget(QLabel("NASA ADS API token", objectName="groupTitle"))
        ads_help = QLabel("Looks up astronomy papers by their ADS bibcode (stamped on ADS scans) on ADS itself, "
                          "which knows more of them than OpenAlex. Get a free token under "
                          f'<a href="{ADS_TOKEN_URL}">Account → Settings → API Token</a>.',
                          objectName="muted", wordWrap=True, openExternalLinks=True)
        lay.addWidget(ads_help)
        token_row = QHBoxLayout()
        self.token = QLineEdit(settings.value("ads_token", ""), placeholderText="Paste your token")
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        check = QPushButton("Check")
        check.clicked.connect(self.check_token)
        token_row.addWidget(self.token, 1)
        token_row.addWidget(check)
        lay.addLayout(token_row)
        self.token_status = QLabel(objectName="muted")
        lay.addWidget(self.token_status)

        foot = QHBoxLayout()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        save = QPushButton("Save", objectName="primary")
        save.clicked.connect(self.save)
        foot.addStretch(1)
        foot.addWidget(cancel)
        foot.addWidget(save)
        lay.addSpacing(8)
        lay.addLayout(foot)

    def check_token(self):
        token = self.token.text().strip()
        if not token:
            self.token_status.setText("Paste a token first.")
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            ok = rp.check_ads_token(token)
            self.token_status.setText("✓ ADS accepts this token." if ok else "✗ ADS doesn't accept this token.")
        except rp.requests.RequestException:
            self.token_status.setText("Couldn't reach ADS. Check your internet connection.")
        finally:
            QApplication.restoreOverrideCursor()

    def save(self):
        self.settings.setValue("email", self.email.text().strip())
        self.settings.setValue("ads_token", self.token.text().strip())
        apply_api_settings(self.settings)
        self.accept()


# ---------------------------------------------------------------- main window

COL_CHECK, COL_STATUS, COL_FILE, COL_NEW, COL_VIA, COL_DOI, COL_DETAIL = range(7)
ABSTRACT_CHARS = 900  # longer abstracts are cut off in the Details tooltip


class MainWindow(QMainWindow):
    def __init__(self, cache=None):
        super().__init__()
        self.settings = QSettings("PaperRenamer", "PaperRenamer")
        apply_api_settings(self.settings)
        self.cache = cache or rp.Cache()
        self.rows, self.undo_stack, self.dup_groups = [], [], []
        self.worker = self.exporter = self.task = None
        self.watcher = self.watch_thread = self.watch_dialog = None
        self.folder = None
        self.scan_order = {}  # pdf -> position in the folder listing
        self.pattern = self.settings.value("pattern", rp.DEFAULT_PATTERN)
        try:
            rp.check_pattern(self.pattern)
        except ValueError:
            self.pattern = rp.DEFAULT_PATTERN

        self.setWindowTitle("Paper Renamer")
        self.setWindowIcon(app_icon(theme()["accent"]))
        QApplication.instance().setWindowIcon(self.windowIcon())  # dialogs and the taskbar too
        self.resize(1340, 800)
        self.setAcceptDrops(True)

        root = QWidget(objectName="root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(28, 24, 28, 20)
        outer.setSpacing(14)

        # header
        head = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(4)
        titles.addWidget(QLabel("Paper Renamer", objectName="title"))
        subtitle = QLabel("Renames PDFs of papers using their DOI or text, e.g. to Fermi1926.pdf. "
                          "Nothing changes until you click Rename.", objectName="subtitle", wordWrap=True)
        titles.addWidget(subtitle)
        head.addLayout(titles, 1)
        self.cache_btn = QPushButton(objectName="link")
        self.cache_btn.setToolTip("Identified papers are remembered by their contents, so scanning a folder "
                                  "again is fast.\nClearing this makes the next scan look everything up again.")
        self.cache_btn.clicked.connect(self.clear_cache)
        head.addWidget(self.cache_btn, 0, Qt.AlignmentFlag.AlignTop)
        api_btn = QPushButton("API settings…", objectName="link")
        api_btn.setToolTip("Optional: your email for faster Crossref/OpenAlex service, and an ADS token.")
        api_btn.clicked.connect(lambda: ApiSettingsDialog(self, self.settings).exec())
        self.theme_btn = QPushButton(objectName="link")
        self.theme_btn.setToolTip("Colour scheme")
        theme_menu = QMenu(self.theme_btn)
        self.theme_actions = {}
        for key in THEME_CHOICES:
            action = theme_menu.addAction("System (follows your computer)" if key == "system" else THEMES[key]["name"],
                                          lambda k=key: self.set_theme(k))
            action.setCheckable(True)
            self.theme_actions[key] = action
        self.theme_btn.setMenu(theme_menu)
        head.addWidget(self.theme_btn, 0, Qt.AlignmentFlag.AlignTop)
        self.watch_btn = QPushButton("Watch folder…", objectName="link")
        self.watch_btn.setToolTip("Automatically rename new PDFs in a folder (e.g. Downloads) and move them "
                                  "to another one.")
        self.watch_btn.clicked.connect(self.open_watch_dialog)
        head.addWidget(self.watch_btn, 0, Qt.AlignmentFlag.AlignTop)
        head.addWidget(api_btn, 0, Qt.AlignmentFlag.AlignTop)
        outer.addLayout(head)

        # folder & options card
        card = QFrame(objectName="card")
        card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        cl = QVBoxLayout(card)
        cl.setContentsMargins(16, 14, 16, 14)
        cl.setSpacing(10)
        row1 = QHBoxLayout()
        self.path_edit = QLineEdit(readOnly=True, placeholderText="No folder chosen")
        self.path_edit.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        choose = QPushButton("Choose folder…")
        choose.clicked.connect(self.choose_folder)
        row1.addWidget(self.path_edit, 1)
        row1.addWidget(choose)
        cl.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("File names"))
        self.pattern_box = QComboBox(editable=True)
        self.pattern_box.addItems(PATTERN_PRESETS)
        self.pattern_box.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.pattern_box.setCurrentText(self.pattern)
        self.pattern_box.setToolTip("Pick a preset or type your own pattern. Placeholders:\n" + "\n".join(
            f"  {{{k}}}  –  {v}" for k, v in rp.PLACEHOLDERS.items()))
        self.pattern_box.editTextChanged.connect(self.pattern_changed)
        self.pattern_preview = QLabel(objectName="muted")
        row2.addWidget(self.pattern_box)
        row2.addSpacing(6)
        row2.addWidget(self.pattern_preview, 1)
        cl.addLayout(row2)

        row3 = QHBoxLayout()
        row3.setSpacing(18)
        self.subfolder_box = QCheckBox("Include subfolders")
        self.subfolder_box.setChecked(self.settings.value("subfolders", False, type=bool))
        self.subfolder_box.setToolTip("Also scan PDFs in folders inside this one. "
                                      "Files are renamed where they are; nothing is moved.")
        self.subfolder_box.toggled.connect(self.subfolders_toggled)
        self.ocr_box = QCheckBox("Read scanned PDFs with OCR")
        self.ocr_box.setChecked(self.settings.value("ocr", True, type=bool))
        self.ocr_box.setToolTip("Only used for PDFs that can't be identified from their text (slower). "
                                "Needs Tesseract to be installed.")
        self.deep_ocr_box = QCheckBox("More pages for scanned books")
        self.deep_ocr_box.setChecked(self.settings.value("deep_ocr", False, type=bool))
        self.deep_ocr_box.setToolTip(
            f"If the first {rp.OCR_PAGES} pages of a scanned book ({rp.BOOK_PAGES}+ pages) don't identify it, "
            f"also read pages {rp.OCR_PAGES + 1}–{rp.DEEP_OCR_PAGES},\nwhere the title and copyright page (with the "
            "ISBN) usually are. Slow: a few seconds per page.")
        self.deep_ocr_box.setEnabled(self.ocr_box.isChecked())
        self.ocr_box.toggled.connect(self.deep_ocr_box.setEnabled)
        self.meta_box = QCheckBox("Write title, authors, DOI && tags into the PDFs")
        self.meta_box.setChecked(self.settings.value("write_meta", False, type=bool) and bool(rp.pymupdf))
        self.meta_box.setEnabled(bool(rp.pymupdf))
        self.meta_box.setToolTip(
            "When renaming, also store the paper's details in the PDF itself, so reference managers and "
            "file browsers show them\nand later scans are instant. Undo restores names, not these details."
            if rp.pymupdf else "Needs PyMuPDF (pip install pymupdf).")
        self.scan_btn = QPushButton("Scan folder", objectName="primary", enabled=False)
        self.scan_btn.setMinimumWidth(150)
        self.scan_btn.clicked.connect(self.scan_or_stop)
        for box in (self.subfolder_box, self.ocr_box, self.deep_ocr_box, self.meta_box):
            row3.addWidget(box)
        row3.addStretch(1)
        row3.addWidget(self.scan_btn)
        cl.addLayout(row3)
        self.progress = QProgressBar(textVisible=False, visible=False)
        self.progress_label = QLabel(objectName="muted", visible=False)
        cl.addWidget(self.progress)
        cl.addWidget(self.progress_label)
        outer.addWidget(card)

        self.banner = QLabel(objectName="banner", visible=False, wordWrap=True)
        outer.addWidget(self.banner)

        # filters
        frow = QHBoxLayout()
        frow.setSpacing(6)
        self.filter_group = QButtonGroup(self, exclusive=True)
        self.filter_btns = {}
        for key, label, _ in FILTERS:
            b = QPushButton(label, objectName="filter", checkable=True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(self.apply_filter)
            self.filter_group.addButton(b)
            self.filter_btns[key] = b
            frow.addWidget(b)
        self.filter_btns["all"].setChecked(True)
        frow.addStretch(1)
        self.search = QLineEdit(placeholderText="Search titles, authors, tags, abstracts…", clearButtonEnabled=True)
        self.search.setMinimumWidth(280)
        self.search.textChanged.connect(self.apply_filter)
        frow.addWidget(self.search)
        self.review_btn = QPushButton("Review matches && editions…", visible=False)
        self.review_btn.setToolTip("See other editions of the books found, and other records the fuzzy searches "
                                   "found,\nand pick the right one for each file.")
        self.review_btn.clicked.connect(lambda: self.review_matches([r for r in self.rows if self.reviewable(r)]))
        frow.addWidget(self.review_btn)
        self.dup_btn = QPushButton("Review duplicates…", visible=False)
        self.dup_btn.clicked.connect(self.review_duplicates)
        frow.addWidget(self.dup_btn)
        self.filter_bar = QWidget()
        self.filter_bar.setLayout(frow)
        frow.setContentsMargins(0, 0, 0, 0)
        self.filter_bar.setVisible(False)
        outer.addWidget(self.filter_bar)

        # table / empty state
        self.stack = QStackedWidget()
        empty = QFrame(objectName="empty")
        el = QVBoxLayout(empty)
        el.addStretch(1)
        self.empty_title = QLabel("Choose a folder of PDFs to get started", objectName="emptyTitle",
                                  alignment=Qt.AlignmentFlag.AlignCenter)
        self.empty_sub = QLabel("…or drag a folder onto this window.", objectName="muted",
                                alignment=Qt.AlignmentFlag.AlignCenter)
        el.addWidget(self.empty_title)
        el.addWidget(self.empty_sub)
        el.addStretch(1)
        self.stack.addWidget(empty)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["", "Status", "Current name", "New name", "Found via", "DOI", "Details"])
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                   | QAbstractItemView.EditTrigger.EditKeyPressed)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setDefaultSectionSize(38)
        self.table.setItemDelegateForColumn(COL_STATUS, StatusDelegate(self.table))
        hh = self.table.horizontalHeader()
        hh.setHighlightSections(False)
        hh.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        for col, width in ((COL_CHECK, 36), (COL_STATUS, 215), (COL_FILE, 225), (COL_NEW, 170),
                           (COL_VIA, 135), (COL_DOI, 205)):
            hh.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
            self.table.setColumnWidth(col, width)
        hh.setSectionResizeMode(COL_CHECK, QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(COL_DETAIL, QHeaderView.ResizeMode.Stretch)
        self.table.itemChanged.connect(self.item_changed)
        self.table.cellDoubleClicked.connect(self.cell_double_clicked)
        self.table.cellClicked.connect(self.cell_clicked)
        self.table.setMouseTracking(True)  # for the hand cursor over DOI links
        self.table.cellEntered.connect(self.cell_entered)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.context_menu)
        self.stack.addWidget(self.table)
        outer.addWidget(self.stack, 1)

        # footer
        foot = QHBoxLayout()
        self.hint = QLabel("Double-click a file to open it, or its new name to edit it. "
                           "Untick files you don't want renamed.", objectName="muted", visible=False)
        self.export_btn = QPushButton("Export citations", objectName="menuButton", enabled=False)
        export_menu = QMenu(self.export_btn)
        export_menu.addAction("BibTeX (.bib) – LaTeX, JabRef, Zotero…", lambda: self.export("bibtex"))
        export_menu.addAction("RIS (.ris) – EndNote, Mendeley, Zotero…", lambda: self.export("ris"))
        self.export_btn.setMenu(export_menu)
        self.undo_btn = QPushButton("Undo", enabled=False)
        self.undo_btn.clicked.connect(self.undo)
        self.apply_btn = QPushButton("Rename files", objectName="primary", enabled=False)
        self.apply_btn.clicked.connect(self.apply_renames)
        foot.addWidget(self.hint)
        foot.addStretch(1)
        foot.addWidget(self.export_btn)
        foot.addWidget(self.undo_btn)
        foot.addWidget(self.apply_btn)
        outer.addLayout(foot)

        self.update_cache_button()
        self.show_theme_choice()
        try:  # follow the computer's light/dark switch while "System" is chosen (Qt 6.5+)
            QApplication.instance().styleHints().colorSchemeChanged.connect(
                lambda _scheme: self.settings.value("theme", "system") == "system" and self.set_theme("system"))
        except AttributeError:
            pass
        self.pattern_changed(self.pattern)
        last = self.settings.value("folder", "")
        if last and Path(last).is_dir():
            self.set_folder(last)
        if self.settings.value("watch_autostart", False, type=bool):
            self.start_watching(quiet=True)

    # ----- helpers

    @property
    def dark(self):
        return theme()["dark"]

    def set_theme(self, choice):
        """Switch the colour scheme now and remember it."""
        self.settings.setValue("theme", choice)
        apply_theme(QApplication.instance(), choice)
        self.setWindowIcon(app_icon(theme()["accent"]))
        for i in range(len(self.rows)):  # the DOI links' colour is set per cell
            self.update_row(i)
        self.table.viewport().update()
        if self.watch_dialog:
            self.watch_dialog.refresh()
        self.show_theme_choice()

    def show_theme_choice(self):
        choice = self.settings.value("theme", "system")
        choice = choice if choice in THEME_CHOICES else "system"
        for key, action in self.theme_actions.items():
            action.setChecked(key == choice)
        self.theme_btn.setText("Theme: " + ("System" if choice == "system" else THEMES[choice]["name"]))

    def display_name(self, path):
        """File name, with its subfolder when subfolders are included."""
        try:
            return Path(path).relative_to(self.folder).as_posix()
        except (ValueError, TypeError):
            return Path(path).name

    def busy(self):
        return self.worker is not None or self.exporter is not None or self.task is not None

    def show_progress(self, visible):
        self.progress.setVisible(visible)
        self.progress_label.setVisible(visible)

    # ----- folder selection & options

    def choose_folder(self):
        start = self.folder or str(Path.home())
        folder = QFileDialog.getExistingDirectory(self, "Choose a folder of PDFs", start)
        if folder:
            self.set_folder(folder)

    def set_folder(self, folder):
        if self.busy():
            return
        self.folder = str(Path(folder))
        self.settings.setValue("folder", self.folder)
        self.path_edit.setText(self.folder)
        self.rows, self.undo_stack, self.dup_groups = [], [], []
        self.dup_btn.setVisible(False)
        self.review_btn.setVisible(False)
        self.table.setRowCount(0)
        self.stack.setCurrentIndex(0)
        self.filter_bar.setVisible(False)
        self.hint.setVisible(False)
        self.banner.setVisible(False)
        self.update_scan_button()
        self.refresh_counts()

    def pdf_count(self):
        try:
            return len(rp.list_pdfs(self.folder, self.subfolder_box.isChecked())) if self.folder else 0
        except OSError:
            return 0

    def update_scan_button(self):
        count = self.pdf_count()
        where = " (incl. subfolders)" if self.subfolder_box.isChecked() else ""
        self.scan_btn.setEnabled(count > 0 and not self.busy() and self.pattern_ok)
        self.scan_btn.setText("Scan again" if self.rows else
                              f"Scan {count} PDF{'s' if count != 1 else ''}" if count else "No PDFs here")
        if count:
            self.empty_title.setText(f"{count} PDF{'s' if count != 1 else ''}{where} ready to scan")
            self.empty_sub.setText("Click Scan to look them up. Nothing is renamed until you confirm.")
        elif self.folder:
            self.empty_title.setText("No PDFs in this folder")
            self.empty_sub.setText("Tick “Include subfolders”, choose another folder, or drag one onto this window."
                                   if not self.subfolder_box.isChecked()
                                   else "Choose another folder, or drag one onto this window.")

    def subfolders_toggled(self, on):
        self.settings.setValue("subfolders", on)
        self.update_scan_button()
        if self.rows:
            self.show_banner("Scan again to " + ("include" if on else "leave out") + " PDFs in subfolders.", "info")

    def pattern_changed(self, text):
        try:
            rp.check_pattern(text)
        except ValueError as e:
            self.pattern_ok = False
            self.pattern_preview.setObjectName("warnText")
            self.pattern_preview.setText(f"⚠ {str(e).capitalize()}")
        else:
            self.pattern_ok = True
            self.pattern = text
            if self.watcher:
                self.watcher.pattern = text
            self.settings.setValue("pattern", text)
            self.pattern_preview.setObjectName("muted")
            examples = [r.plan.meta for r in self.rows if r.plan.meta][:2] or SAMPLE_PAPERS
            self.pattern_preview.setText("e.g.  " + ",   ".join(f"{rp.make_name(m, text)}.pdf" for m in examples))
            if self.rows:
                self.reassign_names()
        self.pattern_preview.style().polish(self.pattern_preview)
        self.update_scan_button()
        self.refresh_counts()

    def update_cache_button(self):
        n = self.cache.count
        self.cache_btn.setText(f"Clear cache ({n} paper{'s' if n != 1 else ''})")
        self.cache_btn.setVisible(n > 0)

    def clear_cache(self):
        self.cache.clear()
        rp.clear_lookup_caches()
        self.update_cache_button()
        self.show_banner("Cache cleared: the next scan will look every paper up again.", "info")

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        path = Path(e.mimeData().urls()[0].toLocalFile())
        self.set_folder(path if path.is_dir() else path.parent)

    # ----- scanning

    def scan_or_stop(self):
        if self.worker:
            self.worker.cancelled = True
            self.scan_btn.setEnabled(False)
            self.scan_btn.setText("Stopping…")
            return
        try:
            pdfs = rp.list_pdfs(self.folder, self.subfolder_box.isChecked())
        except OSError as e:
            self.show_banner(f"Can't read {self.folder}: {e.strerror or e}", "warn")
            return
        self.settings.setValue("ocr", self.ocr_box.isChecked())
        self.settings.setValue("deep_ocr", self.deep_ocr_box.isChecked())
        self.rows, self.undo_stack, self.dup_groups = [], [], []
        self.dup_btn.setVisible(False)
        self.review_btn.setVisible(False)
        self.table.setRowCount(0)
        self.stack.setCurrentIndex(1)
        self.filter_bar.setVisible(True)
        self.filter_btns["all"].setChecked(True)
        self.banner.setVisible(False)
        self.hint.setVisible(True)
        self.progress.setRange(0, len(pdfs))
        self.progress.setValue(0)
        self.show_progress(True)
        self.scan_btn.setText("Stop")
        for w in (self.ocr_box, self.deep_ocr_box, self.subfolder_box, self.pattern_box, self.apply_btn,
                  self.undo_btn, self.export_btn):
            w.setEnabled(False)

        self.scan_order = {pdf: i for i, pdf in enumerate(pdfs)}
        self.worker = ScanWorker(pdfs, self.ocr_box.isChecked(), self.pattern, self.cache,
                                 self.ocr_box.isChecked() and self.deep_ocr_box.isChecked())
        self.worker.progress.connect(self.on_progress)
        self.worker.planned.connect(self.on_planned)
        self.worker.finished_scan.connect(self.on_scan_done)
        self.worker.start()

    def on_progress(self, done, name):
        self.progress.setValue(done)
        if name:
            self.progress_label.setText(f"Looking up {done + 1} of {self.progress.maximum()}  ·  {name}")
        elif not done:
            self.progress_label.setText("Checking which papers are remembered from earlier scans…")

    def on_planned(self, plan):
        # Cached papers arrive first; keep the table in file order anyway.
        r = Row(plan)
        r.order = self.scan_order.get(plan.pdf, len(self.scan_order))
        i = sum(other.order < r.order for other in self.rows)
        self.rows.insert(i, r)
        self.table.insertRow(i)
        self.update_row(i)
        self.refresh_counts()

    def on_scan_done(self, cancelled):
        self.worker.wait()
        self.worker = None
        self.show_progress(False)
        for w in (self.ocr_box, self.subfolder_box, self.pattern_box):
            w.setEnabled(True)
        self.deep_ocr_box.setEnabled(self.ocr_box.isChecked())
        self.update_scan_button()
        self.update_cache_button()
        self.pattern_changed(self.pattern)  # refresh the preview with real papers

        warnings = []
        if cancelled:
            warnings.append(f"Scan stopped after {len(self.rows)} files.")
        if self.rows and all(r.detail.startswith("network error") for r in self.rows if r.state == "skip") \
                and any(r.state == "skip" for r in self.rows) and not any(r.state in ACTIONABLE for r in self.rows):
            warnings.append("Couldn't reach the online databases. Check your internet connection and scan again.")
        if self.ocr_box.isChecked() and not rp.ocr_available:
            warnings.append("OCR isn't set up on this computer, so scanned PDFs were skipped. "
                            "Install Tesseract to enable it.")
        if not rp.pymupdf:
            warnings.append("PyMuPDF isn't installed, so text is read less reliably (pip install pymupdf).")
        self.update_duplicates()
        self.review_btn.setVisible(any(map(self.reviewable, self.rows)))
        n = len(self.dup_groups)
        if n:
            warnings.append(f"{n} paper{'s have' if n != 1 else ' has'} more than one copy in this folder. "
                            "Click “Review duplicates” to tidy up.")
        self.show_banner(" ".join(warnings), "warn" if len(warnings) > bool(n) else "info")

    # ----- table

    def update_row(self, i):
        r = self.rows[i]
        t = self.table
        t.blockSignals(True)
        chk = QTableWidgetItem()
        if r.state in ACTIONABLE:
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            chk.setCheckState(Qt.CheckState.Checked if r.checked else Qt.CheckState.Unchecked)
        else:
            chk.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        t.setItem(i, COL_CHECK, chk)

        status = QTableWidgetItem()
        status.setData(Qt.ItemDataRole.UserRole, r.state)
        status.setData(DUPLICATE_ROLE, r.dup is not None or bool(r.dup_of))
        if r.dup_of:
            status.setToolTip(f"Same paper as {r.dup_of}")
        status.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        t.setItem(i, COL_STATUS, status)

        def text_item(text, tip=None, editable=False):
            it = QTableWidgetItem(text)
            flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
            if editable:
                flags |= Qt.ItemFlag.ItemIsEditable
            it.setFlags(flags)
            it.setToolTip(tip or text)
            return it

        t.setItem(i, COL_FILE, text_item(self.display_name(r.path), str(r.path)))
        editable = r.state not in ("renamed", "ok")
        new = text_item(r.new_name if r.state != "skip" else "", editable=editable,
                        tip="Double-click to type a name yourself" if editable else None)
        if r.state == "skip":
            new.setData(Qt.ItemDataRole.ForegroundRole, self.palette().color(QPalette.ColorRole.PlaceholderText))
        t.setItem(i, COL_NEW, new)
        t.setItem(i, COL_VIA, text_item(r.method, tip_html([html.escape(r.method_tip)], 380) if r.method_tip else ""))
        doi = text_item(r.doi, f"Open https://doi.org/{r.doi}" if r.doi else "")
        if r.doi:
            font = doi.font()
            font.setUnderline(True)
            doi.setFont(font)
            doi.setForeground(QColor(theme()["accent"]))
        t.setItem(i, COL_DOI, doi)
        if r.state in ("skip", "error"):
            detail, tip = r.detail, r.detail
        else:
            detail, tip = r.plan.title or r.detail, r.details_tip
        t.setItem(i, COL_DETAIL, text_item(detail, tip))
        t.blockSignals(False)
        self.apply_filter()

    def item_changed(self, item):
        i = item.row()
        r = self.rows[i]
        if item.column() == COL_CHECK:
            r.checked = item.checkState() == Qt.CheckState.Checked
        elif item.column() == COL_NEW:
            name = clean_filename(item.text())
            if not name:
                self.update_row(i)
                return
            if name != r.new_name:
                r.new_name, r.checked = name, True
                if r.state in ("skip", "error", "check", "rename"):
                    r.state = "manual"
            self.update_row(i)
        self.refresh_counts()

    def cell_double_clicked(self, i, col):
        if col not in (COL_NEW, COL_CHECK, COL_DOI):
            self.open_pdf(self.rows[i].path)

    def cell_clicked(self, i, col):
        if col == COL_DOI and self.rows[i].doi:
            QDesktopServices.openUrl(QUrl(f"https://doi.org/{self.rows[i].doi}"))

    def cell_entered(self, i, col):
        link = col == COL_DOI and bool(self.rows[i].doi)
        self.table.viewport().setCursor(Qt.CursorShape.PointingHandCursor if link else Qt.CursorShape.ArrowCursor)

    def context_menu(self, pos):
        i = self.table.rowAt(pos.y())
        if i < 0:
            return
        r = self.rows[i]
        menu = QMenu(self)
        menu.addAction("Open PDF", lambda: self.open_pdf(r.path))
        edit = menu.addAction("Edit new name", lambda: self.table.editItem(self.table.item(i, COL_NEW)))
        edit.setEnabled(r.state not in ("renamed", "ok"))
        copy = menu.addAction("Copy DOI", lambda: QApplication.clipboard().setText(r.doi))
        copy.setEnabled(bool(r.doi))
        menu.addAction("Open containing folder", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(r.path.parent))))
        menu.addSeparator()
        ident = menu.addAction("Identify by DOI, ISBN, arXiv ID or bibcode…", lambda: self.identify_by_id(r))
        other = menu.addAction("Choose another match or edition…", lambda: self.review_matches([r]))
        ident.setEnabled(not self.busy())
        other.setEnabled(bool(r.plan.meta) and not self.busy())
        if r.dup is not None:
            menu.addSeparator()
            menu.addAction("Review duplicates…", self.review_duplicates)
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def open_pdf(self, path):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def apply_filter(self):
        key = next((k for k, b in self.filter_btns.items() if b.isChecked()), "all")
        shows = dict((k, f) for k, _, f in FILTERS)[key]
        words = rp.fold(self.search.text()).split()
        for i, r in enumerate(self.rows):
            self.table.setRowHidden(i, not shows(r) or bool(words) and not all(w in r.search_text for w in words))

    def refresh_counts(self):
        for key, label, shows in FILTERS:
            n = sum(map(shows, self.rows))
            b = self.filter_btns[key]
            b.setText(f"{label}  {n}")
            b.setVisible(key == "all" or n > 0 or b.isChecked())
        todo = sum(r.checked and r.state in ACTIONABLE for r in self.rows)
        self.apply_btn.setText(f"Rename {todo} file{'s' if todo != 1 else ''}" if todo else "Rename files")
        self.apply_btn.setEnabled(todo > 0 and not self.busy() and self.pattern_ok)
        self.export_btn.setEnabled(any(r.plan.meta for r in self.rows) and not self.busy())

    def show_banner(self, text, kind):
        self.banner.setText(text)
        self.banner.setProperty("kind", kind)
        self.banner.style().polish(self.banner)
        self.banner.setVisible(bool(text))

    def reassign_names(self):
        """Work out new names again, after the pattern changed or duplicates were removed."""
        taken = {rp.path_key(r.path.parent / r.new_name) for r in self.rows if r.state == "manual"}
        for r in self.rows:
            if r.state not in ("rename", "check", "ok") or not r.plan.meta:
                continue
            stem = rp.make_name(r.plan.meta, self.pattern)
            if rp.already_named(r.path, stem):
                r.state, r.new_name, r.checked = "ok", r.path.name, False
                taken.add(rp.path_key(r.path))
            else:
                target = rp.unique_target(r.path.parent, stem, taken)
                taken.add(rp.path_key(target))
                if r.state == "ok":
                    r.state, r.checked = r.base_state, True
                r.new_name = target.name
        for i in range(len(self.rows)):
            self.update_row(i)
        self.refresh_counts()

    # ----- renaming

    def apply_renames(self):
        jobs = [r for r in self.rows if r.checked and r.state in ACTIONABLE]
        taken = set()
        for folder in {r.path.parent for r in jobs}:
            try:
                taken |= {rp.path_key(p) for p in folder.iterdir()}
            except OSError:
                pass  # the folder is gone; renaming its files will fail and say so
        write_meta = self.meta_box.isChecked() and bool(rp.pymupdf)
        self.settings.setValue("write_meta", self.meta_box.isChecked())
        done = failed = meta_written = 0
        meta_failed = []
        batch = []
        for i, r in enumerate(self.rows):
            if r not in jobs:
                continue
            src, target = r.path, r.path.parent / r.new_name
            if rp.path_key(target) != rp.path_key(src) and rp.path_key(target) in taken:
                target = rp.unique_target(src.parent, Path(r.new_name).stem, taken)
            try:
                src.rename(target)
            except OSError as e:
                r.detail = (f"Couldn't rename: {e.strerror or e}. "
                            "Is the file open in another program?")
                r.state, r.checked = "error", False
                failed += 1
            else:
                taken.discard(rp.path_key(src))
                taken.add(rp.path_key(target))
                batch.append((r, src, r.state, r.new_name))
                r.path, r.new_name, r.state, r.checked = target, target.name, "renamed", False
                done += 1
                if write_meta and r.plan.meta:
                    try:
                        rp.write_metadata(target, r.plan.meta)
                        r.hash = rp.file_hash(target)
                        self.cache.put(r.hash, r.plan.meta, r.plan.detail)  # new contents, same paper
                        meta_written += 1
                    except Exception as e:
                        meta_failed.append(f"{target.name} ({e})")
            self.update_row(i)
        self.cache.save()
        self.update_cache_button()
        if batch:
            self.undo_stack.append(batch)
        self.undo_btn.setEnabled(bool(self.undo_stack))
        self.refresh_counts()
        msg = f"Renamed {done} file{'s' if done != 1 else ''}."
        if meta_written:
            msg += f" Saved title, authors, DOI and tags in {meta_written} of them."
        if failed:
            msg += f" {failed} couldn't be renamed (see the Not found tab)."
        if meta_failed:
            msg += f" Couldn't write details into: {', '.join(meta_failed[:3])}" + ("…" if len(meta_failed) > 3 else "")
        self.show_banner(msg + (" Click Undo to put the old names back." if done else ""),
                         "good" if not (failed or meta_failed) else "warn")

    def undo(self):
        if not self.undo_stack:
            return
        batch = self.undo_stack.pop()
        restored = 0
        for r, src, state, new_name in reversed(batch):
            if src.exists() or r not in self.rows:
                continue  # the old name was taken in the meantime; leave it be
            try:
                r.path.rename(src)
            except OSError:
                continue
            r.path, r.state, r.new_name, r.checked = src, state, new_name, True
            restored += 1
            self.update_row(self.rows.index(r))
        self.undo_btn.setEnabled(bool(self.undo_stack))
        self.refresh_counts()
        self.show_banner(f"Restored the original names of {restored} file{'s' if restored != 1 else ''}.", "good")

    # ----- citations

    def export(self, fmt):
        if self.busy():
            return
        ext, label = (".bib", "BibTeX files (*.bib)") if fmt == "bibtex" else (".ris", "RIS files (*.ris)")
        start = str(Path(self.folder, "library" + ext))
        out, _ = QFileDialog.getSaveFileName(self, "Export citations", start, label)
        if not out:
            return
        entries = []
        for r in self.rows:
            if not r.plan.meta:
                continue
            # The citation key matches the file name (the planned one if it hasn't been renamed yet).
            key = Path(r.new_name).stem if r.state in ACTIONABLE and r.checked else r.path.stem
            entries.append((r.plan.meta, key, r.path.resolve()))
        self.pending_renames = any(r.checked and r.state in ACTIONABLE for r in self.rows)
        self.progress.setRange(0, len(entries))
        self.progress.setValue(0)
        self.show_progress(True)
        self.exporter = ExportWorker(entries, fmt, out, self.cache)
        self.exporter.progress.connect(self.on_export_progress)
        self.exporter.finished_export.connect(lambda w, f, err: self.on_export_done(w, f, err, out))
        self.exporter.start()
        self.refresh_counts()
        self.update_scan_button()

    def on_export_progress(self, n, total, key):
        self.progress.setValue(n)
        self.progress_label.setText(f"Fetching citation {n + 1} of {total}  ·  {key}")

    def on_export_done(self, written, failed, error, out):
        self.exporter.wait()
        self.exporter = None
        self.show_progress(False)
        self.refresh_counts()
        self.update_scan_button()
        if error:
            self.show_banner(f"Couldn't save {Path(out).name}: {error}", "warn")
            return
        msg = f"Exported {written} citation{'s' if written != 1 else ''} to {Path(out).name}."
        if failed:
            msg += (f" doi.org had no citation for {len(failed)}, so they were built from the looked-up details: "
                    f"{', '.join(failed[:3])}" + ("…" if len(failed) > 3 else "."))
        if self.pending_renames:
            msg += " Tip: export again after renaming so the file links point to the new names."
        self.show_banner(msg, "good")

    # ----- your own identifications

    def reviewable(self, r):
        """Rows worth offering alternatives for: fuzzy matches and books."""
        return bool(r.plan.meta) and r.state != "renamed" and (r.state == "check" or r.plan.meta.kind == "book")

    def run_task(self, job, label, then):
        """Run job(worker) in the background; then(result) afterwards, or show its error."""
        if self.busy():
            return
        self.progress.setRange(0, 0)  # busy indicator until the job reports progress
        self.progress_label.setText(label)
        self.show_progress(True)
        self.task = TaskWorker(job)
        self.task.progress.connect(self.on_task_progress)

        def done(result, error):
            self.task.wait()
            self.task = None
            self.show_progress(False)
            self.refresh_counts()
            self.update_scan_button()
            if error:
                self.show_banner(error, "warn")
            else:
                then(result)
        self.task.finished_task.connect(done)
        self.task.start()
        self.refresh_counts()
        self.update_scan_button()

    def on_task_progress(self, n, total, what):
        self.progress.setRange(0, total)
        self.progress.setValue(n)
        self.progress_label.setText(f"Looking for other editions and matches: {n + 1} of {total}  ·  {what}")

    def identify_by_id(self, r):
        text, ok = QInputDialog.getText(self, "Identify by ID", "DOI, ISBN, arXiv ID or ADS bibcode "
                                        f"for {self.display_name(r.path)}\n(doi.org and arxiv.org links work too):")
        if not ok or not text.strip():
            return

        def then(result):
            info, how, warning = result
            self.set_identification(r, info, how)
            self.show_banner(f"{self.display_name(r.path)} will be renamed to {r.new_name}. " + warning,
                             "warn" if warning else "good")
        self.run_task(lambda worker: rp.identify_by_id(text, r.path), f"Looking up {text.strip()}…", then)

    def review_matches(self, rows):
        def job(worker):
            found = []
            for n, r in enumerate(rows):
                worker.progress.emit(n, len(rows), self.display_name(r.path))
                options = rp.alternatives(r.plan.meta, r.plan.detail, r.path)
                if options:
                    found.append((r, options))
            return found

        def then(found):
            if not found:
                self.show_banner("No other editions or matches were found." if len(rows) > 1 else
                                 "No other editions or matches were found for this file.", "info")
                return
            dialog = ReviewDialog(self, found, self.display_name)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self.use_choices(dialog.changes())
        self.run_task(job, "Looking for other editions and matches…", then)

    def use_choices(self, changes):
        def job(worker):
            return [(r, rp.finish_choice(meta, r.path), how) for r, (meta, how, _) in changes]

        def then(results):
            for r, info, how in results:
                self.set_identification(r, info, f"chosen {how}", refresh=False)
            self.reassign_names()
            self.update_duplicates()
            self.show_banner(f"Updated {len(results)} file{'s' if len(results) != 1 else ''} with the records "
                             "you picked.", "good")
        self.run_task(job, "Getting the details of the records you picked…", then)

    def set_identification(self, r, info, how, refresh=True):
        """Use a record you entered or picked for this row (and remember it)."""
        r.plan.meta, r.plan.detail, r.plan.status, r.plan.cached = info, how, "rename", False
        r.detail, r.doi, r.base_state = how, info.doi, "rename"
        r.state, r.checked = "rename", True
        if r.hash:
            self.cache.put(r.hash, info, how)
            self.cache.save()
        self.update_cache_button()
        if refresh:
            self.reassign_names()
            self.update_duplicates()

    # ----- watching a folder

    def open_watch_dialog(self):
        if not self.settings.value("watch_dir", "") and (Path.home() / "Downloads").is_dir():
            self.settings.setValue("watch_dir", str(Path.home() / "Downloads"))
        self.watch_dialog = WatchDialog(self)
        self.watch_dialog.exec()
        self.watch_dialog = None

    def watch_log(self):
        return self.watcher.log if self.watcher else rp.load_watch_log()

    def status_colors(self, status):
        """(background, text) colours of a status pill in the current theme."""
        return pill_colors(status)

    def start_watching(self, quiet=False):
        watch_dir, output_dir = self.settings.value("watch_dir", ""), self.settings.value("output_dir", "")
        if not watch_dir or not output_dir or not Path(watch_dir).is_dir():
            if not quiet:
                self.show_banner("Choose an existing folder to watch and an output folder first.", "warn")
            return
        try:
            self.watcher = rp.Watcher(watch_dir, output_dir, self.pattern, self.cache,
                                      use_ocr=self.ocr_box.isChecked(),
                                      deep_ocr=self.ocr_box.isChecked() and self.deep_ocr_box.isChecked(),
                                      file_fuzzy=self.settings.value("watch_fuzzy", False, type=bool),
                                      write_meta=self.meta_box.isChecked() and bool(rp.pymupdf))
        except ValueError as e:
            self.watcher = None
            self.show_banner(str(e), "warn")
            return
        self.watch_thread = WatchWorker(self.watcher)
        self.watch_thread.event.connect(self.on_watch_event)
        self.watch_thread.start()
        self.update_watch_button()

    def stop_watching(self):
        if self.watch_thread:
            self.watch_thread.cancelled = True
            self.watch_thread.wait(15000)
            self.watch_thread = None
        if self.watcher:
            self.watcher.save()
            self.watcher = None
        self.watch_btn.setText("Watch folder…")

    def update_watch_button(self):
        if not self.watch_thread:
            self.watch_btn.setText("Watch folder…")
            return
        waiting = sum(waiting_for_you(e, self.watcher.watch_dir) for e in self.watcher.log)
        self.watch_btn.setText(f"● Watching {self.watcher.watch_dir.name}"
                               + (f"  ·  {waiting} to decide" if waiting else ""))

    def decision_watcher(self):
        """The running watcher, or one for the saved folders, to act on the activity list."""
        if self.watcher:
            return self.watcher
        return rp.Watcher(self.settings.value("watch_dir", ""), self.settings.value("output_dir", ""),
                          self.pattern, self.cache, use_ocr=self.ocr_box.isChecked(),
                          write_meta=self.meta_box.isChecked() and bool(rp.pymupdf))

    def watch_decide(self, job, dialog, label="Filing…"):
        """Run job(watcher) in the background, then show the entry's new state in the dialog."""
        if self.busy():
            dialog.status.setText("Wait until the current scan or lookup has finished.")
            return
        try:
            watcher = self.decision_watcher()
        except ValueError as e:
            dialog.status.setText(str(e))
            return

        def then(entry):  # (a stand-in watcher has saved the activity list to watch.json)
            dialog.refresh()
            dialog.status.setText(self.describe_decision(entry))
            self.update_cache_button()
        self.run_task(lambda worker: job(watcher), label, then)
        self.task.finished_task.connect(lambda _r, error: error and dialog.status.setText(error))

    def describe_decision(self, entry):
        name = Path(entry.get("target", "")).name
        if entry["result"] == "filed":
            return f"Filed {entry['source']} as {name}."
        if entry["result"] == "duplicate":
            return f"{entry['source']} is a duplicate of {name}; it wasn't filed."
        return f"{entry['source']}: {WATCH_RESULTS.get(entry['result'], (entry['result'],))[0]}."

    def watch_identify(self, entry_id, pdf, dialog):
        text, ok = QInputDialog.getText(dialog, "Identify by ID", f"DOI, ISBN, arXiv ID or ADS bibcode for {pdf.name}:")
        if ok and text.strip():
            def job(watcher):
                info, how, _warning = rp.identify_by_id(text, pdf)
                return watcher.resolve(entry_id, info, how)
            self.watch_decide(job, dialog, f"Looking up {text.strip()}…")

    def watch_choose(self, entry_id, pdf, dialog):
        """Pick another match for a fuzzy one, as in the review dialog, and file the PDF with it."""
        def job(worker):
            hit = self.cache.get(rp.file_hash(pdf))
            if not hit:
                raise ValueError(f"{pdf.name} hasn't been identified yet.")
            info, how = hit
            stem = rp.make_name(info, self.pattern)
            row = Row(rp.Plan(pdf, "rename", how, stem, pdf.with_name(stem + ".pdf"), info))
            return row, rp.alternatives(info, how, pdf)

        def then(result):
            row, options = result
            if not options:
                dialog.status.setText(f"No other matches were found for {pdf.name}.")
                return
            review = ReviewDialog(dialog, [(row, options)], lambda p: p.name)
            if review.exec() == QDialog.DialogCode.Accepted and review.changes():
                _row, (meta, how, _note) = review.changes()[0]
                self.watch_decide(lambda w: w.resolve(entry_id, rp.finish_choice(meta, pdf), f"chosen {how}"), dialog)
        self.run_task(job, "Looking for other matches…", then)

    def watch_dismiss(self, entry_ids, dialog, undo=False):
        """Stop (or start again) asking about PDFs that stay in the watched folder as they are."""
        def job(worker):  # in the background: a running poll may hold the watcher's lock
            watcher.dismiss(entry_ids, undo)
            return len(entry_ids)

        def then(n):
            dialog.refresh()
            files = f"{n} PDF{'s' if n != 1 else ''}"
            dialog.status.setText(f"You'll be asked about {files} again." if undo else
                                  f"Dismissed {files}: left as they are, and not counted as waiting any more.")
        if self.busy():
            dialog.status.setText("Wait until the current scan or lookup has finished.")
            return
        try:
            watcher = self.decision_watcher()
        except ValueError as e:
            dialog.status.setText(str(e))
            return
        self.run_task(job, "Updating the activity list…", then)

    def watch_trash(self, entry_id, pdf, duplicate_of, dialog):
        if self.busy():
            dialog.status.setText("Wait until the current scan or lookup has finished.")
            return
        if not QFile.moveToTrash(str(pdf)):
            dialog.status.setText(f"Couldn't move {pdf.name} to the {TRASH}. Is it open in another program?")
            return
        def then(_result):
            dialog.refresh()
            dialog.status.setText(f"Moved {pdf.name} to the {TRASH}.")
        try:
            watcher = self.decision_watcher()
        except ValueError as e:
            dialog.status.setText(str(e))
            return
        # In the background: a running poll may hold the watcher's lock for a while (OCR).
        self.run_task(lambda worker: watcher.mark(entry_id, "trashed", f"duplicate of {Path(duplicate_of).name}"),
                      "Updating the activity list…", then)

    def on_watch_event(self, e):
        kind = {"filed": "good", "duplicate": "warn", "error": "warn"}.get(e.result, "info")
        self.show_banner(rp.describe_event(e) + (f" in {Path(e.target).parent.name}." if e.result == "filed" else "."),
                         kind)
        self.update_cache_button()
        if e.result in ("filed", "duplicate") and self.watcher:
            # If the watched folder is the one shown, update the file's row.
            source = rp.path_key(self.watcher.watch_dir / e.source)
            for i, r in enumerate(self.rows):
                if rp.path_key(r.path) != source:
                    continue
                if e.result == "filed":
                    r.path, r.new_name, r.state, r.checked = Path(e.target), Path(e.target).name, "renamed", False
                else:
                    r.dup_of, r.checked = e.target, False
                self.update_row(i)
            self.refresh_counts()
        if self.watch_dialog:
            self.watch_dialog.refresh()
        self.update_watch_button()

    # ----- duplicates

    def update_duplicates(self):
        groups = rp.duplicate_groups([rp.duplicate_keys(r.plan.meta, r.hash) for r in self.rows])
        for r in self.rows:
            r.dup = None
        for gid, group in enumerate(groups):
            for i in group:
                self.rows[i].dup = gid
        self.dup_groups = [[self.rows[i] for i in group] for group in groups]
        for i in range(len(self.rows)):
            self.update_row(i)
        n = len(groups)
        self.dup_btn.setText(f"Review {n} duplicate{'s' if n != 1 else ''}…")
        self.dup_btn.setVisible(n > 0)
        self.refresh_counts()

    def review_duplicates(self):
        if not self.dup_groups or self.busy():
            return
        dialog = DuplicatesDialog(self, self.dup_groups, self.display_name)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.trash_rows(dialog.to_trash())

    def trash_rows(self, rows):
        moved = failed = 0
        for r in rows:
            if QFile.moveToTrash(str(r.path)):
                i = self.rows.index(r)
                self.rows.pop(i)
                self.table.removeRow(i)
                moved += 1
            else:
                r.state, r.checked = "error", False
                r.detail = f"Couldn't move this to the {TRASH}. Is the file open in another program?"
                failed += 1
        # Undo can't bring trashed files back, so forget about them there.
        self.undo_stack = [b for b in ([e for e in batch if e[0] in self.rows] for batch in self.undo_stack) if b]
        self.undo_btn.setEnabled(bool(self.undo_stack))
        self.reassign_names()
        self.update_duplicates()
        msg = f"Moved {moved} duplicate file{'s' if moved != 1 else ''} to the {TRASH}; you can restore them from there."
        if failed:
            msg += f" {failed} couldn't be moved (see the Not found tab)."
        self.show_banner(msg, "warn" if failed else "good")

    def closeEvent(self, e):
        self.stop_watching()
        for worker in (self.worker, self.exporter, self.task):
            if worker:
                worker.cancelled = True
                worker.wait(15000)
        self.cache.save()
        super().closeEvent(e)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Paper Renamer")
    apply_theme(app, QSettings("PaperRenamer", "PaperRenamer").value("theme", "system"))
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
