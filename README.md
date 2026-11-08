# pdf2md

Converts PDF files to plain text (`.txt`) or Markdown (`.md`). One Python file, runs on Windows, Linux and macOS. Built for turning PDFs into notes for Obsidian and for feeding them to AI tools.

## Features

- **Formulas**: display equations (math fonts) are saved as PNG next to the other images and referenced like figures; their plain text follows as an HTML comment `<!-- formula: ... -->` for readers that cannot see images. Needs image export (default for `md`).
- **Markdown output**: headings (by font size), lists, bold, inline code and code blocks (monospace text), tables, links.
- **Works with Obsidian**: images and diagrams are saved as files and linked as wikilinks (`![[file.png]]`). Image folder is `IMG-<pdf name>/`.
- **Images and vector diagrams**: embedded images are saved as PNG. Diagrams drawn with vector graphics are rendered to PNG, and the text inside them is not repeated as text.
- **Scans**: pages that consist only of an image are saved as JPEG page images. There is no OCR, so no text is extracted from them.
- **Split into notes**: `--split` writes one note per chapter plus an index with wikilinks.
- **Clean text**: line breaks and hyphenation are resolved (a hyphen at a line end is kept for real compounds such as `post-compromise`, decided from the words of the document itself), two-column pages are read column by column, headers/footers and page numbers are removed, paragraphs that continue across a page break are rejoined, table-of-contents dot leaders are shortened.
- **Standards-friendly headings**: identifiers like `AC-2 ACCOUNT MANAGEMENT` and section numbers like `3.1 Title` become headings.
- **Review page**: `--review` writes `<name>.review.html`, a single self-contained viewer. Page list on the left (dots show differences), the PDF page next to the converted Markdown rendered like Obsidian's reading view (headings, tables, images). `Diff` switches the right side to an inline word diff (red = missing, blue = extra, lilac = moved, amber = re-joined). Dark/light theme, "differences only" filter, keys `n`/`p` (next/previous difference) and `d` (toggle diff).
- **Batch mode**: whole folders (`-r` for recursive) with a summary table.

## Installation

```bash
pip install pymupdf
```

`pypdf` is used as a plain-text fallback if PyMuPDF is missing (no Markdown, no images):

```bash
pip install pypdf
```

Python 3.8 or newer.

## Usage

```bash
python3 pdf2md.py document.pdf                  # -> document.txt
python3 pdf2md.py document.pdf -f md            # -> document.md + IMG-document/
python3 pdf2md.py folder -r -f md -o out        # all PDFs in a folder, recursively
python3 pdf2md.py document.pdf -p 1-3,7         # selected pages only
python3 pdf2md.py book.pdf -f md --split 1      # one note per chapter in book/
python3 pdf2md.py folder -f md --image-dir ~/Vault/Attachments
```

### Options

| Option | Meaning |
| --- | --- |
| `-f txt\|md` | Output format (default `txt`) |
| `-o PATH` | Output file or folder |
| `-p PAGES` | Pages, e.g. `1-3,7` |
| `-r` | Search folders recursively |
| `--password PW` | Password for encrypted PDFs |
| `--split 1\|2\|3` | With `-f md`: one note per chapter (split at headings up to this level) in a folder `<name>/` plus `000 Index.md` |
| `--image-dir DIR` | Shared image folder (e.g. your Obsidian attachments folder) instead of `IMG-<name>/` |
| `--md-links` | Standard Markdown image links instead of Obsidian wikilinks |
| `--no-images` | Do not save images or diagrams |
| `--no-tables` | No Markdown tables, running text only |
| `--no-columns` | No column detection, keep the PDF order |
| `--keep-headers` | Keep recurring headers, footers and page numbers |
| `--keep-linebreaks` | Keep the PDF's line breaks and hyphenation |
| `--page-markers` | Page separators (`<!-- Page N -->` in Markdown, `--- Page N ---` in text) |
| `--review` | Also write `<name>.review.html` (PDF page vs. converted text, differences marked). Up to 200 pages the page images are embedded (single file); above that they are written as JPEGs to `<name>.review/` next to the HTML, which then needs that folder |
| `--overwrite` | Overwrite existing output |

Existing output is never overwritten without `--overwrite`.

## Output layout

```
Vault/
  document.md                      # text, images linked as ![[document_s003_01.png]]
  IMG-document/
    document_s003_01.png           # embedded image, page 3
    document_s013_fig01.png        # rendered vector diagram, page 13
    document_s042_page.jpg         # image-only page (scan)
```

File names contain the PDF name, so wikilinks stay unique across the whole vault.
With `--split`, the notes are in `document/` (`000 Index.md`, `001 …md`, …); the images stay in `IMG-document/`.

## Limits

- No OCR. Scanned pages are kept as images only.
- Tables without ruling lines (booktabs style, e.g. papers) are rebuilt from the word positions; stacked headers, centred cells and cells spanning columns can still need manual cleanup.
- Formulas are only recognised in PDFs with real math fonts (LaTeX, Word equations); formula images are cropped by line boxes and can include a sliver of the neighbouring line.
- Markdown is built from heuristics. Complicated layouts (merged table cells, multi-column forms, tables without ruling lines) may need manual cleanup.
- Detected "tables" that cut through running text (small caps, code, form boxes) are rejected and read as normal text.
- Table detection depends on the PyMuPDF version (`pip install -U pymupdf`); results can differ between machines.
- Formulas are images plus plain text, not LaTeX.

## Note

pdf2md is an independent project and is not affiliated with or endorsed by Obsidian. "Obsidian" is a trademark of its owner; it is mentioned only to describe compatibility.
