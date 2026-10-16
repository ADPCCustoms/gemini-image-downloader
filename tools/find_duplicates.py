"""
find_duplicates.py - find images that were saved again by a LATER run of the downloader.

Needs Pillow:   python -m pip install pillow

  python find_duplicates.py "F:\\Gemini dl\\gemini_images"           report only, changes nothing
  python find_duplicates.py "F:\\Gemini dl\\gemini_images" --move    move the repeat copies into
                                                                    a "_duplicates" sub-folder

How it decides: files saved in one go (one run of the downloader) are one picture each, so
pictures from the SAME run are never treated as copies of each other, even if they look alike.
A file from a LATER run is a repeat copy only if it looks like a picture from an EARLIER run.
The earliest copy is kept. Nothing is ever deleted.
"""
import argparse, csv, hashlib, json, random, shutil, sys, time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    sys.exit("This needs Pillow. Install it with:  python -m pip install pillow")

EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
CACHE_NAME = ".dup_cache.json"


def dhash(path):
    """256-bit 'difference hash': survives re-encoding (PNG <-> JPEG) and resizing."""
    try:
        with Image.open(path) as im:
            w, h = im.size
            if im.format == "JPEG":
                im.draft("L", (128, 128))
            g = im.convert("L").resize((17, 16), Image.LANCZOS)
            px = g.tobytes()
        bits = 0
        for row in range(16):
            for col in range(16):
                bits = (bits << 1) | (px[row * 17 + col] > px[row * 17 + col + 1])
        return bits, w, h
    except Exception:
        return None


def pixel_digest(path):
    """Fingerprint of the decoded picture itself (ignores file metadata and compression)."""
    try:
        with Image.open(path) as im:
            return hashlib.sha256(im.convert("RGBA").tobytes()).hexdigest()
    except Exception:
        return None


def has_marker(path):
    """True if the file carries content-credentials / provenance metadata."""
    try:
        data = Path(path).read_bytes()
        return b"c2pa" in data or b"jumb" in data
    except Exception:
        return False


def verify(repeats):
    sample = random.sample(repeats, min(400, len(repeats)))
    print(f"\nChecking {len(sample)} repeat copies in detail (a minute or two)...")
    def check(it):
        m = it["match"]
        return (pixel_digest(it["path"]) == pixel_digest(m["path"]) is not None,
                has_marker(it["path"]), has_marker(m["path"]), it["size"] - m["size"])
    with ThreadPoolExecutor(max_workers=8) as ex:
        res = list(ex.map(check, sample))
    n = len(res)
    print(f"   exactly the same picture, pixel for pixel: {sum(r[0] for r in res)} of {n}")
    print(f"   repeat copy carries provenance metadata:    {sum(r[1] for r in res)} of {n}")
    print(f"   earlier copy carries provenance metadata:   {sum(r[2] for r in res)} of {n}")
    diffs = Counter(r[3] for r in res).most_common(5)
    print("   most common file-size differences (repeat minus earlier, in bytes): "
          + ", ".join(f"{d:+d} (x{c})" for d, c in diffs))


def fmt(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--move", action="store_true", help="move the repeat copies to a _duplicates sub-folder")
    ap.add_argument("--threshold", type=int, default=8,
                    help="max difference (out of 256) to count as the same picture (default 8)")
    ap.add_argument("--verify", action="store_true",
                    help="also check, on a sample of repeat copies, whether the pictures are exactly identical")
    ap.add_argument("--gap", type=int, default=10,
                    help="minutes of quiet between files that separates one run from the next (default 10)")
    a = ap.parse_args()

    folder = Path(a.folder)
    files = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in EXTS)

    # ---- read pictures (cached, so repeat runs of this script are quick) ----
    cache_path = folder / CACHE_NAME
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        cache = {}
    items, todo = [], []
    for p in files:
        st = p.stat()
        c = cache.get(p.name)
        if c and c["size"] == st.st_size and c["mtime"] == int(st.st_mtime):
            items.append({"path": p, "hash": int(c["hash"], 16), "w": c["w"], "h": c["h"],
                          "size": st.st_size, "mtime": st.st_mtime})
        else:
            todo.append(p)
    if todo:
        print(f"Reading {len(todo)} images (a few minutes for thousands; remembered for next time)...")
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=8) as ex:
            results = list(ex.map(dhash, todo))
        print(f"  done in {time.time() - t0:.0f}s")
        unreadable = 0
        for p, r in zip(todo, results):
            if r is None:
                unreadable += 1
                continue
            st = p.stat()
            items.append({"path": p, "hash": r[0], "w": r[1], "h": r[2], "size": st.st_size, "mtime": st.st_mtime})
            cache[p.name] = {"size": st.st_size, "mtime": int(st.st_mtime), "hash": format(r[0], "x"), "w": r[1], "h": r[2]}
        if unreadable:
            print(f"  {unreadable} files couldn't be read as images (left alone)")
        try:
            cache_path.write_text(json.dumps(cache), encoding="utf-8")
        except Exception:
            pass

    # ---- split into runs by the gaps between save times ----
    items.sort(key=lambda it: it["mtime"])
    run = 0
    for i, it in enumerate(items):
        if i and it["mtime"] - items[i - 1]["mtime"] > a.gap * 60:
            run += 1
        it["run"] = run
    runs = defaultdict(list)
    for it in items:
        runs[it["run"]].append(it)

    print(f"\nImages read: {len(items)}   Runs detected (files saved in separate sessions): {len(runs)}")
    for r, its in runs.items():
        print(f"  run {r + 1}: {fmt(its[0]['mtime'])} -> {fmt(its[-1]['mtime'])}   {len(its):>5} files")
    if len(runs) < 2:
        print("\nOnly one run found, so there's nothing that counts as a repeat copy.")
        return

    # ---- for each file of a later run, find the closest picture from an earlier run ----
    def bucket(it): return round(it["w"] / it["h"] * 10)
    earlier = defaultdict(list)          # aspect bucket -> items from earlier runs
    repeats, best_hist = [], Counter()
    for r in sorted(runs):
        if r > 0:
            for it in runs[r]:
                best, best_it = 999, None
                k = bucket(it)
                for kk in (k - 1, k, k + 1):
                    for e in earlier.get(kk, ()):
                        d = (it["hash"] ^ e["hash"]).bit_count()
                        if d < best:
                            best, best_it = d, e
                it["best"], it["match"] = best, best_it
                lab = ("0" if best == 0 else "1-3" if best <= 3 else "4-8" if best <= 8 else
                       "9-12" if best <= 12 else "13-20" if best <= 20 else "21+")
                best_hist[lab] += 1
                if best <= a.threshold:
                    repeats.append(it)
        for it in runs[r]:
            earlier[bucket(it)].append(it)

    later_total = sum(len(v) for r, v in runs.items() if r > 0)
    print(f"\nFiles from later runs: {later_total}. How close is each to a picture from an earlier run?")
    for lab in ("0", "1-3", "4-8", "9-12", "13-20", "21+"):
        print(f"   difference {lab:>5}: {best_hist[lab]}")
    print(f"\nRepeat copies (difference <= {a.threshold}): {len(repeats)}")
    print(f"Pictures that look new in later runs: {later_total - len(repeats)}")
    if repeats:
        same_dims = sum(1 for it in repeats if (it["w"], it["h"]) == (it["match"]["w"], it["match"]["h"]))
        diff_type = sum(1 for it in repeats if it["path"].suffix.lower() != it["match"]["path"].suffix.lower())
        bigger = sum(1 for it in repeats if it["w"] * it["h"] > it["match"]["w"] * it["match"]["h"])
        smaller = sum(1 for it in repeats if it["w"] * it["h"] < it["match"]["w"] * it["match"]["h"])
        print("What the repeat copies are like compared with the earlier copy:")
        print(f"   same dimensions: {same_dims}   larger: {bigger}   smaller: {smaller}   different file type: {diff_type}")
        if a.verify:
            verify(repeats)
        report = Path.cwd() / "duplicates_report.csv"
        with open(report, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["repeat_file", "earlier_file", "difference", "repeat_size", "earlier_size",
                        "repeat_bytes", "earlier_bytes", "repeat_saved_at"])
            for it in repeats:
                m = it["match"]
                w.writerow([it["path"].name, m["path"].name, it["best"], f'{it["w"]}x{it["h"]}',
                            f'{m["w"]}x{m["h"]}', it["size"], m["size"], fmt(it["mtime"])])
        print(f"\nFull list written to: {report}")

    if a.move and repeats:
        dest = folder / "_duplicates"
        dest.mkdir(exist_ok=True)
        for it in repeats:
            target = dest / it["path"].name
            if target.exists():
                target = dest / f'{it["path"].stem}_{int(it["mtime"])}{it["path"].suffix}'
            shutil.move(str(it["path"]), str(target))
        print(f"\nMoved {len(repeats)} repeat copies to: {dest}")
        print("Nothing was deleted. Check the folder, then delete it yourself if you're happy.")
    elif repeats:
        print("\nNothing was changed. Add --move to move the repeat copies into a _duplicates sub-folder.")


if __name__ == "__main__":
    main()
