#!/usr/bin/env python3
"""Paper Renamer: a small window around rename_papers.py.

    pip install pyside6 pymupdf requests
    python paper_renamer_gui.py

Keep this file next to rename_papers.py.
"""

import re
import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import QFile, QSettings, QSize, Qt, QThread, QUrl, Signal
from PySide6.QtGui import (
    QColor, QDesktopServices, QFont, QFontMetrics, QIcon, QPainter, QPalette, QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QFileDialog, QFrame,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMenu, QProgressBar, QPushButton,
    QRadioButton, QScrollArea, QSizePolicy, QStackedWidget, QStyle, QStyledItemDelegate, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

import rename_papers as rp

# ---------------------------------------------------------------- theme

THEMES = {
    "light": dict(
        bg="#f5f6f8", surface="#ffffff", border="#e2e5ea", text="#1d2430", muted="#6b7280",
        accent="#3b5bdb", accent_hover="#364fc7", accent_text="#ffffff", selection="#e8edff",
        alt="#fafbfc", field="#f5f6f8",
    ),
    "dark": dict(
        bg="#15171c", surface="#1e2128", border="#2f333c", text="#e6e8eb", muted="#9aa1ad",
        accent="#5c7cfa", accent_hover="#748ffc", accent_text="#ffffff", selection="#28304a",
        alt="#22252d", field="#191b21",
    ),
}

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
    ("dups", "Duplicates", lambda r: r.dup is not None),
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


def apply_theme(app, dark):
    t = THEMES["dark" if dark else "light"]
    app.setStyle("Fusion")
    pal = QPalette()
    for role, key in [
        (QPalette.ColorRole.Window, "bg"), (QPalette.ColorRole.Base, "surface"),
        (QPalette.ColorRole.AlternateBase, "alt"), (QPalette.ColorRole.Text, "text"),
        (QPalette.ColorRole.WindowText, "text"), (QPalette.ColorRole.Button, "surface"),
        (QPalette.ColorRole.ButtonText, "text"), (QPalette.ColorRole.Highlight, "accent"),
        (QPalette.ColorRole.HighlightedText, "accent_text"), (QPalette.ColorRole.ToolTipBase, "surface"),
        (QPalette.ColorRole.ToolTipText, "text"), (QPalette.ColorRole.PlaceholderText, "muted"),
    ]:
        pal.setColor(role, QColor(t[key]))
    app.setPalette(pal)
    warn_fg = STATUSES["skip"][2 if dark else 1][1]
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
        #banner[kind="warn"] {{ background: {STATUSES['check'][2 if dark else 1][0]};
                                color: {STATUSES['check'][2 if dark else 1][1]}; }}
        #banner[kind="good"] {{ background: {STATUSES['renamed'][2 if dark else 1][0]};
                                color: {STATUSES['renamed'][2 if dark else 1][1]}; }}
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
            image: url("{svg_file(f"radio_{t['accent'][1:]}", RADIO_SVG.format(t['accent']))}");
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
            background: {t['accent']}; border-color: {t['accent']}; image: url("{svg_file("check", CHECK_SVG)}");
        }}
    """)
    return dark


CHECK_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
             '<path d="M3.5 8.5l3 3 6-7" fill="none" stroke="white" stroke-width="2.2" '
             'stroke-linecap="round" stroke-linejoin="round"/></svg>')
CHEVRON_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
               '<path d="M1.5 3.5l3.5 3.5 3.5-3.5" fill="none" stroke="{}" stroke-width="1.6" '
               'stroke-linecap="round" stroke-linejoin="round"/></svg>')
RADIO_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
             '<circle cx="8" cy="8" r="8" fill="{}"/><circle cx="8" cy="8" r="3" fill="white"/></svg>')


def svg_file(name, svg):
    """Stylesheets can only load images from files, so write tiny ones."""
    path = Path(tempfile.gettempdir(), f"paper_renamer_{name}.svg")
    if not path.exists():
        path.write_text(svg)
    return path.as_posix()


def app_icon(color):
    """A little document-with-lines icon, drawn so no image files need shipping."""
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

    def __init__(self, parent, dark):
        super().__init__(parent)
        self.dark = dark

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
        x += self.pill(painter, option.rect, x, font, *STATUSES[key]) + 6
        if index.data(DUPLICATE_ROLE):
            self.pill(painter, option.rect, x, font, *DUPLICATE_PILL)
        painter.restore()

    def pill(self, painter, rect, x, font, label, light, dark):
        bg, fg = dark if self.dark else light
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

    def __init__(self, pdfs, use_ocr, pattern, cache):
        super().__init__()
        self.pdfs, self.use_ocr, self.pattern, self.cache = pdfs, use_ocr, pattern, cache
        self.cancelled = False

    def run(self):
        done = 0
        self.progress.emit(0, self.pdfs[0].name if self.pdfs else "")
        for plan in rp.plan_renames(self.pdfs, self.use_ocr, self.pattern, self.cache):
            done += 1
            self.planned.emit(plan)
            if self.cancelled:
                break
            if done < len(self.pdfs):
                self.progress.emit(done, self.pdfs[done].name)
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
        except OSError as e:
            self.finished_export.emit(0, [], str(e))


class Row:
    def __init__(self, plan):
        self.plan = plan
        self.path = plan.pdf
        self.detail = plan.detail
        self.doi = plan.doi
        self.hash = plan.hash
        self.dup = None  # index of its duplicate group, if any
        # Title/text searches are fuzzy matches: flag them for a second look.
        self.base_state = "check" if plan.detail.startswith(("title search", "text search")) else "rename"
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
        base = ("DOI" if d.startswith("doi") else "arXiv" if d.startswith("arXiv")
                else "Title search" if d.startswith("title") else "Text search")
        return base + (" + OCR" if "(OCR)" in d else "")


def clean_filename(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip().strip(".")
    if name and not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name


# ---------------------------------------------------------------- duplicates

def pdf_facts(path):
    """What helps decide which copy to keep: annotations, a text layer, pages, size."""
    facts = {"size": path.stat().st_size, "pages": None, "annots": 0, "text": None}
    try:
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
        sub = QLabel(f"These files are the same paper (same DOI) or identical copies. Choose the copy "
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
        cl.addWidget(QLabel(("Identical files" if same_bytes else "Same DOI, different files")
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


# ---------------------------------------------------------------- main window

COL_CHECK, COL_STATUS, COL_FILE, COL_NEW, COL_VIA, COL_DOI, COL_DETAIL = range(7)


class MainWindow(QMainWindow):
    def __init__(self, dark, cache=None):
        super().__init__()
        self.dark = dark
        self.settings = QSettings("PaperRenamer", "PaperRenamer")
        self.cache = cache or rp.Cache()
        self.rows, self.undo_stack, self.dup_groups = [], [], []
        self.worker = self.exporter = None
        self.folder = None
        self.pattern = self.settings.value("pattern", rp.DEFAULT_PATTERN)
        try:
            rp.check_pattern(self.pattern)
        except ValueError:
            self.pattern = rp.DEFAULT_PATTERN

        self.setWindowTitle("Paper Renamer")
        self.setWindowIcon(app_icon(THEMES["dark" if dark else "light"]["accent"]))
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
        self.meta_box = QCheckBox("Write title, authors && DOI into the PDFs")
        self.meta_box.setChecked(self.settings.value("write_meta", False, type=bool) and bool(rp.pymupdf))
        self.meta_box.setEnabled(bool(rp.pymupdf))
        self.meta_box.setToolTip(
            "When renaming, also store the paper's details in the PDF itself, so reference managers and "
            "file browsers show them\nand later scans are instant. Undo restores names, not these details."
            if rp.pymupdf else "Needs PyMuPDF (pip install pymupdf).")
        self.scan_btn = QPushButton("Scan folder", objectName="primary", enabled=False)
        self.scan_btn.setMinimumWidth(150)
        self.scan_btn.clicked.connect(self.scan_or_stop)
        for box in (self.subfolder_box, self.ocr_box, self.meta_box):
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
        self.table.setItemDelegateForColumn(COL_STATUS, StatusDelegate(self.table, dark))
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
        self.pattern_changed(self.pattern)
        last = self.settings.value("folder", "")
        if last and Path(last).is_dir():
            self.set_folder(last)

    # ----- helpers

    def display_name(self, path):
        """File name, with its subfolder when subfolders are included."""
        try:
            return Path(path).relative_to(self.folder).as_posix()
        except (ValueError, TypeError):
            return Path(path).name

    def busy(self):
        return self.worker is not None or self.exporter is not None

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
        rp.lookup_doi.cache_clear()
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
        pdfs = rp.list_pdfs(self.folder, self.subfolder_box.isChecked())
        self.settings.setValue("ocr", self.ocr_box.isChecked())
        self.rows, self.undo_stack, self.dup_groups = [], [], []
        self.dup_btn.setVisible(False)
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
        for w in (self.ocr_box, self.subfolder_box, self.pattern_box, self.apply_btn, self.undo_btn,
                  self.export_btn):
            w.setEnabled(False)

        self.worker = ScanWorker(pdfs, self.ocr_box.isChecked(), self.pattern, self.cache)
        self.worker.progress.connect(self.on_progress)
        self.worker.planned.connect(self.on_planned)
        self.worker.finished_scan.connect(self.on_scan_done)
        self.worker.start()

    def on_progress(self, done, name):
        self.progress.setValue(done)
        self.progress_label.setText(f"Reading {done + 1} of {self.progress.maximum()}  ·  {name}")

    def on_planned(self, plan):
        self.rows.append(Row(plan))
        self.table.insertRow(self.table.rowCount())
        self.update_row(len(self.rows) - 1)
        self.refresh_counts()

    def on_scan_done(self, cancelled):
        self.worker.wait()
        self.worker = None
        self.show_progress(False)
        for w in (self.ocr_box, self.subfolder_box, self.pattern_box):
            w.setEnabled(True)
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
        status.setData(DUPLICATE_ROLE, r.dup is not None)
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
        t.setItem(i, COL_VIA, text_item(r.method, r.method + (" (remembered from an earlier scan)"
                                                              if r.plan.cached else "")))
        doi = text_item(r.doi, f"Open https://doi.org/{r.doi}" if r.doi else "")
        if r.doi:
            font = doi.font()
            font.setUnderline(True)
            doi.setFont(font)
            doi.setForeground(QColor(THEMES["dark" if self.dark else "light"]["accent"]))
        t.setItem(i, COL_DOI, doi)
        if r.state in ("skip", "error"):
            detail, tip = r.detail, r.detail
        else:
            meta = r.plan.meta
            authors = ", ".join(a[1] for a in meta.authors[:3]) + (" et al." if len(meta.authors) > 3 else "")
            detail = r.plan.title or r.detail
            tip = (f"{r.plan.title}\n{authors} ({meta.year})" + (f", {meta.journal}" if meta.journal else "")
                   + f"\n\nIdentified via {r.detail}" + (" (cached)" if r.plan.cached else ""))
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
        if r.dup is not None:
            menu.addSeparator()
            menu.addAction("Review duplicates…", self.review_duplicates)
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def open_pdf(self, path):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def apply_filter(self):
        key = next((k for k, b in self.filter_btns.items() if b.isChecked()), "all")
        shows = dict((k, f) for k, _, f in FILTERS)[key]
        for i, r in enumerate(self.rows):
            self.table.setRowHidden(i, not shows(r))

    def refresh_counts(self):
        for key, label, shows in FILTERS:
            n = sum(map(shows, self.rows))
            b = self.filter_btns[key]
            b.setText(f"{label}  {n}")
            b.setVisible(key == "all" or n > 0 or b.isChecked())
        todo = sum(r.checked and r.state in ACTIONABLE for r in self.rows)
        self.apply_btn.setText(f"Rename {todo} file{'s' if todo != 1 else ''}" if todo else "Rename files")
        self.apply_btn.setEnabled(todo > 0 and not self.busy() and self.pattern_ok)
        self.export_btn.setEnabled(any(r.doi for r in self.rows) and not self.busy())

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
        taken = {rp.path_key(p) for folder in {r.path.parent for r in jobs} for p in folder.iterdir()}
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
            msg += f" Saved title, authors and DOI in {meta_written} of them."
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
            if not r.doi:
                continue
            # The citation key matches the file name (the planned one if it hasn't been renamed yet).
            key = Path(r.new_name).stem if r.state in ACTIONABLE and r.checked else r.path.stem
            entries.append((r.doi, key, r.path.resolve()))
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
            msg += f" {len(failed)} couldn't be fetched: {', '.join(failed[:3])}" + ("…" if len(failed) > 3 else ".")
        if self.pending_renames:
            msg += " Tip: export again after renaming so the file links point to the new names."
        self.show_banner(msg, "warn" if failed else "good")

    # ----- duplicates

    def update_duplicates(self):
        groups = rp.duplicate_groups([(r.doi, r.hash) for r in self.rows])
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
        for worker in (self.worker, self.exporter):
            if worker:
                worker.cancelled = True
                worker.wait(15000)
        self.cache.save()
        super().closeEvent(e)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Paper Renamer")
    dark = apply_theme(app, is_dark(app))
    win = MainWindow(dark)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
