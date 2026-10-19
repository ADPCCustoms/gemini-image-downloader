"""
calibrate_previews.py - READ-ONLY test: can small previews safely tell your saved pictures apart?

It does not change, move or delete anything, and it makes no network requests.

Why: to skip downloading full-size pictures you already have, the downloader could fetch a small
preview and compare it with your saved pictures. That is only safe if a preview of a picture always
looks MORE like that same picture than like any OTHER picture in your folder, including pictures that
look very alike (for example several anime versions of the same photo).

This script measures exactly that, on your own folder:
  1. how different a simulated small preview is from its own full-size picture, and
  2. how different each picture is from its closest DIFFERENT picture.
If (2) is comfortably bigger than (1), previews can be compared safely.

Needs:   python -m pip install pillow numpy
Run:     python calibrate_previews.py "F:\\Gemini dl\\gemini_images"
(Reads every image once; a few minutes for thousands.)  --limit N tests only the first N images.
"""
import argparse, csv, hashlib, io, sys, time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

try:
    import numpy as np
    from PIL import Image
except ImportError:
    sys.exit("This needs numpy and Pillow. Install them with:  python -m pip install pillow numpy")

EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
VARIANTS = ["lanczos+jpeg85", "bilinear+jpeg70", "box (no jpeg)"]   # ways a server might make a 128px preview


def jpeg_roundtrip(img, quality):
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=quality)
    buf.seek(0)
    return Image.open(buf).convert("RGB")


def make_preview(im, variant):
    p = im.convert("RGB")
    if variant == "lanczos+jpeg85":
        p.thumbnail((128, 128), Image.LANCZOS)
        return jpeg_roundtrip(p, 85)
    if variant == "bilinear+jpeg70":
        p.thumbnail((128, 128), Image.BILINEAR)
        return jpeg_roundtrip(p, 70)
    p.thumbnail((128, 128), Image.BOX)
    return p


def dhash_bits(px):                      # px: bytes of a 17x16 grayscale image
    bits = 0
    for row in range(16):
        for col in range(16):
            bits = (bits << 1) | (px[row * 17 + col] > px[row * 17 + col + 1])
    return bits


def signatures(im):
    """(grayscale 32x32, colour 16x16, 256-bit difference hash) of a picture."""
    rgb = im.convert("RGB")
    g = np.frombuffer(rgb.convert("L").resize((32, 32), Image.LANCZOS).tobytes(), np.uint8)
    c = np.frombuffer(rgb.resize((16, 16), Image.LANCZOS).tobytes(), np.uint8)
    d = dhash_bits(rgb.convert("L").resize((17, 16), Image.LANCZOS).tobytes())
    return g, c, d


def analyse(path):
    try:
        with Image.open(path) as im:
            im.load()
            full = signatures(im)
            sha = hashlib.sha256(im.convert("RGBA").tobytes()).hexdigest()
            prev = [signatures(make_preview(im, v)) for v in VARIANTS]
        return {"path": path, "sha": sha, "full": full, "prev": prev}
    except Exception:
        return None


def nearest_other(A, groups, chunk=8):
    """For each row of A: (smallest average difference to any DIFFERENT picture, index of it)."""
    n = len(A)
    Af = A.astype(np.int16)
    best, idx = np.empty(n), np.empty(n, dtype=int)
    for s in range(0, n, chunk):
        blk = Af[s:s + chunk]
        dist = np.abs(blk[:, None, :] - Af[None, :, :]).mean(axis=2)
        for k in range(len(blk)):
            dist[k, groups[s + k]] = np.inf          # itself and exact duplicates don't count
        j = dist.argmin(axis=1)
        best[s:s + len(blk)] = dist[np.arange(len(blk)), j]
        idx[s:s + len(blk)] = j
    return best, idx


def pct(a, p):
    return float(np.percentile(a, p))


def report(title, same, other, unit, cutoff):
    worst_same, closest_other = float(np.max(same)), float(np.min(other))
    print(f"\n=== {title} ===")
    print(f"  Preview vs its own picture:      average {np.mean(same):.2f}   99% below {pct(same, 99):.2f}   worst {worst_same:.2f} {unit}")
    print(f"  Closest DIFFERENT picture:       closest {closest_other:.2f}   1% of pictures closer than {pct(other, 1):.2f}   typical {pct(other, 50):.2f} {unit}")
    ratio = closest_other / max(worst_same, 1e-9)
    close = int(np.sum(other <= worst_same * 1.5))
    print(f"  Pictures whose nearest different picture is within 1.5x the worst preview difference: {close}")
    if ratio >= 2.0:
        verdict = f"CLEAR GAP ({ratio:.1f}x). A cut-off around {(worst_same * closest_other) ** 0.5:.2f} would separate them."
    elif ratio >= 1.2:
        verdict = f"TIGHT ({ratio:.1f}x). Risky: needs a very strict cut-off, or a second check."
    else:
        verdict = f"OVERLAP ({ratio:.2f}x). Previews can NOT be trusted to tell these pictures apart."
    print(f"  Verdict: {verdict}")
    return ratio


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--limit", type=int, default=0, help="only test the first N images (quick try-out)")
    a = ap.parse_args()

    files = sorted(p for p in Path(a.folder).iterdir() if p.is_file() and p.suffix.lower() in EXTS)
    if a.limit:
        files = files[:a.limit]
    print(f"Reading {len(files)} images and making simulated previews (a few minutes for thousands)...")
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=8) as ex:
        items = [r for r in ex.map(analyse, files) if r]
    print(f"  done in {time.time() - t0:.0f}s ({len(files) - len(items)} unreadable files skipped)")
    n = len(items)
    if n < 10:
        sys.exit("Need at least 10 readable images to say anything useful.")

    by_sha = defaultdict(list)
    for i, it in enumerate(items):
        by_sha[it["sha"]].append(i)
    groups = [by_sha[it["sha"]] for it in items]
    dups = sum(len(v) - 1 for v in by_sha.values())
    print(f"  {n} pictures read; {dups} exact duplicate file(s) are treated as the same picture")

    G = np.stack([it["full"][0] for it in items])
    C = np.stack([it["full"][1] for it in items])
    print("Comparing every picture with every other (about a minute per signature)...")
    og, ig = nearest_other(G, groups)
    oc, _ = nearest_other(C, groups)
    hashes = [it["full"][2] for it in items]
    od = np.empty(n)
    for i in range(n):
        skip = set(groups[i])
        hi = hashes[i]
        od[i] = min((hi ^ hashes[j]).bit_count() for j in range(n) if j not in skip)

    def same_dist(kind):
        out = []
        for it in items:
            for pv in it["prev"]:
                if kind == "g":
                    out.append(np.abs(pv[0].astype(np.int16) - it["full"][0].astype(np.int16)).mean())
                elif kind == "c":
                    out.append(np.abs(pv[1].astype(np.int16) - it["full"][1].astype(np.int16)).mean())
                else:
                    out.append((pv[2] ^ it["full"][2]).bit_count())
        return np.array(out, dtype=float)

    r1 = report("Grayscale 32x32 (average pixel difference, 0-255)", same_dist("g"), og, "levels", 0)
    r2 = report("Colour 16x16 (average pixel difference, 0-255)", same_dist("c"), oc, "levels", 0)
    r3 = report("Difference hash (bits out of 256)", same_dist("d"), od, "bits", 0)

    pairs, seen = [], set()
    for i in np.argsort(og):
        j = int(ig[i])
        key = frozenset((int(i), j))
        if key in seen:
            continue                         # the same pair seen from the other side
        seen.add(key)
        cd = float(np.abs(C[i].astype(np.int16) - C[j].astype(np.int16)).mean())
        pairs.append((items[i]["path"].name, items[j]["path"].name, og[i], cd))
        if len(pairs) >= 100:
            break
    out = Path.cwd() / "preview_calibration_closest_pairs.csv"
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["file_a", "closest_different_file", "grayscale_difference", "colour_difference"])
        for a_name, b_name, gd, cd in pairs:
            w.writerow([a_name, b_name, f"{gd:.2f}", f"{cd:.2f}"])
    print("\nThe 5 most alike pairs of DIFFERENT pictures (open them and see if you'd call them the same):")
    for a_name, b_name, gd, cd in pairs[:5]:
        print(f"   {a_name}   vs   {b_name}   (grayscale difference {gd:.2f}, colour difference {cd:.2f})")
    print(f"\nTop 100 closest pairs written to: {out}")
    names = ["grayscale 32x32", "colour 16x16", "difference hash"]
    best_i = max(range(3), key=lambda k: (r1, r2, r3)[k])
    best = (r1, r2, r3)[best_i]
    print(f"\nSUMMARY (best comparison: {names[best_i]}, gap {best:.1f}x):",
          "previews look safe to compare." if best >= 2.0 else
          "previews are NOT safe to compare on their own; keep downloading full pictures." if best < 1.2 else
          "borderline; I would not rely on previews alone.")
    print("Nothing was changed. Paste this output into the chat.")


if __name__ == "__main__":
    main()
