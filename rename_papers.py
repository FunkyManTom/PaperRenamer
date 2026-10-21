#!/usr/bin/env python3
"""Rename academic paper PDFs to e.g. <FirstAuthorSurname><Year>.pdf.

Strategy, per file:
  1. Look for DOIs in the file name (publisher download names like
     978-3-030-13876-9.pdf), the PDF metadata or the text of the first pages,
     then look them up (Crossref first, then doi.org for DataCite/other DOIs).
  2. Otherwise look for an arXiv ID and ask the arXiv API.
  3. Otherwise look for an ADS bibcode and ask ADS (with ADS_TOKEN) or OpenAlex.
  4. Otherwise look for ISBNs (books) in the file name or after an "ISBN"
     label and look them up on Crossref, then Open Library.
  5. Otherwise, for library-style file names ("Author - Title (Year)"), look
     the book up by its file name.
  6. Otherwise, if the PDF metadata has a title, search Crossref by title
     and accept the top hit only if the titles closely match.
  7. Otherwise search Crossref with the start of page 1 (title, authors,
     journal reference) and accept a hit only if its title and first author
     both appear in the PDF.
If none of that works, OCR the first pages (for scans without a usable text
layer) and try again. A short PDF of a book is named after its chapter.
Results are cached by file content, so re-scanning a folder only looks up
new files.

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
    {part}     chapter of a book excerpt       Ch21 (added at the end if left out)

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
import html
import json
import logging
import os
import re
import shutil
import sys
import threading
import time
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from urllib.parse import quote

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
BOOK_PAGES = 50  # PDFs this long are treated as books
DEEP_OCR_PAGES = 6  # with deep OCR, scanned books get this many pages read: title and copyright pages
EXCERPT_PAGES = 60  # a "book" PDF shorter than this is a chapter or excerpt of it
MAX_DOI_CANDIDATES = 5
DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>]+)", re.I)
# Elsevier's Publisher Item Identifier, e.g. "PII: 0375-9474(95)00035-Y" or "PII: S0370269398012345".
PII_RE = re.compile(r"PII:?\s*(S?\d{4}-\d{3}[\dX]\s*\(\d{2}\)\s*\d{5}-[\dA-Z]|S\d{7}[\dX]\d{7}[\dA-Z])", re.I)
ARXIV_RE = re.compile(r"arXiv[:.]\s*(\d{4}\.\d{4,5}|[a-z\-]+(?:\.[A-Z]{2})?/\d{7})", re.I)
# ISBN-13 or ISBN-10, with hyphens, en dashes or spaces between the digits.
ISBN_RE = re.compile(r"(?<![\d\-–_])(97[89](?:[\-–\s]?\d){10}|(?:\d[\-–\s]?){9}[\dX])(?![\dX\-–_])", re.I)
ISBN_LABEL_RE = re.compile(r"ISBN", re.I)  # also matches "eISBN", "ISBN-13", "ISBN:"
ISBN_WINDOW = 150  # how far after an "ISBN" label to look for numbers
MAX_ISBN_CANDIDATES = 5
MAX_TAGS = 8
AUTHOR_YEAR_NAME_RE = re.compile(r"[A-Za-z]{3,}[\s_\-]*(?:1[89]|20)\d\d")  # "lanser2009", "Smith_2010"
BOOK_TYPES = ("book", "monograph", "edited-book", "reference-book")  # Crossref types, not chapters
# ADS bibcode: year, journal, volume, section letter, page, first author's initial ("1989A&A...216...44D").
# ADS stamps it on every page of its scans of old journals.
BIBCODE_RE = re.compile(r"(?<![\w&.])((?:1[89]|20)\d\d)([A-Za-z&.]{5})([\d.]{4})([A-Za-z.])([\d.]{4})([A-Z.])(?![\w&])")
# Name particles that belong to the surname: "E. J. de Geus" -> family name "de Geus".
NAME_PARTICLES = {"de", "van", "von", "der", "den", "du", "la", "le", "di", "da", "del", "della", "dos", "ter", "ten"}
# Put your email here (or pass --email, or use the window's API settings): Crossref and
# OpenAlex route "polite" requests with a contact address to faster servers.
HEADERS = {"User-Agent": "rename_papers/1.2 (mailto:you@example.com)"}
# Optional, free: https://ui.adsabs.harvard.edu/user/settings/token. Without it, bibcodes
# are looked up on OpenAlex, which lacks some astronomy papers.
ADS_TOKEN = os.environ.get("ADS_TOKEN", "")
ADS_API = "https://api.adsabs.harvard.edu/v1/search/query"

session = requests.Session()
session.headers.update(HEADERS)


def api_get(url, **kwargs):
    """session.get that raises on rate limits and server errors (429, 5xx). Those are worth
    trying again later, unlike "not found", and the lookups' lru_caches mustn't remember them."""
    r = session.get(url, **kwargs)
    if r.status_code == 429 or r.status_code >= 500:
        raise requests.HTTPError(f"{r.status_code} from {url.split('/')[2]}", response=r)
    return r


def clear_lookup_caches():
    """Forget this session's online lookups (with "Clear cache")."""
    for lookup in (lookup_doi, arxiv_candidates, arxiv_record, crossref_by_isbn, openlibrary_by_isbn,
                   openalex_by_doi, ads_by_bibcode, openalex_by_bibcode, search_by_filename):
        lookup.cache_clear()


def set_contact(email):
    """Use this email for Crossref's and OpenAlex's polite pools (empty: the placeholder)."""
    session.headers["User-Agent"] = re.sub(r"mailto:[^)]*", f"mailto:{email.strip() or 'you@example.com'}",
                                           HEADERS["User-Agent"])


@dataclass
class Meta:
    """What we know about a paper once it's identified."""
    authors: list = field(default_factory=list)  # [given, family] pairs, first author first
    year: int = None
    title: str = ""
    doi: str = ""
    journal: str = ""
    abstract: str = ""
    tags: list = field(default_factory=list)  # keywords
    isbn: str = ""
    publisher: str = ""
    kind: str = ""  # "article", "book", "chapter" or "" (unknown); see kind_of()
    part: str = ""  # for a PDF that's only part of a book: "Ch21" or "Excerpt"

    @property
    def surname(self):
        return self.authors[0][1] if self.authors else ""


# ---------------------------------------------------------------- reading PDFs

def read_pdf(path):
    """Return (metadata dict, text of the first few pages). The dict also holds "pages"."""
    if pymupdf:
        with pymupdf.open(path) as doc:
            meta = {k.lower(): v or "" for k, v in (doc.metadata or {}).items()}
            meta["pages"] = doc.page_count
            text = "\n".join(doc[i].get_text() for i in range(min(PAGES_TO_SCAN, doc.page_count)))
    else:
        reader = PdfReader(path)
        meta = {k.lstrip("/").lower(): str(v) for k, v in (reader.metadata or {}).items()}
        meta["pages"] = len(reader.pages)
        text = ""
        for page in reader.pages[:PAGES_TO_SCAN]:
            try:
                text += (page.extract_text() or "") + "\n"
            except Exception:
                pass
    # NFKC turns ligatures like "ﬁ" into "fi" and odd slashes/dashes into plain ones.
    return meta, unicodedata.normalize("NFKC", text)


def ocr_pdf(path, first=0, count=OCR_PAGES):
    """Text of some pages via Tesseract OCR. Raises RuntimeError if OCR is unavailable."""
    if not pymupdf:
        raise RuntimeError("OCR needs PyMuPDF (pip install pymupdf)")
    with pymupdf.open(path) as doc:
        text = "\n".join(
            page.get_text(textpage=page.get_textpage_ocr(dpi=OCR_DPI, full=True))
            for page in doc.pages(first, min(first + count, doc.page_count))
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


# ---------------------------------------------------------------- finding ISBNs

def find_isbns(meta, text, stem=""):
    """ISBN-13s of a book, most likely first. In the text only numbers shortly after an
    "ISBN" label count (copyright pages often list several: "ISBN 978-0-19-968076-4 (Hbk.)
    978-0-19-968077-1 (Pbk.)"); IEEE papers print their proceedings' ISBN unlabeled."""
    found = [m.group(1) for m in ISBN_RE.finditer(stem) if m.group(1)[:3] in ("978", "979")]
    for source in [meta.get(k, "") for k in ("subject", "keywords")] + [text]:
        for label in ISBN_LABEL_RE.finditer(source):
            found += [m.group(1) for m in ISBN_RE.finditer(source[label.end():label.end() + ISBN_WINDOW])]
    isbns = []
    for isbn in filter(None, map(isbn13, found)):
        if isbn not in isbns:
            isbns.append(isbn)
    return isbns[:MAX_ISBN_CANDIDATES]


def isbn13(isbn):
    """'0-691-12213-7' or '978–0–691–12213–7' -> '9780691122137'; None if the check digit is wrong."""
    isbn = re.sub(r"[^\dX]", "", isbn.upper())
    if len(isbn) == 10 and "X" not in isbn[:9]:
        total = sum((10 - i) * (10 if c == "X" else int(c)) for i, c in enumerate(isbn))
        if total % 11:
            return None
        isbn = "978" + isbn[:9]
        return isbn + str(-sum((3 if i % 2 else 1) * int(c) for i, c in enumerate(isbn)) % 10)
    if len(isbn) == 13 and isbn.isdigit():
        if sum((3 if i % 2 else 1) * int(c) for i, c in enumerate(isbn)) % 10 == 0:
            return isbn
    return None


# ---------------------------------------------------------------- online lookups

# arXiv IDs in download names: "2406.14694v1.pdf"; before 2007 "0501430v2.pdf", whose archive
# ("astro-ph/0501430") the name leaves out, or "astro-ph0501430.pdf".
ARXIV_NAME_RE = re.compile(r"(?<![\d.])(\d{4}\.\d{4,5})(?:v\d+)?(?![\d.])")
OLD_ARXIV_NAME_RE = re.compile(r"(?:([a-z]+(?:-[a-z]+)?)[_/ ]?)?((?:9[1-9]|0[0-7])(?:0[1-9]|1[0-2])\d{3})(?:v\d+)?", re.I)


def filename_arxiv_ids(stem):
    """arXiv IDs from the file name. An old-style ID without its archive is "*/0501430"."""
    m = OLD_ARXIV_NAME_RE.fullmatch(stem.strip())
    if m:
        return [f"{(m.group(1) or '*').lower()}/{m.group(2)}"]
    return [m.group(1) for m in ARXIV_NAME_RE.finditer(stem)][:1]


@functools.lru_cache(maxsize=None)
def arxiv_candidates(arxiv_id):
    """The IDs an "*/0501430" could be (math/0501430, astro-ph/0501430, ...), from DataCite,
    which registers every arXiv paper's DOI. Other IDs are returned as they are."""
    if not arxiv_id.startswith("*/"):
        return (arxiv_id,)
    r = api_get("https://api.datacite.org/dois", timeout=20,
                params={"query": f"doi:10.48550/arxiv.*{arxiv_id[2:]}", "page[size]": 25})
    if r.status_code != 200:
        return ()
    ids = [d["id"].split("arxiv.", 1)[-1] for d in r.json().get("data") or []]
    return tuple(i for i in ids if i.endswith("/" + arxiv_id[2:]))


@functools.lru_cache(maxsize=None)
def arxiv_record(arxiv_id):
    """An arXiv paper's record: from DataCite via doi.org (fast, with abstract), else the arXiv
    API (which allows only one request every few seconds)."""
    info = lookup_doi(f"10.48550/arXiv.{arxiv_id}")
    if info:
        return replace(info, doi=f"10.48550/arXiv.{arxiv_id}", journal="arXiv", kind="article")
    return arxiv_lookup(arxiv_id)


@functools.lru_cache(maxsize=None)  # the OCR pass may retry the same DOIs
def lookup_doi(doi):
    try:
        r = api_get(f"https://api.crossref.org/works/{doi_path(doi)}", timeout=20)
        if r.status_code == 200:
            return parse_csl(r.json()["message"])
    except requests.HTTPError:
        pass  # Crossref is busy: doi.org can answer for its DOIs too
    # Not a Crossref DOI (e.g. DataCite for arXiv/Zenodo): ask doi.org for CSL-JSON.
    r = api_get(f"https://doi.org/{doi_path(doi)}", timeout=20,
                headers={"Accept": "application/vnd.citationstyles.csl+json"})
    if r.status_code == 200 and "json" in r.headers.get("Content-Type", ""):
        return parse_csl(r.json())
    return None


def doi_path(doi):
    """A DOI as a URL path: DOIs may contain "#", "?" or "%"."""
    return quote(doi, safe="/():;[]")


@functools.lru_cache(maxsize=None)
def crossref_by_isbn(isbn):
    """The book itself, not one of its chapters (they carry the book's ISBN too)."""
    r = api_get(
        "https://api.crossref.org/works",
        params={"filter": ",".join([f"isbn:{isbn}"] + [f"type:{t}" for t in BOOK_TYPES]), "rows": 1},
        timeout=20,
    )
    if r.status_code != 200:
        return None
    items = r.json()["message"]["items"]
    return parse_csl(items[0]) if items else None


@functools.lru_cache(maxsize=None)
def openlibrary_by_isbn(isbn):
    """Most textbooks have no DOI; Open Library knows them by ISBN."""
    r = api_get(
        "https://openlibrary.org/api/books",
        params={"bibkeys": f"ISBN:{isbn}", "format": "json", "jscmd": "data"},
        timeout=20,
    )
    if r.status_code != 200:
        return None
    book = r.json().get(f"ISBN:{isbn}") or {}
    authors = [a for a in (split_name(author.get("name") or "") for author in book.get("authors") or []) if a[1]]
    m = re.search(r"\b(1[5-9]|20)\d\d\b", book.get("publish_date") or "")
    if not authors or not m or not book.get("title"):
        return None
    subjects = [s.get("name", "") for s in book.get("subjects") or []]
    publisher = ((book.get("publishers") or [{}])[0]).get("name", "")
    return Meta(authors, int(m.group(0)), book["title"], tags=[s for s in subjects if s][:MAX_TAGS],
                isbn=isbn, publisher=publisher, kind="book")


@functools.lru_cache(maxsize=None)
def openalex_by_doi(doi):
    """(abstract, keywords) from OpenAlex, which has them for far more papers than Crossref."""
    r = api_get(f"https://api.openalex.org/works/doi:{quote(doi, safe='/()')}",
                params={"select": "abstract_inverted_index,keywords"}, timeout=20)
    if r.status_code != 200:
        return None
    work = r.json()
    return openalex_abstract(work), [k["display_name"] for k in work.get("keywords") or []]


def openalex_abstract(work):
    """OpenAlex stores abstracts as {word: [positions]}."""
    index = work.get("abstract_inverted_index") or {}
    return " ".join(w for _, w in sorted((pos, w) for w, positions in index.items() for pos in positions))


@functools.lru_cache(maxsize=None)
def ads_by_bibcode(bibcode):
    """The bibcode's own record from ADS (needs ADS_TOKEN)."""
    r = api_get(ADS_API, params={"q": f'bibcode:"{bibcode}"', "fl": "title,author,year,doi,pub,abstract,keyword"},
                headers={"Authorization": f"Bearer {ADS_TOKEN}"}, timeout=20)
    if r.status_code != 200:
        return None
    docs = (r.json().get("response") or {}).get("docs") or []
    if not docs:
        return None
    doc = docs[0]
    authors = [a for a in map(split_name, doc.get("author") or []) if a[1]]  # "de Geus, E. J."
    year = str(doc.get("year") or "")
    if not authors or not year.isdigit() or not first_string(doc.get("title")):
        return None
    return Meta(authors, int(year), first_string(doc.get("title")), first_string(doc.get("doi")),
                doc.get("pub") or "", doc.get("abstract") or "", (doc.get("keyword") or [])[:MAX_TAGS],
                kind="article")


def check_ads_token(token):
    """True if ADS accepts the token, False if it doesn't. Raises on network errors."""
    r = session.get(ADS_API, params={"q": "bibcode:1989A&A...216...44D", "fl": "bibcode", "rows": 1},
                    headers={"Authorization": f"Bearer {token}"}, timeout=15)
    return r.status_code == 200


@functools.lru_cache(maxsize=None)
def openalex_by_bibcode(bibcode):
    """Papers with an ADS bibcode's year, volume and first page. ADS itself needs an API
    token, and old journals (e.g. A&A before 2000) have no DOIs, but OpenAlex lists them."""
    m = BIBCODE_RE.fullmatch(bibcode)
    year, journal, volume, page = m.group(1), m.group(2).strip("."), m.group(3).strip("."), m.group(5).strip(".")
    if not volume.isdigit() or not page.isdigit():  # books and conferences ("1979rpa..book.....R")
        return ()
    r = api_get(
        "https://api.openalex.org/works",
        params={"filter": f"publication_year:{year},biblio.volume:{volume},biblio.first_page:{page}", "per_page": 10},
        timeout=20,
    )
    if r.status_code != 200:
        return ()
    return tuple(filter(None, (parse_openalex(work, journal) for work in r.json()["results"])))


def parse_openalex(work, journal=""):
    authors = [split_name((a.get("author") or {}).get("display_name") or "") for a in work.get("authorships") or []]
    authors = [a for a in authors if a[1]]
    if not authors or not work.get("publication_year") or not work.get("title"):
        return None
    source = ((work.get("primary_location") or {}).get("source") or {}).get("display_name")
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", work.get("doi") or "")
    return Meta(authors, work["publication_year"], work["title"], doi, source or journal,
                openalex_abstract(work), [k["display_name"] for k in work.get("keywords") or []],
                kind=kind_of(work.get("type")))


def filename_parts(stem):
    """(authors, title, year) from library-style file names, or None:
    "(Series) Jean Dufay (auth.) - Galactic Nebulae and Interstellar Matter (1968, Dover) - site.org",
    "Peter Schneider - Gravitational Lenses-Springer-Verlag Berlin Heidelberg (1992)" (the title may
    have the publisher glued on), or "Title -- Authors -- Publisher, Year -- ..." (Anna's Archive)."""
    parts = stem.split(" -- ")
    if len(parts) > 2:
        title, authors, rest = parts[0], parts[1], " ".join(parts[2:])
    else:
        authors, sep, rest = re.sub(r"^\s*(?:[(\[][^)\]]*[)\]]\s*)+", "", stem).partition(" - ")
        if not sep:
            return None
        title = re.split(r" - |\(", rest)[0]  # drop a " - site.org" suffix and "(1968, Dover)"
    years = re.findall(r"\b(1[5-9]\d\d|20\d\d)\b", rest)
    title = " ".join(title.replace("_", ":").split()).strip(" -.")  # such names write ":" as "_"
    authors = re.sub(r"\([^)]*\)", "", authors)  # "(auth.)", "(eds.)"
    if len(letters(title)) < 8 or not letters(authors):
        return None
    return authors, title, int(years[-1]) if years else None


def filename_match(info, authors, title):
    """The record's title starts the file name's title (which may carry a subtitle or the
    publisher), or the other way round for cut-off names, and its first author is named."""
    have, named = letters(info.title), letters(title)
    return (len(have) >= 8 and author_in(info, authors)
            and (named.startswith(have) or (have.startswith(named) and len(named) >= 20)))


@functools.lru_cache(maxsize=None)
def search_by_filename(stem):
    """For books and excerpts whose text doesn't say what they are, but whose file name
    does: Crossref, then Open Library (which also knows old books without ISBN)."""
    parts = filename_parts(stem)
    if not parts:
        return None
    authors, title, year = parts
    first = re.sub(r"\b(?:Dr|Prof|Professor)\.?\s+", "", re.split(r"[,;&_]| and ", authors)[0]).strip()
    # Books only: Crossref lists book reviews under the reviewed book's title and author.
    r = api_get(
        "https://api.crossref.org/works",
        params={"query.bibliographic": title, "query.author": first, "rows": 5,
                "filter": ",".join(f"type:{t}" for t in BOOK_TYPES)},
        timeout=30,
    )
    if r.status_code == 200:
        hits = [info for info in map(parse_csl, r.json()["message"]["items"])
                if info and filename_match(info, authors, title)]
        if hits:  # prefer the edition from the file name's year
            return next((info for info in hits if info.year == year), hits[0])
    # Open Library's title search wants the bare title: cut "Lenses-Springer-Verlag" back to "Lenses".
    for query in dict.fromkeys([re.sub(r"(?<=[a-z.])-[A-Z].*$", "", title), title]):
        r = api_get(
            "https://openlibrary.org/search.json",
            params={"title": query, "author": first, "limit": 5,
                    "fields": "title,author_name,first_publish_year,publish_year"},
            timeout=30,
        )
        if r.status_code != 200:
            continue
        for doc in r.json().get("docs") or []:
            people = [a for a in map(split_name, doc.get("author_name") or []) if a[1]]
            # The file name's year picks the edition (a 1968 reprint of a 1957 book).
            when = year if year in (doc.get("publish_year") or []) else doc.get("first_publish_year")
            if people and when and doc.get("title"):
                info = Meta(people, when, doc["title"], kind="book")
                if filename_match(info, authors, title):
                    return info
    return None


def crossref_by_title(title):
    r = api_get(
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
    surname both appear in the PDF. Long affiliations can drown the title, so if the
    first 500 characters find nothing, the first 200 (title and authors) get a try."""
    words = " ".join(text.split())
    if len(words) < 50:
        return None
    for query in (words[:500], words[:200]) if len(words) > 250 else (words[:500],):
        r = api_get(
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
                    and author_in(info, text)):
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
    isbn = first_string(msg.get("ISBN"))
    return Meta(authors, year, first_string(msg.get("title")), msg.get("DOI") or "", journal,
                clean_abstract(msg.get("abstract")), isbn=isbn13(isbn) or isbn,
                publisher=msg.get("publisher") or "", kind=kind_of(msg.get("type")))


def kind_of(work_type):
    """Crossref, CSL-JSON and OpenAlex types -> "article", "book", "chapter" or ""."""
    work_type = (work_type or "").lower()
    if work_type in ("book", "monograph", "edited-book", "reference-book", "book-set", "book-series"):
        return "book"
    if work_type in ("book-chapter", "book-section", "book-part", "chapter", "reference-entry"):
        return "chapter"
    if work_type in ("journal-article", "article-journal", "article", "proceedings-article", "paper-conference",
                     "posted-content", "preprint", "review", "letter", "editorial"):
        return "article"
    return ""


def clean_abstract(text):
    """Crossref abstracts are JATS XML: '<jats:title>Abstract</jats:title><jats:p>...'."""
    text = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", text or "")).split())
    text = re.sub(r" (?=[.,;:)])", "", text)  # "<italic>x</italic>." left a space behind
    return re.sub(r"^(?:Abstract|Summary)\b[.:]?\s*", "", text, flags=re.I)


def add_details(info, meta):
    """Abstract and keywords for the GUI's tooltips: keywords from the PDF itself (where
    publishers like Springer put the author's own), otherwise both from OpenAlex."""
    keywords = [k.strip() for k in re.split(r"[;,]", meta.get("keywords", ""))]
    keywords = [k for k in keywords if 1 < len(k) <= 60 and not re.search(r"doi|https?:|url:", k, re.I)]
    if keywords:
        info.tags = keywords[:MAX_TAGS]
    if info.doi and (not info.abstract or not info.tags):
        try:
            found = openalex_by_doi(info.doi)
        except requests.RequestException:
            found = None  # nice to have, no reason to fail
        if found:
            info.abstract = info.abstract or found[0]
            info.tags = info.tags or found[1][:MAX_TAGS]
    return info


def arxiv_lookup(arxiv_id):
    r = api_get("https://export.arxiv.org/api/query", params={"id_list": arxiv_id}, timeout=20)
    if r.status_code != 200:
        return None
    ns = {"a": "http://www.w3.org/2005/Atom"}
    entry = ET.fromstring(r.text).find("a:entry", ns)
    if entry is None or entry.find("a:author/a:name", ns) is None:
        return None
    authors = [split_name(name.text) for name in entry.findall("a:author/a:name", ns)]
    year = int(entry.find("a:published", ns).text[:4])
    title = " ".join(entry.find("a:title", ns).text.split())
    summary = " ".join((entry.findtext("a:summary", "", ns)).split())
    # Every arXiv paper has this DataCite DOI.
    return Meta(authors, year, title, f"10.48550/arXiv.{arxiv_id}", "arXiv", summary, kind="article")


# ---------------------------------------------------------------- matching helpers

def split_name(name):
    """'Jean Dufay' -> ['Jean', 'Dufay'], 'E. J. de Geus' -> ['E. J.', 'de Geus'],
    'Dufay, Jean' -> ['Jean', 'Dufay']."""
    name = " ".join((name or "").split())
    if "," in name:
        family, _, given = name.partition(",")
        return [given.strip(), family.strip()]
    words = name.split(" ")
    i = len(words) - 1
    while i > 1 and words[i - 1].lower() in NAME_PARTICLES:  # i > 1: keep a given name
        i -= 1
    return [" ".join(words[:i]), " ".join(words[i:])]


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


def author_in(info, text):
    """Does the text contain the first author's surname? (A surname without Latin letters
    can't be checked, and doesn't count.)"""
    surname = letters(info.surname)
    return bool(surname) and surname in letters(text)


def named_in(info, stem):
    """Does the file name contain the first author's surname?"""
    surname = letters(info.surname)
    return len(surname) >= 3 and surname in letters(stem)


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


def identify(path, use_ocr=True, deep_ocr=False):
    """Return (Meta, method) or (None, reason). deep_ocr: if OCR of the first pages doesn't
    identify a scanned book, also OCR the following pages (slow, so it's opt-in)."""
    global ocr_available
    meta, text = read_pdf(path)
    reasons = []

    named = filename_dois(path.stem)
    info, how = identify_from(meta, text, reasons, named, path.stem)
    if info:
        return add_details(excerpt_of(info, meta, text), meta), how

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
            # Metadata was already tried. Keep the text layer after the OCR text: it may hold
            # identifiers OCR misses (ADS stamps its bibcode in the margin of its scans).
            info, how = identify_from({}, ocr_text + "\n" + text, reasons, named, path.stem)
            if not info and deep_ocr and meta.get("pages", 0) >= BOOK_PAGES:
                # A book's title and copyright page (with the ISBN) come after the cover and blank pages.
                ocr_text += "\n" + ocr_pdf(path, OCR_PAGES, DEEP_OCR_PAGES - OCR_PAGES)
                info, how = identify_from({}, ocr_text + "\n" + text, reasons, named, path.stem)
            if info:
                return add_details(excerpt_of(info, meta, ocr_text + "\n" + text), meta), how + " (OCR)"
            if not ocr_text.strip():
                reasons.append("OCR found no text either")

    if not text.strip() and not (use_ocr and ocr_available):
        reasons.append("no extractable text (scanned PDF? OCR is off or unavailable)")
    if not reasons:
        reasons.append("no DOI, arXiv ID, bibcode, ISBN or metadata title found, "
                       "and neither the file name nor the text gave a confident match")
    return None, "; ".join(dict.fromkeys(reasons))


def excerpt_of(info, meta, text):
    """If a book was found but the PDF is much too short for it, it's a chapter (or another
    excerpt): name it after that, and use the chapter's own record if Crossref has one."""
    if info.kind != "book" or not 0 < meta.get("pages", 0) < EXCERPT_PAGES:
        return info
    chapter = crossref_chapter(info, text)
    number = chapter_number(text) or chapter_number(chapter.title if chapter else "", prefix=False)
    part = f"Ch{number}" if number else "Excerpt"
    return replace(chapter, part=part) if chapter else replace(info, part=part)  # copies: info may be cached


def crossref_chapter(book, text):
    """The chapter record whose title appears in the text, from this book (preferably the same
    publisher's edition: its chapter DOIs extend the book's DOI)."""
    query = " ".join(text.split())[:300] + " " + book.title
    r = api_get("https://api.crossref.org/works",
                params={"query.bibliographic": query, "filter": "type:book-chapter", "rows": 5}, timeout=30)
    if r.status_code != 200:
        return None
    hits = []
    for item in r.json()["message"]["items"]:
        title, container = first_string(item.get("title")), letters(first_string(item.get("container-title")))
        same_book = container and (container.startswith(letters(book.title)[:30])
                                   or letters(book.title).startswith(container[:30]))
        if title and same_book and title_plausible(title, text, threshold=0.8):
            hits.append(item)
    if not hits:
        return None
    item = next((h for h in hits if book.doi and h.get("DOI", "").lower().startswith(book.doi.lower())), hits[0])
    own = parse_csl(item)  # chapters of edited books have their own authors
    return Meta(own.authors if own else book.authors, own.year if own else book.year,
                re.sub(r"^\d+\.\s*", "", first_string(item.get("title"))), item.get("DOI", ""), book.title,
                isbn=book.isbn, publisher=book.publisher or item.get("publisher", ""), kind="chapter")


NUMBER_WORDS = {w: n for n, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
    "seventeen eighteen nineteen twenty".split())}
NUMBER_WORDS.update({"thirty": 30, "forty": 40, "fifty": 50, "sixty": 60})


def chapter_number(text, prefix=True):
    """'Chapter Twenty-one', 'CHAPTER 21', 'Chapter XXI' (or '21. Title' with prefix=False) -> 21."""
    # At the start of a line (headings, running heads), so "In this chapter I show" doesn't count.
    m = (re.search(r"^\s*chapter\s+(\d{1,2}|[ivxl]{1,6}|[a-z]+(?:-[a-z]+)?)\b", text[:2000], re.I | re.M) if prefix
         else re.match(r"\s*(\d{1,2})\.\s", text))
    if not m:
        return None
    word = m.group(1).lower()
    if word.isdigit():
        return int(word)
    if re.fullmatch(r"[ivxl]+", word):
        values = [{"i": 1, "v": 5, "x": 10, "l": 50}[c] for c in word]
        return sum(-v if i + 1 < len(values) and v < values[i + 1] else v for i, v in enumerate(values))
    parts = word.split("-")
    if all(w in NUMBER_WORDS for w in parts):
        return sum(NUMBER_WORDS[w] for w in parts)
    return None


def identify_from(meta, text, reasons, from_filename=(), stem=""):
    """Run the lookup chain on one version of the text. Appends failure reasons."""
    hits = []  # (Meta, DOI) that match the text
    for doi in find_dois(meta, text, from_filename):
        for candidate in doi_variants(doi):
            info = lookup_doi(candidate)
            if info is None:
                continue
            if title_plausible(info.title, text):
                info.doi = info.doi or candidate  # in case the record didn't echo its DOI
                hits.append((info, candidate))
            else:
                reasons.append(f"DOI {candidate} seems to be a different paper: \"{info.title[:50]}\"")
            break
        else:
            reasons.append(f"DOI {doi} not found")
        # Usually the first match is it. But a journal page can end one article and start the
        # next (with both DOIs on it), so for "lanser2009.pdf" look on for an author in the name.
        if hits and not (AUTHOR_YEAR_NAME_RE.search(stem) and not named_in(hits[0][0], stem)):
            break
    if hits:
        info, candidate = next((h for h in hits if named_in(h[0], stem)), hits[0])
        return info, f"doi {candidate}"

    m = ARXIV_RE.search(text)
    for arxiv_id in dict.fromkeys(([m.group(1)] if m else []) + filename_arxiv_ids(stem)):
        for candidate in arxiv_candidates(arxiv_id):
            info = arxiv_record(candidate)
            if info and title_plausible(info.title, text):
                return info, f"arXiv {candidate}"
        reasons.append(f"arXiv {arxiv_id.replace('*/', '')} lookup failed")

    m = BIBCODE_RE.search(text)
    if m:
        found = ([ads_by_bibcode(m.group(0))] if ADS_TOKEN else []) + list(openalex_by_bibcode(m.group(0)))
        for info in filter(None, found):
            if title_plausible(info.title, text) and author_in(info, text):
                return info, f"bibcode {m.group(0)}"
        reasons.append(f"no match for ADS bibcode {m.group(0)}")

    # Crossref first for every ISBN (its records have a DOI, so citation export works).
    isbns = find_isbns(meta, text, stem)
    for lookup in (crossref_by_isbn, openlibrary_by_isbn):
        for isbn in isbns:
            info = lookup(isbn)
            # A front-matter ISBN may belong to another book in the series: check the
            # title, and the first author (or editor) too, as for text search.
            if info and title_plausible(info.title, text) and author_in(info, text):
                info.isbn, info.kind = isbn, "book"  # the edition at hand
                return info, f"isbn {isbn}"
    if isbns:
        reasons.append(f"no matching book for ISBN {', '.join(isbns)}")

    # Trusts the file name rather than the text (an excerpt may not contain its book's
    # title), so the GUI marks these hits for checking.
    info = search_by_filename(stem)
    if info:
        return info, "file name search"

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


# ---------------------------------------------------------------- your own choices

ARXIV_ID_RE = re.compile(r"(?:arXiv:\s*)?(\d{4}\.\d{4,5}|[a-z\-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?", re.I)


def parse_identifier(text):
    """("doi" | "arXiv" | "bibcode" | "isbn", key) for a typed DOI, arXiv ID, ADS bibcode or
    ISBN, also as a doi.org or arxiv.org link. Raises ValueError for anything else."""
    s = text.strip()
    m = re.search(r"arxiv\.org/(?:abs|pdf)/(.+?)(?:\.pdf)?/?$", s, re.I)
    if m:
        s = m.group(1)
    m = DOI_RE.search(s)
    if m:
        return "doi", m.group(1).rstrip(".,;")
    m = ARXIV_ID_RE.fullmatch(s)
    if m:
        return "arXiv", m.group(1)
    if BIBCODE_RE.fullmatch(s):
        return "bibcode", s
    isbn = isbn13(re.sub(r"^e?ISBN(?:-?1[03])?:?", "", s, flags=re.I))
    if isbn:
        return "isbn", isbn
    raise ValueError("That doesn't look like a DOI, ISBN, arXiv ID or ADS bibcode.")


def identify_by_id(identifier, path):
    """Look up an identifier the user typed for a PDF. Returns (Meta, how, warning); the
    warning says if the record's title isn't in the PDF (the user may still be right)."""
    kind, key = parse_identifier(identifier)
    try:
        pdf_meta, text = read_pdf(path)
    except Exception:
        pdf_meta, text = {}, ""
    if kind == "doi":
        info = lookup_doi(key)
        if info:
            info.doi = info.doi or key
    elif kind == "arXiv":
        info = arxiv_record(key)
    elif kind == "isbn":
        info = crossref_by_isbn(key) or openlibrary_by_isbn(key)
        info = info and replace(info, isbn=key, kind="book")
    else:
        found = ([ads_by_bibcode(key)] if ADS_TOKEN else []) + list(openalex_by_bibcode(key))
        found = [f for f in found if f]
        info = next((f for f in found if title_plausible(f.title, text)), found[0] if found else None)
    if not info:
        raise ValueError(f"Nothing found for {'ISBN' if kind == 'isbn' else kind} {key}.")
    warning = ""
    if len(letters(text)) > 200 and not title_plausible(info.title, text):  # scans have no text to compare
        warning = f"The title “{info.title[:60]}” doesn't appear in the PDF's text, so check it's the right one."
    return add_details(excerpt_of(info, pdf_meta, text), pdf_meta), f"entered {kind} {key}", warning


def alternatives(info, how, path):
    """Other records a PDF could be, for the review dialog: other editions of a book, and
    other hits of a fuzzy search. Returns [(Meta, how, note)], not including `info` itself."""
    pdf_meta, text = read_pdf(path)
    options = book_editions(info) if info.kind == "book" else []
    if how.startswith(("title search", "text search", "file name search")):
        hits = search_candidates(pdf_meta, text, path.stem)
        # For a book only other books count: articles with its title and author are reviews
        # of it, and chapters are parts of it.
        options += [h for h in hits if info.kind != "book" or h[0].kind == "book"]
        if info.kind == "chapter":  # an excerpt: only other records of this chapter of this book
            options = [h for h in options if h[0].kind == "chapter" and same_title(h[0].journal, info.journal)]
    # One option per edition (year and publisher; hardback, paperback and e-book ISBNs are the
    # same edition), preferring a record with a DOI (for citations), then one with an ISBN.
    options.sort(key=lambda o: (not o[0].doi, not o[0].isbn, not o[2]))
    seen_ids, seen_editions, unique = {x for x in (info.doi.lower(), info.isbn) if x}, {edition_key(info)}, []
    for option in options:
        ids = {x for x in (option[0].doi.lower(), option[0].isbn) if x}
        if ids & seen_ids or edition_key(option[0]) in seen_editions:
            continue
        seen_ids |= ids
        seen_editions.add(edition_key(option[0]))
        unique.append(option)
    return sorted(unique, key=lambda o: o[0].year or 0)


def edition_key(meta):
    """Same year and publisher: "Hutchinson" and "Hutchinson's Scientific..." count as one."""
    return f"{letters(meta.title)[:30]}|{meta.year}|{letters(meta.publisher)[:8]}"


def same_title(a, b):
    a, b = letters(a), letters(b)
    return len(a) >= 8 and len(b) >= 8 and (a.startswith(b) or b.startswith(a))


def book_editions(book):
    """Crossref's records of the book (they have DOIs), then Open Library's editions."""
    if not book.authors:
        return []
    first = book.authors[0][1]
    options = []
    r = api_get("https://api.crossref.org/works", timeout=30,
                params={"query.bibliographic": book.title, "query.author": first, "rows": 10,
                        "filter": ",".join(f"type:{t}" for t in BOOK_TYPES)})
    if r.status_code == 200:
        for item in r.json()["message"]["items"]:
            m = parse_csl(item)
            if m and same_title(m.title, book.title) and letters(m.surname) == letters(book.surname):
                options.append((m, f"doi {m.doi}", ""))
    r = api_get("https://openlibrary.org/search.json", timeout=30,
                params={"title": book.title, "author": first, "limit": 3, "fields": "key,title"})
    works = [d["key"] for d in (r.json().get("docs") or []) if same_title(d.get("title", ""), book.title)] \
        if r.status_code == 200 else []
    for work in works[:2]:
        r = api_get(f"https://openlibrary.org{work}/editions.json", params={"limit": 30}, timeout=30)
        if r.status_code != 200:
            continue
        for ed in r.json().get("entries") or []:
            m = re.search(r"\b(1[5-9]|20)\d\d\b", ed.get("publish_date") or "")
            if not m:
                continue
            isbn = next(filter(None, map(isbn13, (ed.get("isbn_13") or []) + (ed.get("isbn_10") or []))), "")
            meta = Meta(book.authors, int(m.group(0)), ed.get("title") or book.title, isbn=isbn,
                        publisher=(ed.get("publishers") or [""])[0], kind="book")
            options.append((meta, f"isbn {isbn}" if isbn else "edition", ed.get("edition_name") or ""))
    for meta, _, _ in options:  # same book: keep what we know about its contents
        meta.tags, meta.abstract = meta.tags or list(book.tags), meta.abstract or book.abstract
    return sorted(options, key=lambda o: o[0].year or 0)


def search_candidates(pdf_meta, text, stem):
    """Crossref's top hits for the metadata title, the file name's title and page 1, keeping
    those with some link to the PDF (title words in the text, or an author in either)."""
    queries = [pdf_meta.get("title", "").strip()]
    parts = filename_parts(stem)
    queries += [f"{parts[1]} {parts[0]}" if parts else "", " ".join(text.split())[:300]]
    options = []
    for query in [q for q in queries if len(q) > 15][:2]:
        r = api_get("https://api.crossref.org/works", params={"query.bibliographic": query, "rows": 5}, timeout=30)
        if r.status_code != 200:
            continue
        for item in r.json()["message"]["items"]:
            m = parse_csl(item)
            if m and (title_plausible(m.title, text, threshold=0.5) or named_in(m, stem)
                      or author_in(m, text)):
                options.append((m, f"doi {m.doi}", "other search hit"))
    return options


def finish_choice(meta, path):
    """Complete a record picked in the review dialog: chapter (for excerpts), abstract, tags."""
    try:
        pdf_meta, text = read_pdf(path)
    except Exception:
        pdf_meta, text = {}, ""
    return add_details(excerpt_of(meta, pdf_meta, text), pdf_meta)


# ---------------------------------------------------------------- naming

DEFAULT_PATTERN = "{author}{year}"
PLACEHOLDERS = {
    "author": "first author's surname, e.g. Fermi",
    "authors": "Fermi, FermiDirac (two authors) or FermiEtAl (three or more)",
    "year": "year of publication, e.g. 1926",
    "title": "first three main words of the title, e.g. QuantelungIdealenEinatomigen",
    "journal": "journal abbreviation, e.g. ZPhysik",
    "part": "chapter of a book excerpt, e.g. Ch21 (added at the end if not in the pattern)",
}
WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"{d}{n}" for d in ("COM", "LPT") for n in range(1, 10)}
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
        "part": meta.part,
    }
    if meta.part and "{part}" not in pattern:
        pattern += "_{part}"
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
    name = re.sub(r"([ _.\-])[ _.\-]+", r"\1", name).strip(" _.-")[:150] or "Unknown"
    if name.upper() in WINDOWS_RESERVED:  # "Con.pdf" can't be created on Windows
        name += "_"
    return name


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
        with self.lock:  # the folder watcher may be adding entries meanwhile
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

    @property
    def same_paper_keys(self):
        return duplicate_keys(self.meta, self.hash)


def file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def duplicate_keys(meta, digest):
    """What makes two files the same paper: identical bytes, the same DOI (but not a whole
    book's, for an excerpt of it), or for books and records without a DOI, the same first
    author, year and title (copies of a book found via different records)."""
    keys = {f"sha:{digest}"} if digest else set()
    if not meta:
        return keys
    doi = "" if meta.part and meta.kind == "book" else meta.doi
    if doi:
        keys.add(f"doi:{doi.lower()}")
    if meta.title and (not doi or meta.kind in ("book", "chapter")):
        keys.add(f"work:{letters(meta.surname)}|{meta.year}|{letters(meta.title)}|{meta.part}")
    return keys


def duplicate_groups(entries):
    """entries: one set of duplicate_keys() per file. Returns groups of indices (2+ each)
    of files that share a key."""
    parent = list(range(len(entries)))

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    first_seen = {}
    for i, keys in enumerate(entries):
        for key in sorted(keys):
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


def plan_renames(pdfs, use_ocr=True, pattern=DEFAULT_PATTERN, cache=None, cached_first=False, started=None,
                 deep_ocr=False):
    """Identify each PDF and yield a Plan for it, without renaming anything.
    Targets are reserved so that two papers never get the same new name.
    cached_first: yield the PDFs found in the cache before looking up the others.
    started(pdf) is called before each (slow) lookup that isn't cached."""
    def digest_of(pdf):
        try:
            return file_hash(pdf)
        except OSError:
            return ""

    digests = {pdf: digest_of(pdf) for pdf in pdfs} if cached_first and cache else {}
    if digests:
        known = [pdf for pdf in pdfs if digests[pdf] and cache.get(digests[pdf])]
        pdfs = known + [pdf for pdf in pdfs if pdf not in set(known)]
    taken = set()
    for pdf in pdfs:
        digest = digests[pdf] if pdf in digests else digest_of(pdf)
        hit = cache.get(digest) if cache and digest else None
        if hit:
            (info, how), cached = hit, True
        else:
            cached = False
            if started:
                started(pdf)
            try:
                info, how = identify(pdf, use_ocr=use_ocr, deep_ocr=deep_ocr)
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
    """Store title, authors, DOI and tags in the PDF's own metadata, so reference managers
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
        if meta.tags:
            info["keywords"] = ", ".join(meta.tags)
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
    r = api_get(f"https://doi.org/{doi_path(doi)}", headers={"Accept": CITATION_FORMATS[fmt]}, timeout=30)
    if r.status_code != 200 or not r.content.strip() or r.content.lstrip()[:1] == b"<":
        return None  # an HTML page means the agency doesn't offer this format
    text = r.content.decode("utf-8", "replace").strip()
    if cache:
        cache.put_citation(fmt, doi, text)
    return text


def make_citation(meta, fmt):
    """BibTeX or RIS built from our own record, for papers and books without a DOI (or whose
    registration agency doesn't offer the format). tailor_citation() adds key, file and extras."""
    title = re.sub(r"<[^>]+>", "", meta.title)
    people = [f"{family}, {given}" if given else family for given, family in meta.authors]
    kind = meta.kind or ("article" if meta.journal else "")
    container = meta.journal if kind in ("article", "chapter") else ""
    if fmt == "bibtex":
        entry = {"book": "book", "chapter": "incollection", "article": "article"}.get(kind, "misc")
        fields = [("author", " and ".join(people)), ("title", title), ("year", str(meta.year or "")),
                  ("booktitle" if kind == "chapter" else "journal", container), ("publisher", meta.publisher),
                  ("isbn", meta.isbn), ("doi", meta.doi)]
        return f"@{entry}{{paper,\n" + ",\n".join(f"  {k} = {{{bib_value(v)}}}" for k, v in fields if v) + "\n}"
    lines = [f"TY  - {dict(book='BOOK', chapter='CHAP', article='JOUR').get(kind, 'GEN')}"]
    lines += [f"AU  - {p}" for p in people] + [f"TI  - {title}", f"PY  - {meta.year or ''}"]
    for tag, value in (("T2" if kind == "chapter" else "JO", container), ("PB", meta.publisher),
                       ("SN", meta.isbn), ("DO", meta.doi)):
        if value:
            lines.append(f"{tag}  - {value}")
    return "\n".join(lines + ["ER  - "])


def bib_value(value):
    """Keep braces balanced and escape the characters that break LaTeX in titles."""
    value = " ".join(str(value).replace("{", "").replace("}", "").split())
    return re.sub(r"(?<!\\)([&%#])", r"\\\1", value)


def tailor_citation(text, fmt, key, pdf, meta=None):
    """Use our file name as the citation key and link the PDF (Zotero/JabRef pick that up).
    Also adds the keywords and abstract we know of, if the citation lacks them."""
    key = re.sub(r"[^A-Za-z0-9_:\-]", "", key) or "paper"
    tags, abstract = (meta.tags, meta.abstract) if meta else ([], "")
    if fmt == "bibtex":
        text = re.sub(r"^(\s*@\w+\s*\{)[^,]*,", rf"\g<1>{key},", text, count=1)
        extra = []
        if tags and not re.search(r"^\s*keywords\s*=", text, re.M | re.I):
            extra.append(("keywords", ", ".join(tags)))
        if abstract and not re.search(r"^\s*abstract\s*=", text, re.M | re.I):
            extra.append(("abstract", abstract))
        if pdf:
            extra.append(("file", Path(pdf).as_posix()))
        body = text.rstrip()
        if extra and body.endswith("}"):
            text = body[:-1].rstrip().rstrip(",") + "".join(
                f",\n  {k} = {{{v if k == 'file' else bib_value(v)}}}" for k, v in extra) + "\n}"
        return text
    lines = [l for l in text.splitlines() if not l.startswith(("ID  -", "L1  -"))]
    lines.insert(1, f"ID  - {key}")
    end = next((i for i, l in enumerate(lines) if l.startswith("ER  -")), len(lines))
    extra = []
    if tags and not any(l.startswith("KW  -") for l in lines):
        extra += [f"KW  - {t}" for t in tags]
    if abstract and not any(l.startswith(("AB  -", "N2  -")) for l in lines):
        extra.append(f"AB  - {abstract}")
    if pdf:
        extra.append(f"L1  - {Path(pdf).as_posix()}")
    lines[end:end] = extra
    return "\n".join(lines)


def export_citations(entries, fmt, out_path, cache=None, progress=None, cancelled=lambda: False):
    """entries: (Meta, key, pdf path) per paper. Writes a .bib or .ris file and returns
    (number written, DOIs whose official citation couldn't be fetched). Those, and papers
    without a DOI, get a citation built from our own record (make_citation)."""
    done, failed, seen = [], [], set()
    for n, (meta, key, pdf) in enumerate(entries):
        if cancelled():
            break
        if progress:
            progress(n, len(entries), key)
        ident = meta.doi.lower() if meta.doi else f"{letters(meta.title)}:{meta.year}"
        if ident in seen:
            continue
        seen.add(ident)
        text = None
        if meta.doi:
            try:
                text = fetch_citation(meta.doi, fmt, cache)
            except requests.RequestException:
                pass
            if not text:
                failed.append(meta.doi)
        done.append(tailor_citation(text or make_citation(meta, fmt), fmt, key, pdf, meta))
    if cache:
        cache.save()
    Path(out_path).write_text("\n\n".join(done) + "\n", encoding="utf-8")
    return len(done), failed


# ---------------------------------------------------------------- watching a folder

WATCH_INTERVAL = 10  # seconds between looks at the watched folder
WATCH_STABLE_SECONDS = 5  # a PDF must be unchanged this long, so downloads can finish
WATCH_RETRY_SECONDS = 300  # wait this long after a network error
WATCH_LOG_SIZE = 300  # activity entries kept
FUZZY_PREFIXES = ("title search", "text search", "file name search")  # "Check" in the window


@dataclass
class WatchEvent:
    when: float
    source: str  # the PDF's name in the watched folder
    result: str  # "filed", "duplicate", "check", "not found", "error"; or "trashed"/"dismissed" by you
    target: str = ""  # full path: where it was filed, or the file it duplicates
    detail: str = ""  # how it was identified, or why not
    id: str = ""  # set when logged; lets you act on the entry later (Watcher.resolve)
    by_you: bool = False  # the result of a decision you made about it


class Watcher:
    """Files new PDFs from a watched folder: identifies them, renames them with the pattern
    and moves them into the output folder. Each PDF is looked at once (watch.json, next to the
    cache, remembers them). Duplicates of papers already in the output folder, PDFs that weren't
    found, and fuzzy matches (unless file_fuzzy) stay where they are and are reported."""

    def __init__(self, watch_dir, output_dir, pattern=DEFAULT_PATTERN, cache=None, use_ocr=True,
                 deep_ocr=False, file_fuzzy=False, write_meta=False, state_path=None):
        self.watch_dir, self.output_dir = Path(watch_dir), Path(output_dir)
        if path_key(self.watch_dir.resolve()) == path_key(self.output_dir.resolve()):
            raise ValueError("The watched folder and the output folder must be different.")
        self.pattern, self.cache, self.use_ocr, self.deep_ocr = pattern, cache, use_ocr, deep_ocr
        self.file_fuzzy, self.write_meta = file_fuzzy, write_meta
        self.state_path = Path(state_path) if state_path else default_cache_path().with_name("watch.json")
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        self.done = data.get("done", {})      # path -> [size, mtime] of PDFs already looked at
        self.seen = data.get("seen", {})      # sha256 -> result, for PDFs that weren't filed
        self.hashes = data.get("hashes", {})  # path -> [size, mtime, sha256], to avoid re-hashing
        self.log = data.get("log", [])        # recent WatchEvents, as dicts
        self.index, self.unidentified = {}, []  # the output folder: duplicate key -> path; unknown files
        self.retry_after = {}  # path -> time, after network errors
        self.problem = ""
        self.forget_requested = False
        self.lock = threading.Lock()  # poll() runs in a thread; your decisions come from another

    def forget(self):
        """Look at every PDF in the watched folder again (from the next poll on)."""
        self.forget_requested = True

    def poll(self, cancelled=lambda: False):
        """Look at the watched folder once; returns the WatchEvents of the PDFs handled."""
        if self.forget_requested:  # done here, so it can't clash with a poll in another thread
            self.done, self.seen, self.forget_requested = {}, {}, False
        now = time.time()
        try:
            pdfs = list_pdfs(self.watch_dir)
        except OSError as e:
            problem = f"Can't read {self.watch_dir}: {e.strerror or e}"
            events = [] if problem == self.problem else [self.note(WatchEvent(now, "", "error", detail=problem))]
            self.problem = problem
            return events
        self.problem = ""
        todo = []
        for pdf in pdfs:
            try:
                st = pdf.stat()
            except OSError:
                continue
            sig = [st.st_size, st.st_mtime]
            if (self.done.get(str(pdf)) != sig and st.st_size and now - st.st_mtime >= WATCH_STABLE_SECONDS
                    and self.retry_after.get(str(pdf), 0) <= now):
                todo.append((pdf, sig))
        if not todo:
            return []
        events = []
        with self.lock:
            self.build_index()
            for pdf, sig in todo:
                if cancelled():
                    break
                event = self.process(pdf, sig)
                if event:
                    events.append(event)
            self.save()
        if self.cache:
            self.cache.save()
        return events

    def process(self, pdf, sig):
        try:
            digest = self.hash_of(pdf, sig)
        except OSError:
            return None  # gone or locked; try again next time
        hit = self.cache.get(digest) if self.cache else None
        if hit:
            info, how = hit  # known already (e.g. scanned in the window): no need to look it up
        elif digest in self.seen:
            self.done[str(pdf)] = sig  # the same file under another name, already looked at once
            return None
        else:
            plan = next(plan_renames([pdf], self.use_ocr, self.pattern, self.cache, deep_ocr=self.deep_ocr))
            info, how = plan.meta, plan.detail
            if not info and how.startswith("network error"):
                self.retry_after[str(pdf)] = time.time() + WATCH_RETRY_SECONDS
                return None
        if not info:
            return self.finish(pdf, sig, digest, "not found", detail=how)
        if how.startswith(FUZZY_PREFIXES) and not self.file_fuzzy:
            return self.finish(pdf, sig, digest, "check",
                               detail=f"maybe {make_name(info, self.pattern)}.pdf (found via {how})")
        same = self.find_duplicate(info, digest)
        if same:
            return self.finish(pdf, sig, digest, "duplicate", target=str(same), detail=how)
        try:
            target = self.move_to_output(pdf, info, how, digest)
        except OSError as e:
            return self.finish(pdf, sig, digest, "error", detail=f"couldn't move it: {e.strerror or e}")
        return self.note(WatchEvent(time.time(), pdf.name, "filed", str(target), how))

    def move_to_output(self, pdf, info, how, digest):
        """Rename and move a PDF into the output folder; returns its new path."""
        target = unique_target(self.output_dir, make_name(info, self.pattern), set())
        self.output_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(pdf), str(target))
        self.done.pop(str(pdf), None)
        self.hashes.pop(str(pdf), None)
        self.add_to_index(target, info, digest)
        if self.write_meta:
            try:
                write_metadata(target, info)
                if self.cache:
                    self.cache.put(file_hash(target), info, how)  # new contents, same paper
            except Exception:
                pass  # the details are a bonus; the file is filed either way
        return target

    def finish(self, pdf, sig, digest, result, target="", detail=""):
        """Remember a PDF that stays in the watched folder, so it isn't looked at again."""
        self.done[str(pdf)] = sig
        if result in ("not found", "check"):
            self.seen[digest] = result
        return self.note(WatchEvent(time.time(), pdf.name, result, target, detail))

    def find_duplicate(self, info, digest):
        """A file in the output folder that is the same paper, or None."""
        keys = duplicate_keys(info, digest)
        same = next((self.index[k] for k in sorted(keys) if k in self.index), None)
        if same:
            return same
        # Output files we know nothing about (not scanned by this app) are identified only if
        # they have the name this one would get, the usual sign of a copy.
        stem = make_name(info, self.pattern)
        for other in [p for p in self.unidentified if already_named(p, stem)]:
            self.unidentified.remove(other)
            plan = next(plan_renames([other], self.use_ocr, self.pattern, self.cache, deep_ocr=self.deep_ocr))
            if not plan.meta:
                self.seen[plan.hash] = "not found"
                continue
            self.add_to_index(other, plan.meta, plan.hash)
            if keys & duplicate_keys(plan.meta, plan.hash):
                return other
        return None

    def build_index(self):
        """Duplicate keys of the PDFs in the output folder (and its subfolders), from the cache."""
        self.index, self.unidentified = {}, []
        try:
            paths = list_pdfs(self.output_dir, recursive=True) if self.output_dir.is_dir() else []
        except OSError:
            paths = []
        watched = path_key(self.watch_dir.resolve())
        for path in paths:
            if path_key(path.parent.resolve()) == watched:
                continue  # the watched folder may sit inside the output folder: its PDFs aren't filed yet
            try:
                digest = self.hash_of(path)
            except OSError:
                continue
            hit = self.cache.get(digest) if self.cache else None
            self.add_to_index(path, hit[0] if hit else None, digest)
            if not hit and digest not in self.seen:
                self.unidentified.append(path)

    def add_to_index(self, path, info, digest):
        for key in duplicate_keys(info, digest):
            self.index.setdefault(key, path)

    def hash_of(self, path, sig=None):
        if sig is None:
            st = path.stat()
            sig = [st.st_size, st.st_mtime]
        known = self.hashes.get(str(path))
        if known and known[:2] == sig:
            return known[2]
        digest = file_hash(path)
        self.hashes[str(path)] = sig + [digest]
        return digest

    def note(self, event):
        event.id = event.id or f"{event.when:.6f}:{event.source}"
        self.log = (self.log + [asdict(event)])[-WATCH_LOG_SIZE:]
        return event

    # Decisions about PDFs that were left in the watched folder (checks, duplicates, not found).

    def entry(self, entry_id):
        for e in self.log:
            if e.get("id", f"{e['when']:.6f}:{e['source']}") == entry_id:
                return e
        raise ValueError("That entry is no longer in the activity list.")

    def resolve(self, entry_id, info=None, how="", force=False):
        """File a PDF you decided about: with `info` (a record you entered or picked), else the one
        found (confirming a fuzzy match). A duplicate is reported again unless `force`. Updates and
        returns the log entry."""
        with self.lock:
            entry = self.entry(entry_id)
            pdf = self.watch_dir / entry["source"]
            if not pdf.exists():
                raise ValueError(f"{entry['source']} is no longer in {self.watch_dir.name}.")
            digest = self.hash_of(pdf)
            if info is None:
                hit = self.cache.get(digest) if self.cache else None
                if not hit:
                    raise ValueError(f"{entry['source']} hasn't been identified; identify it by its ID first.")
                info, how = hit
                core = f"doi {info.doi}" if info.doi else f"isbn {info.isbn}" if info.isbn else "edition"
                how = how if how.startswith(("entered ", "chosen ")) else f"chosen {core}"
            if self.cache:
                self.cache.put(digest, info, how)  # remember your decision for later scans
            self.build_index()
            same = None if force else self.find_duplicate(info, digest)
            if same:
                entry.update(result="duplicate", target=str(same), detail=how, by_you=False)
            else:
                target = self.move_to_output(pdf, info, how, digest)
                entry.update(result="filed", target=str(target), detail=how, by_you=True)
            self.save()
            if self.cache:
                self.cache.save()
            return entry

    def mark(self, entry_id, result, detail=""):
        """Record what you did with a PDF outside the watcher, e.g. "trashed"."""
        with self.lock:
            self.entry(entry_id).update(result=result, detail=detail, by_you=True)
            self.save()

    def dismiss(self, entry_ids, undo=False):
        """Stop asking about PDFs you want left as they are (e.g. not papers at all), or ask again."""
        with self.lock:
            for entry_id in entry_ids:
                e = self.entry(entry_id)
                if undo and e["result"] == "dismissed":
                    e.update(result=e.pop("was", "not found"), by_you=False)
                elif not undo and not e.get("by_you"):
                    e.update(was=e["result"], result="dismissed", by_you=True)
            self.save()

    def save(self):
        # Forget files that are gone, so the state doesn't grow forever.
        self.done = {p: v for p, v in self.done.items() if os.path.exists(p)}
        self.hashes = {p: v for p, v in self.hashes.items() if os.path.exists(p)}
        data = {"version": 1, "done": self.done, "seen": self.seen, "hashes": self.hashes, "log": self.log}
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, self.state_path)
        except OSError:
            pass


def load_watch_log(state_path=None):
    """The watcher's recent activity (dicts of WatchEvent fields), also when it isn't running."""
    path = Path(state_path) if state_path else default_cache_path().with_name("watch.json")
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("log", [])
    except (OSError, ValueError):
        return []


def forget_watched(state_path=None):
    """Like Watcher.forget(), for when no watcher is running. Keeps the activity log."""
    path = Path(state_path) if state_path else default_cache_path().with_name("watch.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data["done"], data["seen"] = {}, {}
        path.write_text(json.dumps(data), encoding="utf-8")
    except (OSError, ValueError):
        pass


def describe_event(e):
    """One line for the command line (and the window's banner)."""
    target = Path(e.target).name if e.target else ""
    return {"filed": f"Filed {e.source} as {target}",
            "duplicate": f"Didn't file {e.source}: it's a duplicate of {target}",
            "check": f"Left {e.source} for you to check: {e.detail}",
            "not found": f"Left {e.source}: couldn't identify it",
            }.get(e.result, f"Problem with {e.source or 'the watched folder'}: {e.detail}")


# ---------------------------------------------------------------- command line

def watch_folder(args, cache):
    try:
        watcher = Watcher(args.folder, args.output, args.pattern, cache, use_ocr=not args.no_ocr,
                          deep_ocr=args.deep_ocr, file_fuzzy=args.file_fuzzy, write_meta=args.write_metadata)
    except ValueError as e:
        print(f"error: {e}")
        return 2
    print(f"Watching {args.folder}; identified PDFs are renamed and moved to {args.output}.\n"
          "PDFs already looked at are skipped. Press Ctrl+C to stop.")
    try:
        while True:
            for event in watcher.poll():
                stamp = time.strftime("%H:%M:%S", time.localtime(event.when))
                print(f"{stamp}  {describe_event(event)}")
            time.sleep(WATCH_INTERVAL)
    except KeyboardInterrupt:
        print("Stopped watching.")
    finally:
        watcher.save()
        if cache:
            cache.save()
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", type=Path)
    ap.add_argument("--apply", action="store_true", help="actually rename (default is a dry run)")
    ap.add_argument("-r", "--recursive", action="store_true", help="include PDFs in subfolders")
    ap.add_argument("--pattern", default=DEFAULT_PATTERN, help=f"naming pattern (default {DEFAULT_PATTERN})")
    ap.add_argument("--no-ocr", action="store_true", help="never OCR (faster, but scans get skipped)")
    ap.add_argument("--deep-ocr", action="store_true",
                    help=f"if needed, OCR the first {DEEP_OCR_PAGES} pages of scanned books, not {OCR_PAGES} (slow)")
    ap.add_argument("--email", help="your email, for faster Crossref/OpenAlex service "
                                    "(set ADS_TOKEN in the environment for ADS lookups)")
    ap.add_argument("--no-cache", action="store_true", help="ignore and don't update the result cache")
    ap.add_argument("--write-metadata", action="store_true",
                    help="with --apply: also store title, authors, DOI and tags inside each renamed PDF")
    ap.add_argument("--bib", type=Path, metavar="FILE", help="export BibTeX for all identified papers")
    ap.add_argument("--ris", type=Path, metavar="FILE", help="export RIS for all identified papers")
    watch = ap.add_argument_group("watching a folder")
    watch.add_argument("--watch", action="store_true",
                       help="keep watching the folder: file new PDFs into --output as they arrive (Ctrl+C stops)")
    watch.add_argument("--output", type=Path, metavar="DIR", help="with --watch: where identified PDFs are moved")
    watch.add_argument("--file-fuzzy", action="store_true",
                       help="with --watch: also file matches from title/text/file name searches")
    args = ap.parse_args()
    if args.watch and not args.output:
        ap.error("--watch needs --output DIR")
    try:
        check_pattern(args.pattern)
    except ValueError as e:
        ap.error(f"--pattern: {e}")

    if args.email:
        set_contact(args.email)
    if not pymupdf:
        print("note: PyMuPDF not installed, using pypdf (less reliable). pip install pymupdf\n")

    cache = None if args.no_cache else Cache()
    if args.watch:
        return watch_folder(args, cache)
    pdfs = list_pdfs(args.folder, args.recursive)
    renamed = failed = from_cache = 0
    seen = []  # (path after this run, plan) for the duplicate report and exports
    try:
        for plan in plan_renames(pdfs, use_ocr=not args.no_ocr, pattern=args.pattern, cache=cache,
                                 deep_ocr=args.deep_ocr):
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
                    try:
                        plan.pdf.rename(plan.target)
                    except OSError as e:
                        print(f"        (couldn't rename it: {e.strerror or e})")
                        failed += 1
                        seen.append((final, plan))
                        continue
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

    groups = duplicate_groups([plan.same_paper_keys for _, plan in seen])
    if groups:
        print("\nPossible duplicates (same paper or identical files):")
        for g in groups:
            doi = next((seen[i][1].doi for i in g if seen[i][1].doi), "")
            names = ", ".join(str(seen[i][0].relative_to(args.folder)) for i in g)
            print(f"  {names}" + (f"  [doi {doi}]" if doi else "  [identical]"))

    for fmt, out in (("bibtex", args.bib), ("ris", args.ris)):
        if out:
            entries = [(plan.meta, path.stem, path.resolve()) for path, plan in seen if plan.meta]
            written, missing = export_citations(entries, fmt, out, cache)
            print(f"\nWrote {written} {fmt.upper() if fmt == 'ris' else 'BibTeX'} entries to {out}"
                  + (f"; built {len(missing)} from the looked-up details, as doi.org had none: "
                     f"{', '.join(missing)}" if missing else "."))

    verb = "renamed" if args.apply else "to rename"
    print(f"\n{renamed} {verb}, {failed} skipped, {len(pdfs)} total"
          + (f" ({from_cache} from cache)." if from_cache else "."))
    if not args.apply and renamed:
        print("Dry run only. Re-run with --apply to rename.")


if __name__ == "__main__":
    sys.exit(main())
