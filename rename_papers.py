#!/usr/bin/env python3
"""Rename academic paper PDFs to e.g. <FirstAuthorSurname><Year>.pdf.

Strategy, per file:
  1. Look for DOIs in the file name (publisher download names like
     978-3-030-13876-9.pdf), the PDF metadata or the text of the first pages,
     then look them up (Crossref first, then doi.org for DataCite/other DOIs).
  2. Otherwise look for an arXiv ID and ask the arXiv API.
  3. Otherwise, if the PDF metadata has a title, search Crossref by title
     and accept the top hit only if the titles closely match.
  4. Otherwise search Crossref with the start of page 1 (title, authors,
     journal reference) and accept a hit only if its title and first author
     both appear in the PDF.
If none of that works, OCR the first pages (for scans without a usable text
layer) and try again. Results are cached by file content, so re-scanning a
folder only looks up new files.

Dry run by default; pass --apply to actually rename.

    pip install pymupdf requests      # (pypdf also works, but extracts text less reliably)
    python rename_papers.py ~/papers                          # preview
    python rename_papers.py ~/papers --apply                  # rename
    python rename_papers.py ~/papers -r --pattern "{author}_{year}_{title}"
    python rename_papers.py ~/papers --apply --write-metadata --bib library.bib

Naming pattern placeholders:
    {author}   first author's surname          Fermi
    {authors}  one, two, or first + EtAl       Fermi, FermiDirac, FermiEtAl
    {year}     year of publication             1926
    {title}    first three main title words    QuantelungIdealenEinatomigen
    {journal}  journal abbreviation            ZPhysik

OCR needs PyMuPDF plus the Tesseract program:
    macOS: brew install tesseract      Debian/Ubuntu: sudo apt install tesseract-ocr
    Windows: https://github.com/UB-Mannheim/tesseract/wiki (the default install
             location is found automatically; otherwise set TESSDATA_PREFIX to
             its tessdata folder)
"""

import argparse
import difflib
import functools
import hashlib
import json
import logging
import os
import re
import sys
import threading
import time
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path

import requests

if sys.platform == "win32" and "TESSDATA_PREFIX" not in os.environ:
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("LOCALAPPDATA", "")):
        tessdata = Path(base, "Tesseract-OCR", "tessdata")
        if base and tessdata.is_dir():
            os.environ["TESSDATA_PREFIX"] = str(tessdata)
            break

# PyMuPDF auto-loads its optional AI layout analyser (pymupdf_layout) and pymupdf4llm when
# installed. We use neither, they slow down startup, and their model files break PyInstaller
# builds, so block them; PyMuPDF handles their absence fine.
sys.modules.setdefault("pymupdf.layout", None)
sys.modules.setdefault("pymupdf4llm", None)
try:
    import pymupdf
except ImportError:
    pymupdf = None
    from pypdf import PdfReader
    logging.getLogger("pypdf").setLevel(logging.ERROR)  # silence font-encoding warnings

PAGES_TO_SCAN = 5
OCR_PAGES = 2  # OCR is slow (a few seconds per page); the title page is usually enough
OCR_DPI = 300
MAX_DOI_CANDIDATES = 5
DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>]+)", re.I)
# Elsevier's Publisher Item Identifier, e.g. "PII: 0375-9474(95)00035-Y" or "PII: S0370269398012345".
PII_RE = re.compile(r"PII:?\s*(S?\d{4}-\d{3}[\dX]\s*\(\d{2}\)\s*\d{5}-[\dA-Z]|S\d{7}[\dX]\d{7}[\dA-Z])", re.I)
ARXIV_RE = re.compile(r"arXiv[:.]\s*(\d{4}\.\d{4,5}|[a-z\-]+(?:\.[A-Z]{2})?/\d{7})", re.I)
# Put your email here: Crossref routes "polite" requests to faster servers.
HEADERS = {"User-Agent": "rename_papers/1.2 (mailto:you@example.com)"}

session = requests.Session()
session.headers.update(HEADERS)


@dataclass
class Meta:
    """What we know about a paper once it's identified."""
    authors: list = field(default_factory=list)  # [given, family] pairs, first author first
    year: int = None
    title: str = ""
    doi: str = ""
    journal: str = ""

    @property
    def surname(self):
        return self.authors[0][1] if self.authors else ""


# ---------------------------------------------------------------- reading PDFs

def read_pdf(path):
    """Return (metadata dict, text of the first few pages)."""
    if pymupdf:
        with pymupdf.open(path) as doc:
            meta = {k.lower(): v or "" for k, v in (doc.metadata or {}).items()}
            text = "\n".join(doc[i].get_text() for i in range(min(PAGES_TO_SCAN, doc.page_count)))
    else:
        reader = PdfReader(path)
        meta = {k.lstrip("/").lower(): str(v) for k, v in (reader.metadata or {}).items()}
        text = ""
        for page in reader.pages[:PAGES_TO_SCAN]:
            try:
                text += (page.extract_text() or "") + "\n"
            except Exception:
                pass
    # NFKC turns ligatures like "ﬁ" into "fi" and odd slashes/dashes into plain ones.
    return meta, unicodedata.normalize("NFKC", text)


def ocr_pdf(path):
    """Text of the first pages via Tesseract OCR. Raises RuntimeError if OCR is unavailable."""
    if not pymupdf:
        raise RuntimeError("OCR needs PyMuPDF (pip install pymupdf)")
    with pymupdf.open(path) as doc:
        text = "\n".join(
            page.get_text(textpage=page.get_textpage_ocr(dpi=OCR_DPI, full=True))
            for page in doc.pages(0, min(OCR_PAGES, doc.page_count))
        )
    return unicodedata.normalize("NFKC", text)


# ---------------------------------------------------------------- finding DOIs

# Publisher download names that encode a DOI: (pattern, DOI prefixes to try).
FILENAME_PATTERNS = [
    # Springer books and chapters are named after the ISBN: 978-3-030-13876-9(_5).pdf
    (re.compile(r"(?<!\d)97[89]-\d{1,5}-\d{1,7}-\d{1,7}-[\dX](?:_\d+)?(?![\d-])", re.I), ["10.1007"]),
    # Springer, Nature and BMC articles: s00220-019-03456-x.pdf
    (re.compile(r"(?<![A-Za-z0-9])s\d{5}-\d{3}-\d{4,5}-[\dX](?![A-Za-z0-9])", re.I), ["10.1007", "10.1038", "10.1186"]),
    # APS: PhysRevLett.116.061102.pdf
    (re.compile(r"PhysRev[A-Za-z]*\.\d+\.\d+"), ["10.1103"]),
]
FULL_DOI_NAME_RE = re.compile(r"(10\.\d{4,9})[_/](\S+)")  # 10.1007_s00220-019-03456-x.pdf
PII_NAME_RE = re.compile(r"(?<![A-Za-z0-9])(S\d{7}[\dX]\d{7}[\dA-Z])(?![A-Za-z0-9])", re.I)  # 1-s2.0-<PII>-main.pdf


def filename_dois(stem):
    """DOIs guessed from the file's own name."""
    m = FULL_DOI_NAME_RE.search(stem)
    if m:
        return [f"{m.group(1)}/{m.group(2)}"]
    m = PII_NAME_RE.search(stem)
    if m:
        return [pii_to_doi(m.group(1))]
    for pattern, prefixes in FILENAME_PATTERNS:
        m = pattern.search(stem)
        if m:
            return [f"{prefix}/{m.group(0)}" for prefix in prefixes]
    return []


def find_dois(meta, text, from_filename=()):
    """All plausible DOIs, most likely first."""
    # DOIs often get broken across lines after a "." "/" or "-"; also try with those rejoined.
    rejoined = re.sub(r"(?<=[./\-_])[ \t]*\n\s*", "", text)
    # OCR likes to insert spaces where a DOI switches from letters to digits ("nature 14539").
    despaced = re.sub(r"(10\.\d{4,9}/\S*[A-Za-z.\-/]) (?=\d)", r"\1", rejoined)
    sources = [meta.get(k, "") for k in ("doi", "subject", "keywords", "title")] + [text, rejoined]

    def collect(sources, found):
        for source in sources:
            # Trailing punctuation is almost never part of the DOI.
            dois = [m.group(1).rstrip(".,;:)]}'") for m in DOI_RE.finditer(source)]
            dois += [pii_to_doi(m.group(1)) for m in PII_RE.finditer(source)]
            for doi in dois:
                if doi.lower() not in (d.lower() for d in found):
                    found.append(doi)
        return found

    found = collect(sources, [])
    # Drop line-broken fragments like "10.1016/j" when the rejoined full DOI was also found.
    found = [d for d in found if not any(o != d and o.startswith(d) for o in found)]
    # De-spaced versions go last: they can also wrongly glue on a following number.
    found = collect([despaced], found)
    # Filename guesses go first (they're specific) but skip the prefix filter above:
    # a book DOI is a prefix of its chapters' DOIs, which may appear in its table of contents.
    named = list(from_filename)
    return named + [d for d in found[:MAX_DOI_CANDIDATES] if d.lower() not in (n.lower() for n in named)]


def pii_to_doi(pii):
    """Elsevier DOIs are 10.1016/<PII>; the compact form S0370269398012345
    is written out as S0370-2693(98)01234-5."""
    pii = re.sub(r"\s", "", pii).upper()
    if "-" not in pii:
        pii = f"S{pii[1:5]}-{pii[5:9]}({pii[9:11]}){pii[11:16]}-{pii[16]}"
    return f"10.1016/{pii}"


def doi_variants(doi):
    """The DOI as found, then with a word that was glued onto its end removed
    (text extraction often turns "doi:10.1038/nature14539 Received" into
    "10.1038/nature14539Received")."""
    yield doi
    trimmed = re.sub(r"(?<=[0-9a-z])[A-Z][a-z]{2,}$", "", doi)
    if trimmed != doi:
        yield trimmed


# ---------------------------------------------------------------- online lookups

@functools.lru_cache(maxsize=None)  # the OCR pass may retry the same DOIs
def lookup_doi(doi):
    r = session.get(f"https://api.crossref.org/works/{doi}", timeout=20)
    if r.status_code == 200:
        return parse_csl(r.json()["message"])
    # Not a Crossref DOI (e.g. DataCite for arXiv/Zenodo): ask doi.org for CSL-JSON.
    r = session.get(f"https://doi.org/{doi}", timeout=20,
                    headers={"Accept": "application/vnd.citationstyles.csl+json"})
    if r.status_code == 200 and "json" in r.headers.get("Content-Type", ""):
        return parse_csl(r.json())
    return None


def crossref_by_title(title):
    r = session.get(
        "https://api.crossref.org/works",
        params={"query.bibliographic": title, "rows": 1},
        timeout=20,
    )
    if r.status_code != 200:
        return None
    items = r.json()["message"]["items"]
    if not items:
        return None
    info = parse_csl(items[0])
    if not info or difflib.SequenceMatcher(None, fold(title), fold(info.title)).ratio() < 0.9:
        return None
    return info


def crossref_by_text(text):
    """Last resort for papers without identifiers (e.g. old scans): Crossref's
    bibliographic search is built for messy reference strings, so feed it the
    start of page 1. Only accept a hit whose title (mostly) and first author's
    surname both appear in the PDF."""
    query = " ".join(text.split())[:500]
    if len(query) < 50:
        return None
    r = session.get(
        "https://api.crossref.org/works",
        params={"query.bibliographic": query, "rows": 5},
        timeout=30,
    )
    if r.status_code != 200:
        return None
    for item in r.json()["message"]["items"]:
        info = parse_csl(item)
        if (info and len(title_words(info.title)) >= 3
                and title_plausible(info.title, text, threshold=0.75)
                and letters(info.surname) in letters(text)):
            return info
    return None


def first_string(value):
    """Crossref gives lists where CSL-JSON gives strings."""
    if isinstance(value, list):
        return value[0] if value else ""
    return value or ""


def parse_csl(msg):
    """Crossref's format and CSL-JSON are close enough to share a parser."""
    people = msg.get("author") or msg.get("editor") or []  # edited books often only list editors
    people = sorted(people, key=lambda a: a.get("sequence") != "first")  # stable: keeps the rest in order
    authors = [[a.get("given", ""), a.get("family") or a.get("name") or a.get("literal") or ""] for a in people]
    authors = [a for a in authors if a[1]]
    year = None
    for key in ("published-print", "published-online", "issued", "created"):
        parts = (msg.get(key) or {}).get("date-parts") or [[None]]
        if parts[0][0]:
            year = int(parts[0][0])
            break
    if not authors or not year:
        return None
    journal = (first_string(msg.get("short-container-title")) or first_string(msg.get("container-title-short"))
               or first_string(msg.get("container-title")))
    return Meta(authors, year, first_string(msg.get("title")), msg.get("DOI") or "", journal)


def arxiv_lookup(arxiv_id):
    r = session.get("https://export.arxiv.org/api/query", params={"id_list": arxiv_id}, timeout=20)
    if r.status_code != 200:
        return None
    ns = {"a": "http://www.w3.org/2005/Atom"}
    entry = ET.fromstring(r.text).find("a:entry", ns)
    if entry is None or entry.find("a:author/a:name", ns) is None:
        return None
    authors = []
    for name in entry.findall("a:author/a:name", ns):
        given, _, family = name.text.strip().rpartition(" ")
        authors.append([given, family])
    year = int(entry.find("a:published", ns).text[:4])
    title = " ".join(entry.find("a:title", ns).text.split())
    # Every arXiv paper has this DataCite DOI.
    return Meta(authors, year, title, f"10.48550/arXiv.{arxiv_id}", "arXiv")


# ---------------------------------------------------------------- matching helpers

def fold(s):
    """Lowercase ASCII: 'Schrödinger' -> 'schrodinger'."""
    return ascii_only(s).lower()


def ascii_only(s):
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()


def letters(s):
    return re.sub(r"[^a-z]", "", fold(s))


def title_words(title):
    title = re.sub(r"<[^>]+>", " ", title)  # Crossref titles can contain <i>, <sub>, MathML...
    return re.findall(r"[a-z]{4,}", fold(title))


def title_plausible(title, text, threshold=0.6):
    """Guard against picking up a DOI from someone else's paper (e.g. a citation):
    most of the looked-up title's words should appear in this PDF's text.
    Compares letters only, so missing spaces and hyphenated line breaks don't matter."""
    words = title_words(title)
    if not words:
        return True
    haystack = letters(text)
    return sum(w in haystack for w in words) / len(words) >= threshold


# ---------------------------------------------------------------- identification

ocr_available = True


def identify(path, use_ocr=True):
    """Return (Meta, method) or (None, reason)."""
    global ocr_available
    meta, text = read_pdf(path)
    reasons = []

    named = filename_dois(path.stem)
    info, how = identify_from(meta, text, reasons, named)
    if info:
        return info, how

    if use_ocr and ocr_available:
        try:
            ocr_text = ocr_pdf(path)
        except Exception as e:
            if not re.search(r"tess|ocr", str(e), re.I):
                raise  # a problem with this particular file, not with the OCR setup
            ocr_available = False
            print(f"warning: OCR unavailable, continuing without it ({e})\n"
                  "         see the install notes at the top of this script")
            reasons.append("OCR unavailable")
        else:
            # Metadata was already tried; only the new text matters.
            info, how = identify_from({}, ocr_text, reasons, named)
            if info:
                return info, how + " (OCR)"
            if not ocr_text.strip():
                reasons.append("OCR found no text either")

    if not text.strip() and not (use_ocr and ocr_available):
        reasons.append("no extractable text (scanned PDF? OCR is off or unavailable)")
    if not reasons:
        reasons.append("no DOI, arXiv ID or metadata title found, and text search found no confident match")
    return None, "; ".join(dict.fromkeys(reasons))


def identify_from(meta, text, reasons, from_filename=()):
    """Run the lookup chain on one version of the text. Appends failure reasons."""
    for doi in find_dois(meta, text, from_filename):
        for candidate in doi_variants(doi):
            info = lookup_doi(candidate)
            if info is None:
                continue
            if title_plausible(info.title, text):
                info.doi = info.doi or candidate  # in case the record didn't echo its DOI
                return info, f"doi {candidate}"
            reasons.append(f"DOI {candidate} seems to be a different paper: \"{info.title[:50]}\"")
            break
        else:
            reasons.append(f"DOI {doi} not found")

    m = ARXIV_RE.search(text)
    if m:
        info = arxiv_lookup(m.group(1))
        if info and title_plausible(info.title, text):
            return info, f"arXiv {m.group(1)}"
        reasons.append(f"arXiv {m.group(1)} lookup failed")

    title = meta.get("title", "").strip()
    if len(title) > 15 and not PII_RE.search(title):
        info = crossref_by_title(title)
        if info:
            return info, "title search"
        reasons.append(f"no close Crossref match for metadata title \"{title[:50]}\"")

    info = crossref_by_text(text)
    if info:
        return info, "text search"
    return None, None


# ---------------------------------------------------------------- naming

DEFAULT_PATTERN = "{author}{year}"
PLACEHOLDERS = {
    "author": "first author's surname, e.g. Fermi",
    "authors": "Fermi, FermiDirac (two authors) or FermiEtAl (three or more)",
    "year": "year of publication, e.g. 1926",
    "title": "first three main words of the title, e.g. QuantelungIdealenEinatomigen",
    "journal": "journal abbreviation, e.g. ZPhysik",
}
STOPWORDS = set("""
    a an the of on in for and or to with from by at as is are be via into onto over under
    its their this that these those using toward towards about between new
    der die das des dem den ein eine einer eines zur zum und von vom im mit fur uber auf bei
    le la les du de et un une sur pour dans
""".split())


def clean_surname(name):
    """'van der Waals' -> 'VanDerWaals', 'FERMI' -> 'Fermi', but keep 'LeCun' and 'McDonald'."""
    parts = re.split(r"[^A-Za-z]+", ascii_only(name))
    return "".join(p.capitalize() if p.isupper() or p.islower() else p for p in parts) or "Unknown"


def camel_words(text, limit, skip_stopwords=False):
    """'Zur Quantelung des idealen Gases' -> 'QuantelungIdealenGases'."""
    words = re.findall(r"[A-Za-z0-9]+", ascii_only(re.sub(r"<[^>]+>", " ", text or "")))
    if skip_stopwords:
        words = [w for w in words if w.lower() not in STOPWORDS]
    return "".join(w.capitalize() if w.islower() else w for w in words[:limit])


def make_name(meta, pattern=DEFAULT_PATTERN):
    """File name (without .pdf) for a paper. Raises ValueError for a bad pattern."""
    surnames = [clean_surname(family) for _, family in meta.authors] or ["Unknown"]
    values = {
        "author": surnames[0],
        "authors": surnames[0] if len(surnames) == 1
        else surnames[0] + (surnames[1] if len(surnames) == 2 else "EtAl"),
        "year": str(meta.year or ""),
        "title": camel_words(meta.title, 3, skip_stopwords=True),
        "journal": camel_words(meta.journal, 6),
    }
    try:
        name = pattern.format_map(values)
    except KeyError as e:
        raise ValueError(f"unknown placeholder {{{e.args[0]}}}") from None
    except IndexError:
        raise ValueError("use named placeholders like {author}") from None
    except ValueError:
        raise ValueError("unbalanced { or } in the pattern") from None
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name)
    # Tidy up separators left behind by empty fields, e.g. "Fermi1926_" without a journal.
    name = re.sub(r"([ _.\-])[ _.\-]+", r"\1", name).strip(" _.-")
    return name[:150] or "Unknown"


def check_pattern(pattern):
    """Raise ValueError if the pattern can't be used."""
    make_name(Meta([["Enrico", "Fermi"]], 1926, "Zur Quantelung des idealen einatomigen Gases"), pattern)


def path_key(path):
    return str(path).lower()


def unique_target(folder, stem, taken):
    """Fermi1926.pdf, then Fermi1926a.pdf, Fermi1926b.pdf, ...
    `taken` holds path_key()s of names already reserved."""
    for suffix in [""] + [chr(c) for c in range(ord("a"), ord("z") + 1)]:
        candidate = folder / f"{stem}{suffix}.pdf"
        if path_key(candidate) not in taken and not candidate.exists():
            return candidate
    raise RuntimeError(f"too many collisions for {stem}")


def already_named(pdf, stem):
    return re.fullmatch(re.escape(stem) + r"[a-z]?", pdf.stem) is not None


# ---------------------------------------------------------------- cache

def default_cache_path():
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home())
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "PaperRenamer" / "cache.json"


class Cache:
    """Remembers identified papers by file content (so renaming doesn't matter)
    and downloaded citations by DOI. Failures aren't cached: they may work next time."""
    VERSION = 1

    def __init__(self, path=None):
        self.path = Path(path) if path else default_cache_path()
        self.lock = threading.Lock()
        self.unsaved = 0
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("version") != self.VERSION:
                data = {}
        except (OSError, ValueError):
            data = {}
        self.files = data.get("files", {})
        self.citations = data.get("citations", {})

    def get(self, digest):
        entry = self.files.get(digest)
        if not entry:
            return None
        try:
            return Meta(**entry["meta"]), entry["how"]
        except (TypeError, KeyError):
            return None

    def put(self, digest, meta, how):
        with self.lock:
            self.files[digest] = {"meta": asdict(meta), "how": how}
            self.unsaved += 1
        if self.unsaved >= 20:
            self.save()

    def citation(self, fmt, doi):
        return self.citations.get(f"{fmt}:{doi.lower()}")

    def put_citation(self, fmt, doi, text):
        with self.lock:
            self.citations[f"{fmt}:{doi.lower()}"] = text
            self.unsaved += 1

    @property
    def count(self):
        """Number of distinct papers (a file whose metadata we rewrote is stored twice).
        Not __len__: an empty cache must still be truthy."""
        return len({e["meta"].get("doi") or digest for digest, e in self.files.items()})

    def clear(self):
        with self.lock:
            self.files.clear()
            self.citations.clear()
            self.unsaved = 1
        self.save()

    def save(self):
        with self.lock:
            if not self.unsaved:
                return
            data = {"version": self.VERSION, "files": self.files, "citations": self.citations}
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(data), encoding="utf-8")
                os.replace(tmp, self.path)
                self.unsaved = 0
            except OSError:
                pass  # a cache that can't be saved is no reason to fail


# ---------------------------------------------------------------- planning

@dataclass
class Plan:
    pdf: Path
    status: str            # "rename", "ok" (already named) or "skip"
    detail: str            # how it was identified, or why it was skipped
    stem: str = ""         # e.g. "Fermi1926"
    target: Path = None    # planned new path (for "rename")
    meta: Meta = None
    hash: str = ""
    cached: bool = False

    @property
    def title(self):
        return self.meta.title if self.meta else ""

    @property
    def doi(self):
        return self.meta.doi if self.meta else ""


def file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def duplicate_groups(entries):
    """entries: one (doi, file_hash) per file. Returns groups of indices (2+ each)
    of files that share a DOI or are byte-for-byte identical."""
    parent = list(range(len(entries)))

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    first_seen = {}
    for i, (doi, digest) in enumerate(entries):
        for key in (f"doi:{doi.lower()}" if doi else None, f"sha:{digest}" if digest else None):
            if key is None:
                continue
            if key in first_seen:
                parent[root(i)] = root(first_seen[key])
            else:
                first_seen[key] = i
    groups = {}
    for i in range(len(entries)):
        groups.setdefault(root(i), []).append(i)
    return [g for g in groups.values() if len(g) > 1]


def list_pdfs(folder, recursive=False):
    """PDFs in the folder (and, if asked, its subfolders), skipping hidden files and folders."""
    folder = Path(folder)
    if not recursive:
        return sorted(p for p in folder.iterdir()
                      if p.suffix.lower() == ".pdf" and not p.name.startswith(".") and p.is_file())
    found = []
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        found += [Path(dirpath, f) for f in filenames if f.lower().endswith(".pdf") and not f.startswith(".")]
    return sorted(found, key=lambda p: [part.lower() for part in p.relative_to(folder).parts])


def plan_renames(pdfs, use_ocr=True, pattern=DEFAULT_PATTERN, cache=None):
    """Identify each PDF and yield a Plan for it, without renaming anything.
    Targets are reserved so that two papers never get the same new name."""
    taken = set()
    for pdf in pdfs:
        try:
            digest = file_hash(pdf)
        except OSError:
            digest = ""
        hit = cache.get(digest) if cache and digest else None
        if hit:
            (info, how), cached = hit, True
        else:
            cached = False
            try:
                info, how = identify(pdf, use_ocr=use_ocr)
            except requests.RequestException as e:
                info, how = None, f"network error: {type(e).__name__}"
            except Exception as e:
                info, how = None, f"error: {e}"
            if info and cache and digest:
                cache.put(digest, info, how)

        if not info:
            yield Plan(pdf, "skip", how, hash=digest)
            continue

        stem = make_name(info, pattern)
        if already_named(pdf, stem):
            taken.add(path_key(pdf))
            yield Plan(pdf, "ok", how, stem, pdf, info, digest, cached)
        else:
            target = unique_target(pdf.parent, stem, taken)
            taken.add(path_key(target))
            yield Plan(pdf, "rename", how, stem, target, info, digest, cached)
        if not cached:
            time.sleep(0.2)  # be gentle with the APIs


# ---------------------------------------------------------------- writing metadata

def write_metadata(path, meta):
    """Store title, authors and DOI in the PDF's own metadata, so reference managers
    and file browsers show them (and a later scan finds the DOI right away)."""
    if not pymupdf:
        raise RuntimeError("writing metadata needs PyMuPDF (pip install pymupdf)")
    path = Path(path)
    doc = pymupdf.open(path)
    try:
        if doc.needs_pass:
            raise RuntimeError("the PDF is password-protected")
        keys = ("title", "author", "subject", "keywords", "creator", "producer", "creationDate", "modDate", "trapped")
        info = {k: v for k, v in (doc.metadata or {}).items() if k in keys and v}
        if meta.title:
            info["title"] = re.sub(r"<[^>]+>", "", meta.title)
        if meta.authors:
            info["author"] = "; ".join(" ".join(p for p in a if p) for a in meta.authors)
        subject = info.get("subject", "")
        if meta.doi and meta.doi.lower() not in subject.lower():
            info["subject"] = f"doi:{meta.doi}" + (f"; {subject}" if subject else "")
        doc.set_metadata(info)
        if doc.can_save_incrementally():
            doc.saveIncr()  # appends the change; the rest of the file stays byte-for-byte the same
        else:
            tmp = path.with_name(path.name + ".tmp")
            doc.save(tmp, garbage=1, deflate=True)
            doc.close()
            os.replace(tmp, path)
    finally:
        if not doc.is_closed:
            doc.close()


# ---------------------------------------------------------------- citations

CITATION_FORMATS = {
    "bibtex": "application/x-bibtex",
    "ris": "application/x-research-info-systems",
}


def fetch_citation(doi, fmt, cache=None):
    """BibTeX or RIS for a DOI, straight from the publisher's registration agency."""
    if cache and cache.citation(fmt, doi):
        return cache.citation(fmt, doi)
    r = session.get(f"https://doi.org/{doi}", headers={"Accept": CITATION_FORMATS[fmt]}, timeout=30)
    if r.status_code != 200 or not r.content.strip() or r.content.lstrip()[:1] == b"<":
        return None  # an HTML page means the agency doesn't offer this format
    text = r.content.decode("utf-8", "replace").strip()
    if cache:
        cache.put_citation(fmt, doi, text)
    return text


def tailor_citation(text, fmt, key, pdf):
    """Use our file name as the citation key and link the PDF (Zotero/JabRef pick that up)."""
    key = re.sub(r"[^A-Za-z0-9_:\-]", "", key) or "paper"
    if fmt == "bibtex":
        text = re.sub(r"^(\s*@\w+\s*\{)[^,]*,", rf"\g<1>{key},", text, count=1)
        if pdf:
            body = text.rstrip()
            if body.endswith("}"):
                text = body[:-1].rstrip().rstrip(",") + f",\n  file = {{{Path(pdf).as_posix()}}}\n}}"
        return text
    lines = [l for l in text.splitlines() if not l.startswith(("ID  -", "L1  -"))]
    lines.insert(1, f"ID  - {key}")
    if pdf:
        end = next((i for i, l in enumerate(lines) if l.startswith("ER  -")), len(lines))
        lines.insert(end, f"L1  - {Path(pdf).as_posix()}")
    return "\n".join(lines)


def export_citations(entries, fmt, out_path, cache=None, progress=None, cancelled=lambda: False):
    """entries: (doi, key, pdf path) per paper. Writes a .bib or .ris file and
    returns (number written, list of DOIs that couldn't be fetched)."""
    done, failed, seen = [], [], set()
    for n, (doi, key, pdf) in enumerate(entries):
        if cancelled():
            break
        if progress:
            progress(n, len(entries), key)
        if not doi or doi.lower() in seen:
            continue
        seen.add(doi.lower())
        try:
            text = fetch_citation(doi, fmt, cache)
        except requests.RequestException:
            text = None
        if text:
            done.append(tailor_citation(text, fmt, key, pdf))
        else:
            failed.append(doi)
    if cache:
        cache.save()
    Path(out_path).write_text("\n\n".join(done) + "\n", encoding="utf-8")
    return len(done), failed


# ---------------------------------------------------------------- command line

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", type=Path)
    ap.add_argument("--apply", action="store_true", help="actually rename (default is a dry run)")
    ap.add_argument("-r", "--recursive", action="store_true", help="include PDFs in subfolders")
    ap.add_argument("--pattern", default=DEFAULT_PATTERN, help=f"naming pattern (default {DEFAULT_PATTERN})")
    ap.add_argument("--no-ocr", action="store_true", help="never OCR (faster, but scans get skipped)")
    ap.add_argument("--no-cache", action="store_true", help="ignore and don't update the result cache")
    ap.add_argument("--write-metadata", action="store_true",
                    help="with --apply: also store title, authors and DOI inside each renamed PDF")
    ap.add_argument("--bib", type=Path, metavar="FILE", help="export BibTeX for all identified papers")
    ap.add_argument("--ris", type=Path, metavar="FILE", help="export RIS for all identified papers")
    args = ap.parse_args()
    try:
        check_pattern(args.pattern)
    except ValueError as e:
        ap.error(f"--pattern: {e}")

    if not pymupdf:
        print("note: PyMuPDF not installed, using pypdf (less reliable). pip install pymupdf\n")

    cache = None if args.no_cache else Cache()
    pdfs = list_pdfs(args.folder, args.recursive)
    renamed = failed = from_cache = 0
    seen = []  # (path after this run, plan) for the duplicate report and exports
    try:
        for plan in plan_renames(pdfs, use_ocr=not args.no_ocr, pattern=args.pattern, cache=cache):
            name = plan.pdf.relative_to(args.folder)
            from_cache += plan.cached
            final = plan.pdf
            if plan.status == "skip":
                print(f"  SKIP  {name}  ({plan.detail})")
                failed += 1
            elif plan.status == "ok":
                print(f"    OK  {name}  (already named)")
            else:
                how = plan.detail if plan.doi.lower() in plan.detail.lower() else f"{plan.detail}, doi {plan.doi}"
                print(f"{'RENAME' if args.apply else ' WOULD'}  {name}  ->  {plan.target.name}  [{how}]")
                if args.apply:
                    plan.pdf.rename(plan.target)
                    final = plan.target
                    if args.write_metadata:
                        try:
                            write_metadata(final, plan.meta)
                            if cache:
                                cache.put(file_hash(final), plan.meta, plan.detail)
                        except Exception as e:
                            print(f"        (couldn't write metadata: {e})")
                renamed += 1
            seen.append((final, plan))
    finally:
        if cache:
            cache.save()

    groups = duplicate_groups([(plan.doi, plan.hash) for _, plan in seen])
    if groups:
        print("\nPossible duplicates (same DOI or identical files):")
        for g in groups:
            doi = next((seen[i][1].doi for i in g if seen[i][1].doi), "")
            names = ", ".join(str(seen[i][0].relative_to(args.folder)) for i in g)
            print(f"  {names}" + (f"  [doi {doi}]" if doi else "  [identical]"))

    for fmt, out in (("bibtex", args.bib), ("ris", args.ris)):
        if out:
            entries = [(plan.doi, path.stem, path.resolve()) for path, plan in seen if plan.doi]
            written, missing = export_citations(entries, fmt, out, cache)
            print(f"\nWrote {written} {fmt.upper() if fmt == 'ris' else 'BibTeX'} entries to {out}"
                  + (f"; couldn't fetch {len(missing)}: {', '.join(missing)}" if missing else "."))

    verb = "renamed" if args.apply else "to rename"
    print(f"\n{renamed} {verb}, {failed} skipped, {len(pdfs)} total"
          + (f" ({from_cache} from cache)." if from_cache else "."))
    if not args.apply and renamed:
        print("Dry run only. Re-run with --apply to rename.")


if __name__ == "__main__":
    sys.exit(main())
