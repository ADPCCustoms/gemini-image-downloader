"""
check_tail_fingerprint.py - READ-ONLY test: do two downloads of the same picture share an identical end?

Idea: if repeat downloads of a picture differ only in a few bytes of metadata near the START of the
file, then the END of the file (the last 64 KB, say) is identical every time. The downloader could then
recognise a picture by fetching just those last bytes (much less data than the whole file).

This script tests that on the repeat copies you already have: it compares each file in
"_duplicates" with the original it was a repeat of, and measures how much of the start and end is
identical. It reads files only; it changes nothing and uses no network.

Run (after find_duplicates.py has written duplicates_report.csv in this folder):
    python check_tail_fingerprint.py "F:\\Gemini dl\\gemini_images"
Options:  --csv PATH       the report to use (default: duplicates_report.csv in the current folder)
          --scratch PATH   instead of the report, use a folder of FRESH copies: pictures are matched to
                           their originals by what they look like (needs Pillow and the downloader's
                           .gemini_index.json in the main folder)
          --sample N       how many pairs to test (default 600)
"""
import argparse, csv, hashlib, json, random, sys
from collections import Counter
from pathlib import Path

try:
    import numpy as np
except ImportError:
    sys.exit("This needs numpy:  python -m pip install numpy")


def common_prefix_suffix(a, b):
    """(length of identical start, length of identical end) of two byte strings."""
    A, B = np.frombuffer(a, np.uint8), np.frombuffer(b, np.uint8)
    m = min(len(A), len(B))
    d = np.flatnonzero(A[:m] != B[:m])
    prefix = int(d[0]) if len(d) else m
    d = np.flatnonzero(A[len(A) - m:] != B[len(B) - m:])
    suffix = m - 1 - int(d[-1]) if len(d) else m
    return prefix, suffix


EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def pairs_by_picture(scratch, folder):
    """Match each fresh copy in `scratch` to the original in `folder` that has the identical picture."""
    try:
        from PIL import Image
    except ImportError:
        sys.exit("Scratch mode needs Pillow:  python -m pip install pillow")
    try:
        files = json.loads((folder / ".gemini_index.json").read_text(encoding="utf-8"))["files"]
    except Exception:
        sys.exit(f"Can't read {folder / '.gemini_index.json'}. Run the downloader (v1.2.2 or newer) on that folder first.")
    by_pix = {rec["pix"]: name for name, rec in files.items() if rec.get("pix")}
    if not Path(scratch).is_dir():
        sys.exit(f"The folder {scratch} doesn't exist yet. Run the downloader with that folder as the save folder\n"
                 "first (and let it save a few hundred images), then run this test again.")
    copies = sorted(p for p in Path(scratch).iterdir() if p.is_file() and p.suffix.lower() in EXTS)
    if not copies:
        sys.exit(f"{scratch} has no images in it yet. Let the downloader save a few hundred images there first.")
    pairs = []
    for p in copies:
        try:
            with Image.open(p) as im:
                pix = hashlib.sha256(im.convert("RGBA").tobytes()).hexdigest()
        except Exception:
            continue
        name = by_pix.get(pix)
        if name and (folder / name).exists():
            pairs.append((p, folder / name))
    print(f"{len(pairs)} of {len(copies)} fresh copies were matched to an original by their picture.")
    return pairs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--csv", default="duplicates_report.csv")
    ap.add_argument("--scratch", default="")
    ap.add_argument("--sample", type=int, default=600)
    a = ap.parse_args()

    folder = Path(a.folder)
    if a.scratch:
        pairs = pairs_by_picture(a.scratch, folder)
    else:
        dupes = folder / "_duplicates"
        if not Path(a.csv).exists():
            sys.exit(f"Can't find {a.csv}. Run this from the folder where find_duplicates.py wrote it, or pass --csv PATH.")
        pairs = []
        with open(a.csv, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                r, e = dupes / row["repeat_file"], folder / row["earlier_file"]
                if r.exists() and e.exists():
                    pairs.append((r, e))
    if not pairs:
        sys.exit("No pairs found. Either the _duplicates folder is gone (look in the Recycle Bin), or use --scratch "
                 "with a folder of fresh copies.")
    random.seed(1)
    pairs = random.sample(pairs, min(a.sample, len(pairs)))
    print(f"Comparing {len(pairs)} repeat copies with their originals...")

    prefixes, suffixes, sizes, size_diffs, kinds = [], [], [], Counter(), Counter()
    for r, e in pairs:
        x, y = r.read_bytes(), e.read_bytes()
        p, s = common_prefix_suffix(x, y)
        prefixes.append(p)
        suffixes.append(s)
        sizes.append(len(y))
        size_diffs[len(x) - len(y)] += 1
        kinds[e.suffix.lower()] += 1
    suffixes, prefixes, sizes = np.array(suffixes), np.array(prefixes), np.array(sizes)
    n = len(pairs)

    print(f"\nFile types: {dict(kinds)}   typical size: {int(np.median(sizes)) // 1024} KB")
    print(f"Identical START of the file:  median {int(np.median(prefixes)):,} bytes   10th percentile {int(np.percentile(prefixes, 10)):,}")
    print(f"Identical END of the file:    median {int(np.median(suffixes)):,} bytes   10th percentile {int(np.percentile(suffixes, 10)):,}")
    print("\nShare of pairs whose last N bytes are identical:")
    for kb in (4, 16, 64, 256):
        print(f"   last {kb:>3} KB identical: {100 * np.mean(suffixes >= kb * 1024):5.1f}%")
    top = ", ".join(f"{d:+d} bytes (x{c})" for d, c in size_diffs.most_common(5))
    print(f"\nMost common file-size differences (repeat minus original): {top}")

    ok64 = float(np.mean(suffixes >= 64 * 1024))
    ok16 = float(np.mean(suffixes >= 16 * 1024))
    print("\nVERDICT:", end=" ")
    if ok64 >= 0.99:
        print("PROMISING. Nearly every repeat download shares its last 64 KB with the original, so the end of\n"
              "the file could identify a picture. The next step would be to test whether Gemini's servers\n"
              "allow fetching just the end of a file.")
    elif ok16 >= 0.99:
        print("PARTLY. The last 16 KB match but not always the last 64 KB. A smaller end-of-file check might work.")
    else:
        print("NO. The end of the file differs too often between downloads to use as a fingerprint.")
    print("Nothing was changed. Paste this output into the chat.")


if __name__ == "__main__":
    main()
