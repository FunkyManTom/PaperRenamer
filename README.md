# Paper Renamer

Renames PDFs of academic papers to something readable, e.g. `randomname.pdf` → `Fermi1926.pdf`.
It identifies each paper online, so names come from the real publication record rather than guesses.

<img width="2438" height="1853" alt="Screenshot_20261021_182241" src="https://github.com/user-attachments/assets/6c77dc38-4965-4e4b-95c4-48d3ea31d520" />

## How it identifies a paper

For each PDF, in this order:

1. **DOI**: from the file name (publisher download names like `978-3-030-13876-9.pdf`,
   `s00220-019-03456-x.pdf`, `1-s2.0-S…-main.pdf`), the PDF's metadata, or the text of its first pages
   (also old Elsevier `PII:` numbers). The DOI is looked up via Crossref, or doi.org for other registries.
2. **arXiv ID**, from the text or the file name (`2406.14694v1.pdf`, also old-style `0501430v2.pdf`, whose
   archive is worked out), looked up via DataCite or the arXiv API.
3. **ADS bibcode** (e.g. `1989A&A...216...44D`, stamped on ADS scans of old journals), looked up on
   NASA ADS if you've added an API token, otherwise via [OpenAlex](https://openalex.org/) by year, volume
   and page.
4. **ISBN**, for textbooks and other books without a DOI: from the file name or the copyright page,
   looked up via Crossref, then [Open Library](https://openlibrary.org/). The book's title and first
   author must appear in the PDF, so the ISBN of another book in the same series isn't used.
5. **File name search** for library-style names such as
   `Jean Dufay - Galactic Nebulae and Interstellar Matter (1968, Dover).pdf`: the book is looked up on
   Crossref, then Open Library. This finds old books without an ISBN, and excerpts whose text doesn't
   name the book. The match is based on the file name, not the text, so it's marked **Check**.
6. **Title search** on Crossref, using the title stored in the PDF.
7. **Text search** on Crossref, using the start of page 1. A match only counts if its title and first
   author actually appear in the PDF.

Scanned PDFs without a text layer are run through OCR first (optional, needs Tesseract). With
**More pages for scanned books** ticked (`--deep-ocr`), a scanned book that the first two pages don't
identify gets pages 3–6 read too, where the title and copyright page with the ISBN usually are.
Every match is checked against the PDF's text, so a DOI from someone else's paper (e.g. a citation)
isn't used by mistake.

A PDF identified as a book but under 60 pages long is treated as a chapter or excerpt: it gets the
chapter's own record if Crossref has one, and a name like `Draine2010_Ch21.pdf`.

## Features

- **Preview before anything changes.** Untick files, or type your own name for files that weren't found.
- **Identify by ID**: right-click a file and enter its DOI, ISBN, arXiv ID or ADS bibcode (doi.org and
  arxiv.org links work too).
- **Matches and editions**: for books and fuzzy matches, **Review matches & editions…** lists the other
  editions (e.g. a 1957 original and its 1968 Dover reprint) and other search hits, and you pick the one
  that fits. Right-click a file to do this for just that one.
- **Search** the results by title, author, tags or abstract. Hovering over **Found via** explains how a
  file was identified, and over **Details** shows its tags and abstract.
- **Naming patterns**, e.g. `{author}{year}` → `Fermi1926`, `{authors}_{year}_{title}` →
  `LeCunEtAl_2015_DeepLearning`. Placeholders: `{author}`, `{authors}`, `{year}`, `{title}`, `{journal}`,
  `{part}`.
- **Undo** for renames.
- **Duplicates**: finds copies of the same paper (same DOI or identical file) and moves the extras to the
  Recycle Bin / Trash. The copy with annotations is suggested for keeping.
- **Citation export** to BibTeX or RIS, with keywords and abstracts. Keys match the file names and the
  PDFs are linked, so Zotero and JabRef can attach them on import. Books and papers without a DOI get an
  entry built from the looked-up details.
- **Write title, authors, DOI and tags into the PDFs** (optional).
- **Subfolders** (optional). Files are renamed where they are; nothing is moved.
- **Watch a folder**, e.g. `~/Downloads`: new PDFs are identified, renamed and moved to an output folder
  such as `~/Documents/BooksAndPapers` while the window is open (**Watch folder…**, optionally started
  with the app). Each PDF is looked at only once. Before a PDF is moved, it's compared with what's already
  in the output folder (same DOI, same author/year/title, or an identical file): duplicates stay where they
  are and are flagged, as do PDFs that couldn't be identified and fuzzy matches (unless you choose to file
  those too). **Recent activity** lists what happened to each file, and **Decide…** lets you settle the
  ones waiting for you right there: file a fuzzy match, pick another match, identify a PDF by its ID,
  move a duplicate to the Trash or file it anyway. **Dismiss** stops asking about a PDF and leaves it as it
  is (e.g. a PDF that isn't a paper); "Dismiss all not found" does that for every unidentified one.
- **Themes**: System (follows your computer), Light, Dark, OLED dark and Catppuccin Latte / Mocha.
- **Cache**: results are remembered by file content, so scanning a folder again is fast. Remembered
  files are shown first, before the others are looked up.

## Installation

Requires Python 3.9 or newer.

```
pip install -r requirements.txt
```

For OCR of scanned papers, also install [Tesseract](https://github.com/tesseract-ocr/tesseract):

- Windows: the [UB Mannheim installer](https://github.com/UB-Mannheim/tesseract/wiki). The default install
  location is found automatically.
- macOS: `brew install tesseract`
- Debian/Ubuntu: `sudo apt install tesseract-ocr`

Without Tesseract everything else still works; scanned PDFs are just skipped.

## Usage

**Window:**

```
python paper_renamer_gui.py
```

Choose a folder (or drag one onto the window), click **Scan**, check the results, then click **Rename**.

**Command line** (a dry run unless you add `--apply`):

```
python rename_papers.py ~/papers                       # preview
python rename_papers.py ~/papers --apply               # rename
python rename_papers.py ~/papers -r --pattern "{author}_{year}_{title}"
python rename_papers.py ~/papers --apply --write-metadata --bib library.bib
python rename_papers.py ~/papers --deep-ocr --email you@example.com
```

For ADS lookups on the command line, set the `ADS_TOKEN` environment variable.

Watching a folder from the command line (until Ctrl+C):

```
python rename_papers.py ~/Downloads --watch --output ~/Documents/BooksAndPapers
```

Run `python rename_papers.py --help` for all options.

## Ready-made builds

Pre-built programs (no Python needed) will in the future be made available until then the program needs to either be run through Python or build from source. OCR still needs Tesseract
installed separately.

To build the Windows program yourself:

```
python -m pip install pyinstaller -r requirements.txt
pyinstaller --onefile --windowed --name "Paper Renamer" --icon icon.png --add-data icon.png:. ^
    --exclude-module pymupdf.layout --exclude-module pymupdf4llm ^
    --exclude-module onnxruntime --exclude-module networkx --exclude-module numpy ^
    paper_renamer_gui.py
```

The excluded modules are optional extras of PyMuPDF that this tool doesn't use; leaving them out keeps
the program smaller. `--icon icon.png` makes it the `.exe`'s icon (PyInstaller converts the PNG if
Pillow is installed: `pip install pillow`), and `--add-data` bundles it for the window. The result is `dist/Paper Renamer.exe`.

## Notes

- Lookups use the free [Crossref](https://www.crossref.org/), arXiv, OpenAlex and Open Library APIs, so an
  internet connection is needed. Consider adding your email under **API settings…** (or `--email`):
  Crossref and OpenAlex serve requests with a contact address faster. The same window takes an optional
  [NASA ADS token](https://ui.adsabs.harvard.edu/user/settings/token) for astronomy papers.
- Most papers published after about 2000 are identified via their DOI. Older scans, preprints and
  theses are harder to identify. Matches from title, text or file name searches are marked **Check** in
  the window, so look at those before renaming.
- This project was created with assistance of AI. Whilst I have read through the code and tested everything
  to the best of my ability, I can't guarantee that everything works as intended.
