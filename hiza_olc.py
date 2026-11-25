"""Character-level registration check (read-only).

Notability's HandwritingIndex stores, for every recognised character, a box (x, y, w, h) relative to
pageContentOrigin that wraps the character's strokes with ~5 units of padding. For a page we slide the
ink vertically and count the characters whose strokes fill their box exactly; the offset with the most
matches is where the ink really belongs. Unlike the first-stroke test this ignores drawings, highlights
and text added after indexing.

  python hiza_olc.py NAMES_FILE OUT_JSONL [--all-pages]

NAMES_FILE: one .nbn name per line. Each note is downloaded on demand and handed back to iCloud afterwards.
"""
from __future__ import annotations

import json
import plistlib
import struct
import subprocess
import sys

import batch
import nbconv

PAD = 4.9          # measured padding between a character box and its strokes: 4.2 - 5.3
EDGE_TOL = 1.3
DY_RANGE = 60.0
DY_STEP = 0.5


def char_boxes(entry) -> list:
    """Absolute page boxes (x0, y0, x1, y1) of the recognised characters of one indexed page."""
    o = entry.get("pageContentOrigin")
    b = entry.get("characterRects")
    if not o or not b:
        return []
    v = struct.unpack(f"<{len(b) // 2}e", b[: len(b) // 8 * 8])
    out = []
    for i in range(0, len(v), 4):
        x, y, w, h = v[i:i + 4]
        if x != x or abs(x) == float("inf") or w <= 0 or h <= 0:
            continue
        out.append((o[0] + x, o[1] + y, o[0] + x + w, o[1] + y + h))
    return out


def score_page(boxes, strokes, dy: float) -> int:
    """Characters whose strokes (shifted by dy) fill their box on all four edges."""
    hit = 0
    for (x0, y0, x1, y1), cand in zip(boxes, strokes):
        ux0 = uy0 = float("inf")
        ux1 = uy1 = float("-inf")
        for sx0, sy0, sx1, sy1 in cand:
            a, b = sy0 + dy, sy1 + dy
            if a >= y0 - 0.5 and b <= y1 + 0.5:
                ux0, uy0, ux1, uy1 = min(ux0, sx0), min(uy0, a), max(ux1, sx1), max(uy1, b)
        if ux1 >= ux0 and abs(ux0 - (x0 + PAD)) <= EDGE_TOL and abs(uy0 - (y0 + PAD)) <= EDGE_TOL \
                and abs(ux1 - (x1 - PAD)) <= EDGE_TOL and abs(uy1 - (y1 - PAD)) <= EDGE_TOL:
            hit += 1
    return hit


def measure(n, idx, k: int) -> dict:
    e = idx.get(str(k))
    if not e or k - 1 >= len(n.pages):
        return {"page": k, "chars": 0, "note": "model disi" if e else "indekste yok"}
    pg = n.pages[k - 1]
    boxes = char_boxes(e)
    on = [(min(q[0] for q in c.pts), c.y0 - pg.top, max(q[0] for q in c.pts), c.y1 - pg.top)
          for c in n.curves if c.y1 >= pg.top - DY_RANGE and c.y0 <= pg.top + pg.height + DY_RANGE]
    strokes = [[s for s in on if s[0] >= b[0] - 0.5 and s[2] <= b[2] + 0.5] for b in boxes]
    best = (0, 0.0)
    s0 = score_page(boxes, strokes, 0.0)
    steps = int(DY_RANGE / DY_STEP)
    for i in range(-steps, steps + 1):
        dy = i * DY_STEP
        s = score_page(boxes, strokes, dy)
        if s > best[0] or (s == best[0] and abs(dy) < abs(best[1])):
            best = (s, dy)
    o = e.get("pageContentOrigin") or [0, 0]
    return {"page": k, "chars": len(boxes), "hit0": s0, "best_hit": best[0], "best_dy": best[1],
            "origin_y": round(o[1], 1), "strokes_on_page": sum(1 for s in on if 0 <= s[1] < pg.height)}


def first_stroke_pages(n, idx) -> list:
    """Indexed pages that fail the first-stroke test (the pages reported as not matching)."""
    import bisect
    edges = [(c.y0 - c.width * max(c.widths, default=1.0) / 2, c.width, c.style) for c in n.curves]
    lists = (sorted(e[:2] for e in edges if e[2] != 4), sorted(e[:2] for e in edges))
    bad, used = [], []
    for ks, e in idx.items():
        k = int(ks)
        o = e.get("pageContentOrigin")
        if not o or o[1] < 3 or k < 1:
            continue
        used.append(k)
        if k - 1 >= len(n.pages):
            bad.append(k)
            continue
        pg = n.pages[k - 1]
        ok = False
        for lst in lists:
            i = bisect.bisect_left(lst, (pg.top,))
            if i < len(lst) and lst[i][0] < pg.top + pg.height and \
                    abs(lst[i][0] - pg.top - o[1]) <= nbconv.INDEX_TOL + nbconv.INDEX_TOL_PER_WIDTH * lst[i][1]:
                ok = True
                break
        if not ok:
            bad.append(k)
    return sorted(bad), sorted(used)


def main():
    names = [l.rstrip("\n") for l in open(sys.argv[1], encoding="utf-8") if l.strip()]
    all_pages = "--all-pages" in sys.argv
    out = open(sys.argv[2], "a", encoding="utf-8")
    for nm in names:
        p = batch.DOCS / nm
        r = {"nbn": nm}
        try:
            cloud = batch.is_dataless(p)
            n = nbconv.read_note(p)
            idx = plistlib.load(open(p / "HandwritingIndex" / "index.plist", "rb")).get("pages", {})
            bad, used = first_stroke_pages(n, idx)
            pages = sorted(int(k) for k in idx) if all_pages else bad
            r.update(pages=len(n.pages), width=n.width, indexed=len(used), failing=bad,
                     kinds=sorted({f"{pg.out_w:.1f}x{pg.out_h:.1f}" if pg.pdf else "kagit" for pg in n.pages})[:4],
                     measures=[measure(n, idx, k) for k in pages],
                     others=[measure(n, idx, k) for k in ([x for x in used if x not in bad][:1] +
                                                          [x for x in used if x not in bad][-1:])] if not all_pages else [])
        except Exception as e:  # noqa: BLE001
            r["err"] = f"{type(e).__name__}: {e}"
        finally:
            try:
                subprocess.run(["brctl", "evict", str(p)], capture_output=True, timeout=120)
            except Exception:  # noqa: BLE001
                pass
        out.write(json.dumps(r, ensure_ascii=False) + "\n")
        out.flush()


if __name__ == "__main__":
    main()
