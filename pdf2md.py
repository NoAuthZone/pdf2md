#!/usr/bin/env python3
"""
pdf2md.py - converts PDF files to Markdown (.md) or plain text (.txt).

Single file, cross-platform (Windows / Linux / macOS).

Dependencies:
    pip install pymupdf          (recommended; required for Markdown and images)
    pip install pypdf            (fallback, plain text only)

Examples:
    python pdf2md.py document.pdf                 -> document.txt
    python pdf2md.py document.pdf -f md           -> document.md (+ IMG-document/)
    python pdf2md.py folder/ -r -f md -o out/     -> all PDFs recursively
    python pdf2md.py document.pdf -p 1-3,7        -> pages 1-3 and 7 only
    python pdf2md.py document.pdf --page-markers  -> "--- Page N ---" separators
    python pdf2md.py folder -f md --image-dir Vault/Attachments
                                                   -> images in one shared folder
    python pdf2md.py document.pdf --keep-headers  -> keep headers/footers

Images and vector diagrams are saved as PNG files and linked in the Markdown.
There is no OCR: pages that consist only of an image (scans) are saved as page
images instead, so their content is not lost. Markdown output is heuristic;
multi-column layouts and merged table cells may need manual cleanup.
"""

import argparse
import base64
import html
import math
import os
import re
import shutil
import sys
import tempfile
import time
import warnings
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import quote, unquote

try:
    import pymupdf as fitz  # PyMuPDF (neuer Name)
except ImportError:
    try:
        import fitz  # PyMuPDF (alter Name)
    except ImportError:
        fitz = None

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None


# ----------------------------------------------------------------- helpers

def parse_pages(spec, total):
    """'1-3,7' -> [0,1,2,6] (0-based, clamped to the page count)."""
    if not spec:
        return list(range(total))
    pages = []
    seen = set()
    for part in spec.split(","):
        part = re.sub(r"\s*-\s*", "-", part.strip())
        if not part or not re.fullmatch(r"(?:[0-9]+|[0-9]+-[0-9]*|-[0-9]+)", part):
            raise ValueError(f"Invalid page selection: {part!r} (e.g. 1-3,7)")
        if "-" in part:
            a, _, b = part.partition("-")
            start = int(a) if a else 1
            end = int(b) if b else total
        else:
            start = end = int(part)
        if start < 1 or end < 1 or start > end:
            raise ValueError(f"Invalid page range: {part!r}")
        # Clamp before looping: even 1-999999999999 stays fast.
        for n in range(start - 1, min(end, total)):
            if n not in seen:
                seen.add(n)
                pages.append(n)
    if not pages:
        raise ValueError(f"No selected page exists (PDF has {total} pages)")
    return pages


def collect_pdfs(src, recursive):
    if src.is_file():
        if src.suffix.lower() != ".pdf":
            raise ValueError(f"Not a PDF file: {src}")
        return [src]
    pattern = "**/*" if recursive else "*"
    return sorted(p for p in src.glob(pattern)
                  if p.is_file() and p.suffix.lower() == ".pdf")


# --------------------------------------------------------- PyMuPDF backend

BULLETS = ("•", "◦", "▪", "●", "·", "–", "-", "*")
BULLET_GLYPHS = set("•◦▪●·–-*■□▶►‣⁃")  # bullet glyph standing alone on a line
TERMINALS = (".", ":", ";", "!", "?", ")", "\"", "”", "“")
MONO_HINTS = ("mono", "courier", "consol", "console", "code", "menlo", "hack",
              "terminal", "typewriter")


def text_dict(page, clip=None):
    """Extract font information without embedded image data."""
    flags = fitz.TEXTFLAGS_DICT & ~fitz.TEXT_PRESERVE_IMAGES
    return page.get_text("dict", flags=flags, clip=clip)


def dict_plain_text(data):
    return "\n".join("".join(s["text"] for s in line["spans"])
                     for block in data["blocks"] for line in block.get("lines", []))


def is_urlish(raw):
    """Single token without spaces such as a URL, path fragment or e-mail: not a code block."""
    t = raw.strip()
    return bool(t) and " " not in t and len(t) < 300 and (
        "://" in t or "@" in t or t.startswith(("www.", "http")) or "/" in t
        or t.endswith((".pdf", ".html", ".htm")))


def inline_code(text):
    """Find valid backtick delimiters and keep the outer whitespace."""
    stripped = text.strip()
    if not stripped:
        return text
    width = max((len(m) for m in re.findall(r"`+", stripped)), default=0) + 1
    fence = "`" * width
    pad = " " if stripped.startswith("`") or stripped.endswith("`") else ""
    start = len(text) - len(text.lstrip())
    end = len(text.rstrip())
    return text[:start] + fence + pad + stripped + pad + fence + text[end:]


def is_mono(span):
    """Monospace font? (flag bit 8 or a typical font name)"""
    return bool(span["flags"] & 8) or any(
        h in span["font"].lower() for h in MONO_HINTS)


def body_font_size(doc, page_idx):
    """Most common font size (by character count) = body text.
    Monospace code is not counted."""
    counts = Counter()
    for i in page_idx:
        for block in text_dict(doc[i])["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    txt = span["text"].strip()
                    if txt and not is_mono(span):
                        counts[round(span["size"], 1)] += len(txt)
    return counts.most_common(1)[0][0] if counts else 11.0


def render_code(run):
    """List of (text, x0, size) -> code lines with preserved indentation."""
    x_min = min(x for _, x, _ in run)
    out = []
    for text, x0, size in run:
        char_w = size * 0.6 or 6.0
        indent = int(round((x0 - x_min) / char_w))
        out.append(" " * indent + text.rstrip())
    width = max((len(m) for text, _, _ in run
                 for m in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, width + 1)
    return [fence] + out + [fence, ""]


def join_wrapped(lines, prose=False):
    """Join wrapped lines (no space after - / \\ = _). With prose=True a hyphenation
    point at the line end is resolved like in running text."""
    res = ""
    for ln in (l.strip() for l in lines):
        if not ln:
            continue
        if prose and res:
            res = join_line(res, ln)
            continue
        if res and not res.endswith(("-", "/", "\\", "=", "_")):
            res += " "
        res += ln
    return res


def cell_to_md(page, bbox):
    """Cell content as single-line Markdown text (monospace -> `code`)."""
    rect = fitz.Rect(bbox)
    lines = []
    for block in text_dict(page, clip=rect)["blocks"]:
        for line in block.get("lines", []):
            spans = [s for s in line["spans"] if s["text"].strip()]
            if spans:
                lines.append(spans)
    if not lines:
        return ""
    all_mono = all(is_mono(sp) for ln in lines for sp in ln)
    # words carry the spaces implied by glyph gaps; spans may lack them
    wl = None
    try:
        per_line = {}
        for w in page.get_text("words", clip=rect):
            per_line.setdefault((w[5], w[6]), []).append(w[4])
        cand = [" ".join(v) for _, v in sorted(per_line.items())]
        dict_txt = "".join("".join(sp["text"] for sp in ln) for ln in lines)
        if cand and "".join(cand).replace(" ", "") == dict_txt.replace(" ", "").replace("\xa0", ""):
            wl = cand
    except Exception:
        wl = None
    if all_mono:
        if wl is not None:
            txt = join_wrapped(wl)
        else:
            txt = join_wrapped("".join(sp["text"] for sp in ln) for ln in lines)
        return inline_code(txt).replace("|", "\\|")
    if wl is not None and not any(is_mono(sp) for ln in lines for sp in ln):
        return join_wrapped(wl, True).replace("|", "\\|")
    parts = []
    for ln in lines:
        t = ""
        for sp in ln:
            seg = sp["text"]
            t += inline_code(seg) if is_mono(sp) and seg.strip() else seg
        parts.append(t)
    return join_wrapped(parts).replace("|", "\\|")


def table_grid(table):
    """Rows (y0, y1, cells) and column boundaries of a detected table.
    Borders drawn as thin filled areas would otherwise create phantom columns;
    nested sub-rows belong to their parent row."""
    rows = []
    for row in table.rows:
        cells = [fitz.Rect(c) for c in row.cells if c]
        if not cells:
            continue
        y0, y1 = min(c.y0 for c in cells), max(c.y1 for c in cells)
        if rows and y0 >= rows[-1][0] - 1 and y1 <= rows[-1][1] + 1:
            rows[-1][2].extend(cells)
            continue
        rows.append([y0, y1, list(cells)])
    xs = sorted(v for _, _, cells in rows for c in cells for v in (c.x0, c.x1))
    bounds, group = [], []
    for x in xs:
        if group and x - group[-1] > 10:
            bounds.append(sum(group) / len(group))
            group = []
        group.append(x)
    if group:
        bounds.append(sum(group) / len(group))
    return rows, bounds


def table_to_md(page, table, cont=False):
    rows, bx = table_grid(table)
    if len(bx) < 3:
        return None
    ncols = len(bx) - 1
    out_rows = []
    for y0, y1, cells in rows:
        start = {}
        for c in cells:
            if c.width < 12:
                continue
            c0 = max((i for i in range(ncols) if bx[i] <= c.x0 + 8), default=0)
            c1 = min((i for i in range(ncols) if bx[i + 1] >= c.x1 - 8),
                     default=ncols - 1)
            start.setdefault(c0, max(c0, c1))
        cols, i = [], 0
        while i < ncols:
            if i in start:
                rect = (bx[i] + 1, y0 + 1, bx[start[i] + 1] - 1, y1 - 1)
                cols.append(cell_to_md(page, rect))
                cols.extend([""] * (start[i] - i))
                i = start[i] + 1
            else:
                cols.append("")
                i += 1
        out_rows.append(cols)
    rows = [r for r in out_rows if any(c.strip() for c in r)]
    # Append a continuation row (empty first cell) as long as the previous row's
    # text does not end with sentence punctuation - typical for multi-line
    # description cells.
    merged = []
    for r in rows:
        if merged and not r[0].strip() and len(r) >= 3:
            prev = merged[-1]
            longest = max(prev, key=len).rstrip()
            if longest and longest[-1] not in ".!?:;":
                merged[-1] = [(x + " " + y).strip() if y.strip() else x
                              for x, y in zip(prev, r)]
                continue
        merged.append(r)
    rows = merged
    # Empty first cells are legitimate data, not proof of a continuation.
    if len(rows) < 2:
        return None
    n = ncols
    # Drop columns that are empty in every row (merged cells)
    keep = [j for j in range(n) if any(r[j].strip() for r in rows)]
    rows = [[r[j] for j in keep] for r in rows]
    n = len(keep)
    if n < 2:
        return None
    out = ["| " + " | ".join(rows[0]) + " |",
           "| " + " | ".join("---" for _ in range(n)) + " |"]
    out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(out) + "\n"


# ---- tables without ruling lines (booktabs style): rebuilt from word positions

def _h_rules(page):
    """Horizontal rules and vertical lines: ([(y, x0, x1)], [vertical rects])."""
    hs, vs = [], []
    try:
        drawings = page.get_drawings()
    except Exception:  # noqa: BLE001
        return hs, vs
    for d in drawings:
        for it in d["items"]:
            if it[0] == "l":
                a, b = it[1], it[2]
                if abs(a.y - b.y) < 1.2 and abs(a.x - b.x) >= 40:
                    hs.append(((a.y + b.y) / 2, min(a.x, b.x), max(a.x, b.x)))
                elif abs(a.x - b.x) < 1.2 and abs(a.y - b.y) >= 10:
                    vs.append(fitz.Rect(a.x, min(a.y, b.y), b.x, max(a.y, b.y)))
            elif it[0] == "re":
                r = it[1]
                if r.height < 2 and r.width >= 40:
                    hs.append(((r.y0 + r.y1) / 2, r.x0, r.x1))
                elif r.width < 2 and r.height >= 10:
                    vs.append(fitz.Rect(r))
    return hs, vs


def booktabs_frames(page):
    """Regions framed by 2-4 horizontal rules of the same width and no vertical lines."""
    hs, vs = _h_rules(page)
    hs.sort()
    frames, used = [], set()
    for i, (y, x0, x1) in enumerate(hs):
        if i in used:
            continue
        group = [(y, x0, x1)]
        for j in range(i + 1, len(hs)):
            y2, a, b = hs[j]
            if j in used or abs(a - x0) > 12 or abs(b - x1) > 12:
                continue
            if y2 - group[-1][0] > 380:
                break
            if y2 - group[-1][0] < 3:
                continue
            group.append((y2, a, b))
            used.add(j)
        if 2 <= len(group) <= 12:
            rect = fitz.Rect(min(g[1] for g in group), group[0][0] - 1,
                             max(g[2] for g in group), group[-1][0] + 1)
            tall = [v for v in vs if rect.intersects(v) and v.height >= 0.3 * rect.height]
            if len(tall) < 3:  # a real grid has tall vertical lines
                frames.append((rect, [g[0] for g in group]))
    return frames


def grid_from_words(page, rect, rule_ys):
    """Markdown table from word positions inside rect, or None."""
    words = [w for w in page.get_text("words", clip=rect)]
    if len(words) < 8:
        return None
    heights = sorted(w[3] - w[1] for w in words)
    mh = heights[len(heights) // 2]
    # text lines
    lines = []
    for w in sorted(words, key=lambda w: (w[1] + w[3]) / 2):
        yc = (w[1] + w[3]) / 2
        if lines and abs(yc - lines[-1]["yc"]) < 0.45 * mh:
            lines[-1]["w"].append(w)
            lines[-1]["yc"] = sum((x[1] + x[3]) / 2 for x in lines[-1]["w"]) / len(lines[-1]["w"])
        else:
            lines.append({"yc": yc, "w": [w]})
    if len(lines) < 3:
        return None
    x_lo, x_hi = min(w[0] for w in words), max(w[2] for w in words)
    width = int(x_hi - x_lo) + 2
    gap = max(4.5, 0.5 * mh)  # wider than a word space: a new cell
    for ln in lines:
        ln["segs"], last = [], None
        for w in sorted(ln["w"], key=lambda w: w[0]):
            if last is not None and w[0] - last < gap:
                ln["segs"][-1].append(w)
            else:
                ln["segs"].append([w])
            last = w[2]
    # columns: only lines that fill (nearly) all cells decide, centred/spanning cells do not
    most = max(len(ln["segs"]) for ln in lines)
    full = [ln for ln in lines if len(ln["segs"]) == most]
    if len(full) < 3:
        full = [ln for ln in lines if len(ln["segs"]) >= max(2, math.ceil(0.6 * most))]
    if len(full) < 3:
        return None
    cover = [0] * width
    for ln in full:
        seen = set()
        for w in ln["w"]:
            for x in range(int(w[0] - x_lo), min(width, int(w[2] - x_lo) + 1)):
                seen.add(x)
        for x in seen:
            cover[x] += 1
    limit = 1 if len(full) >= 8 else 0
    gutters, start = [], None
    for x in range(width):
        if cover[x] <= limit:
            if start is None:
                start = x
        else:
            if start is not None and start > 0 and x - start >= 4:
                gutters.append((start + x) / 2 + x_lo)
            start = None
    cols = len(gutters) + 1
    if cols < 2 or cols > 14:
        return None
    edges = [-1e9] + gutters + [1e9]

    def col_of(x):
        return max(i for i in range(cols) if edges[i] <= x)

    header_end = rule_ys[1] if len(rule_ys) >= 3 else None  # first inner rule
    rows, hdr_lines = [], []
    for ln in lines:
        cells = [[] for _ in range(cols)]
        is_hdr = header_end is not None and ln["yc"] < header_end
        segs = ln["segs"]
        for seg in segs:
            sx0, sx1 = seg[0][0], seg[-1][2]
            text = " ".join(w[4] for w in seg)
            if is_hdr:  # a header spanning several columns is repeated for each of them
                span = max(sx1 - sx0, 1)
                targets = [c for c in range(cols)
                           if min(sx1, edges[c + 1]) - max(sx0, edges[c]) >= 0.4 * span]
                targets = targets or [col_of((sx0 + sx1) / 2)]
            else:
                targets = [col_of(sx0 + 0.5)]
            for c in targets:
                cells[c].append(text)
        cells = [" ".join(c) for c in cells]
        band = sum(1 for y in rule_ys if y < ln["yc"])
        (hdr_lines if is_hdr else rows).append((cells, band))
    if header_end is None:
        hdr_lines, rows = rows[:1], rows[1:]
    if not rows:
        return None
    header = [" ".join(x for x in col if x) for col in zip(*[h[0] for h in hdr_lines])]
    # a label centred in its group of rows ("(A)") belongs to the first row of that group
    labels = {}
    kept = []
    for cells, band in rows:
        filled = [i for i, c in enumerate(cells) if c]
        if filled == [0] and len(cells[0]) <= 14 and any(b == band for _, b in rows if _ is not cells):
            labels[band] = cells[0]
        else:
            kept.append((cells, band))
    first_seen = set()
    rows = []
    for cells, band in kept:
        if band not in first_seen:
            first_seen.add(band)
            if band in labels and not cells[0]:
                cells = [labels[band]] + cells[1:]
        rows.append(cells)

    def texty(s):
        return len(re.findall(r"[^\W\d_]", s)) >= 2

    merged = []
    for cells in rows:
        filled = [i for i, c in enumerate(cells) if c]
        if (merged and filled and (filled[0] >= 1 or cells[0][:1].islower())
                and all(merged[-1][i] and texty(merged[-1][i]) and texty(cells[i]) for i in filled)):
            for i in filled:
                merged[-1][i] = (merged[-1][i] + " " + cells[i]).strip()
        else:
            merged.append(list(cells))
    keep = [j for j in range(cols) if header[j] or any(r[j] for r in merged)]
    if len(keep) < 3 or len(merged) < 2:
        return None
    # evidence of a real table: a filled header, a reasonably filled body, short cells
    hdr_fill = sum(1 for j in keep if header[j]) / len(keep)
    cells_all = [r[j] for r in merged for j in keep]
    filled = [c for c in cells_all if c]
    if (hdr_fill < 0.7 or len(filled) < 0.3 * len(cells_all)
            or sum(len(c.split()) for c in filled) / max(len(filled), 1) > 6):
        return None

    def esc(c):
        return c.replace("|", "\\|")
    out = ["| " + " | ".join(esc(header[j]) for j in keep) + " |",
           "| " + " | ".join("---" for _ in keep) + " |"]
    out += ["| " + " | ".join(esc(r[j]) for j in keep) + " |" for r in merged]
    return "\n".join(out) + "\n"


def _ncols(md):
    return md.split("\n", 1)[0].count(" | ") + 1


def _nrows(md):
    return md.count("\n")


def _glued_rows(md):
    """Several table rows ended up in one cell: long runs of numbers in a cell."""
    num = re.compile(r"^[-+−]?[\d.,]+[%K×]?$")
    for ln in md.splitlines()[2:]:
        for cell in ln.strip("| ").split(" | "):
            toks = cell.split()
            if len(toks) >= 5 and sum(bool(num.match(t)) for t in toks) >= 0.7 * len(toks):
                return True
    return False


def find_md_tables(page):
    """Detectable tables (>=2 columns, >=2 rows) with ready-made Markdown."""
    found = []
    try:
        tabs = page.find_tables().tables
    except AttributeError:  # older PyMuPDF version without table detection
        return found
    except Exception as exc:  # keep extracting text when table detection fails
        warnings.warn(f"Table detection failed: {exc}", stacklevel=2)
        return found
    cont = False
    if not tabs:  # tables without ruling lines: accept only with a clear header row
        cont = True
        try:
            tabs = [t for t in page.find_tables(strategy="text").tables
                    if looks_like_header_table(t)]
        except Exception:
            tabs = []
    for t in tabs:
        if t.col_count < 2 or t.row_count < 2 or is_junk_table(t) or cuts_words(page, t):
            continue
        md = table_to_md(page, t, cont)
        if md and prose_box(md):
            continue  # form boxes with running text: the normal text path reads them better
        if md:
            found.append((fitz.Rect(t.bbox), md))
    # borderless (booktabs) tables: the column grid comes from the word positions
    try:
        for rect, ys in booktabs_frames(page):
            md = grid_from_words(page, rect, ys)
            if not md:
                continue
            clash = [k for k, (r, _) in enumerate(found)
                     if (r & rect).get_area() > 0.3 * min(r.get_area(), rect.get_area())]
            # keep a detected table unless it has fewer columns or glued rows
            if clash and not any(_ncols(found[k][1]) < _ncols(md) or _glued_rows(found[k][1])
                                 or (_nrows(md) > 1.2 * _nrows(found[k][1])
                                     and _ncols(md) >= _ncols(found[k][1]))
                                 for k in clash):
                continue
            found = [f for k, f in enumerate(found) if k not in clash]
            found.append((rect, md))
    except Exception as exc:  # noqa: BLE001
        warnings.warn(f"Borderless table detection failed: {exc}", stacklevel=2)
    return found


def prose_box(md):
    """A 'table' of at most 3 rows with very long cells is a text box, not a table."""
    lines = [ln for ln in md.splitlines() if ln.startswith("|") and not ln.startswith("| ---")]
    return len(lines) <= 3 and any(len(c) > 300 for ln in lines for c in ln.split(" | "))


def cuts_words(page, t):
    """True if the column grid slices through running text: a word crossing a column
    edge, or two words of one line split by an edge with a gap smaller than a space.
    Real tables never do this; layouts with small caps, drop caps or header rules do."""
    try:
        edges = sorted({round(c[0], 1) for c in t.cells if c} | {round(c[2], 1) for c in t.cells if c})
        x0, x1 = t.bbox[0], t.bbox[2]
        edges = [e for e in edges if x0 + 2 < e < x1 - 2]
        if not edges:
            return False
        words = page.get_text("words", clip=fitz.Rect(t.bbox))
    except Exception:
        return False
    bad, prev = 0, None
    for w in sorted(words, key=lambda w: (w[5], w[6], w[7])):
        for e in edges:
            if w[0] + 0.5 < e < w[2] - 0.5:
                bad += 1
                break
        if prev and prev[5:7] == w[5:7]:
            gap, h = w[0] - prev[2], max(w[3] - w[1], 1)
            if 0 <= gap < 0.2 * h and any(prev[2] - 0.5 <= e <= w[0] + 0.5 for e in edges):
                bad += 1
        prev = w
    return bad >= 2


def is_junk_table(t):
    """Figures (e.g. attention plots) yield pseudo tables with duplicate rows."""
    try:
        rows = [tuple((c or "").strip() for c in r) for r in t.extract()]
    except Exception:
        return False
    rows = [r for r in rows if any(r)]
    return len(rows) >= 3 and len(set(rows)) <= len(rows) * 0.75


def looks_like_header_table(t):
    """Accept text-based candidates only with 3-10 columns, >=3 rows, a short
    header whose cells each start with a capital letter/digit/#, and at least
    one narrow column (flags, check marks, numbers). Two-column running text
    does not qualify."""
    if not (3 <= t.col_count <= 10 and t.row_count >= 3):
        return False
    try:
        rows = t.extract()
    except Exception:
        return False
    head = rows[0]
    if not all(c and c.strip() and len(c) <= 25 and "\n" not in c.strip()
               and not c.strip()[0].islower() for c in head):
        return False
    narrow = 0
    for j in range(1, t.col_count):
        cells = sorted(len((r[j] or "").strip()) for r in rows[1:] if (r[j] or "").strip())
        if cells and cells[len(cells) // 2] <= 5:
            narrow += 1
    return narrow >= 1


# Vocabulary of the document being converted (words that do not sit at a line end).
# It decides whether a hyphen at a line end is real ("post-compromise") or only
# a hyphenation point ("certifi-cates").  Empty -> always join (old behaviour).
HYPHEN_VOCAB = {"words": {}, "hyphenated": {}}
TOKEN = re.compile(r"[^\W\d_]+(?:-[^\W\d_]+)*")


def build_hyphen_vocab(doc, idx):
    words, hyph = Counter(), Counter()
    for i in idx:
        try:
            ws = doc[i].get_text("words")
        except Exception:
            continue
        for w in ws:
            t = w[4].strip()
            if t.endswith("-") or t.startswith("-"):
                continue  # may be cut at the line end
            for m in TOKEN.finditer(t):
                tok = m.group(0).casefold()
                if "-" in tok:
                    parts = tok.split("-")
                    hyph.update(f"{x}-{y}" for x, y in zip(parts, parts[1:]))
                    words.update(parts)
                else:
                    words[tok] += 1
    HYPHEN_VOCAB["words"], HYPHEN_VOCAB["hyphenated"] = words, hyph


def keeps_hyphen(a, b):
    """True if the hyphen at the end of line `a` belongs to a compound with the start of `b`."""
    left = re.search(r"([^\W\d_]+)-$", a)
    right = re.match(r"[^\W\d_]+", b)
    words = HYPHEN_VOCAB["words"]
    if not (left and right and words):
        return False
    lw, rw = left.group(1).casefold(), right.group(0).casefold()
    one, two = words.get(lw + rw, 0), HYPHEN_VOCAB["hyphenated"].get(f"{lw}-{rw}", 0)
    if two > one:
        return True  # the document writes it with a hyphen more often (built-in)
    if one:
        return False  # the one-word form exists elsewhere in the document
    if two:
        return True
    if len(lw) >= 5 and left.group(1)[0].isupper() and rw in words:
        return True  # proper name + word (Lagrange-interpolation)
    return len(lw) >= 3 and len(rw) >= 3 and lw in words and rw in words


def join_line(a, b):
    """Join two lines; resolve hyphenation at the line end."""
    if a.endswith("­"):
        return a[:-1] + b
    if len(a) >= 2 and a.endswith("-") and a[-2].isalpha():
        if not b[:1].islower() or keeps_hyphen(a, b):
            return a + b
        return a[:-1] + b
    return a + " " + b


def merge_lines(lines, merge=True):
    """[(text, x0, x1, bullet, y0, y1)] of a block -> [(paragraph, bullet)].
    Lines are joined when the vertical gap is normal, the previous line is not
    conspicuously short and does not end (shortly) with sentence punctuation.
    Otherwise a new paragraph starts."""
    if not merge:
        return [(t, bl) for t, _, _, bl, _, _ in lines]
    left = min(l[1] for l in lines)
    span = max(max(l[2] for l in lines) - left, 1.0)
    paras, cur, prev = [], None, None
    for text, x0, x1, bullet, y0, y1 in lines:
        if cur is not None and not bullet:
            ratio = (prev[0] - left) / span
            gap = y0 - prev[2]
            close = gap <= 0.35 * max(prev[2] - prev[1], 1.0)
            ends = cur[0].endswith((".", ":", ";", "!", "?"))
            if close and ratio >= 0.6 and not (ends and ratio < 0.85):
                cur = (join_line(cur[0], text), cur[1])
                prev = (x1, y0, y1)
                continue
        if cur is not None:
            paras.append(cur)
        cur, prev = (text, bullet), (x1, y0, y1)
    if cur is not None:
        paras.append(cur)
    return paras


def insert_by_y(texts, imgs):
    out = list(texts)
    for im in sorted(imgs, key=lambda b: b["bbox"][1]):
        # after the last block that lies above the image; the content-stream order of
        # blocks is not always top-down, so "first block below" would misplace it
        above = [j for j, b in enumerate(out)
                 if (b["bbox"][1] + b["bbox"][3]) / 2 <= im["bbox"][1] + 1]
        out.insert(above[-1] + 1 if above else 0, im)
    return out


def order_blocks(blocks, fixed=(), columns=True):
    """Detect two-column pages line by line: read the left column before the
    right one (even if PyMuPDF mixes both columns in one block) and sort images
    in by position. Other pages keep their order."""
    texts = [b for b in blocks if b.get("type") == 0 and b.get("bbox")]
    imgs = [b for b in blocks if b.get("type") == 1]
    entries = [(no, b, ln) for no, b in enumerate(texts)
               for ln in b.get("lines", [])
               if any(sp["text"].strip() for sp in ln["spans"])]
    if not columns or len(entries) < 8:
        return insert_by_y(texts, imgs)
    xs0 = min(ln["bbox"][0] for _, _, ln in entries)
    xs1 = max(ln["bbox"][2] for _, _, ln in entries)
    width = xs1 - xs0
    if width <= 0:
        return insert_by_y(texts, imgs)
    mid, tol = (xs0 + xs1) / 2, width * 0.03

    def kind(ln):
        x0, y0, x1, y1 = ln["bbox"]
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        if any(r.x0 <= cx <= r.x1 and r.y0 <= cy <= r.y1 for r in fixed):
            return "W"
        if x1 <= mid + tol:
            return "L"
        if x0 >= mid - tol:
            return "R"
        return "W"

    typed = [(kind(ln), no, b, ln) for no, b, ln in entries]
    left = [t for t in typed if t[0] == "L"]
    right = [t for t in typed if t[0] == "R"]
    widths = sorted(t[3]["bbox"][2] - t[3]["bbox"][0] for t in left + right)
    if (len(left) < 4 or len(right) < 4
            or len(left) + len(right) < 0.6 * len(typed)
            or widths[len(widths) // 2] < 0.35 * width  # column lines are long
            or not any(l[3]["bbox"][1] < r[3]["bbox"][3]
                       and r[3]["bbox"][1] < l[3]["bbox"][3]
                       for l in left for r in right)):
        return insert_by_y(texts, imgs)

    def y0(t):
        return (t[3] or t[2])["bbox"][1], (t[3] or t[2])["bbox"][0]

    ordered, pend_l, pend_r = [], [], []

    def flush():
        ordered.extend(sorted(pend_l, key=y0))
        ordered.extend(sorted(pend_r, key=y0))
        pend_l.clear()
        pend_r.clear()

    for t in sorted(typed + [("W", -1, im, None) for im in imgs], key=y0):
        if t[0] == "L":
            pend_l.append(t)
        elif t[0] == "R":
            pend_r.append(t)
        else:
            flush()
            ordered.append(t)
    flush()

    result, prev = [], None
    for k, no, b, ln in ordered:
        if ln is None:  # image
            result.append(b)
            prev = None
        elif prev == (k, no):
            blk = result[-1]
            blk["lines"].append(ln)
            blk["bbox"] = (min(blk["bbox"][0], ln["bbox"][0]),
                           min(blk["bbox"][1], ln["bbox"][1]),
                           max(blk["bbox"][2], ln["bbox"][2]),
                           max(blk["bbox"][3], ln["bbox"][3]))
        else:
            result.append({**b, "lines": [ln], "bbox": tuple(ln["bbox"])})
            prev = (k, no)
    return result


# ---- headers/footers and images

def margin_key(text):
    return re.sub(r"\d+", "0", " ".join(text.split()).casefold())


def margin_zone(line, height):
    if line["bbox"][3] <= height * 0.10:
        return "top"
    if line["bbox"][1] >= height * 0.90:
        return "bottom"
    return None


def line_text(line):
    return "".join(s["text"] for s in line["spans"]).strip()


def detect_furniture(doc, idx, find_images=False):
    """Lines in the top/bottom page margin (page numbers with normalized
    digits) that recur on at least 40 % of the pages, plus recurring images
    (logos). Only from 3 pages on."""
    n = len(idx)
    if n < 3:
        return set(), set()
    need = max(2, math.ceil(0.4 * n))
    text_counts, image_counts = Counter(), Counter()
    for i in idx:
        page = doc[i]
        height = (page.rect * page.derotation_matrix).height
        keys = set()
        for block in text_dict(page)["blocks"]:
            for line in block.get("lines", []):
                zone, text = margin_zone(line, height), line_text(line)
                if zone and text:
                    keys.add((zone, margin_key(text)))
        text_counts.update(keys)
        if find_images:
            image_counts.update({inf["xref"] for inf in
                                 page.get_image_info(xrefs=True) if inf.get("xref")})
    return ({k for k, c in text_counts.items() if c >= need},
            {x for x, c in image_counts.items() if c >= need})


def strip_furniture(page, data, keys):
    if not keys:
        return data
    height = (page.rect * page.derotation_matrix).height
    blocks = []
    for block in data["blocks"]:
        if block.get("type") != 0:
            blocks.append(block)
            continue
        lines = []
        for line in block.get("lines", []):
            zone = margin_zone(line, height)
            if zone and (zone, margin_key(line_text(line))) in keys:
                continue
            lines.append(line)
        if lines:
            blocks.append({**block, "lines": lines})
    return {**data, "blocks": blocks}


def image_ref(image_dir, name, cfg, alt):
    """Markdown reference to a saved image (standard link or Obsidian wikilink)."""
    if cfg.get("wiki"):
        return f"![[{name}]]"
    md_dir = cfg.get("md_dir") or image_dir.parent
    try:
        rel = Path(os.path.relpath(image_dir / name, md_dir)).as_posix()
    except ValueError:  # e.g. different drive on Windows
        rel = (image_dir / name).as_posix()
    return f"![{alt}]({quote(rel)})"


def save_clip(page, rect, path, dpi=150):
    pix = page.get_pixmap(clip=rect, dpi=dpi, alpha=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    pix.save(str(path))


def extract_images(doc, page, image_dir, saved, skip_xrefs, cfg=None):
    """Save the page's images and vector graphics as PNG; pseudo blocks for Markdown.
    Returns (blocks, rectangles of graphics whose text is part of the image)."""
    cfg = cfg or {}
    prefix = cfg.get("prefix", "img")
    visible = page.rect * page.derotation_matrix
    blocks, covered = [], []
    for info in page.get_image_info(xrefs=True):
        xref = info.get("xref", 0)
        rect = fitz.Rect(info["bbox"]) & visible
        if (not xref or xref in skip_xrefs or rect.is_empty
                or not all(math.isfinite(v) for v in rect)
                or rect.width < 24 or rect.height < 24
                or info["width"] < 32 or info["height"] < 32
                or rect.get_area() > visible.get_area() * 0.85):
            continue  # tiny, recurring or full-page scan
        if xref not in saved:
            try:
                pix = fitz.Pixmap(doc, xref)
                if pix.n - pix.alpha > 3:
                    pix = fitz.Pixmap(fitz.csRGB, pix)
                name = f"{prefix}_s{page.number + 1:03d}_{len(saved) + 1:02d}.png"
                image_dir.mkdir(parents=True, exist_ok=True)
                pix.save(str(image_dir / name))
                saved[xref] = name
            except Exception as exc:  # noqa: BLE001
                warnings.warn(f"Image on page {page.number + 1} not saved: {exc}",
                              stacklevel=2)
                continue
        blocks.append({"type": 1, "bbox": tuple(rect),
                       "md": image_ref(image_dir, saved[xref], cfg,
                                       f"Image page {page.number + 1}")})
        covered.append(rect)
    # Vector graphics (diagrams, schematics): render as a clip
    try:
        clusters = page.cluster_drawings()
    except Exception:  # noqa: BLE001
        clusters = []
    for k, rect in enumerate(clusters, 1):
        rect = fitz.Rect(rect) & visible
        if (rect.is_empty or rect.width < 80 or rect.height < 60
                or rect.get_area() > visible.get_area() * 0.85
                or any((rect & c).get_area() > 0.5 * rect.get_area() for c in covered)
                or not is_graphic_cluster(page, rect)):
            continue
        clip = fitz.Rect(rect.x0 - 3, rect.y0 - 3, rect.x1 + 3, rect.y1 + 3) & visible
        name = f"{prefix}_s{page.number + 1:03d}_fig{k:02d}.png"
        try:
            save_clip(page, clip, image_dir / name)
        except Exception as exc:  # noqa: BLE001
            warnings.warn(f"Graphic on page {page.number + 1} not saved: {exc}",
                          stacklevel=2)
            continue
        saved[("fig", page.number, k)] = name
        blocks.append({"type": 1, "bbox": tuple(clip),
                       "md": image_ref(image_dir, name, cfg, f"Figure page {page.number + 1}")})
        covered.append(clip)
        cfg.setdefault("_vector", []).append(clip)
    return blocks, [c for c in cfg.pop("_vector", [])]


MATH_FONT = re.compile(r"(?i)(^|[+,\-])(CM(MI|SY|EX|R|BX|TI|SS|TT)\d*|MSAM|MSBM|EUFM|RSFS|"
                       r"MTMI|MTSY|MTEX|MT-?Extra|Symbol(MT)?|CambriaMath|Cambria Math|"
                       r"STIX.*Math|LatinModernMath|LMMath|TeXGyre.*Math|MathematicalPi|"
                       r"Mathematica|Math\d*)(?!\w*(Roman|Text))")
MATH_CHARS = set("∑∏∫√∞≤≥≠≈±×÷∂∇∈∉⊂⊆∪∩∀∃→←↔⇒⇔·⋅∘⊕⊗αβγδεζηθικλμνξπρστυφχψωΓΔΘΛΞΠΣΦΨΩϵ−")


def _line_math_ratio(line):
    total = math_chars = 0
    for sp in line["spans"]:
        n = len(sp["text"].replace(" ", ""))
        if not n:
            continue
        total += n
        if MATH_FONT.search(sp.get("font", "")) or any(c in MATH_CHARS for c in sp["text"]):
            math_chars += n
    return (math_chars / total) if total else 0.0, total


def _segments(line):
    """Number of text groups in a line separated by wide gaps (cells of a table row)."""
    spans = [sp for sp in line["spans"] if sp["text"].strip()]
    spans.sort(key=lambda sp: sp["bbox"][0])
    n, last = 1, None
    for sp in spans:
        if last is not None and sp["bbox"][0] - last > 14:
            n += 1
        last = max(last or 0, sp["bbox"][2])
    return n


def find_formulas(page, data, visible):
    """Display equations: runs of lines set (mostly) in math fonts, with fraction parts
    and the equation number. Returns [(rect, text)]."""
    lines = []
    for block in data["blocks"]:
        for ln in block.get("lines", []) if block.get("type") == 0 else []:
            if any(sp["text"].strip() for sp in ln["spans"]):
                ratio, n = _line_math_ratio(ln)
                lines.append((fitz.Rect(ln["bbox"]), ratio, n, ln))
    if not lines:
        return []
    text_w = max(r.x1 for r, *_ in lines) - min(r.x0 for r, *_ in lines)
    # left margins of running text (one per column); math lines that start on a margin are
    # continuations of a sentence with inline math, display equations are indented/centred
    margins = [m for m, c in Counter(round(r.x0 / 3) * 3 for r, ratio, n, _ in lines
                                     if ratio < 0.3 and n >= 25).items() if c >= 3]

    def at_margin(r):
        return any(abs(r.x0 - m) <= 5 for m in margins)
    seeds = [x for x in lines if x[1] >= 0.5 and x[2] >= 2 and x[0].width < 0.8 * text_w
             and not at_margin(x[0])]
    seeds.sort(key=lambda x: (x[0].y0, x[0].x0))
    clusters = []
    for s in seeds:
        r = s[0]
        for c in clusters:
            cr = c["rect"]
            gap = max(r.y0 - cr.y1, cr.y0 - r.y1)
            if gap < 0.9 * max(r.height, 8) and r.x0 < cr.x1 + 40 and r.x1 > cr.x0 - 40:
                c["rect"] = cr | r
                c["seeds"].append(s)
                break
        else:
            clusters.append({"rect": fitz.Rect(r), "seeds": [s]})
    out = []
    for c in clusters:
        cr = fitz.Rect(c["rect"])
        # fraction parts, indices and the equation number on the same band
        band = fitz.Rect(cr.x0 - 6, cr.y0 - 14, cr.x1 + 6, cr.y1 + 14)
        members = list(c["seeds"])
        for x in lines:
            if x in members:
                continue
            r, ratio, n, ln = x
            if r.y1 < band.y0 or r.y0 > band.y1:
                continue
            txt = "".join(sp["text"] for sp in ln["spans"]).strip()
            number = re.fullmatch(r"\(\d+(?:\.\d+)*[a-z]?\)", txt) is not None
            inside = band.x0 <= r.x0 and r.x1 <= band.x1 and r.width < 0.8 * text_w
            same_row = (min(r.y1, cr.y1) - max(r.y0, cr.y0) > 0.5 * r.height
                        and max(r.x0 - cr.x1, cr.x0 - r.x1) < 15 and r.width < 0.9 * text_w)
            if number or same_row or (inside and (ratio >= 0.2 or n <= 12)):
                members.append(x)
        rect = fitz.Rect(members[0][0])
        for m in members:
            rect |= m[0]
        eq_no = any(re.fullmatch(r"\(\d+(?:\.\d+)*[a-z]?\)",
                                 "".join(sp["text"] for sp in m[3]["spans"]).strip())
                    for m in members)
        seed_chars = sum(s[2] for s in c["seeds"])
        row_cells = max(_segments(s0[3]) for s0 in c["seeds"])
        if row_cells >= 3:
            continue  # cells of a table row, not an equation
        if seed_chars < 3 or (len(c["seeds"]) == 1 and seed_chars < 6 and not eq_no):
            continue  # inline symbols, bullets, page decorations
        if rect.width < 20 or rect.height > 0.5 * visible.height:
            continue
        members.sort(key=lambda m: (round(m[0].y0 / 4), m[0].x0))
        text = " ".join("".join(sp["text"] for sp in m[3]["spans"]).strip() for m in members)
        if not (eq_no or re.search(r"[=<>≤≥≈≠∈≡∝→←⇒⇔∑∏∫]", text)):
            continue  # stacked values (table column), no relation or operator
        out.append((rect & visible, re.sub(r"\s+", " ", text)))
    return out


def extract_formulas(page, data, image_dir, cfg):
    """Render display equations as PNG (Obsidian shows them as images); the plain text
    follows as a comment for readers that cannot see images."""
    visible = page.rect * page.derotation_matrix
    blocks, rects = [], []
    prefix = cfg.get("prefix", "img")
    for k, (rect, text) in enumerate(find_formulas(page, data, visible), 1):
        clip = fitz.Rect(rect.x0 - 4, rect.y0 - 1.5, rect.x1 + 4, rect.y1 + 1.5) & visible
        name = f"{prefix}_s{page.number + 1:03d}_eq{k:02d}.png"
        try:
            save_clip(page, clip, image_dir / name, dpi=200)
        except Exception as exc:  # noqa: BLE001
            warnings.warn(f"Formula on page {page.number + 1} not saved: {exc}", stacklevel=2)
            continue
        text = text.replace("--", "- -").replace(">", "›")
        blocks.append({"type": 1, "bbox": tuple(clip),
                       "md": image_ref(image_dir, name, cfg, f"Formula page {page.number + 1}")
                             + f"\n\n<!-- formula: {text} -->"})
        rects.append(clip)
    return blocks, rects


def is_graphic_cluster(page, rect):
    """Diagram rather than table/form: contains curves or diagonal lines."""
    curves = diag = 0
    for d in page.get_drawings():
        if not rect.intersects(d["rect"]):
            continue
        for it in d["items"]:
            if it[0] == "c":
                curves += 1
            elif it[0] == "l":
                a, b = it[1], it[2]
                if abs(a.x - b.x) > 1.5 and abs(a.y - b.y) > 1.5:
                    diag += 1
        if curves + diag >= 4:
            return True
    return False


def save_page_image(page, image_dir, cfg):
    """Save an image-only page as JPEG so that its content is not lost."""
    name = f"{cfg.get('prefix', 'page')}_s{page.number + 1:03d}_page.jpg"
    try:
        pix = page.get_pixmap(dpi=130, alpha=False)
        image_dir.mkdir(parents=True, exist_ok=True)
        pix.save(str(image_dir / name), jpg_quality=82)
    except Exception as exc:  # noqa: BLE001
        warnings.warn(f"Page image {page.number + 1} not saved: {exc}", stacklevel=2)
        return None
    return {"type": 1, "bbox": tuple(page.rect),
            "md": image_ref(image_dir, name, cfg, f"Page {page.number + 1} (scan)")}


def drop_lines_in(data, rects):
    blocks = []
    for block in data["blocks"]:
        if block.get("type") != 0:
            blocks.append(block)
            continue
        lines = []
        for line in block["lines"]:
            lb = line["bbox"]
            cx, cy = (lb[0] + lb[2]) / 2, (lb[1] + lb[3]) / 2
            if not any(r.x0 <= cx <= r.x1 and r.y0 <= cy <= r.y1 for r in rects):
                lines.append(line)
        if lines:
            blocks.append({**block, "lines": lines})
    return {**data, "blocks": blocks}


def is_bullet_line(text):
    return text.startswith(BULLETS) and len(text) > 2 and text[1] == " "


ID_ONLY = re.compile(r"\d+(\.\d+)*\.?|[A-Z]{2,3}-\d+(\(\d+\))?")
ID_HEAD = re.compile(r"([A-Z]{2,3}-\d+(?:\(\d+\))?|\d+(?:\.\d+)+) \S")


def bold_line(text):
    """Bold line; standards/section identifiers ('AC-2 TITLE', '3.1 Title') become headings."""
    m = ID_HEAD.match(text)
    if not m:
        return f"**{text}**"
    ident = m.group(1)
    level = 4 if "-" in ident else min(6, 3 + ident.count("."))
    return "#" * level + " " + text


LEADERS = re.compile(r"(?:\s?\.){4,}\s?")


def clean_leaders(text):
    """Replace TOC dot leaders ('Title ..... 12') by a single ellipsis."""
    return LEADERS.sub(" … ", text)


def collapse_word_runs(out, min_run=6):
    """Join figure labels with one word per line into a single line."""
    res, i, n = [], 0, len(out)
    def single(k):
        if k + 1 >= n:
            return False
        t = out[k]
        return (out[k + 1] == "" and t and " " not in t
                and len(t) <= 15 and not t.startswith(("#", "-", "|", "`", "*", "<!--")))
    while i < n:
        j = i
        while single(j):
            j += 2
        if (j - i) // 2 >= min_run:
            res.extend([" ".join(out[k] for k in range(i, j, 2)), ""])
            i = j
        else:
            res.append(out[i])
            i += 1
    return res


def page_to_markdown(page, body, tables=True, data=None, columns=True,
                     merge=True):
    out = []
    code_run = []
    run = []          # text lines of the current block: (text, x0, x1, bullet)
    last_head = None  # (level, index in out, text, block no.)
    last_bold = None  # (Index in out, Text)
    pending_bullet = False  # bullet glyph stood alone on the previous line
    tabs = find_md_tables(page) if tables else []
    emitted = set()

    def flush_code():
        if code_run:
            out.extend(render_code(code_run))
            code_run.clear()

    def flush_run():
        if not run:
            return
        paras = merge_lines(run, merge)
        for k, (text, bullet) in enumerate(paras):
            text = clean_leaders(text)
            out.append(f"- {text}" if bullet else text)
            if not (bullet and k + 1 < len(paras) and paras[k + 1][1]):
                out.append("")
        run.clear()

    blocks = (data if data is not None else text_dict(page))["blocks"]
    blocks = order_blocks(blocks, [r for r, _ in tabs], columns)
    for block_no, block in enumerate(blocks):
        if block.get("type") == 1:  # image
            flush_run()
            flush_code()
            out.extend([block["md"], ""])
            continue
        if block.get("type") != 0:
            continue
        flush_run()
        for line in block["lines"]:
            spans = [s for s in line["spans"] if s["text"].strip()]
            if not spans:
                continue
            raw = "".join(s["text"] for s in line["spans"])

            # line lies inside a table -> emit the table once
            lb = line["bbox"]
            cx, cy = (lb[0] + lb[2]) / 2, (lb[1] + lb[3]) / 2
            hit = next(
                (k for k, (r, _) in enumerate(tabs)
                 if r.x0 - 1 <= cx <= r.x1 + 1 and r.y0 - 1 <= cy <= r.y1 + 1),
                None)
            if hit is not None:
                if hit not in emitted:
                    flush_run()
                    flush_code()
                    out.append(tabs[hit][1])
                    emitted.add(hit)
                continue

            # line entirely monospace -> code block
            if all(is_mono(s) for s in spans) and not is_urlish(raw):
                flush_run()
                code_run.append((raw, lb[0], spans[0]["size"]))
                continue
            flush_code()

            # mixed line: monospace parts as `inline code`
            parts = []
            for s in line["spans"]:
                t = s["text"]
                parts.append(inline_code(t) if is_mono(s) and t.strip()
                             and len(spans) > 1 else t)
            text = "".join(parts).strip()
            if text in BULLET_GLYPHS:  # bullet glyph stands alone on the line
                pending_bullet = True
                continue
            was_bullet, pending_bullet = pending_bullet, False
            plain = [s for s in spans if not is_mono(s)] or spans
            size = max(s["size"] for s in plain)
            bold = all(bool(s["flags"] & 16) or "bold" in s["font"].lower()
                       for s in plain)
            ratio = size / body if body else 1.0
            level = 1 if ratio >= 1.6 else 2 if ratio >= 1.3 else 3 if ratio >= 1.12 else 0
            if level and len(text) > 150:
                level = 0  # sentence, not a heading
            if level and text[:1].islower() and not (last_head and last_head[3] == block_no):
                level = 0  # sentence continuation, not a heading

            if level:
                flush_run()
                if (merge and last_head and last_head[0] == level
                        and last_head[3] == block_no
                        and len(out) == last_head[1] + 2):
                    joined = join_line(last_head[2], text)  # multi-line heading
                    if len(joined) > 120:  # too long: sentence, not a heading
                        out[last_head[1]] = joined
                        last_head = None
                    else:
                        out[last_head[1]] = "#" * level + " " + joined
                        last_head = (level, last_head[1], joined, block_no)
                else:
                    out.extend(["#" * level + " " + text, ""])
                    last_head = (level, len(out) - 2, text, block_no)
            elif bold and len(text) < 100:
                flush_run()
                if (merge and last_bold and len(out) == last_bold[0] + 2
                        and ID_ONLY.fullmatch(last_bold[1])):
                    out[last_bold[0]] = bold_line(f"{last_bold[1]} {text}")  # "3.1" + title
                    last_bold = None
                else:
                    out.extend([bold_line(text), ""])
                    last_bold = (len(out) - 2, text)
            elif was_bullet or is_bullet_line(text):
                run.append((text if was_bullet else text[2:].strip(),
                            lb[0], lb[2], True, lb[1], lb[3]))
            else:
                run.append((text, lb[0], lb[2], False, lb[1], lb[3]))
    flush_run()
    flush_code()
    out = collapse_word_runs(out) if merge else out
    # consecutive list items without a blank line in between
    return re.sub(r"(?m)^(- .*)\n\n(?=- )", r"\1\n", "\n".join(out))


def page_to_plain(data, columns=True, merge=True):
    """Plain text: paragraphs merged, code/list lines preserved."""
    out = []
    pending, last_bullets = False, False
    for block in order_blocks(data["blocks"], (), columns):
        if block.get("type") != 0:
            continue
        items, run = [], []  # items: (text, kind)

        def flush_run():
            for text, bullet in merge_lines(run, merge) if run else ():
                items.append((clean_leaders(text), "bullet" if bullet else "text"))
            run.clear()

        for line in block["lines"]:
            spans = [s for s in line["spans"] if s["text"].strip()]
            if not spans:
                continue
            raw = "".join(s["text"] for s in line["spans"])
            if all(is_mono(s) for s in spans) and not is_urlish(raw):
                flush_run()
                pending = False
                items.append((raw.rstrip(), "code"))
                continue
            text = raw.strip()
            if text in BULLET_GLYPHS:  # bullet glyph stands alone on the line
                pending = True
                continue
            bullet = is_bullet_line(text)
            if pending:
                text, bullet, pending = "• " + text, True, False
            run.append((text, line["bbox"][0], line["bbox"][2], bullet,
                        line["bbox"][1], line["bbox"][3]))
        flush_run()
        text = ""
        for k, (t, art) in enumerate(items):
            if k:
                same = art == items[k - 1][1] and art != "text"
                text += "\n" if same or not merge else "\n\n"
            text += t
        if text.strip():
            only_bullets = bool(items) and all(a == "bullet" for _, a in items)
            if only_bullets and last_bullets and out:
                out[-1] += "\n" + text  # list items from separate blocks
            else:
                out.append(text)
            last_bullets = only_bullets
    return "\n\n".join(out)


def continues_on_next_page(prev, nxt):
    """Does the last line end mid-sentence and the next one start lowercase?"""
    pl = prev.rstrip().split("\n")[-1]
    nl = nxt.lstrip().split("\n")[0]
    special = ("#", "|", "`", "- ", "!", "<!--", "**", "•", "[")
    return bool(pl and nl and not pl.startswith(special) and not nl.startswith(special)
                and not pl.rstrip().endswith(TERMINALS) and nl[0].islower())


def join_pages(texts):
    """Rejoin paragraphs that run across a page break."""
    out = []
    for text in texts:
        if out and continues_on_next_page(out[-1], text):
            head, _, tail = out[-1].rstrip().rpartition("\n")
            first, _, rest = text.lstrip().partition("\n")
            out[-1] = (head + "\n" if head else "") + join_line(tail, first) + "\n" + rest
        else:
            out.append(text)
    return out


def convert_pymupdf(path, fmt, pages_spec, password, page_markers,
                    tables=True, keep_headers=False, merge=True, columns=True,
                    image_dir=None, stats=None):
    with fitz.open(path) as doc:
        if doc.needs_pass and not doc.authenticate(password or ""):
            raise RuntimeError("PDF is password-protected (use --password)")
        idx = parse_pages(pages_spec, len(doc))
        build_hyphen_vocab(doc, idx)
        body = body_font_size(doc, idx) if fmt == "md" else 0
        want_images = image_dir is not None and fmt == "md"
        furniture, logos = ((set(), set()) if keep_headers
                            else detect_furniture(doc, idx, want_images))
        saved = {}
        chunks = []
        review = (stats or {}).get("review")
        textless = []  # pages that only consist of an image (scans)
        icfg = {"prefix": (stats or {}).get("img_prefix", "img"),
                "wiki": (stats or {}).get("img_wiki", False),
                "md_dir": (stats or {}).get("md_dir")}
        for i in idx:
            page = doc[i]
            data = text_dict(page)
            raw = dict_plain_text(data)
            data = strip_furniture(page, data, furniture)
            imgs = []
            if want_images:
                imgs, figs = extract_images(doc, page, image_dir, saved, logos, icfg)
                fblocks, frects = extract_formulas(page, data, image_dir, icfg)
                imgs += fblocks
                figs = list(figs) + frects
                if figs:  # text inside graphics / equations is already part of the image
                    data = drop_lines_in(data, figs)
            if len(raw.strip()) < 20 and not imgs and page.get_image_info():
                textless.append(i + 1)
                if want_images:  # keep the content as a page image
                    pg = save_page_image(page, image_dir, icfg)
                    imgs = [pg] if pg else []
            expected = dict_plain_text(data) if review is not None else ""
            if imgs:
                data = {**data, "blocks": data["blocks"] + imgs}
            text = (page_to_markdown(page, body, tables, data, columns, merge)
                    if fmt == "md" else page_to_plain(data, columns, merge))
            if review is not None:
                review.append(review_page(page, i + 1, raw, expected, text,
                                          True,
                                          image_dir if want_images else None,
                                          stats["img_budget"],
                                          stats["review_dir"] if len(idx) > REVIEW_IMAGE_LIMIT else None,
                                          stats["review_dir"].parent))
            if page_markers:
                chunks.append(f"\n<!-- Page {i + 1} -->\n" if fmt == "md"
                              else f"\n--- Page {i + 1} ---\n")
            chunks.append(text.strip("\n") + "\n")
        if merge and not page_markers:
            chunks = join_pages(chunks)
        if textless:
            n = len(textless)
            if not (stats is not None and stats.get("quiet")):
                print(f"WARNING: {n} image-only page(s) without a text layer (first: "
                      f"{textless[0]}). No OCR: "
                      + ("they were saved as page images." if want_images
                         else "use -f md to keep them as page images."),
                      file=sys.stderr)
            if want_images:
                chunks.insert(0, f"<!-- Note: {n} page(s) consist only of an image "
                                 f"and were saved as page images; no text extracted. -->\n")
        if stats is not None:
            stats["images"] = len(saved)
            stats["textless"] = len(textless)
    return "\n".join(chunks), len(idx)


# ------------------------------------------------------------ pypdf backend

def convert_pypdf(path, pages_spec, password, page_markers):
    # A dedicated file context also works with older pypdf versions.
    with Path(path).open("rb") as stream:
        reader = PdfReader(stream)
        if reader.is_encrypted and not reader.decrypt(password or ""):
            raise RuntimeError("PDF is password-protected (use --password)")
        idx = parse_pages(pages_spec, len(reader.pages))
        chunks = []
        for i in idx:
            if page_markers:
                chunks.append(f"\n--- Page {i + 1} ---\n")
            chunks.append((reader.pages[i].extract_text() or "").strip("\n") + "\n")
    return "\n".join(chunks), len(idx)



# ------------------------------------------------------------------- review

REVIEW_IMAGE_LIMIT = 200  # more pages: page images go to <name>.review/ instead of into the HTML
WORD = re.compile(r"\w+", re.UNICODE)
TOOL_COMMENT = r"<!--\s*(?:Page \d+|Note: .*?|formula: .*?)-->"
MD_NOISE = re.compile(r"!\[\[.*?\]\]|!\[[^\]]*\]\([^)]*\)|" + TOOL_COMMENT, re.S)


def page_image(page, out_dir=None, number=0):
    """Data URI of a small page image; with out_dir a JPEG file is written there
    instead and its name is returned (large PDFs, keeps the HTML small)."""
    if out_dir is not None:
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            name = f"p{number:05d}.jpg"
            pix = page.get_pixmap(dpi=80, alpha=False)
            (out_dir / name).write_bytes(pix.tobytes("jpeg", jpg_quality=70))
            return quote(out_dir.name) + "/" + name
        except Exception:  # noqa: BLE001
            return ""
    try:
        pix = page.get_pixmap(dpi=70, alpha=False)
        try:
            return "data:image/jpeg;base64," + base64.b64encode(
                pix.tobytes("jpeg", jpg_quality=62)).decode()
        except Exception:  # noqa: BLE001
            return "data:image/png;base64," + base64.b64encode(pix.tobytes("png")).decode()
    except Exception:  # noqa: BLE001
        return ""



# Minimal Markdown renderer for the review's "preview" pane (no dependencies).
# Covers what this tool writes: headings, paragraphs, lists, tables, code, images.
PREVIEW_IMG_LIMIT = 600_000      # bytes per embedded image
PREVIEW_IMG_TOTAL = 60_000_000   # all embedded images together


def _preview_image(name, image_dir, budget, html_dir=None):
    if image_dir is None:
        return ""
    f = Path(image_dir) / Path(name).name
    try:
        size = f.stat().st_size
        if size > PREVIEW_IMG_LIMIT or budget[0] + size > PREVIEW_IMG_TOTAL:
            if html_dir is None:
                return ""
            return quote(Path(os.path.relpath(f, html_dir)).as_posix())  # linked, not embedded
        budget[0] += size
        mime = "image/jpeg" if f.suffix.lower() in (".jpg", ".jpeg") else "image/png"
        return f"data:{mime};base64," + base64.b64encode(f.read_bytes()).decode("ascii")
    except OSError:
        return ""


def _inline_md(text, image_dir, budget, html_dir=None):
    holes = []

    def hold(h):
        holes.append(h)
        return f"\x00{len(holes) - 1}\x00"

    def img(name, alt):
        src = _preview_image(name, image_dir, budget, html_dir)
        if src:
            return hold(f'<img class="pv" src="{src}" alt="{html.escape(alt)}">')
        return hold(f'<span class="noimg">[image: {html.escape(name)}]</span>')

    text = re.sub(r"`([^`]+)`", lambda m: hold(f"<code>{html.escape(m.group(1))}</code>"), text)
    text = re.sub(r"!\[\[([^\]|]+)(?:\|[^\]]*)?\]\]", lambda m: img(m.group(1), m.group(1)), text)
    text = re.sub(r"!\[([^\]]*)\]\(([^)]*)\)",
                  lambda m: img(unquote(m.group(2)), m.group(1)), text)
    text = re.sub(r"\[\[([^\]|]+)(?:\|([^\]]*))?\]\]",
                  lambda m: hold(f'<a class="wl">{html.escape(m.group(2) or m.group(1))}</a>'), text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]*)\)",
                  lambda m: hold(f'<a class="wl">{html.escape(m.group(1))}</a>'), text)
    text = html.escape(text, quote=False)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<em>\1</em>", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: holes[int(m.group(1))], text)


def md_to_html(md, image_dir=None, budget=None, html_dir=None):
    budget = budget if budget is not None else [0]
    md = re.sub(TOOL_COMMENT, "", md, flags=re.S)
    lines = md.split("\n")
    out, i, n = [], 0, len(lines)
    inl = lambda t: _inline_md(t, image_dir, budget, html_dir)  # noqa: E731
    while i < n:
        ln = lines[i]
        if not ln.strip():
            i += 1
            continue
        m = re.match(r"^(```|~~~)", ln)
        if m:
            i += 1
            code = []
            while i < n and not lines[i].startswith(m.group(1)):
                code.append(lines[i])
                i += 1
            i += 1
            out.append("<pre class=\"code\">" + html.escape("\n".join(code)) + "</pre>")
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", ln)
        if m:
            lv = len(m.group(1))
            out.append(f"<h{lv}>{inl(m.group(2))}</h{lv}>")
            i += 1
            continue
        if re.match(r"^\s*([-*_])(\s*\1){2,}\s*$", ln):
            out.append("<hr>")
            i += 1
            continue
        if ln.lstrip().startswith("|") and i + 1 < n and re.match(r"^\s*\|?[\s:|-]+\|[\s:|-]*$", lines[i + 1]):
            def cells(r):
                r = r.strip().strip("|")
                return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", r)]
            head = cells(ln)
            i += 2
            body = []
            while i < n and lines[i].lstrip().startswith("|"):
                body.append(cells(lines[i]))
                i += 1
            t = "<table class=\"mdt\"><thead><tr>" + "".join(f"<th>{inl(c)}</th>" for c in head) + "</tr></thead><tbody>"
            for r in body:
                t += "<tr>" + "".join(f"<td>{inl(c)}</td>" for c in r) + "</tr>"
            out.append(t + "</tbody></table>")
            continue
        if ln.startswith(">"):
            q = []
            while i < n and lines[i].startswith(">"):
                q.append(lines[i].lstrip("> ").rstrip())
                i += 1
            out.append("<blockquote>" + inl(" ".join(q)) + "</blockquote>")
            continue
        m = re.match(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$", ln)
        if m:
            stack = []  # (indent, tag)
            while i < n:
                m = re.match(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$", lines[i])
                if not m:
                    if lines[i].strip() and lines[i].startswith(" ") and stack:  # continuation
                        out.append(" " + inl(lines[i].strip()))
                        i += 1
                        continue
                    break
                ind, tag = len(m.group(1)), ("ol" if m.group(2)[0].isdigit() else "ul")
                while stack and ind < stack[-1][0]:
                    out.append(f"</li></{stack.pop()[1]}>")
                if stack and ind == stack[-1][0]:
                    out.append("</li>")
                else:
                    stack.append((ind, tag))
                    out.append(f"<{tag}>")
                out.append("<li>" + inl(m.group(3)))
                i += 1
            while stack:
                out.append(f"</li></{stack.pop()[1]}>")
            continue
        para = []
        while i < n and lines[i].strip() and not re.match(r"^(#{1,6}\s|```|~~~|>|\s*([-*+]|\d+[.)])\s)", lines[i]) \
                and not lines[i].lstrip().startswith("|"):
            para.append(lines[i].strip())
            i += 1
        if not para:  # safety: never loop forever
            para, i = [ln.strip()], i + 1
        out.append("<p>" + inl("<br>".join(para)).replace("&lt;br&gt;", "<br>") + "</p>")
    return "\n".join(out)


def review_page(page, number, raw, expected, converted, with_image, image_dir=None, budget=None,
                page_dir=None, html_dir=None):
    """Ordered word diff of one page: expected PDF text vs. converted text."""
    converted_view = converted
    converted = re.sub(r"[`*]", "", converted)  # emphasis / code marks must not split words
    exp_toks = WORD.findall(expected)
    exp_keys = [w.casefold() for w in exp_toks]
    toks, pos = [], 0  # (text, is_word, key)

    def add(text):
        for m in re.finditer(r"\w+|\W+", text):
            t = m.group(0)
            word = bool(WORD.fullmatch(t))
            toks.append((t, word, t.casefold() if word else None))
    for m in MD_NOISE.finditer(converted):
        add(converted[pos:m.start()])
        toks.append((m.group(0), False, None))  # image link / comment: not compared
        pos = m.end()
    add(converted[pos:])
    widx = [i for i, t in enumerate(toks) if t[1]]
    out_keys = [toks[i][2] for i in widx]

    marks, dels, joined = {}, {}, []
    del_list, ins_list = [], []  # (position, word) / position
    sm = SequenceMatcher(None, exp_keys, out_keys, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        a = exp_toks[i1:i2]
        if tag == "replace" and "".join(exp_keys[i1:i2]) == "".join(out_keys[j1:j2]):
            for k in range(j1, j2):
                marks[k] = ("j", " ".join(a))
            joined.append((" ".join(a), " ".join(toks[widx[k]][0] for k in range(j1, j2))
                           if j2 - j1 > 1 else toks[widx[j1]][0]))
            continue
        if tag in ("delete", "replace"):
            del_list.extend((j1, w) for w in a)
        if tag in ("insert", "replace"):
            ins_list.extend(range(j1, j2))
    # words that are "missing" in one place and "extra" in another were only moved
    # (reading order differs, e.g. tables or columns)
    budget = Counter(w.casefold() for _, w in del_list) & Counter(out_keys[k] for k in ins_list)
    moved = sum(budget.values())
    left = Counter(budget)
    for j, w in del_list:
        key = w.casefold()
        cls = "v" if left.get(key, 0) > 0 else "d"
        if cls == "v":
            left[key] -= 1
        dels.setdefault(j, []).append((w, cls))
    left = Counter(budget)
    for k in ins_list:
        cls = "v" if left.get(out_keys[k], 0) > 0 else "x"
        if cls == "v":
            left[out_keys[k]] -= 1
        marks[k] = (cls, "")
    missing = len(del_list) - moved
    extra = len(ins_list) - moved

    parts, k = [], 0

    def flush(at):
        for w, cls in dels.get(at, ()):
            tip = ("in the PDF at another position (moved)" if cls == "v"
                   else "in the PDF, missing here")
            parts.append(f'<del class="{cls}" title="{tip}">{html.escape(w)}</del> ')
    for text, word, _key in toks:
        if word:
            flush(k)
            m = marks.get(k)
            if m:
                tip = (f"PDF: {html.escape(m[1])}" if m[0] == "j"
                       else "moved: also in the PDF at another position" if m[0] == "v"
                       else "not in the PDF text")
                parts.append(f'<mark class="{m[0]}" title="{tip}">{html.escape(text)}</mark>')
            else:
                parts.append(html.escape(text))
            k += 1
        else:
            parts.append(html.escape(text))
    flush(k)
    removed = len(WORD.findall(raw)) - len(exp_toks)
    return {"n": number, "image": page_image(page, page_dir, number) if with_image else "",
            "pane": "".join(parts), "total": len(exp_keys), "missing": missing,
            "extra": extra, "moved": moved, "joined": joined, "removed": max(removed, 0),
            "preview": md_to_html(converted_view, image_dir, budget, html_dir)}


def build_review(title, pages, fmt):
    """Self-contained HTML viewer: PDF page left, rendered Markdown (or word diff) right."""
    items, sections = [], []
    for pg in pages:
        miss, extra, nj, mv = pg["missing"], pg["extra"], len(pg["joined"]), pg["moved"]
        total = max(pg["total"], 1)
        cov = 100.0 * (1 - min(miss, total) / total)
        state = ("bad" if (miss or extra) and cov < 90 else "warn" if (miss or extra)
                 else "note" if (nj or mv) else "ok")
        n = pg["n"]
        tip = f"Page {n}: {miss} missing, {extra} extra, {mv} moved, {nj} re-joined"
        items.append(f'<a href="#p{n}" class="pi {state}" data-n="{n}" title="{tip}">'
                     f'<i></i>{n}</a>')
        img = (f'<img loading="lazy" src="{pg["image"]}" alt="PDF page {n}">' if pg["image"]
               else '<div class="noimg">page image omitted (too many pages)</div>')
        notes = ""
        if nj:
            shown = "".join(f"<li>{html.escape(a)} → <b>{html.escape(b)}</b></li>"
                            for a, b in pg["joined"][:12])
            more = f"<li>… and {nj - 12} more</li>" if nj > 12 else ""
            notes += (f'<details class="notes"><summary>{nj} re-joined word group(s) '
                      f"(PDF → output; check for lost hyphens)</summary><ul>{shown}{more}</ul></details>")
        if pg["removed"]:
            notes += (f'<p class="notes muted">{pg["removed"]} words dropped on purpose '
                      "(header/footer, text inside figures)</p>")
        stat = f"{miss} missing · {extra} extra · {mv} moved · {nj} re-joined"
        sections.append(
            f'<section id="p{n}" data-state="{state}"><div class="pdf"><div class="tag">PDF · page {n}</div>{img}</div>'
            f'<div class="md"><div class="tag">Markdown · page {n} <span class="st {state}">{stat}</span></div>'
            f'<article class="preview">{pg["preview"] or "<p class=muted>(empty page)</p>"}</article>'
            f'<pre class="diffv">{pg["pane"]}</pre>{notes}</div></section>')
    tot = {k: sum(p[k] for p in pages) for k in ("missing", "extra", "moved")}
    tot_join = sum(len(p["joined"]) for p in pages)
    words = max(sum(p["total"] for p in pages), 1)
    cov = 100.0 * (1 - min(tot["missing"], words) / words)
    summary = (f'{len(pages)} pages · {words} words · <b>{cov:.2f} %</b> coverage · '
               f'{tot["missing"]} missing · {tot["extra"]} extra · {tot["moved"]} moved · {tot_join} re-joined')
    css = """
:root{--bg:#1e1e1e;--bg2:#262626;--bg3:#2f2f2f;--fg:#dcddde;--mut:#8a8a8a;--line:#3a3a3a;--acc:#a78bfa;--h:#fff;--code:#2b2b2b}
body.light{--bg:#fff;--bg2:#f5f5f7;--bg3:#e9e9ee;--fg:#222;--mut:#777;--line:#d8d8de;--acc:#7b3ff2;--h:#000;--code:#f1f1f4}
*{box-sizing:border-box}html,body{height:100%}
body{margin:0;display:flex;flex-direction:column;background:var(--bg);color:var(--fg);font:14px system-ui,sans-serif}
header{flex:none;display:flex;flex-wrap:wrap;align-items:center;gap:6px 14px;padding:8px 14px;background:var(--bg2);border-bottom:1px solid var(--line)}
header h1{font-size:14px;margin:0;font-weight:600}.sum{color:var(--mut);font-size:12.5px}.sum b{color:var(--fg)}
.sp{flex:1}.seg{display:inline-flex;border:1px solid var(--line);border-radius:6px;overflow:hidden}
button{font:inherit;background:var(--bg3);color:var(--fg);border:0;padding:4px 11px;cursor:pointer}
button:hover{background:var(--line)}.seg button.on{background:var(--acc);color:#fff}
.btn{border:1px solid var(--line);border-radius:6px}label{cursor:pointer;color:var(--mut)}
.legend{display:none;width:100%;font-size:12px;color:var(--mut)}body.diff .legend{display:block}
#wrap{flex:1;display:flex;min-height:0}
nav{flex:none;width:64px;overflow-y:auto;background:var(--bg2);border-right:1px solid var(--line);padding:6px 0}
.pi{display:flex;align-items:center;gap:6px;padding:3px 10px;color:var(--mut);text-decoration:none;font-size:12.5px}
.pi:hover{background:var(--bg3)}.pi.cur{background:var(--bg3);color:var(--fg);font-weight:600}
.pi i{width:8px;height:8px;border-radius:50%;background:#3fb26b}.pi.note i{background:#6c8fb8}
.pi.warn i{background:#e8833a}.pi.bad i{background:#e5484d}
main{flex:1;overflow-y:auto;scroll-behavior:smooth}
section{display:grid;grid-template-columns:1fr 1fr;border-bottom:3px solid var(--line);scroll-margin-top:0}
.pdf,.md{padding:14px 18px;min-width:0}.pdf{background:var(--bg2);border-right:1px solid var(--line)}
.tag{font-size:11.5px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);margin-bottom:8px}
.st{text-transform:none;letter-spacing:0;margin-left:8px}.st.ok{color:#3fb26b}.st.note{color:#6c8fb8}.st.warn{color:#e8833a}.st.bad{color:#e5484d}
.pdf img{display:block;width:100%;background:#fff;box-shadow:0 1px 6px #0006}
.noimg{padding:30px;color:var(--mut);border:1px dashed var(--line)}
.preview{font:16px/1.65 -apple-system,"Segoe UI",system-ui,sans-serif;max-width:46em}
.preview h1,.preview h2,.preview h3,.preview h4{color:var(--h);line-height:1.25;margin:1.2em 0 .4em}
.preview>:first-child{margin-top:0}.preview h1{font-size:2em}.preview h2{font-size:1.55em}.preview h3{font-size:1.3em}
.preview p{margin:.65em 0}.preview a.wl{color:var(--acc)}.preview ul,.preview ol{padding-left:1.6em}
.preview code,.preview pre.code{font:.88em ui-monospace,Menlo,monospace;background:var(--code);border-radius:4px;padding:1px 5px}
.preview pre.code{display:block;padding:10px;overflow:auto;white-space:pre}
.preview blockquote{margin:.7em 0;padding:0 14px;border-left:3px solid var(--acc);color:var(--mut)}
.preview hr{border:0;border-top:1px solid var(--line)}
.preview table.mdt{border-collapse:collapse;margin:.8em 0;display:block;overflow-x:auto}
.mdt td,.mdt th{border:1px solid var(--line);padding:4px 10px;text-align:left}.mdt th{background:var(--bg3)}
.preview img.pv{max-width:100%;height:auto;display:block;margin:.8em 0;background:#fff}
.preview .noimg{display:inline-block;padding:3px 8px}
pre.diffv{display:none;white-space:pre-wrap;word-break:break-word;margin:0;font:13px/1.55 ui-monospace,Menlo,monospace}
body.diff pre.diffv{display:block}body.diff .preview{display:none}
.notes{margin-top:10px;font-size:12.5px;color:var(--mut)}body:not(.diff) .notes:not(.muted){display:none}
.muted{color:var(--mut)}.notes ul{margin:4px 0 0 18px;padding:0}.notes b{color:var(--fg)}
mark.x{background:#1f5f99;color:#fff}mark.j{background:#8a6d00;color:#fff}mark.v{background:#5b3ca8;color:#fff}
del.v{background:#5b3ca8;color:#fff;text-decoration:line-through}
del.d{background:#8a1f2e;color:#fff;text-decoration:none;border-bottom:2px solid #e5484d;padding:0 2px}
body.light mark.x{background:#bfe0ff;color:#000}body.light mark.j{background:#ffe08a;color:#000}
body.light mark.v,body.light del.v{background:#e4d4ff;color:#3b1d80}body.light del.d{background:#ffc9c9;color:#8a0020}
.hide-j mark.j{background:none;color:inherit}
.cur-d{outline:3px solid var(--acc);outline-offset:2px}
@media(max-width:900px){section{grid-template-columns:1fr}nav{display:none}}
"""
    js = """
const $=id=>document.getElementById(id),main=document.querySelector('main'),B=document.body;
function setDiff(d){B.classList.toggle('diff',d);$('bPrev').classList.toggle('on',!d);$('bDiff').classList.toggle('on',d)}
$('bPrev').onclick=()=>setDiff(false);$('bDiff').onclick=()=>setDiff(true);
$('theme').onclick=()=>B.classList.toggle('light');
$('only').onchange=filt;$('showj').onchange=()=>{B.classList.toggle('hide-j',!$('showj').checked)};
function filt(){const o=$('only').checked;
 document.querySelectorAll('section').forEach(s=>s.style.display=(o&&s.dataset.state==='ok')?'none':'');
 document.querySelectorAll('.pi').forEach(a=>a.style.display=(o&&a.classList.contains('ok'))?'none':'')}
let items=[],cur=-1;
function go(d){if(!B.classList.contains('diff'))setDiff(true);
 items=[...document.querySelectorAll('pre.diffv mark.x,pre.diffv del.d,pre.diffv mark.v,pre.diffv del.v'
  +($('showj').checked?',pre.diffv mark.j':''))].filter(e=>e.closest('section').style.display!=='none');
 if(!items.length)return;if(items[cur])items[cur].classList.remove('cur-d');
 cur=(cur+d+items.length)%items.length;const e=items[cur];e.classList.add('cur-d');
 e.scrollIntoView({block:'center'})}
$('next').onclick=()=>go(1);$('prev').onclick=()=>go(-1);
addEventListener('keydown',e=>{if(e.target.tagName==='INPUT')return;
 if(e.key==='n')go(1);else if(e.key==='p')go(-1);else if(e.key==='d')setDiff(!B.classList.contains('diff'))});
const links=[...document.querySelectorAll('.pi')];
const io=new IntersectionObserver(es=>{es.forEach(e=>{if(e.isIntersecting){
 links.forEach(l=>l.classList.toggle('cur',l.dataset.n===e.target.id.slice(1)));
 const a=document.querySelector('.pi.cur');if(a)a.scrollIntoView({block:'nearest'})}})},
 {root:main,rootMargin:'-10% 0px -80% 0px'});
document.querySelectorAll('section').forEach(s=>io.observe(s));
document.querySelectorAll('.pi').forEach(a=>a.onclick=e=>{e.preventDefault();
 document.getElementById('p'+a.dataset.n).scrollIntoView()});
"""
    return (f'<!doctype html><html><head><meta charset="utf-8"><title>Review: {html.escape(title)}</title>'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><style>{css}</style></head>'
            f'<body><header><h1>{html.escape(title)}</h1><span class="sum">{summary}</span><span class="sp"></span>'
            '<span class="seg"><button id="bPrev" class="on">Preview</button><button id="bDiff">Diff</button></span>'
            '<span><button id="prev" class="btn">◀</button> <button id="next" class="btn">next difference ▶</button></span>'
            '<label><input type="checkbox" id="only"> differences only</label>'
            '<label><input type="checkbox" id="showj" checked> re-joined</label>'
            '<button id="theme" class="btn">Light/Dark</button>'
            '<div class="legend"><del class="d">red</del> in the PDF, missing in the output · '
            '<mark class="x">blue</mark> only in the output · <mark class="v">lilac</mark> moved (other reading order) · '
            '<mark class="j">amber</mark> re-joined (hyphenation / line wraps) · keys: n / p / d</div></header>'
            f'<div id="wrap"><nav>{"".join(items)}</nav><main>{"".join(sections)}</main></div>'
            f"<script>{js}</script></body></html>")


# --------------------------------------------------------------------- main

def convert(path, fmt, pages_spec, password, page_markers,
            tables=True, keep_headers=False, merge=True,
            columns=True, image_dir=None, stats=None):
    if fitz is not None:
        return convert_pymupdf(path, fmt, pages_spec, password, page_markers,
                               tables, keep_headers, merge, columns, image_dir, stats)
    if PdfReader is not None:
        if fmt == "md":
            print("  Note: without PyMuPDF only plain text is produced "
                  "(pip install pymupdf)", file=sys.stderr)
        return convert_pypdf(path, pages_spec, password, page_markers)
    raise SystemExit("Neither PyMuPDF nor pypdf is installed: pip install pymupdf")


def safe_name(title, limit=80):
    name = re.sub(r"[\\/:*?\"<>|#^\[\]]+", " ", re.sub(r"[`*_]", "", title))
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:limit].rstrip(" .") or "Untitled"


def split_markdown(text, level):
    """Split Markdown at headings of level <= `level` (code fences are respected).
    Returns [(title, body)]; text before the first heading becomes 'Front matter'."""
    parts, title, buf, fence = [], "Front matter", [], False
    for line in text.split("\n"):
        if line.startswith("```"):
            fence = not fence
        m = None if fence else re.match(r"(#{1,6}) (.+)", line)
        if m and len(m.group(1)) <= level:
            parts.append((title, buf))
            title, buf = m.group(2).strip(), []
        buf.append(line)
    parts.append((title, buf))
    result = []
    for t, b in parts:
        body = "\n".join(b).strip("\n")
        if body.strip() and (t != "Front matter" or body.strip()):
            result.append((t, body + "\n"))
    return result


def write_split(folder, text, level, overwrite=False):
    """One note per chapter plus '00 Index.md' with wikilinks. Returns the file count."""
    chunks = split_markdown(text, level)
    index = ["# Index", ""]
    names = []
    for n, (title, body) in enumerate(chunks, 1):
        name = f"{n:03d} {safe_name(title)}"
        names.append(name)
        write_atomic(folder / f"{name}.md", body, overwrite)
        index.append(f"- [[{name}|{safe_name(title, 120)}]]")
    write_atomic(folder / "000 Index.md", "\n".join(index) + "\n", overwrite)
    return len(chunks)


def write_atomic(target, text, overwrite=False):
    """Write completely first, then publish; protects existing files."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=target.parent, prefix=".pdf2md-",
                                         suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text)
        if overwrite:
            os.replace(temporary, target)
        elif os.name == "nt":
            os.rename(temporary, target)  # Windows: fails if the target exists
        else:
            os.link(temporary, target)  # POSIX: exclusive, never overwrites
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="PDF -> TXT/Markdown converter (images are saved, no OCR)")
    ap.add_argument("input", help="PDF file or folder")
    ap.add_argument("-o", "--output", help="output file or folder")
    ap.add_argument("-f", "--format", choices=["txt", "md"], default="txt",
                    help="output format (default: txt)")
    ap.add_argument("-p", "--pages", help="pages, e.g. 1-3,7")
    ap.add_argument("-r", "--recursive", action="store_true",
                    help="search folders recursively")
    ap.add_argument("--password", help="password for encrypted PDFs")
    ap.add_argument("--page-markers", action="store_true",
                    help="insert page separators (md: <!-- Page N -->, "
                         "txt: --- Page N ---)")
    ap.add_argument("--no-tables", action="store_true",
                    help="do not create Markdown tables (running text only)")
    ap.add_argument("--keep-headers", action="store_true",
                    help="keep recurring headers/footers and page numbers")
    ap.add_argument("--keep-linebreaks", action="store_true",
                    help="do not resolve the PDF's line breaks and hyphenation")
    ap.add_argument("--no-columns", action="store_true",
                    help="no column detection (keep the PDF's order)")
    ap.add_argument("--no-images", action="store_true",
                    help="do not save images/graphics (default with -f md)")
    ap.add_argument("--md-links", action="store_true",
                    help="link images as standard Markdown ![alt](path) instead of "
                         "Obsidian wikilinks ![[file.png]] (the default for -f md)")
    ap.add_argument("--obsidian", action="store_true", help=argparse.SUPPRESS)  # now default
    ap.add_argument("--image-dir", metavar="DIR",
                    help="shared image folder (e.g. your Obsidian attachments "
                         "folder) instead of IMG-<name>/")
    ap.add_argument("--split", type=int, choices=[1, 2, 3], metavar="LEVEL",
                    help="with -f md: write one note per chapter (split at headings up "
                         "to this level, 1-3) into a folder <name>/ plus an index note")
    ap.add_argument("--review", action="store_true",
                    help="also write <name>.review.html: PDF page next to the converted "
                         "text with missing/extra words marked (use -p for large PDFs)")
    ap.add_argument("--overwrite", action="store_true",
                    help="overwrite existing output files")
    args = ap.parse_args(argv)

    src = Path(args.input).expanduser()
    if not src.exists():
        ap.error(f"Not found: {src}")
    if not src.is_file() and not src.is_dir():
        ap.error(f"Neither a regular file nor a folder: {src}")
    try:
        pdfs = collect_pdfs(src, args.recursive)
    except ValueError as exc:
        ap.error(str(exc))
    if not pdfs:
        ap.error("No PDF files found.")
    if fitz is None and PdfReader is None:
        ap.error("Neither PyMuPDF nor pypdf is installed: pip install pymupdf")
    if args.split and args.format != "md":
        ap.error("--split works only with -f md")
    save_images = args.format == "md" and fitz is not None and not args.no_images

    out_arg = Path(args.output).expanduser() if args.output else None
    single_file_out = (out_arg is not None and len(pdfs) == 1
                       and src.is_file() and not out_arg.is_dir()
                       and out_arg.suffix != "")
    if out_arg and out_arg.exists() and not single_file_out and not out_arg.is_dir():
        ap.error(f"Output folder is a file: {out_arg}")

    # Check all targets up front: never destroy source files or colliding names.
    jobs = []
    seen_targets = {}
    source_paths = {os.path.normcase(str(pdf.resolve())) for pdf in pdfs}
    for pdf in pdfs:
        if single_file_out:
            target = out_arg
        else:
            base = out_arg if out_arg else pdf.parent
            if out_arg and src.is_dir():
                base = out_arg / pdf.parent.relative_to(src)
            target = base / (pdf.stem + "." + args.format)

        key = os.path.normcase(str(target.resolve()))
        if target.suffix.lower() == ".pdf" or key in source_paths:
            ap.error(f"Output must not be a PDF source file: {target}")
        if target.exists() and target.samefile(pdf):
            ap.error(f"Output and input are the same file: {target}")
        if key in seen_targets:
            ap.error(f"Output collision: {seen_targets[key]} and {pdf} -> {target}")
        seen_targets[key] = pdf
        jobs.append((pdf, target))

    ok = fail = skipped = 0
    multi = len(jobs) > 1
    t_all = time.monotonic()
    problems = []
    if multi:
        width = shutil.get_terminal_size((100, 20)).columns
        nw = len(str(len(jobs))) * 2 + 1
        name_w = max(20, min(max(len(p.stem) for p, _ in jobs), width - nw - 42))

        def clip(t, w):
            return t if len(t) <= w else t[:w - 1] + "…"

        def row(i, status, name, pages, secs, note):
            print(f"{i:>{nw}} | {status:<6} | {clip(name, name_w):<{name_w}} | "
                  f"{pages:>6} | {secs:>6} | {note}")

        pend = []  # consecutive skips are collapsed into a single row

        def flush_skips():
            if not pend:
                return
            i0, name0, why = pend[0]
            if len(pend) == 1:
                row(i0, "skip", name0, "-", "-", why)
            else:
                row(f"{i0}-{pend[-1][0]}", "skip", f"{len(pend)} files", "-", "-", why)
            pend.clear()

        row("#", "Status", "File", "Pages", "Time", "Note")
        print("-" * min(width, nw + name_w + 42))

    for i, (pdf, target) in enumerate(jobs, 1):
        t0 = time.monotonic()
        split_dir = target.parent / target.stem
        if args.split:
            target_exists = split_dir.is_dir() and any(split_dir.iterdir())
        else:
            target_exists = target.exists()
        if target_exists and not args.overwrite:
            if multi:
                pend.append((i, pdf.stem, "target exists (--overwrite)"))
            else:
                print(f"[skip] {target} exists (use --overwrite)")
            skipped += 1
            continue
        shared = Path(args.image_dir).expanduser() if args.image_dir else None
        image_dir = ((shared or target.with_name("IMG-" + target.stem))
                     if save_images else None)
        if (image_dir is not None and shared is None and image_dir.exists()
                and any(image_dir.iterdir()) and not args.overwrite):
            if multi:
                pend.append((i, pdf.stem, "image folder exists (--overwrite)"))
            else:
                print(f"[skip] {image_dir} exists (use --overwrite)")
            skipped += 1
            continue
        stats = {"img_budget": [0], "quiet": multi, "review": [] if args.review else None,
                 "img_prefix": re.sub(r"[^\w\-]+", "_", target.stem).strip("_")[:60] or "img",
                 "img_wiki": not args.md_links,
                 "review_dir": (split_dir.parent if args.split else target.parent) / f"{target.stem}.review",
                 "md_dir": split_dir if args.split else target.parent}
        try:
            text, n = convert(pdf, args.format, args.pages,
                              args.password, args.page_markers,
                              not args.no_tables,
                              keep_headers=args.keep_headers,
                              merge=not args.keep_linebreaks,
                              columns=not args.no_columns,
                              image_dir=image_dir, stats=stats)
            notes = []
            if args.review:
                rp = (split_dir.parent if args.split else target.parent) / f"{target.stem}.review.html"
                write_atomic(rp, build_review(pdf.name, stats["review"], args.format), True)
                notes.append("review.html")
            if args.split:
                notes.append(f"{write_split(split_dir, text, args.split, args.overwrite)} notes")
            else:
                write_atomic(target, text, args.overwrite)
            if stats.get("images"):
                notes.append(f"{stats['images']} images")
            if stats.get("textless"):
                k = stats["textless"]
                notes.append(f"{k} image-only page(s)" +
                             (", saved as images" if save_images else ", no text"))
                problems.append(f"{pdf.name}: {k} image-only page(s), no text extracted")
            status = "ok"
            if not text.strip():
                notes = ["no text found"]
                status = "empty"
                problems.append(f"{pdf.name}: no text found")
            if multi:
                flush_skips()
                row(i, status, pdf.stem, n, f"{time.monotonic() - t0:.1f}s", ", ".join(notes))
            else:
                note = ("  (" + ", ".join(notes) + ")") if notes else ""
                print(f"[ok]   {pdf.name} -> {split_dir if args.split else target}  ({n} pages){note}")
            ok += 1
        except FileExistsError:
            if multi:
                pend.append((i, pdf.stem, "created in the meantime"))
            else:
                print(f"[skip] {target} was created in the meantime")
            skipped += 1
        except Exception as exc:  # noqa: BLE001
            if multi:
                flush_skips()
                row(i, "ERROR", pdf.stem, "-", "-", str(exc)[:80])
            else:
                print(f"[error] {pdf}: {exc}", file=sys.stderr)
            problems.append(f"{pdf.name}: {exc}")
            fail += 1

    if multi:
        flush_skips()
        print("-" * min(width, nw + name_w + 42))
    print(f"\nDone: {ok} succeeded, {skipped} skipped, {fail} errors"
          + (f"  ({time.monotonic() - t_all:.0f}s)" if multi else "."))
    if problems:
        print("Notable:")
        for p in problems[:20]:
            print(f"  - {p}")
        if len(problems) > 20:
            print(f"  ... and {len(problems) - 20} more")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
