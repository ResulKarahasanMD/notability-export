"""Notability .nbn / .note -> PDF (with handwriting drawn) + Markdown text + audio.

Format facts this relies on (derived from the notes themselves, see README):
- Session.plist is an NSKeyedArchiver; root '$0' is a NoteTakingSession.
- The note is one vertical canvas of width W (reflowState.pageWidthInDocumentCoordsKey).
- Pages stack with no gap; a PDF-backed page is W * (pdf_h / pdf_w) tall,
  a paper page is W * paper ratio (letter 11/8.5, a4/a3 297/210).
- Ink lives in richText['Handwriting Overlay'].SpatialHash: curvespoints (float32 x,y),
  curvesnumpoints (int32 per curve), each curve a cubic bezier chain of 3k+1 points,
  curvesfractionalwidths (float32, k+1 per curve), curveswidth (float32), curvescolors (RGBA u8),
  curvesstyles (u8: 3 pen, 4 highlighter, 5 other).
- HandwritingIndex/index.plist holds Notability's own handwriting recognition per page (1-based).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import plistlib
import re
import shutil
import signal
import struct
import sys
import tempfile
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

pymupdf.TOOLS.mupdf_display_errors(False)  # keep stdout/stderr clean; problems are reported as warnings

FONT = "/System/Library/Fonts/Supplemental/Arial Unicode.ttf"
# Height/width of a paper (non-PDF) page in canvas units. "letter" is 21/16, not 11/8.5: measured against
# HandwritingIndex.pageContentOrigin on multi-page paper notes (residual < 1 unit on every page).
# a4/a3 could not be measured on local notes; each note is re-checked against its own index (fit_paper_ratio).
PAPER_RATIO = {"letter": 21 / 16, "a4": 297 / 210, "a3": 297 / 210}
DEFAULT_RATIO = 21 / 16
RATIO_CANDIDATES = (21 / 16, 11 / 8.5, 297 / 210)
# Accepted |top edge of the first stroke - index origin| on a page, canvas units. Whether highlighter
# strokes belong to the index differs between notes (seen both ways), so a page agrees when either the
# first pen stroke or the first stroke of any kind matches. The index box follows the stroke outline,
# so thick strokes need a wider margin than thin ones.
INDEX_TOL = 1.5
INDEX_TOL_PER_WIDTH = 0.15
BLANK_PAGE_WIDTH = 612.0  # output width (pt) for pages without a PDF behind them
APPLE_EPOCH = dt.datetime(2001, 1, 1, tzinfo=dt.timezone.utc)


class Archive:
    def __init__(self, path: Path):
        p = plistlib.load(open(path, "rb"))
        self.o = p["$objects"]
        self.top = p["$top"]

    def r(self, x):
        return self.o[x.data] if isinstance(x, plistlib.UID) else x

    def cls(self, d) -> str | None:
        c = d.get("$class") if isinstance(d, dict) else None
        return self.r(c)["$classname"] if c is not None else None

    def items(self, d):
        """NSDictionary -> dict, NSArray -> list, str-like -> str."""
        d = self.r(d)
        if isinstance(d, dict):
            if "NS.keys" in d:
                return {self.r(k): self.r(v) for k, v in zip(d["NS.keys"], d["NS.objects"])}
            if "NS.objects" in d:
                return [self.r(v) for v in d["NS.objects"]]
            if "NS.string" in d:
                return d["NS.string"]
            if "NS.bytes" in d:
                return d["NS.bytes"].decode("utf-8", "replace")
        return d


def parse_point(s) -> tuple[float, float]:
    a = [float(v) for v in re.findall(r"-?[\d.]+(?:e-?\d+)?", s or "")]
    return (a[0], a[1]) if len(a) >= 2 else (0.0, 0.0)


def apple_date(x) -> str:
    try:
        return (APPLE_EPOCH + dt.timedelta(seconds=x["NS.time"])).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


def apple_ts(x) -> float:
    """NSDate -> unix timestamp (0 if unreadable)."""
    try:
        return (APPLE_EPOCH + dt.timedelta(seconds=x["NS.time"])).timestamp()
    except Exception:
        return 0.0


class EmptyNote(Exception):
    """The package has no Session.plist: nothing to convert."""


class TargetUnavailable(Exception):
    """The output volume is not mounted / the parent batch is gone: nothing may be written."""


def read_meta(nbn: Path) -> dict:
    """uuid / subject / dates from metadata.plist ({} if the file is missing)."""
    meta = nbn / "metadata.plist"
    if not meta.exists():
        return {}
    m = Archive(meta)
    mr = m.r(m.top.get("root") or m.top.get("$0"))
    return {"uuid": m.items(mr.get("uuidKey")) or "",
            "subject": m.items(mr.get("noteSubject")) or "",
            "created": apple_date(m.r(mr.get("noteCreationDateKey"))),
            "modified": apple_date(m.r(mr.get("noteModifiedDateKey"))),
            "modified_ts": apple_ts(m.r(mr.get("noteModifiedDateKey")))}


@dataclass
class Curve:
    pts: list
    widths: list  # per bezier node
    width: float
    rgba: tuple
    style: int
    y0: float = 0.0
    y1: float = 0.0


@dataclass
class Page:
    top: float
    height: float
    pdf: str | None = None
    pdf_page: int | None = None  # 0-based
    template: str | None = None  # paper page drawn on a template PDF
    out_w: float = BLANK_PAGE_WIDTH
    out_h: float = 0.0


@dataclass
class Note:
    path: Path
    name: str
    uuid: str = ""
    subject: str = ""
    created: str = ""
    modified: str = ""
    modified_ts: float = 0.0
    width: float = 0.0
    width_source: str = ""
    curves: list = field(default_factory=list)
    pages: list = field(default_factory=list)
    images: list = field(default_factory=list)  # (x, y, w, h, file)
    textboxes: list = field(default_factory=list)  # (x, y, w, h, text, size)
    body_text: str = ""
    hw_text: dict = field(default_factory=dict)  # page(1-based) -> text
    recordings: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    paper_id: str = ""
    line_style: str = ""
    paper_ratio: float = 0.0
    paper_ratio_source: str = ""   # "pdf" (no paper pages), "indeks", "varsayilan", "ozel"
    index_used: int = 0            # indexed pages usable as a geometry check
    index_ok: int = 0              # ... of which agree with the page model


# Paper colours seen in the notes: Legacy:13 = white, Legacy:15 = black. Others fall back to white.
# Notes older than the layout model store paperIndex N, which is Legacy:N+1 (135 notes carry both).
PAPER_BG = {"Legacy:13": (1, 1, 1), "Legacy:15": (0, 0, 0)}
LEGACY_LINES = {1: "Lines:0.5", 9: "Dots:0.5"}


def paper_background(paper_id: str) -> tuple | None:
    """RGB of a paper, None when unknown."""
    if paper_id in PAPER_BG:
        return PAPER_BG[paper_id]
    m = re.search(r"#([0-9A-Fa-f]{6})$", paper_id or "")
    if m:
        h = m.group(1)
        return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return None


WIDTH_CANDIDATES = (716.7999877929688, 593.3302612304688, 610.0, 583.7999877929688, 574.0)


def _f32(x: float) -> float:
    return struct.unpack("f", struct.pack("f", x))[0]


_NUM = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)")


def raw_box(doc, xref: int, name: str):
    """A page box as written in the PDF (native coordinates), following /Parent inheritance."""
    for _ in range(64):
        kind, val = doc.xref_get_key(xref, name)
        if kind == "array":
            v = [float(x) for x in _NUM.findall(val)]
            if len(v) == 4:
                return min(v[0], v[2]), min(v[1], v[3]), max(v[0], v[2]), max(v[1], v[3])
            return None
        kind, val = doc.xref_get_key(xref, "Parent")
        if kind != "xref":
            return None
        xref = int(val.split()[0])
    return None


def page_geom(doc, page) -> tuple:
    """(lo, hi, span): the visible box's extent along the displayed vertical axis, and its displayed width."""
    try:
        mb = raw_box(doc, page.xref, "MediaBox")
        cb = raw_box(doc, page.xref, "CropBox") or mb
        if mb and cb:
            x0, y0, x1, y1 = max(cb[0], mb[0]), max(cb[1], mb[1]), min(cb[2], mb[2]), min(cb[3], mb[3])
            if x1 > x0 and y1 > y0:
                return (x0, x1, y1 - y0) if page.rotation in (90, 270) else (y0, y1, x1 - x0)
    except Exception:  # noqa: BLE001
        pass
    return 0.0, page.rect.height, page.rect.width


def pdf_page_height(width: float, geom) -> float:
    """Canvas height of a PDF-backed page, as Notability lays it out:

        s = W / w        H = ceil(hi * s) - floor(lo * s)        (single precision)

    i.e. the scaled visible box, expanded to whole units. For the usual box that starts at 0 this is ceil(h * s).

    Evidence (HandwritingIndex.pageContentOrigin as ground truth):
    - 125 notes / 720 indexed pages: the exact value agrees on 29.0 %, rounding up on 97.1 %
      (the exact value drifts 0.4-0.8 units per page).
    - Single precision matters: PDFs exported by Notability itself have sizes that make the product a whole
      number. In double precision both 593.33 x 842.7541 / 595.2756 and 716.8 x 594.442 / 792 land ~3.5e-5
      above an integer, yet the first must stay (840) and the second must round up (539). No tolerance
      separates them; float32 gives 840.0 and 538.00006 and is right on 6 of 6 such notes.
    - The box origin matters: two crop boxes of identical size 611.859 x 792 at W 610 give 791 when they start
      at y 10.729 and 790 when they start at y 13.271. One 234-page note has 11 such pages; every step of the
      measured drift (1, 2, 3, 5 units by pages 9, 74, 81, 136) falls on a page of the first kind."""
    lo, hi, span = geom
    s = _f32(_f32(width) / _f32(span))
    return float(math.ceil(_f32(_f32(hi) * s)) - math.floor(_f32(_f32(lo) * s)))


def page_tops(width: float, kinds: list, ratio: float, count: int) -> list:
    """Top of every page; kinds[i] is a PDF page's canvas height, None for a paper page."""
    tops = [0.0]
    for i in range(count):
        k = kinds[i] if i < len(kinds) else None
        tops.append(tops[-1] + (k if k is not None else width * ratio))
    return tops


def index_agreement(width, kinds, ratio, ys, origins, paper_only=False) -> tuple[int, int]:
    """(usable indexed pages, pages whose first ink matches the index) for a paper ratio.

    ys: one or more sorted lists of (top edge, width); top edge = lowest control point minus half the width.
    origins: page (1-based) -> pageContentOrigin.y, only pages whose ink starts inside the page.
    paper_only: count only pages that have a paper page above them (the ones a ratio can move)."""
    import bisect
    if not origins:
        return 0, 0
    tops = page_tops(width, kinds, ratio, max(origins))
    used = ok = 0
    for k, oy in origins.items():
        if paper_only and not any((kinds[i] if i < len(kinds) else None) is None for i in range(k - 1)):
            continue
        used += 1
        for lst in (ys if isinstance(ys, tuple) else (ys,)):
            i = bisect.bisect_left(lst, (tops[k - 1],))
            if i < len(lst) and lst[i][0] < tops[k]:
                tol = INDEX_TOL + INDEX_TOL_PER_WIDTH * lst[i][1]
                if abs((lst[i][0] - tops[k - 1]) - oy) <= tol:
                    ok += 1
                    break
    return used, ok


def fit_paper_ratio(width, kinds, default, ys, origins) -> tuple[float, str, str]:
    """(ratio, source, warning): the candidate the note's own handwriting index agrees with most."""
    cands = [default] + [c for c in RATIO_CANDIDATES if abs(c - default) > 1e-6]
    scored = [(index_agreement(width, kinds, c, ys, origins, True), c) for c in cands]
    used = scored[0][0][0]
    if used == 0:
        return default, "varsayilan", ""
    (_, ok), best = max(scored, key=lambda t: t[0][1])  # ties keep the earliest, i.e. the default
    if ok > 0:
        return best, "indeks", ""
    if used >= 2:  # no known ratio fits: search
        grid = max(((index_agreement(width, kinds, k / 2000, ys, origins, True)[1], k / 2000)
                    for k in range(2400, 3001)), key=lambda t: t[0])
        if grid[0] >= max(2, used // 2):
            return grid[1], "indeks", f"unusual paper ratio {grid[1]:.4f}"
    return default, "varsayilan", "paper ratio not confirmed by handwriting index"
PAPER_RULE_FRACTION = 15.1 / 756  # rule/dot pitch as a fraction of page height, measured on thumb12x.png


def read_note(nbn: Path) -> Note:
    if not (nbn / "Session.plist").exists():
        raise EmptyNote("Session.plist yok")
    a = Archive(nbn / "Session.plist")
    root = a.r(a.top.get("$0") or a.top.get("root"))
    rt = a.r(root["richText"])
    n = Note(path=nbn, name=a.items(root.get("name")) or nbn.stem)

    try:
        for k, v in read_meta(nbn).items():
            setattr(n, k, v)
    except Exception as e:  # noqa: BLE001
        n.warnings.append(f"metadata.plist unreadable: {e}")

    # canvas width
    rs = a.r(rt.get("reflowState"))
    lm = a.r(root.get("NBNoteTakingSessionDocumentPaperLayoutModelKey"))
    pa = a.r(lm.get("documentPaperAttributes")) if isinstance(lm, dict) else {}
    paper = a.r(pa.get("paperSize")) if isinstance(pa, dict) else None
    if isinstance(pa, dict):
        n.paper_id = str(a.r(pa.get("paperIdentifier")) or "")
        n.line_style = str(a.r(pa.get("lineStyle2")) or "")
    if not n.paper_id and isinstance(a.r(root.get("paperIndex")), int):
        n.paper_id = f"Legacy:{a.r(root.get('paperIndex')) + 1}"
    if not n.line_style or n.line_style == "None":
        n.line_style = LEGACY_LINES.get(a.r(root.get("paperLineStyle")), "No Lines")
    sizing = str(a.r(pa.get("paperSizingBehavior"))) if isinstance(pa, dict) else ""
    if isinstance(rs, dict) and rs.get("pageWidthInDocumentCoordsKey"):
        n.width, n.width_source = float(rs["pageWidthInDocumentCoordsKey"]), "reflowState"
    elif m_ := re.match(r"lockedWidth:([\d.]+)", sizing):
        n.width, n.width_source = float(m_.group(1)), "lockedWidth"
    else:
        n.width, n.width_source = 574.0, "default"  # overwritten below if ink says otherwise

    # ink
    ho = a.r(rt.get("Handwriting Overlay"))
    sh = a.r(ho.get("SpatialHash")) if isinstance(ho, dict) else None
    if isinstance(sh, dict) and sh.get("curvesnumpoints"):
        f32 = lambda b: struct.unpack(f"<{len(b)//4}f", b)
        pts = f32(sh["curvespoints"])
        nump = struct.unpack(f"<{len(sh['curvesnumpoints'])//4}i", sh["curvesnumpoints"])
        fw = f32(sh.get("curvesfractionalwidths", b""))
        cw = f32(sh.get("curveswidth", b""))
        cc = sh.get("curvescolors", b"")
        cs = sh.get("curvesstyles", b"")
        pi = fi = 0
        per_node = len(fw) == sum((k - 1) // 3 + 1 for k in nump)
        for i, k in enumerate(nump):
            xy = [(pts[2 * j], pts[2 * j + 1]) for j in range(pi, pi + k)]
            nodes = (k - 1) // 3 + 1
            w = list(fw[fi:fi + nodes]) if per_node else []
            pi += k
            fi += nodes if per_node else 0
            rgba = tuple(cc[4 * i:4 * i + 4]) if len(cc) >= 4 * i + 4 else (0, 0, 0, 255)
            ys = [p[1] for p in xy] or [0]
            n.curves.append(Curve(xy, w, cw[i] if i < len(cw) else 1.5, rgba,
                                  cs[i] if i < len(cs) else 3, min(ys), max(ys)))
        if not per_node and fw:
            n.warnings.append("fractional widths not per-node; constant widths used")

    # media objects
    for mo in a.items(rt.get("mediaObjects")) or []:
        c = a.cls(mo)
        x, y = parse_point(a.r(mo.get("documentOrigin")))
        w, h = parse_point(a.r(mo.get("unscaledContentSize")))
        if c == "ImageMediaObject":
            fig = a.r(mo.get("figure")) or {}
            bg = a.r(fig.get("FigureBackgroundObjectKey")) or {}
            snap = a.r(bg.get("kImageObjectSnapshotKey")) or {}
            rel = a.items(snap.get("relativePath")) if isinstance(snap, dict) else None
            if rel:
                n.images.append((x, y, w, h, rel))
            else:
                n.warnings.append("image without relativePath")
        elif c == "TextBlockMediaObject":
            ts = a.r(mo.get("textStore"))
            s = a.items(a.items(ts.get("attributedString")).get("stringKey")) if isinstance(ts, dict) else ""
            if s and s.strip():
                n.textboxes.append((x, y, w, h, s, float(a.r(mo.get("captionFontSize")) or 12)))
        elif c:
            n.warnings.append(f"unsupported media {c}")

    body = a.items(rt.get("attributedString"))
    if isinstance(body, dict):
        n.body_text = (a.items(body.get("stringKey")) or "").strip()

    # pages: the layout lives in the paper model (format 6+) or, in older notes, under richText
    layout = a.items(lm.get("pageLayoutArray")) if isinstance(lm, dict) else None
    if not layout:
        layout = a.items(rt.get("pageLayoutArray")) or []
    entries = []
    for e in layout:
        e = a.items(e)
        f = e.get("kPageLayoutPDFFileNameKey")
        if not f and isinstance(a.r(e.get("kPageLayoutPDFFileKey")), dict):
            f = a.items(a.r(e["kPageLayoutPDFFileKey"]).get("pdfFileName"))
        entries.append((f, e.get("kPageLayoutPDFPageNumberKey")))
    pdf_cache: dict[str, pymupdf.Document] = {}

    def pdf_doc(f):
        if f not in pdf_cache:
            try:
                pdf_cache[f] = pymupdf.open(nbn / "PDFs" / f) if (nbn / "PDFs" / f).exists() else None
            except Exception as e:  # noqa: BLE001
                n.warnings.append(f"PDF unreadable {f}: {e}")
                pdf_cache[f] = None
        return pdf_cache[f]

    if not entries:  # no layout at all: every page of every attached document, in order
        for pf in a.items(rt.get("pdfFiles")) or []:
            if not isinstance(pf, dict) or a.r(pf.get("type")) == 1:
                # type 1 is a paper template, not a document. It is drawn only when the paper identifier
                # names it (TemplatePDF:<uuid>, handled below); a template left over from an earlier paper
                # choice must not appear (seen: black paper note carrying an unused white lined template).
                continue
            f = a.items(pf.get("pdfFileName"))
            d = pdf_doc(f) if f else None
            if d is not None:
                entries += [(f, i + 1) for i in range(d.page_count)]
        if entries:
            n.warnings.append("no page layout; PDF pages placed in file order")

    template = None
    m_t = re.match(r"TemplatePDF:([0-9A-Fa-f-]+)", n.paper_id)
    if m_t and pdf_doc(f"{m_t.group(1)}.pdf") is not None:
        template = f"{m_t.group(1)}.pdf"

    specs = []   # (pdf file, 0-based page, rect) or None for a paper page
    for f, pn in entries:
        d = pdf_doc(f) if f else None
        if d is not None and pn and 1 <= pn <= d.page_count:
            specs.append((f, pn - 1, d[pn - 1].rect, page_geom(d, d[pn - 1])))
        else:
            if f:
                n.warnings.append(f"missing PDF page {f}#{pn}")
            specs.append(None)

    # canvas width fallback for old notes: fit to ink extent
    if n.width_source == "default" and n.curves:
        maxx = max(p[0] for c in n.curves for p in c.pts)
        if maxx > n.width:
            n.width = 612.0 if maxx <= 612 else 768.0
            n.warnings.append(f"canvas width guessed {n.width} (ink max x {maxx:.0f})")

    def kinds_for(width):
        return [pdf_page_height(width, sp[3]) if sp else None for sp in specs]

    # Notability's own handwriting recognition; its page origins double as a geometry check
    origins = {}
    hwi = nbn / "HandwritingIndex" / "index.plist"
    if hwi.exists():
        try:
            for k, v in plistlib.load(open(hwi, "rb")).get("pages", {}).items():
                t = (v.get("text") or "").strip()
                if t:
                    n.hw_text[int(k)] = t
                o = v.get("pageContentOrigin")
                if o and len(o) >= 2 and o[1] >= 3 and int(k) >= 1:
                    origins[int(k)] = float(o[1])
        except Exception as e:  # noqa: BLE001
            n.warnings.append(f"handwriting index unreadable: {e}")

    # paper page height
    m_c = re.match(r"custom:([\d.]+)", str(paper or ""))
    if m_c and float(m_c.group(1)) > 0:
        default, fixed = 1 / float(m_c.group(1)), True
    else:
        default, fixed = PAPER_RATIO.get(paper, DEFAULT_RATIO), False
    edges = [(c.y0 - c.width * max(c.widths, default=1.0) / 2, c.width, c.style) for c in n.curves]
    ys = (sorted(e[:2] for e in edges if e[2] != 4), sorted(e[:2] for e in edges))

    # A few notes carry a canvas width that is not the one their ink was laid out with (seen: paper
    # switched to A3 later). If the index rejects every page, try the other known widths.
    kinds = kinds_for(n.width)
    used, ok = index_agreement(n.width, kinds, default, ys, origins)
    if used >= 3 and ok == 0 and any(specs):
        alt = max(((index_agreement(w, kinds_for(w), default, ys, origins)[1], w)
                   for w in WIDTH_CANDIDATES if abs(w - n.width) > 1), default=(0, 0))
        if alt[0] >= max(2, math.ceil(0.6 * used)):
            n.warnings.append(f"canvas width {n.width:.1f} rejected by handwriting index; {alt[1]:.1f} used")
            n.width, n.width_source = alt[1], "indeks"
            kinds = kinds_for(n.width)
    ends = [c.y1 for c in n.curves] + [y + h for _, y, _, h, _ in n.images] + \
           [y + h for _, y, _, h, _, _ in n.textboxes]
    bottom = max(ends, default=0)
    if fixed:
        ratio, source = default, "ozel"
    else:
        ratio, source, warn = fit_paper_ratio(n.width, kinds, default, ys, origins)
        if warn:
            n.warnings.append(warn)
    n.paper_ratio, n.paper_ratio_source = ratio, source
    n.index_used, n.index_ok = index_agreement(n.width, kinds, ratio, ys, origins)

    top = 0.0
    for sp in specs:
        if sp:
            f, i, r, g = sp
            pg = Page(top=top, height=pdf_page_height(n.width, g), pdf=f, pdf_page=i, out_w=r.width, out_h=r.height)
        else:
            pg = Page(top=top, height=n.width * ratio, template=template,
                      out_w=BLANK_PAGE_WIDTH, out_h=BLANK_PAGE_WIDTH * ratio)
        n.pages.append(pg)
        top += pg.height
    while top < bottom or not n.pages:  # paper pages until all content is covered
        h = n.width * ratio
        n.pages.append(Page(top=top, height=h, template=template,
                            out_w=BLANK_PAGE_WIDTH, out_h=BLANK_PAGE_WIDTH * ratio))
        top += h
    if all(p.pdf for p in n.pages):
        n.paper_ratio_source = "pdf"
    elif paper_background(n.paper_id) is None and n.paper_id:
        n.warnings.append(f"unknown paper {n.paper_id}, drawn white")
    for d in pdf_cache.values():
        if d is not None:
            d.close()

    rec = nbn / "Recordings"
    if rec.is_dir():
        n.recordings = sorted(p for p in rec.iterdir() if p.suffix.lower() in (".m4a", ".caf", ".aac", ".wav"))
    return n


def _page_for(pages, y):
    for i, p in enumerate(pages):
        if p.top <= y < p.top + p.height:
            return i
    return len(pages) - 1


def render_pdf(n: Note, out: Path) -> dict:
    src = {}
    doc = pymupdf.open()
    for p in n.pages:
        page = doc.new_page(width=p.out_w, height=p.out_h)
        if p.pdf:
            s = src.setdefault(p.pdf, pymupdf.open(n.path / "PDFs" / p.pdf))
            place_pdf_page(page, s, p.pdf_page, n, f"{p.pdf}#{p.pdf_page + 1}")
        else:
            draw_paper(page, n)
            if p.template:
                s = src.setdefault(p.template, pymupdf.open(n.path / "PDFs" / p.template))
                place_pdf_page(page, s, 0, n, f"{p.template}#1", keep_proportion=False)
    outs = list(doc)  # re-fetch: page handles from new_page() go stale

    def pages_touching(y0, y1):
        return [i for i, p in enumerate(n.pages) if y1 >= p.top and y0 <= p.top + p.height]

    def xf(i):
        p = n.pages[i]
        s = p.out_w / n.width
        sy = p.out_h / p.height  # differs from s by < 0.2 %: PDF page heights are whole canvas units
        return lambda x, y: pymupdf.Point(x * s, (y - p.top) * sy), s

    # images (below ink)
    for x, y, w, h, rel in n.images:
        f = n.path / rel
        if not f.exists():
            n.warnings.append(f"missing image {rel}")
            continue
        for i in pages_touching(y, y + h):
            t, s = xf(i)
            a, b = t(x, y), t(x + w, y + h)
            try:
                outs[i].insert_image(pymupdf.Rect(a, b), filename=str(f), keep_proportion=False, overlay=True)
            except Exception as e:  # noqa: BLE001
                n.warnings.append(f"image {rel}: {e}")

    # text boxes
    for x, y, w, h, text, size in n.textboxes:
        i = _page_for(n.pages, y)
        t, s = xf(i)
        r = pymupdf.Rect(t(x, y), t(x + max(w, 40), y + max(h, 20) + 400))
        outs[i].insert_textbox(r, text, fontsize=size * s, fontfile=FONT, fontname="AU")

    # ink: highlighter first, then pen. One Shape per page; a curve's segments are grouped
    # into runs of similar width so each run is a single stroked path.
    shapes = {}
    drawn = 0
    for c in sorted(n.curves, key=lambda c: c.style != 4):
        r, g, b, alpha = [v / 255 for v in c.rgba]
        for i in pages_touching(c.y0 - c.width, c.y1 + c.width):
            t, s = xf(i)
            sh = shapes.get(i) or shapes.setdefault(i, outs[i].new_shape())
            P = [t(*q) for q in c.pts]
            k = len(P)
            base = max(c.width * s, 0.2)
            if k == 1:
                sh.draw_circle(P[0], base / 2)
                sh.finish(color=None, fill=(r, g, b), fill_opacity=alpha)
            elif (k - 1) % 3 == 0 and len(c.widths) == (k - 1) // 3 + 1:
                runs, cur, cw = [], [], None
                for j in range(0, k - 1, 3):
                    node = j // 3
                    wq = round((c.widths[node] + c.widths[node + 1]) * 4) / 8  # quantise to 0.125
                    if cw is not None and wq != cw:
                        runs.append((cw, cur))
                        cur = []
                    cw = wq
                    cur.append(j)
                runs.append((cw, cur))
                for wq, js in runs:
                    for j in js:
                        sh.draw_bezier(P[j], P[j + 1], P[j + 2], P[j + 3])
                    sh.finish(color=(r, g, b), width=max(base * wq, 0.2), lineCap=1, lineJoin=1,
                              stroke_opacity=alpha, closePath=False)
            elif (k - 1) % 3 == 0:
                for j in range(0, k - 1, 3):
                    sh.draw_bezier(P[j], P[j + 1], P[j + 2], P[j + 3])
                sh.finish(color=(r, g, b), width=base, lineCap=1, lineJoin=1, stroke_opacity=alpha, closePath=False)
            else:
                sh.draw_polyline(P)
                sh.finish(color=(r, g, b), width=base, lineCap=1, lineJoin=1, stroke_opacity=alpha, closePath=False)
            drawn += 1
    for sh in shapes.values():
        sh.commit(overlay=True)
    doc.set_metadata({"title": n.name, "creator": "nbconv (Notability export)",
                      "creationDate": "", "modDate": ""})
    try:
        doc.save(out, garbage=3, deflate=True)
    except Exception as e:  # noqa: BLE001  (damaged objects in a source PDF)
        n.warnings.append(f"saved without cleanup: {type(e).__name__}: {e}")
        doc.save(out, garbage=0, deflate=True)
    pc = doc.page_count
    doc.close()
    for s in src.values():
        s.close()
    return {"pages": pc, "curves": len(n.curves), "curve_draws": drawn}


def place_pdf_page(page, src, pno: int, n: Note, label: str, keep_proportion: bool = True):
    """Put a source PDF page behind the ink. A damaged page must not cost the whole note:
    fall back to a rendered image, then to an empty page, and say so in the warnings."""
    try:
        page.show_pdf_page(page.rect, src, pno, keep_proportion=keep_proportion)
        return
    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {e}"
    try:
        pix = src[pno].get_pixmap(dpi=150, alpha=False)
        if pix.is_unicolor:  # nothing readable on the page: do not pretend there was content
            n.warnings.append(f"damaged PDF page {label} is EMPTY in the source file ({err})")
            return
        page.insert_image(page.rect, pixmap=pix, keep_proportion=keep_proportion)
        n.warnings.append(f"damaged PDF page {label} placed as image ({err})")
    except Exception as e2:  # noqa: BLE001
        n.warnings.append(f"damaged PDF page {label} left EMPTY ({err}; {type(e2).__name__}: {e2})")


def draw_paper(page, n: Note):
    bg = paper_background(n.paper_id) or (1, 1, 1)
    r = page.rect
    if bg != (1, 1, 1):
        page.draw_rect(r, color=None, fill=bg, overlay=False)
    ls = n.line_style.lower()
    if not (ls.startswith("lines") or ls.startswith("dots")):
        return
    pitch = r.height * PAPER_RULE_FRACTION
    ink = (0.25, 0.25, 0.25) if sum(bg) < 1.5 else (216 / 255, 224 / 255, 236 / 255)
    sh = page.new_shape()
    y = pitch
    while y < r.height:
        if ls.startswith("lines"):
            sh.draw_line((0, y), (r.width, y))
        else:
            x = pitch
            while x < r.width:
                sh.draw_circle((x, y), 0.5)
                x += pitch
        y += pitch
    if ls.startswith("lines"):
        sh.finish(color=ink, width=0.4)
    else:
        sh.finish(color=None, fill=ink)
    sh.commit(overlay=False)


def render_markdown(n: Note, pdf_out: Path, pdf_name: str, category: str, audio_names: list[str]) -> str:
    L = [f"# {n.name}", "", f"- Kategori: {category}", f"- Notability'deki ad: {n.path.name}",
         f"- Oluşturma: {n.created}", f"- Değişiklik: {n.modified}",
         f"- UUID: {n.uuid}", f"- PDF: [{pdf_name}](<{pdf_name}>)"]
    for a_ in audio_names:
        L.append(f"- Ses: [{a_}](<{a_}>)")
    L.append("")
    if n.body_text:
        L += ["## Yazılmış metin", "", n.body_text, ""]
    doc = pymupdf.open(pdf_out)
    tb_by_page: dict[int, list[str]] = {}
    for x, y, w, h, text, _ in n.textboxes:
        tb_by_page.setdefault(_page_for(n.pages, y), []).append(text.strip())
    for i, pg in enumerate(n.pages):
        parts = []
        if pg.pdf:
            try:
                t = doc[i].get_text("text", flags=pymupdf.TEXT_PRESERVE_WHITESPACE).strip()
            except Exception:  # noqa: BLE001
                t = ""
            # our own inserted text boxes are part of the page text; strip nothing, but mark source
            if t:
                parts.append(t)
        for t in tb_by_page.get(i, []):
            parts.append(f"> [metin kutusu] {t}")
        if n.hw_text.get(i + 1):
            parts.append(f"> [el yazısı, Notability tanıması] {n.hw_text[i + 1]}")
        if parts:
            L += [f"## Sayfa {i + 1}", "", *[p + "\n" for p in parts]]
    doc.close()
    return "\n".join(L)


UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')  # not portable across ext4 / SMB / exFAT / NTFS
PARTIAL = ".partial-"


def safe(s: str, maxbytes: int = 200) -> str:
    """Portable file or folder name: NFC, no reserved characters, no leading/trailing dot or space,
    at most `maxbytes` UTF-8 bytes (ext4 allows 255 per component; suffixes need headroom)."""
    s = UNSAFE.sub("-", unicodedata.normalize("NFC", s or "")).strip(". ")
    while len(s.encode("utf-8")) > maxbytes:
        s = s[:-1]
    return s.strip(". ") or "adsiz"


def mount_root(p: Path) -> Path | None:
    """/Volumes/<X> for a path on an external/network volume, else None."""
    parts = Path(os.path.abspath(p)).parts
    return Path("/Volumes", parts[2]) if len(parts) >= 3 and parts[1] == "Volumes" else None


def target_ready(out: Path) -> bool:
    """True when `out` may be written: its volume (if any) is mounted and the folder exists."""
    m = mount_root(out)
    if m is not None and not os.path.ismount(m):
        return False
    return Path(out).is_dir()


def publish(src: Path, dst: Path, mtime: float = 0.0) -> int:
    """Copy src to dst atomically: write a private partial file, fsync, verify size, rename."""
    tmp = dst.with_name(f"{PARTIAL}{os.getpid()}-{dst.name}")
    try:
        with open(src, "rb") as fi, open(tmp, "wb") as fo:
            shutil.copyfileobj(fi, fo, 4 << 20)
            fo.flush()
            os.fsync(fo.fileno())
        size = os.path.getsize(src)
        if os.path.getsize(tmp) != size:
            raise OSError(f"boyut uyuşmuyor: {dst.name}")
        os.replace(tmp, dst)
    finally:
        if os.path.lexists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
    if mtime > 0:
        try:
            os.utime(dst, (mtime, mtime))
        except OSError:
            pass
    return size


def convert(nbn: Path, dest_dir: Path, category: str, base: str | None = None,
            workdir: Path | None = None, parent: int | None = None) -> dict:
    """Render one note locally, then publish audio -> PDF -> Markdown (Markdown last = note complete)."""
    nbn, dest_dir = Path(nbn), Path(dest_dir)
    n = read_note(nbn)
    base = base or safe(nbn.name[:-4] if nbn.name.endswith(".nbn") else n.name)
    if workdir:
        Path(workdir).mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="nb-", dir=workdir))
    try:
        pdf_tmp = tmp / "note.pdf"
        stats = render_pdf(n, pdf_tmp)
        audio = [(f"{base} - kayıt {j}{r.suffix.lower()}", r) for j, r in enumerate(n.recordings, 1)]
        md_tmp = tmp / "note.md"
        md_tmp.write_text(render_markdown(n, pdf_tmp, f"{base}.pdf", category, [a for a, _ in audio]),
                          encoding="utf-8")

        if parent and os.getppid() != parent:
            raise TargetUnavailable("ana süreç yok")
        m = mount_root(dest_dir)
        if m is not None and not os.path.ismount(m):
            raise TargetUnavailable(f"{m} bağlı değil")
        dest_dir.mkdir(parents=True, exist_ok=True)
        audio_bytes = sum(publish(r, dest_dir / an, n.modified_ts) for an, r in audio)
        pdf_bytes = publish(pdf_tmp, dest_dir / f"{base}.pdf", n.modified_ts)
        md_bytes = publish(md_tmp, dest_dir / f"{base}.md", n.modified_ts)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return {"durum": "ok", "name": base, "uuid": n.uuid, "width_source": n.width_source,
            "width": round(n.width, 2),
            "images": len(n.images), "textboxes": len(n.textboxes), "hw_pages": len(n.hw_text),
            "audio": len(audio), "audio_bytes": audio_bytes, "pdf_bytes": pdf_bytes, "md_bytes": md_bytes,
            "paper_ratio": round(n.paper_ratio, 4), "paper_ratio_source": n.paper_ratio_source,
            "index_used": n.index_used, "index_ok": n.index_ok,
            "warnings": "; ".join(n.warnings), **stats}


def main() -> int:
    ap = argparse.ArgumentParser(description="Convert one Notability .nbn package")
    ap.add_argument("src")
    ap.add_argument("dest")
    ap.add_argument("category", nargs="?", default="?")
    ap.add_argument("--base")
    ap.add_argument("--workdir")
    ap.add_argument("--parent", type=int)
    a = ap.parse_args()
    if a.parent:
        signal.signal(signal.SIGINT, signal.SIG_IGN)  # the batch decides when to stop
    try:
        r = convert(Path(a.src), Path(a.dest), a.category, a.base, a.workdir, a.parent)
        code = 0
    except EmptyNote as e:
        r, code = {"durum": "bos", "warnings": str(e)}, 0
    except TargetUnavailable as e:
        r, code = {"durum": "hedef_yok", "warnings": str(e)}, 2
    except Exception as e:  # noqa: BLE001
        r, code = {"durum": "hata", "warnings": f"{type(e).__name__}: {e}"}, 1
    print(json.dumps(r, ensure_ascii=False), flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
