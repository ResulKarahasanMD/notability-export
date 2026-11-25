"""Convert every Notability note into a category-organised folder tree. Resumable.

  python batch.py OUT_DIR [--jobs 6] [--state DIR] [--names FILE] [--limit N]
                          [--no-evict] [--retry-failed] [--mount-wait SEC]
  python batch.py OUT_DIR --verify [--state DIR] [--names FILE]

Interrupted (crash, kill, sleep, network drop)? Run the same command again.

How resuming works
- durum/plan.tsv   fixed note -> (category folder, file name) mapping, built once, extended for new notes.
- durum/rapor.tsv  append-only, one row per attempt, fsync'ed. A note is skipped only when its last
                   row says ok and its .pdf and .md exist in OUT_DIR with the recorded sizes.
- Files reach OUT_DIR through a private ".partial-<pid>-" name and an atomic rename, Markdown last,
  so a finished-looking note is a finished note. Leftover partials are removed on start.
- State lives on this Mac (not on the share) and is mirrored to OUT_DIR/_rapor on every clean exit.
- Every converted note is handed back to iCloud (brctl evict), so this Mac's disk usage stays flat.

Exit codes: 0 all done, 1 done but some notes failed, 2 target volume unavailable, 3 already running,
            4 stopped by a safety rule (disk space / consecutive errors), 5 interrupted by a signal.
"""
from __future__ import annotations

import argparse
import atexit
import collections
import concurrent.futures as cf
import glob
import json
import os
import plistlib
import shutil
import signal
import subprocess
import sys
import threading
import time
import unicodedata
from pathlib import Path

import nbconv

HERE = Path(__file__).resolve().parent
DOCS = Path.home() / "Library/Mobile Documents/ZP9ZJ4EF3S~com~gingerlabs~Notability/Documents"
APPLIB = Path.home() / "Library/Containers/com.gingerlabs.Notability/Data/Library"
SF_DATALESS = 0x40000000
MIN_FREE_MAC_GB = 10.0
MIN_FREE_OUT_GB = 10.0
MAX_ATTEMPTS = 3
MAX_CONSECUTIVE_ERRORS = 20
MAX_REQUEUE = 5
MAX_JOBS = 6
NOTE_TIMEOUT_S = 1800

PLAN_COLS = ["nbn", "uuid", "kategori", "dosya"]
REP_COLS = ["zaman", "nbn", "durum", "deneme", "kategori", "dosya", "sayfa", "cizgi", "resim",
            "metin_kutusu", "el_yazisi_sayfa", "ses", "pdf_bayt", "md_bayt", "ses_bayt",
            "genislik_kaynagi", "kagit_orani", "oran_kaynagi", "indeks_sayfa", "indeks_uyan",
            "bulutta_idi", "sure_s", "uyari"]
DONE = ("ok", "bos")

NFC = lambda s: unicodedata.normalize("NFC", s)  # noqa: E731
_print_lock = threading.Lock()


def log(msg: str):
    with _print_lock:
        print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


# ---------------------------------------------------------------- Notability organizer

def category_map() -> tuple[dict, dict]:
    """(NFC package name -> (uuid, subject)), (uuid -> [category, ..., subject])"""
    p = plistlib.load(open(APPLIB / "NBMetadataCache.plist", "rb"))
    o = p["$objects"]
    R = lambda x: o[x.data] if isinstance(x, plistlib.UID) else x  # noqa: E731
    root = R(p["$top"]["root"])
    top = {R(k): R(v) for k, v in zip(root["NS.keys"], root["NS.objects"])}
    mc = top["NBNoteDocumentMetadataCacheKey"]
    cache = {}
    for v in mc["NS.objects"]:
        e = R(v)
        cache[NFC(R(e["notePackagePath"]))] = (R(e["uuidKey"]), R(e["noteSubject"]))
    org = json.load(open(glob.glob(str(APPLIB / "Organizer/*/assets/1/Asset"))[0]))
    u2p: dict[str, list] = {}

    def walk(n, path):
        ob = n["value"]["organizer"]
        pp = path + [nbconv.safe(ob.get("name") or "adsiz", 160)]
        for i in ob.get("noteIDs") or []:
            u2p.setdefault(i, pp)
        for c in n.get("children", []):
            walk(c, pp)

    for n in org["NBOrganizerRecordOrganizerKey"]:
        walk(n, [])
    return cache, u2p


def is_dataless(p: Path) -> bool:
    return bool(os.stat(p).st_flags & SF_DATALESS)


def free_gb(p: Path) -> float:
    s = os.statvfs(str(p))
    return s.f_bavail * s.f_frsize / 1e9


# ---------------------------------------------------------------- TSV state

def clean(v) -> str:
    """One-line, single-spaced text (for free-text fields such as warnings)."""
    return " ".join(str(v if v is not None else "").split())


_ESC = {"\\": "\\\\", "\t": "\\t", "\n": "\\n", "\r": "\\r"}
_UNESC = {"t": "\t", "n": "\n", "r": "\r", "\\": "\\"}


def esc(v) -> str:
    """Field -> TSV cell, exactly reversible (note names may hold double or leading spaces)."""
    s = str(v if v is not None else "")
    return "".join(_ESC.get(ch, ch) for ch in s)


def unesc(s: str) -> str:
    if "\\" not in s:
        return s
    out, i = [], 0
    while i < len(s):
        if s[i] == "\\" and i + 1 < len(s) and s[i + 1] in _UNESC:
            out.append(_UNESC[s[i + 1]])
            i += 2
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def read_tsv(path: Path, cols: list[str]) -> list[dict]:
    """Rows of a TSV written by TsvAppender; malformed rows (torn write) are skipped."""
    if not path.exists():
        return []
    rows = []
    with open(path, encoding="utf-8", errors="replace", newline="") as fh:
        for i, line in enumerate(fh):
            parts = line.rstrip("\r\n").split("\t")
            if i == 0 and parts[:2] == cols[:2]:
                continue
            if len(parts) == len(cols):
                rows.append(dict(zip(cols, map(unesc, parts))))
    return rows


class TsvAppender:
    def __init__(self, path: Path, cols: list[str]):
        self.cols = cols
        new = not path.exists() or path.stat().st_size == 0
        torn = False
        if not new:
            with open(path, "rb") as fh:  # a torn last line must not swallow the next row
                fh.seek(-1, os.SEEK_END)
                torn = fh.read(1) != b"\n"
        self.fh = open(path, "a", encoding="utf-8", newline="")
        if new:
            self.fh.write("\t".join(cols) + "\n")
        elif torn:
            self.fh.write("\n")
        self._sync()

    def _sync(self):
        self.fh.flush()
        os.fsync(self.fh.fileno())

    def write(self, row: dict):
        self.fh.write("\t".join(esc(row.get(c, "")) for c in self.cols) + "\n")
        self._sync()

    def close(self):
        self.fh.close()


# ---------------------------------------------------------------- plan

def build_plan(state: Path, names: list[str]) -> dict[str, dict]:
    """nbn -> plan row. Existing rows are kept verbatim so names never change between runs."""
    path = state / "plan.tsv"
    plan = {r["nbn"]: r for r in read_tsv(path, PLAN_COLS)}
    missing = [n for n in names if n not in plan]
    if not missing:
        return plan
    cache, u2p = category_map()
    canon: dict[str, str] = {}   # casefolded folder path -> spelling used
    taken: set[tuple] = set()    # (casefolded folder, casefolded file name)
    for r in plan.values():
        acc = ""
        for comp in r["kategori"].split("/"):
            acc = f"{acc}/{comp.casefold()}"
            canon.setdefault(acc, comp)
        taken.add((r["kategori"].casefold(), r["dosya"].casefold()))
    out = TsvAppender(path, PLAN_COLS)
    looked_up = 0
    for n in sorted(missing):
        uuid, subj = cache.get(NFC(n[:-4]), ("", ""))
        if not uuid:  # newer than the app's cache: ask the note itself (downloads it)
            try:
                m = nbconv.read_meta(DOCS / n)
                uuid, subj = m.get("uuid", ""), m.get("subject", "")
                looked_up += 1
            except Exception as e:  # noqa: BLE001
                log(f"UYARI metadata okunamadı: {n}: {e}")
        if uuid in u2p:
            cat = u2p[uuid]
        elif subj == "unsortedNotesKey":
            cat = ["_Kategorisiz"]
        else:
            cat = ["_Eslesmeyen"]
        comps, acc = [], ""
        for comp in cat:
            acc = f"{acc}/{comp.casefold()}"
            comps.append(canon.setdefault(acc, comp))
        rel = "/".join(comps)
        base = nbconv.safe(n[:-4])
        k = 0
        while (rel.casefold(), base.casefold()) in taken:
            k += 1
            tag = (uuid[:8] or "kopya") + (f"-{k}" if k > 1 else "")
            base = f"{nbconv.safe(n[:-4], 185)} [{tag}]"
        taken.add((rel.casefold(), base.casefold()))
        row = {"nbn": n, "uuid": uuid, "kategori": rel, "dosya": base}
        plan[n] = row
        out.write(row)
    out.close()
    log(f"plan: {len(missing)} yeni not eklendi ({looked_up} tanesinin kategorisi nottan okundu), toplam {len(plan)}")
    return plan


# ---------------------------------------------------------------- report

def last_rows(state: Path) -> tuple[dict, collections.Counter]:
    """(nbn -> last report row, nbn -> number of failed attempts)"""
    last, fails = {}, collections.Counter()
    for r in read_tsv(state / "rapor.tsv", REP_COLS):
        last[r["nbn"]] = r
        if r["durum"] == "hata":
            fails[r["nbn"]] += 1
    return last, fails


def outputs_ok(out: Path, plan_row: dict, rep_row: dict) -> bool:
    d = out / plan_row["kategori"]
    try:
        return (os.path.getsize(d / f"{plan_row['dosya']}.pdf") == int(rep_row["pdf_bayt"])
                and os.path.getsize(d / f"{plan_row['dosya']}.md") == int(rep_row["md_bayt"]))
    except (OSError, ValueError):
        return False


# ---------------------------------------------------------------- target volume

class Target:
    def __init__(self, out: Path, wait_s: int):
        self.out, self.wait_s = out, wait_s
        self.mount = nbconv.mount_root(out)
        self.url = None
        if self.mount is not None and os.path.ismount(self.mount):
            for line in subprocess.run(["mount"], capture_output=True, text=True).stdout.splitlines():
                if f" on {self.mount} (smbfs" in line:
                    self.url = "smb:" + line.split(" on ")[0]

    def mounted(self) -> bool:
        return self.mount is None or os.path.ismount(self.mount)

    def ready(self) -> bool:
        return nbconv.target_ready(self.out)

    def wait(self, check) -> bool:
        """Block until check() holds (tries to remount the share); False after wait_s seconds."""
        t0 = time.time()
        while not check():
            waited = time.time() - t0
            if waited >= self.wait_s:
                return False
            log(f"hedef yok: {self.mount or self.out} - bekleniyor ({int(waited)}/{self.wait_s} sn)")
            if self.url and not self.mounted():
                try:
                    subprocess.run(["osascript", "-e", f'mount volume "{self.url}"'],
                                   capture_output=True, timeout=60)
                except Exception:  # noqa: BLE001
                    pass
            time.sleep(max(1, min(30, self.wait_s - (time.time() - t0))))
        return True


# ---------------------------------------------------------------- housekeeping

def remove_partials(out: Path, workdir: Path) -> int:
    n = 0
    for root, _dirs, files in os.walk(out):
        for f in files:
            if f.startswith(nbconv.PARTIAL):
                try:
                    os.unlink(os.path.join(root, f))
                    n += 1
                except OSError:
                    pass
    if workdir.is_dir():
        for d in workdir.iterdir():
            if d.name.startswith("nb-"):
                shutil.rmtree(d, ignore_errors=True)
    return n


def evict(nbn: str) -> bool:
    """Hand the local copy back to iCloud. brctl refuses items that are not fully uploaded."""
    try:
        subprocess.run(["brctl", "evict", str(DOCS / nbn)], capture_output=True, timeout=120)
        return is_dataless(DOCS / nbn)
    except Exception:  # noqa: BLE001
        return False


def sweep(candidates: list[str], jobs: int) -> int:
    """Evict processed notes that are still local (e.g. after a crash between convert and evict)."""
    todo = [n for n in candidates if (DOCS / n).exists() and not is_dataless(DOCS / n)]
    if not todo:
        return 0
    with cf.ThreadPoolExecutor(jobs) as ex:
        done = sum(ex.map(evict, todo))
    log(f"süpürme: {done}/{len(todo)} not iCloud'a geri bırakıldı")
    return done


def lock(state: Path):
    p = state / "calisiyor.pid"
    if p.exists():
        try:
            pid = int(p.read_text().strip())
            cmd = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True).stdout
            if pid != os.getpid() and "batch.py" in cmd:
                log(f"DUR: zaten çalışıyor (pid {pid})")
                sys.exit(3)
        except ValueError:
            pass
    p.write_text(str(os.getpid()))

    def release():
        try:
            if p.read_text().strip() == str(os.getpid()):
                p.unlink()
        except OSError:
            pass
    atexit.register(release)


def mirror_state(state: Path, out: Path):
    try:
        if not nbconv.target_ready(out):
            return
        d = out / "_rapor"
        d.mkdir(exist_ok=True)
        for f in ("plan.tsv", "rapor.tsv"):
            if (state / f).exists():
                nbconv.publish(state / f, d / f)
    except OSError as e:
        log(f"UYARI rapor hedefe kopyalanamadı: {e}")


# ---------------------------------------------------------------- one note

def run_note(nbn: str, prow: dict, out: Path, workdir: Path) -> dict:
    cmd = [sys.executable, str(HERE / "nbconv.py"), str(DOCS / nbn), str(out / prow["kategori"]),
           prow["kategori"].replace("/", " / "), "--base", prow["dosya"], "--workdir", str(workdir),
           "--parent", str(os.getpid())]
    t0 = time.time()
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                             errors="replace", env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        try:
            so, se = p.communicate(timeout=NOTE_TIMEOUT_S)
            lines = [l for l in so.splitlines() if l.startswith("{")]
            r = json.loads(lines[-1]) if lines else {
                "durum": "hata", "warnings": f"çıktı yok (kod {p.returncode}): {se.strip()[-300:]}"}
        except subprocess.TimeoutExpired:
            p.kill()
            try:
                p.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                pass  # stuck in the kernel; leave it behind
            r = {"durum": "hata", "warnings": f"zaman aşımı ({NOTE_TIMEOUT_S} sn)"}
    except Exception as e:  # noqa: BLE001
        r = {"durum": "hata", "warnings": f"{type(e).__name__}: {e}"}
    r["sure_s"] = round(time.time() - t0, 1)
    return r


# ---------------------------------------------------------------- verify

def verify(out: Path, state: Path, only: set | None) -> int:
    import pymupdf
    plan = {r["nbn"]: r for r in read_tsv(state / "plan.tsv", PLAN_COLS)}
    last, _ = last_rows(state)
    keys = sorted(n for n in plan if only is None or n in only)
    listings: dict[Path, list[str]] = {}

    def listing(d: Path) -> list[str]:
        if d not in listings:
            try:
                listings[d] = [NFC(x) for x in os.listdir(d)]
            except OSError:
                listings[d] = []
        return listings[d]

    c = collections.Counter()
    bad = []
    for n in keys:
        r, p = last.get(n), plan[n]
        d = out / p["kategori"]
        if r is None:
            c["eksik"] += 1
            bad.append((n, "raporda yok"))
        elif r["durum"] == "bos":
            c["bos"] += 1
        elif r["durum"] != "ok":
            c["hata"] += 1
            bad.append((n, r["uyari"][:160]))
        elif not outputs_ok(out, p, r):
            c["dosya_yok"] += 1
            bad.append((n, "hedefte pdf/md yok ya da boyut farklı"))
        else:
            try:
                with pymupdf.open(d / f"{p['dosya']}.pdf") as doc:
                    pc = doc.page_count
                if pc != int(r["sayfa"]):
                    raise ValueError(f"sayfa {pc} != rapor {r['sayfa']}")
                aud = [x for x in listing(d) if x.startswith(NFC(p["dosya"]) + " - kayıt ")]
                if len(aud) != int(r["ses"] or 0):
                    raise ValueError(f"ses {len(aud)} != rapor {r['ses']}")
                c["ok"] += 1
            except Exception as e:  # noqa: BLE001
                c["bozuk"] += 1
                bad.append((n, f"{type(e).__name__}: {e}"))
    partial = sum(1 for d in listings for x in listings[d] if x.startswith(nbconv.PARTIAL))
    print(f"DOĞRULAMA {len(keys)} not: " + ", ".join(f"{k}={v}" for k, v in sorted(c.items()))
          + f" | yarım dosya={partial}")
    for n, why in bad:
        print(f"  ! {n} :: {why}")
    return 0 if not bad and not partial else 1


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--jobs", type=int, default=MAX_JOBS)
    ap.add_argument("--state", default=str(HERE / "durum"))
    ap.add_argument("--names", help="file with one .nbn name per line (default: every note)")
    ap.add_argument("--limit", type=int, help="process at most N notes in this run")
    ap.add_argument("--no-evict", action="store_true")
    ap.add_argument("--retry-failed", action="store_true", help="retry notes that already failed 3 times")
    ap.add_argument("--force", action="store_true", help="with --names: convert the listed notes again")
    ap.add_argument("--mount-wait", type=int, default=600)
    ap.add_argument("--verify", action="store_true")
    a = ap.parse_args()
    a.jobs = max(1, min(MAX_JOBS, a.jobs))
    out, state = Path(a.out), Path(a.state)
    state.mkdir(parents=True, exist_ok=True)
    workdir = state / "tmp"

    names = sorted(x for x in os.listdir(DOCS) if x.endswith(".nbn"))
    only = None
    if a.names:
        want = {NFC(l.rstrip("\n")) for l in open(a.names, encoding="utf-8") if l.strip()}
        names = [x for x in names if NFC(x) in want]
        only = set(names)

    target = Target(out, a.mount_wait)
    if a.verify:
        if not target.ready():
            log(f"DUR: hedef yok: {out}")
            return 2
        return verify(out, state, only)

    lock(state)
    if not target.wait(target.mounted):
        log(f"DUR: hedef birim bağlı değil: {target.mount}. Hiçbir şey yazılmadı.")
        return 2
    out.mkdir(parents=True, exist_ok=True)

    stop = {"why": None, "code": 0}

    def on_signal(sig, _frm):
        if not stop["why"]:
            stop.update(why=f"sinyal {sig}", code=5)
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    n_part = remove_partials(out, workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    plan = build_plan(state, names)
    last, fails = last_rows(state)

    todo, skipped = [], collections.Counter()
    for n in names:
        r = last.get(n)
        if a.force and only is not None:
            todo.append(n)
        elif r and r["durum"] == "bos":
            skipped["bos"] += 1
        elif r and r["durum"] == "ok" and outputs_ok(out, plan[n], r):
            skipped["ok"] += 1
        elif fails[n] >= MAX_ATTEMPTS and not a.retry_failed:
            skipped["vazgecildi"] += 1
        else:
            todo.append(n)
    cloud = {n: is_dataless(DOCS / n) for n in todo}
    todo.sort(key=lambda n: (cloud[n], n))  # local notes first: frees this Mac's disk early
    if a.limit:
        todo = todo[:a.limit]
    pending = set(todo)
    log(f"başlangıç: {len(names)} not | atlanan {dict(skipped)} | yapılacak {len(todo)} "
        f"(yerelde {sum(1 for n in todo if not cloud[n])}) | silinen yarım dosya {n_part} | "
        f"Mac boş {free_gb(Path.home()):.1f} GB, hedef boş {free_gb(out):.1f} GB")
    if not a.no_evict:
        sweep([n for n in names if n in last and n not in pending], a.jobs)

    rep = TsvAppender(state / "rapor.tsv", REP_COLS)
    queue = collections.deque(todo)
    requeued = collections.Counter()
    stats = collections.Counter()
    consecutive = 0
    t_start = time.time()
    flying: dict[cf.Future, str] = {}

    def work(n):
        r = run_note(n, plan[n], out, workdir)
        if r["durum"] in DONE and not a.no_evict:
            evict(n)
        return r

    with cf.ThreadPoolExecutor(a.jobs) as ex:
        while queue or flying:
            while queue and len(flying) < a.jobs and not stop["why"]:
                if free_gb(Path.home()) < MIN_FREE_MAC_GB:
                    stop.update(why=f"Mac boş alanı < {MIN_FREE_MAC_GB} GB", code=4)
                elif not target.ready():
                    if flying:
                        break  # let running notes settle, then wait for the volume
                    if not target.wait(target.ready):
                        stop.update(why="hedef birim yok", code=2)
                elif free_gb(out) < MIN_FREE_OUT_GB:
                    stop.update(why=f"hedef boş alanı < {MIN_FREE_OUT_GB} GB", code=4)
                else:
                    n = queue.popleft()
                    flying[ex.submit(work, n)] = n
            if not flying:
                if stop["why"] or not queue:
                    break
                continue
            done, _ = cf.wait(flying, timeout=5, return_when=cf.FIRST_COMPLETED)
            for f in done:
                n = flying.pop(f)
                try:
                    r = f.result()
                except Exception as e:  # noqa: BLE001
                    r = {"durum": "hata", "warnings": f"{type(e).__name__}: {e}", "sure_s": 0}
                if r["durum"] not in DONE and not target.ready():
                    r["durum"] = "hedef_yok"  # the share went away: not the note's fault
                if r["durum"] == "hedef_yok":
                    requeued[n] += 1
                    if requeued[n] <= MAX_REQUEUE:
                        queue.appendleft(n)
                        log(f"yeniden sıraya alındı (hedef yok): {n}")
                        continue
                    r["durum"] = "hata"
                    r["warnings"] = f"hedef yok: {r.get('warnings', '')}"
                if r["durum"] == "hata" and stop["code"] == 5:
                    log(f"iptal (sinyal): {n}")  # interrupted, not failed: no attempt is recorded
                    continue
                deneme = fails[n] + 1
                if r["durum"] == "hata":
                    fails[n] += 1
                    consecutive += 1
                else:
                    consecutive = 0
                stats[r["durum"]] += 1
                stats["bayt"] += sum(int(r.get(k, 0) or 0) for k in ("pdf_bytes", "md_bytes", "audio_bytes"))
                rep.write({"zaman": time.strftime("%Y-%m-%d %H:%M:%S"), "nbn": n, "durum": r["durum"],
                           "deneme": deneme, "kategori": plan[n]["kategori"], "dosya": plan[n]["dosya"],
                           "sayfa": r.get("pages", ""), "cizgi": r.get("curves", ""), "resim": r.get("images", ""),
                           "metin_kutusu": r.get("textboxes", ""), "el_yazisi_sayfa": r.get("hw_pages", ""),
                           "ses": r.get("audio", ""), "pdf_bayt": r.get("pdf_bytes", ""),
                           "md_bayt": r.get("md_bytes", ""), "ses_bayt": r.get("audio_bytes", ""),
                           "genislik_kaynagi": r.get("width_source", ""),
                           "kagit_orani": r.get("paper_ratio", ""), "oran_kaynagi": r.get("paper_ratio_source", ""),
                           "indeks_sayfa": r.get("index_used", ""), "indeks_uyan": r.get("index_ok", ""),
                           "bulutta_idi": int(cloud[n]),
                           "sure_s": r.get("sure_s", ""), "uyari": clean(r.get("warnings", ""))})
                k = stats["ok"] + stats["bos"] + stats["hata"]
                if r["durum"] == "hata":
                    log(f"[{k}/{len(todo)}] HATA {n} :: {r.get('warnings', '')[:200]}")
                if k % 25 == 0 or k == len(todo):
                    el = time.time() - t_start
                    eta = (len(todo) - k) * el / max(k, 1)
                    log(f"[{k}/{len(todo)}] ok {stats['ok']} bos {stats['bos']} hata {stats['hata']} | "
                        f"{stats['bayt'] / 1e9:.2f} GB yazıldı | {k / el * 60:.0f} not/dk | "
                        f"kalan ~{eta / 60:.0f} dk | Mac boş {free_gb(Path.home()):.1f} GB")
                if consecutive >= MAX_CONSECUTIVE_ERRORS and not stop["why"]:
                    stop.update(why=f"art arda {consecutive} hata", code=4)
    rep.close()

    last, fails = last_rows(state)
    if not a.no_evict:
        sweep([n for n in names if n in last], a.jobs)
    mirror_state(state, out)
    remaining = [n for n in names if not (last.get(n) and last[n]["durum"] in DONE)]
    el = time.time() - t_start
    if stop["why"]:
        log(f"DURDU ({stop['why']}): bu çalıştırmada {dict(stats)} | kalan {len(remaining)} not | "
            f"{el / 60:.1f} dk. Devam etmek için aynı komutu yeniden çalıştırın.")
        return stop["code"]
    log(f"BİTTİ: bu çalıştırmada ok {stats['ok']}, bos {stats['bos']}, hata {stats['hata']} | "
        f"tamamlanmayan {len(remaining)} not | {el / 60:.1f} dk")
    return 0 if not remaining else 1


if __name__ == "__main__":
    sys.exit(main())
