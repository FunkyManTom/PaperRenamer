# Paper Renamer

Renames PDFs of academic papers to something readable, e.g. `randomname.pdf` → `Fermi1926.pdf`.
It identifies each paper online, so names come from the real publication record rather than guesses.

## How it identifies a paper

For each PDF, in this order:

1. **DOI**: from the file name (publisher download names like `978-3-030-13876-9.pdf`,
   `s00220-019-03456-x.pdf`, `1-s2.0-S…-main.pdf`), the PDF's metadata, or the text of its first pages
   (also old Elsevier `PII:` numbers). The DOI is looked up via Crossref, or doi.org for other registries.
2. **arXiv ID**, looked up via the arXiv API.
3. **Title search** on Crossref, using the title stored in the PDF.
4. **Text search** on Crossref, using the start of page 1. A match only counts if its title and first
   author actually appear in the PDF.

Scanned PDFs without a text layer are run through OCR first (optional, needs Tesseract).
Every match is checked against the PDF's text, so a DOI from someone else's paper (e.g. a citation)
isn't used by mistake.

## Features

- **Preview before anything changes.** Untick files, or type your own name for files that weren't found.
- **Naming patterns**, e.g. `{author}{year}` → `Fermi1926`, `{authors}_{year}_{title}` →
  `LeCunEtAl_2015_DeepLearning`. Placeholders: `{author}`, `{authors}`, `{year}`, `{title}`, `{journal}`.
- **Undo** for renames.
- **Duplicates**: finds copies of the same paper (same DOI or identical file) and moves the extras to the
  Recycle Bin / Trash. The copy with annotations is suggested for keeping.
- **Citation export** to BibTeX or RIS. Keys match the file names and the PDFs are linked, so Zotero and
  JabRef can attach them on import.
- **Write title, authors and DOI into the PDFs** (optional).
- **Subfolders** (optional). Files are renamed where they are; nothing is moved.
- **Cache**: results are remembered by file content, so scanning a folder again is fast.

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
```

Run `python rename_papers.py --help` for all options.

## Ready-made builds

Pre-built programs (no Python needed) will in the future be located within in the [`build`](build) folder. OCR still needs Tesseract
installed separately.

To build the Windows program yourself:

```
pip install pyinstaller -r requirements.txt
pyinstaller --onefile --windowed --name "Paper Renamer" ^
    --exclude-module pymupdf.layout --exclude-module pymupdf4llm ^
    --exclude-module onnxruntime --exclude-module networkx --exclude-module numpy ^
    paper_renamer_gui.py
```

The excluded modules are optional extras of PyMuPDF that this tool doesn't use; leaving them out keeps
the program smaller. The result is `dist/Paper Renamer.exe`.

## Notes

- Lookups use the free [Crossref](https://www.crossref.org/) and arXiv APIs, so an internet connection is
  needed. Consider putting your email address in `HEADERS` at the top of `rename_papers.py`: Crossref
  serves requests with a contact address faster.
- Most papers published after about 2000 are identified via their DOI. Older scans, preprints and
  theses are harder to identify. Matches from title or text searches are marked **Check** in the
  window, so look at those before renaming.
