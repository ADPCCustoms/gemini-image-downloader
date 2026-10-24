"""
Gemini Image Downloader
-----------------------
Downloads the images in your Gemini "Library" to a folder on your computer.

Everything runs locally. You log in to your own Google account in a normal
browser window; nothing is sent to anyone else.

This is an unofficial tool and is not affiliated with Google. It depends on
Gemini's web page layout, so it may stop working if Google changes the site.

Options:  --cli    run in the console instead of the window
          --debug  extra diagnostic output
"""
import asyncio, base64, contextlib, hashlib, io, json, os, queue, re, sys, threading, time, traceback
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

# Windowed (no-console) builds have no stdin/stdout; give them harmless stand-ins
for _name in ("stdin", "stdout", "stderr"):
    if getattr(sys, _name) is None:
        setattr(sys, _name, open(os.devnull, "r" if _name == "stdin" else "w"))

from playwright.async_api import async_playwright

try:
    from PIL import Image
except Exception:      # Pillow missing: fall back to exact-file matching only
    Image = None

VERSION = "1.4.0"

# ---------------- SETTINGS ----------------
SECTION = "Library"      # Sidebar item that holds your generated images
MIN_SIZE = 120           # Ignore images smaller than this (icons, avatars)
DEBUG = "--debug" in sys.argv
CLI = "--cli" in sys.argv
# ------------------------------------------

GEMINI_URL = "https://gemini.google.com/app"

# Save everything next to the .exe (or next to this script when run normally)
BASE = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
DEFAULT_OUT = BASE / "gemini_images"
PROFILES = BASE / "browser_profile"      # keeps you logged in between runs
TOP_WINDOW = 500         # new-pictures-only mode: how many of the newest pictures to look at first
TOP_KNOWN_TAIL = 100     # ...and it stops when this many at the end of that window are already saved
FULL_CHECK_DAYS = 7      # a full scan is done at least this often, even in new-pictures-only mode


class Options:
    """What the person asked for in this run (the window's tick boxes / command-line switches)."""
    def __init__(self, new_only=True, quick=False, full_days=FULL_CHECK_DAYS):
        self.new_only = new_only       # only look at the newest part of the Library (when it is safe to)
        self.quick = quick             # experimental: jump to the bottom of the Library to list it
        self.full_days = full_days


OPTS = Options(new_only="--full" not in sys.argv, quick="--quick-listing" in sys.argv)
PREVIEW_SCAN = "--preview-scan" in sys.argv   # ask for small previews while scanning (measured: no real speed-up)

BROWSERS = [("chrome", "Google Chrome"), ("msedge", "Microsoft Edge")]

COLLECT_JS = """() => {
    const seen = (window.__gemSeen = window.__gemSeen || new Map());
    const out = [];
    for (const e of document.querySelectorAll('img')) {
        const r = e.getBoundingClientRect();
        const w = e.naturalWidth || Math.round(r.width), h = e.naturalHeight || Math.round(r.height);
        const src = e.currentSrc || e.src, area = w * h, old = seen.get(src);
        if (old === undefined || area > old) {
            seen.set(src, area);
            out.push({src: src, w: w, h: h});
        }
    }
    return out;
}"""


class Cancelled(Exception):
    """Raised when the user presses Stop."""


def resource_path(name):
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / name


def stamp():
    return time.strftime("%H:%M:%S")


def stamped(msg):
    """Prefix a log line with the time of day (blank lines stay blank)."""
    return f"[{stamp()}] {msg}" if msg else ""


def fmt_dur(sec):
    """'23m 56s', '1h 02m 05s', '8s'"""
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m {s:02d}s" if h else f"{m}m {s:02d}s" if m else f"{s}s"


def fmt_clock(sec):
    """'3:07' or '1:03:07' for the running timer"""
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


TIMING = defaultdict(float)     # seconds spent in each part of the current run
COUNTS = Counter()              # numbers for the summary (downloads, bytes)


class RunLog:
    """Keeps a copy of everything shown in the log in last_run_log.txt next to the program."""

    def __init__(self):
        self.path = BASE / "last_run_log.txt"
        self.lock = threading.Lock()
        self.ok = True

    def start(self, out_dir):
        with self.lock:
            try:
                self.path.write_text(f"Gemini Image Downloader v{VERSION}\n"
                                     f"Started {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                                     f"Save folder: {out_dir}\n\n", encoding="utf-8")
                self.ok = True
            except Exception:
                self.ok = False

    def write(self, line):
        if not self.ok:
            return
        with self.lock:
            try:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception:
                self.ok = False


runlog = RunLog()


def console_banner():
    print("=" * 60)
    print(f" Gemini Image Downloader v{VERSION}")
    print("=" * 60)
    print(" This tool opens a browser window, goes to your Gemini")
    print(" Library and saves your images into a folder. You log in")
    print(" yourself; everything stays on your computer and nothing")
    print(" is sent anywhere else.")
    print(" (Unofficial tool, not affiliated with Google.)")
    print("=" * 60)
    print()


# ======================================================================
# Core logic (works with any "ui" object: ConsoleUI or the window)
#   ui.banner()  ui.log(text)  ui.status(text)  ui.progress(done, total)
#   ui.ask(text) -> blocks until the user continues   ui.alert(text)
#   ui.check()   -> raises Cancelled if the user pressed Stop
# ======================================================================

async def ask(ui, msg):
    t = time.monotonic()
    try:
        await asyncio.get_running_loop().run_in_executor(None, ui.ask, msg)
    finally:
        TIMING["Waiting for you"] += time.monotonic() - t


async def safe_goto(page, url, tries=3):
    """Go to a page, retrying if a redirect interrupts the navigation
    (Google redirects while you're signing in, which can cancel a goto)."""
    for attempt in range(tries):
        try:
            await page.goto(url, wait_until="domcontentloaded")
            return
        except Exception as e:
            if attempt == tries - 1:
                raise
            if DEBUG:
                print(f"(navigation interrupted, retrying: {e})")
            await page.wait_for_timeout(3000)


def looks_signed_out(url):
    """True if we're on a Google sign-in page or Gemini's signed-out landing page."""
    u = urlparse(url)
    if "accounts.google.com" in u.netloc:
        return True
    return u.netloc == "gemini.google.com" and u.path in ("", "/", "/welcome")


LIBRARY_URL_RE = re.compile(r"^https://lh3\.googleusercontent\.com/gg/")


def is_library_url(src):
    return bool(LIBRARY_URL_RE.match(src))


def qualifies(im):
    """Is this <img> one of your Library pictures (not an icon, avatar or template)?
    Sizes are the loaded size, or the on-screen size when the picture hasn't been loaded."""
    src = im["src"]
    if is_ui_image(src):
        return False
    if im["w"] >= MIN_SIZE and im["h"] >= MIN_SIZE:
        return True
    return is_library_url(src) and im["w"] >= 16 and im["h"] >= 16


def is_ui_image(src):
    return ("gstatic.com" in src or "/ogw/" in src
            or "googleusercontent.com/a/" in src or src.endswith(".svg"))


async def launch_browser(p, ui):
    """Try Chrome first, then Edge. Each browser gets its own profile folder."""
    for channel, label in BROWSERS:
        ui.check()
        try:
            ctx = await p.chromium.launch_persistent_context(
                str(PROFILES / channel), headless=False, channel=channel,
                args=["--disable-blink-features=AutomationControlled",
                      # keep loading images even if the window is covered or in the background
                      "--disable-backgrounding-occluded-windows",
                      "--disable-renderer-backgrounding",
                      "--disable-background-timer-throttling"],
            )
            ui.log(f"Using {label}.")
            return ctx
        except Exception as e:
            if DEBUG:
                ui.log(f"({label} not available: {e})")
    return None


async def click_section(page, name):
    attempts = [
        lambda: page.get_by_role("link", name=name, exact=True).first,
        lambda: page.get_by_role("button", name=name, exact=True).first,
        lambda: page.get_by_text(name, exact=True).first,
    ]
    for make in attempts:
        try:
            await make().click(timeout=4000)
            return True
        except Exception:
            continue
    return False


async def scroll_everything_down(page, rescan=True, jump=False):
    """Scroll every scrollable element down a bit (or, with jump=True, straight to its bottom) and
    return how far anything moved. The scrollable elements are remembered between steps
    (rescan=True looks for them again)."""
    return await page.evaluate("""([rescan, jump]) => {
        let els = window.__gemScrollers;
        if (!els || rescan) {
            els = [document.scrollingElement, ...document.querySelectorAll('*')]
                .filter(el => el && el.scrollHeight > el.clientHeight + 50);
            window.__gemScrollers = els;
        }
        let moved = 0;
        for (const el of els) {
            if (!el.isConnected) continue;
            const before = el.scrollTop;
            el.scrollTop = jump ? el.scrollHeight : before + el.clientHeight * 0.9;
            moved += el.scrollTop - before;
        }
        return moved;
    }""", [rescan, jump])


PREVIEW_SIZE_RE = re.compile(r"=(w\d+-h\d+|s\d+)")


def preview_url(url, size=128):
    """The same picture address, asking for a small preview instead of the full-size picture."""
    def small(m):
        return f"=w{size}-h{size}" if m.group(1).startswith("w") else f"=s{size}"
    return PREVIEW_SIZE_RE.sub(small, url, count=1)


async def block_library_images(page):
    """While scrolling, make the page ask for small previews of the Library pictures instead of
    full-size ones. The scan only needs their addresses, and loading thousands of full-size
    pictures was most of the scan time. (The pictures still load, so the page behaves normally;
    simply blocking them stopped it from listing more than the first few.)"""
    async def handler(route):
        try:
            url = route.request.url
            small = preview_url(url)
            if small != url:
                await route.continue_(url=small)
            else:
                await route.continue_()
        except Exception:
            try:
                await route.continue_()
            except Exception:
                pass
    try:
        await page.route(LIBRARY_URL_RE, handler)
        return handler
    except Exception:
        return None


async def unblock_library_images(page, handler):
    try:
        await page.unroute(LIBRARY_URL_RE, handler)
    except Exception:
        pass


async def settle(page, min_ms, max_ms):
    """Wait for newly scrolled-in images to finish loading: as soon as nothing is still loading
    (and nothing new has appeared), carry on; never longer than max_ms."""
    await page.wait_for_timeout(min_ms)
    waited, prev = min_ms, None
    while waited < max_ms:
        state = await page.evaluate("""() => {
            let pending = 0;
            for (const i of document.images) if (!i.complete) pending++;
            return [document.images.length, pending];
        }""")
        if state[1] == 0 and state == prev:
            break
        prev = state
        await page.wait_for_timeout(150)
        waited += 150


INDEX_NAME = ".gemini_index.json"
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
NAME_KEY_RE = re.compile(r"^gemini_([0-9a-f]{16})\.")
CONCURRENCY = 6          # full-size pictures downloaded at the same time (limited by bandwidth)
LOOKUP_CONCURRENCY = 24  # pictures checked at the same time (small requests, limited by round-trip time)
FULL_LIMIT = None        # set per run: caps simultaneous full-size downloads
TAIL_BYTES = 32768       # how much of the END of a file identifies a picture (see fetch_tail)
TAIL_VERIFY_EVERY = 50   # spot-check every 50th end-of-file match against the full picture


ORDER_JS = """() => [...document.querySelectorAll('img')].map(e => e.currentSrc || e.src)"""


async def dom_positions(page):
    """Where each picture sits in the page (its place in the Library list), by image key."""
    try:
        srcs = await page.evaluate(ORDER_JS)
    except Exception:
        return {}
    pos = {}
    for i, src in enumerate(srcs):
        pos.setdefault(image_key(src)[0], i)
    return pos


def image_key(src):
    """Stable name for an image so re-runs can skip ones already saved."""
    full = re.sub(r"=[sw]\d+.*$", "=s0", src)
    return hashlib.md5(full.encode()).hexdigest()[:16], full


def file_tail_hash(path, n=None):
    """Fingerprint of the last `n` bytes of a file. Two downloads of the same picture differ only in a
    few bytes of metadata near the start, so their ends are identical."""
    n = n or TAIL_BYTES
    with open(path, "rb") as f:
        size = f.seek(0, 2)
        f.seek(max(0, size - n))
        return hashlib.sha256(f.read()).hexdigest()


CTYPE_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}


def norm_ext(ext):
    ext = ext.lower().lstrip(".")
    return "jpg" if ext == "jpeg" else ext


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def pixel_fingerprint(data):
    """(pixel hash, 256-bit difference hash, width, height) of the decoded picture, or None.
    Ignores file metadata and compression, so two downloads of the same picture match
    even when the files themselves differ byte for byte."""
    if Image is None:
        return None
    try:
        with Image.open(io.BytesIO(data)) as im:
            w, h = im.size
            pix = hashlib.sha256(im.convert("RGBA").tobytes()).hexdigest()
            px = im.convert("L").resize((17, 16), Image.LANCZOS).tobytes()
        bits = 0
        for row in range(16):
            for col in range(16):
                bits = (bits << 1) | (px[row * 17 + col] > px[row * 17 + col + 1])
        return pix, bits, w, h
    except Exception:
        return None


def url_shape(src):
    """Describes the *form* of an image address with the long private tokens hidden."""
    if src.startswith(("blob:", "data:")):
        return src.split(":")[0] + ":"
    u = urlparse(src)
    parts = []
    for seg in (s for s in u.path.split("/") if s):
        base = seg.split("=")[0]
        parts.append(base if len(base) <= 12 else f"<{len(base)} chars>")
    m = re.search(r"=([A-Za-z0-9_-]+)$", u.path)
    tail = "=" + re.sub(r"\d+", "#", m.group(1)) if m else ""
    return f"{u.netloc}/{'/'.join(parts)}{tail}{'?query' if u.query else ''}"


class ImageIndex:
    """Remembers which images are already in the save folder. An image counts as already
    saved if its web address, its file contents, or its decoded picture matches - so copies
    are recognised even when the address changed, or the file differs only in metadata."""

    def __init__(self, out_dir):
        self.out_dir = out_dir
        self.path = out_dir / INDEX_NAME
        self.files = {}    # file name -> {"size","mtime","hash","pix","dh","w","h"}
        self.urls = {}     # image key (from its web address) -> file content hash
        self.hashes = {}   # file content hash -> file name
        self.pix = {}      # decoded-picture hash -> file name
        self.near = defaultdict(list)   # (w, h, ext) -> [(difference hash, size, name)]
        self.dirty = False
        self.library_total = 0   # how many Library images the last complete run found
        self.last_short = 0      # count found by the last run that ended short of the expected size
        self.fast_failures = 0   # runs in a row where the fast (preview-size) scan came up short
        self.fast_failed_version = ""   # tool version those failures happened with
        self.tails = defaultdict(list)  # fingerprint of a file's end -> [file names]
        self.newest_first = None        # True/False once a full scan has shown where new pictures appear
        self.last_full_scan = 0.0       # when the whole Library was last listed
        self.jump_failures = 0          # runs in a row where the quick listing came up short
        self.jump_failed_version = ""
        self.claims = {}                # file name -> the Library entry that matched it first (this run)
        self.tail_enabled = True
        self.tail_ready = False         # True when the folder already held pictures at the start of the run
        self.tail_off_version = ""      # tool version in which an end-of-file match was found to be wrong

    def _register(self, name, rec):
        self.files[name] = rec
        self.hashes.setdefault(rec["hash"], name)
        if rec.get("pix"):
            self.pix.setdefault(rec["pix"], name)
        if rec.get("tail"):
            self.tails[rec["tail"]].append(name)
        if rec.get("dh"):
            ext = Path(name).suffix.lower().lstrip(".")
            self.near[(rec["w"], rec["h"], ext)].append((int(rec["dh"], 16), rec["size"], name))

    def load(self, ui):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            old_files, old_urls = data.get("files", {}), data.get("urls", {})
            self.library_total = int(data.get("library_total", 0))
            self.last_short = int(data.get("last_short", 0))
            self.fast_failures = int(data.get("fast_failures", 0))
            self.fast_failed_version = str(data.get("fast_failed_version", ""))
            if self.fast_failed_version != VERSION:
                self.fast_failures = 0          # a new version of the tool gets to try the fast scan again
            self.newest_first = data.get("newest_first")
            self.last_full_scan = float(data.get("last_full_scan", 0.0))
            self.jump_failures = int(data.get("jump_failures", 0))
            self.jump_failed_version = str(data.get("jump_failed_version", ""))
            if self.jump_failed_version != VERSION:
                self.jump_failures = 0          # a new version gets to try the quick listing again
            self.tail_off_version = str(data.get("tail_off_version", ""))
            self.tail_enabled = self.tail_off_version != VERSION
        except Exception:
            old_files, old_urls = {}, {}

        names = [p for p in self.out_dir.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS]
        total = len(names)

        def cached(p):
            rec = old_files.get(p.name)
            st = p.stat()
            if rec and rec.get("size") == st.st_size and rec.get("mtime") == int(st.st_mtime) \
                    and ("pix" in rec or Image is None):
                return rec
            return None

        recs = {p.name: cached(p) for p in names}
        need = [p for p in names if recs[p.name] is None]
        if need:
            ui.log(f"Checking {len(need)} images already in this folder so they aren't downloaded "
                   "twice (this can take a few minutes the first time)...")

            def work(p):
                data = p.read_bytes()
                return sha256_bytes(data), pixel_fingerprint(data)

            ex = ThreadPoolExecutor(max_workers=6)
            try:
                for n, (p, (h, fp)) in enumerate(zip(need, ex.map(work, need)), 1):
                    ui.check()
                    st = p.stat()
                    recs[p.name] = {"size": st.st_size, "mtime": int(st.st_mtime), "hash": h,
                                    "pix": fp[0] if fp else "", "dh": format(fp[1], "x") if fp else "",
                                    "w": fp[2] if fp else 0, "h": fp[3] if fp else 0}
                    if n % 25 == 0:
                        ui.progress(n, len(need))
                        ui.status(f"Checking existing images... {n}/{len(need)}")
            finally:
                ex.shutdown(wait=False, cancel_futures=True)
            self.dirty = True

        for p in names:                          # fingerprint of each file's end (quick: reads only the end)
            rec = recs[p.name]
            if "tail" not in rec:
                try:
                    rec["tail"] = file_tail_hash(p)
                except Exception:
                    rec["tail"] = ""
                self.dirty = True
        for name, rec in recs.items():
            self._register(name, rec)
        if set(recs) != set(old_files):
            self.dirty = True
        self.urls = dict(old_urls)
        for name, rec in recs.items():
            m = NAME_KEY_RE.match(name)
            if m:
                self.urls[m.group(1)] = rec["hash"]
        self.urls = {k: v for k, v in self.urls.items() if v in self.hashes}
        self.tail_ready = bool(self.tails)
        if total:
            ui.log(f"{total} images are already in this folder.")

    def has_url(self, key):
        return self.urls.get(key) in self.hashes

    def expected_library_size(self):
        """How many images the Library should hold: what the last complete run found, or, before
        any run has recorded that, the number of images this tool has already saved here."""
        if self.library_total:
            return self.library_total
        return sum(1 for n in self.files if n.startswith("gemini_"))

    def find_pixel(self, pix):
        return self.pix.get(pix)

    def find_near(self, dh, w, h, ext, size):
        """Same size and type, almost the same picture, almost the same file size."""
        for odh, osize, name in self.near.get((w, h, ext), ()):
            if (dh ^ odh).bit_count() <= 1 and abs(size - osize) <= 0.02 * max(size, osize):
                return name
        return None

    def find_tail(self, h, total, ext):
        """An existing file whose last bytes are identical, with the same type and almost the same size."""
        for name in self.tails.get(h, ()):
            if abs(self.files[name]["size"] - total) <= 16 and norm_ext(Path(name).suffix) == ext:
                return name
        return None

    def has_ranks(self):
        """Has a full scan recorded where each saved picture sits in the Library list?"""
        have = sum(1 for r in self.files.values() if r.get("rank") is not None)
        return bool(self.files) and have >= 0.5 * len(self.files)

    def set_ranks_full(self, order_names):
        ranks = {}
        for pos, name in enumerate(order_names):
            ranks.setdefault(name, pos)
        for name, rec in self.files.items():
            if name in ranks:
                rec["rank"] = ranks[name]
            else:
                rec.pop("rank", None)          # no longer in the Library
        self.dirty = True

    def assign_new_ranks(self, new_names):
        """New pictures sit above everything already saved, in the order they were listed."""
        existing = [r["rank"] for r in self.files.values() if r.get("rank") is not None]
        base = (min(existing) if existing else 0) - len(new_names)
        for j, name in enumerate(new_names):
            if name in self.files:
                self.files[name]["rank"] = base + j
        if new_names:
            self.dirty = True

    def rank_consistency(self, names):
        """Do these saved pictures, listed in this order now, appear in the same order as in the last
        full scan? Returns the share of neighbouring pairs in order, or None if there are too few."""
        ranks = [self.files[n].get("rank") for n in names if n in self.files]
        ranks = [r for r in ranks if r is not None]
        if len(ranks) < 20:
            return None
        return sum(1 for a, b in zip(ranks, ranks[1:]) if b >= a) / (len(ranks) - 1)

    def new_only_blocker(self, days):
        """None if a new-pictures-only scan is safe; otherwise why a full scan is needed first."""
        if self.newest_first is False:
            return "Gemini doesn't list new pictures at the top of the Library, so new-pictures-only mode can't be used."
        if not self.has_ranks():
            return "the order of your Library hasn't been recorded yet (that happens during a full scan)."
        if self.newest_first is None:
            return ("the tool hasn't yet confirmed that Gemini lists new pictures at the top. "
                    "This full scan will confirm it if you have made new pictures since the last one.")
        age = (time.time() - self.last_full_scan) if self.last_full_scan else None
        if age is None or age > days * 86400:
            when = ("a full scan has never been completed" if age is None
                    else f"it's been {int(age // 86400)} days since the last full scan")
            return f"{when} (one is done at least every {days} days)."
        return None

    def claim(self, name, key):
        """Note that Library entry `key` matched file `name`. Returns the entry that matched it first,
        if that was a different one (two Library entries for what looks like the same picture)."""
        first = self.claims.setdefault(name, key)
        return first if first != key else None

    def alias(self, key, name):
        h = self.files[name]["hash"]
        if self.urls.get(key) != h:
            self.urls[key] = h
            self.dirty = True

    def add(self, key, h, path, fp, body=None):
        st = path.stat()
        rec = {"size": st.st_size, "mtime": int(st.st_mtime), "hash": h,
               "tail": sha256_bytes(body[-TAIL_BYTES:]) if body else file_tail_hash(path),
               "pix": fp[0] if fp else "", "dh": format(fp[1], "x") if fp else "",
               "w": fp[2] if fp else 0, "h": fp[3] if fp else 0}
        self._register(path.name, rec)
        self.urls[key] = h
        self.dirty = True

    def save(self):
        if not self.dirty:
            return
        try:
            self.path.write_text(json.dumps({"files": self.files, "urls": self.urls,
                                                 "library_total": self.library_total,
                                                 "last_short": self.last_short,
                                                 "fast_failures": self.fast_failures,
                                                 "fast_failed_version": self.fast_failed_version,
                                                 "tail_off_version": self.tail_off_version,
                                                 "newest_first": self.newest_first,
                                                 "last_full_scan": self.last_full_scan,
                                                 "jump_failures": self.jump_failures,
                                                 "jump_failed_version": self.jump_failed_version}), encoding="utf-8")
            self.dirty = False
        except Exception:
            pass


async def fetch_image(page, ctx, src, full):
    if src.startswith("blob:") or src.startswith("data:"):
        b64 = await page.evaluate("""async (u) => {
            const b = await (await fetch(u)).blob();
            return await new Promise(r => {
                const fr = new FileReader();
                fr.onload = () => r(fr.result.split(',')[1]);
                fr.readAsDataURL(b);
            });
        }""", src)
        return base64.b64decode(b64), ""
    last = "no response"
    for attempt in range(3):
        async with (FULL_LIMIT or contextlib.nullcontext()):
            try:
                r = await ctx.request.get(full)
            except Exception as e:                 # network hiccup
                last = str(e)
            else:
                if r.ok:
                    body = await r.body()
                    COUNTS["downloads"] += 1
                    COUNTS["bytes"] += len(body)
                    return body, r.headers.get("content-type", "")
                last = f"HTTP {r.status}"
                if r.status not in (403, 408, 429) and r.status < 500:
                    break                          # not worth retrying (e.g. 404)
        await asyncio.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"download failed ({last})")


async def fetch_tail(ctx, full):
    """Ask the server for just the last TAIL_BYTES of the file (much less data than the whole picture).
    Returns ('tail', bytes, total file size, content type), or ('full', bytes, size, type) if the server
    ignored the request and sent everything, or None."""
    for attempt in range(3):
        try:
            r = await ctx.request.get(full, headers={"Range": f"bytes=-{TAIL_BYTES}"})
        except Exception:
            r = None
        if r is not None:
            ctype = r.headers.get("content-type", "")
            if r.status == 206:
                m = re.search(r"/(\d+)\s*$", r.headers.get("content-range", ""))
                if not m:
                    return None
                body = await r.body()
                COUNTS["tail_bytes"] += len(body)
                return "tail", body, int(m.group(1)), ctype
            if r.status == 200:
                body = await r.body()
                COUNTS["downloads"] += 1
                COUNTS["bytes"] += len(body)
                return "full", body, len(body), ctype
            if r.status not in (403, 408, 429) and r.status < 500:
                return None                     # e.g. 404 or 416: not worth retrying
        await asyncio.sleep(1.0 * (attempt + 1))
    return None


async def save_image(page, ctx, src, out_dir, index, lock, ui=None):
    """Returns ('skipped', 'address' | 'tail' | 'content', file name) or ('saved', 'new', file name)."""
    key, full = image_key(src)
    if index.has_url(key):
        name = index.hashes.get(index.urls[key])
        if name:
            index.claim(name, key)
        return "skipped", "address", name     # quick check by web address

    body = ctype = None
    if index.tail_enabled and index.tail_ready and Image is not None \
            and not src.startswith(("blob:", "data:")):
        got = await fetch_tail(ctx, full)     # try to recognise it from the end of the file alone
        if got and got[0] == "full":
            _, body, _, ctype = got
        elif got:
            _, data, total, ctype = got
            COUNTS["tail_tried"] += 1
            kind_ext = CTYPE_EXT.get(ctype.split(";")[0].strip())
            COUNTS[f"tried_{kind_ext}"] += 1
            name = index.find_tail(sha256_bytes(data), total, kind_ext)
            if name:
                COUNTS[f"matched_{kind_ext}"] += 1
                shared = index.claim(name, key) is not None     # another entry already matched this same file
                if shared:
                    COUNTS["tail_shared"] += 1
                if not shared and int(key, 16) % TAIL_VERIFY_EVERY:
                    index.alias(key, name)
                    COUNTS["tail_hits"] += 1
                    return "skipped", "tail", name
                # check in full: spot checks, and every picture that shares a file with another entry
                body, ctype = await fetch_image(page, ctx, src, full)
                fp = await asyncio.get_running_loop().run_in_executor(None, pixel_fingerprint, body)
                if fp and fp[0] == index.files[name].get("pix"):
                    index.alias(key, name)
                    COUNTS["tail_verified"] += 1
                    if shared:
                        COUNTS["tail_shared_same"] += 1
                    return "skipped", "tail", name
                COUNTS["tail_mismatch"] += 1          # an end-of-file match was wrong: stop using it
                index.tail_enabled = False
                index.tail_off_version = VERSION
                index.dirty = True
                if ui:
                    ui.log("  Warning: a picture recognised from its file end turned out to be different. "
                           "That shortcut is switched off; every picture will be checked in full.")
    if body is None:
        body, ctype = await fetch_image(page, ctx, src, full)
    h = sha256_bytes(body)
    ext = {"image/jpeg": "jpg", "image/webp": "webp"}.get(ctype)
    if not ext:
        ext = "jpg" if body[:3] == b"\xff\xd8\xff" else "webp" if body[8:12] == b"WEBP" else "png"
    fp = await asyncio.get_running_loop().run_in_executor(None, pixel_fingerprint, body)

    async with lock:                          # check-and-save as one step, so parallel downloads can't both save it
        name = index.hashes.get(h)
        if name is None and fp:
            name = index.find_pixel(fp[0]) or index.find_near(fp[1], fp[2], fp[3], ext, len(body))
        if name:
            index.claim(name, key)
            index.alias(key, name)
            return "skipped", "content", name
        path = out_dir / f"gemini_{key}.{ext}"
        path.write_bytes(body)
        index.add(key, h, path, fp, body)
        return "saved", "new", path.name


async def scroll_to_top(page):
    await page.evaluate("""() => {
        const els = [document.scrollingElement, ...document.querySelectorAll('*')];
        for (const el of els) {
            if (el && el.scrollHeight > el.clientHeight + 50) el.scrollTop = 0;
        }
    }""")


def count_distinct(best):
    return len({image_key(src)[0] for src in best})


async def nudge_bottom(page):
    """Real mouse-wheel and End-key presses, in case the page only loads more on those."""
    try:
        vp = page.viewport_size or {"width": 1280, "height": 720}
        await page.mouse.move(vp["width"] / 2, vp["height"] / 2)
        await page.mouse.wheel(0, 800)
        await page.keyboard.press("End")
    except Exception:
        pass


async def collect_images(ui, page, expected=0, best=None, passes=3, careful=False, jump=False, target=None):
    """Scroll the whole Library (up to `passes` passes, stopping when a pass finds nothing new)
    and return one web address per distinct image. `best` accumulates what has been seen.
    careful=True always waits the full time after each scroll (slower, used when retrying)."""
    best = {} if best is None else best     # src -> sighting with the largest size
    keys = {image_key(src)[0] for src in best}

    def sighting(im):
        if not qualifies(im):
            return
        old = best.get(im["src"])
        if old is None or im["w"] * im["h"] > old["w"] * old["h"]:
            best[im["src"]] = im
            keys.add(image_key(im["src"])[0])

    async def grab():
        for im in await page.evaluate(COLLECT_JS):
            sighting(im)

    async def one_pass(label, wait_ms, idle_limit, jump=False):
        idle = 0
        await grab()
        last = len(keys)
        for step in range(6000):
            ui.check()
            moved = await scroll_everything_down(page, rescan=(step % 25 == 0 or idle > 0 or jump), jump=jump)
            if idle or careful or jump:
                await page.wait_for_timeout(wait_ms)        # stalled or careful: give the page its full time
            else:
                await settle(page, 250, wait_ms)            # moving: carry on as soon as images are in
            await grab()
            now = len(keys)
            stalled = moved < 5 and now == last
            idle = idle + 1 if stalled else 0
            last = now
            if idle >= idle_limit:
                break
            if target and now >= target:
                break                       # enough of the top of the Library for new-pictures-only mode
            if stalled and idle in (2, 4):
                await nudge_bottom(page)
            if step % 5 == 0:
                ui.status(f"Loading your images... found {now} so far ({label})")
        return len(keys)

    previous = -1
    for pass_no in range(1, (1 if jump else passes) + 1):
        if pass_no > 1:
            ui.log(f"Checking for any missed images (pass {pass_no})...")
            await scroll_to_top(page)
            await page.wait_for_timeout(1500)
        t_pass = time.monotonic()
        label = "quick listing" if jump else f"pass {pass_no}"
        total = await one_pass(label, 1000 if jump else (1100 if pass_no == 1 else 800), 6, jump=jump)
        ui.log(f"  {label}: {total} images ({fmt_dur(time.monotonic() - t_pass)})")
        if total == previous:
            break
        if expected and total == expected:
            break                    # exactly what we expected: no need for a second look
        if expected and total < 0.95 * expected:
            break                    # far short: the page has stalled, a second pass won't help
        previous = total

    by_key = {}
    for src, im in best.items():
        k = image_key(src)[0]
        size = im["w"] * im["h"]
        if k not in by_key or size > by_key[k][1]:
            by_key[k] = (src, size)
    pos = await dom_positions(page)         # return them in the order the Library lists them
    ordered = sorted(by_key.items(), key=lambda kv: pos.get(kv[0], 10 ** 9))
    return [v[0] for k, v in ordered]


async def session(ui, ctx, out_dir, index):
    t_open, w_open = time.monotonic(), TIMING["Waiting for you"]
    page = await ctx.new_page()
    await safe_goto(page, GEMINI_URL)
    ui.log("A browser window has opened on Gemini.")
    await ask(ui, "Sign in to Gemini in the browser window if asked (use the Google account "
                  "whose images you want). When you can see your chats, continue.")

    # Reload Gemini so the page has settled, and make sure you're really signed in.
    signed_in = False
    for attempt in range(4):
        ui.check()
        await safe_goto(page, GEMINI_URL)
        await page.wait_for_timeout(5000)
        if not looks_signed_out(page.url):
            signed_in = True
            break
        if attempt == 3:
            break
        await ask(ui, "It looks like you're not signed in to Gemini yet. Sign in in the browser "
                      "window and wait until you can see your chats, then continue.")
    if not signed_in:
        ui.alert("Couldn't confirm that you're signed in to Gemini. "
                 "Please sign in when the browser opens and try again.")
        return None

    ui.check()
    ui.log(f"Opening '{SECTION}'...")
    if not await click_section(page, SECTION):
        await ask(ui, f"Couldn't find '{SECTION}' in the Gemini sidebar. Click '{SECTION}' "
                      "(or wherever your images are shown) in the browser window, then continue.")
    await page.wait_for_timeout(6000)

    TIMING["Opening Gemini"] += (time.monotonic() - t_open) - (TIMING["Waiting for you"] - w_open)

    # ---- Scroll to load everything ----
    t_scan, w_scan = time.monotonic(), TIMING["Waiting for you"]
    expected = index.expected_library_size()
    s_scan0 = TIMING["Saving images"]
    best = {}
    blocker = None
    fast_tried = False
    if PREVIEW_SCAN and expected and index.fast_failures < 2:
        blocker = await block_library_images(page)     # we have a size to check the result against
        fast_tried = blocker is not None
        if fast_tried:
            ui.log("Scanning the Library using small previews (faster)...")
    elif PREVIEW_SCAN and expected:
        ui.log("Using the careful full-size scan (the faster preview scan didn't work on the last runs).")

    async def reload_library():
        nonlocal blocker
        if blocker:
            await unblock_library_images(page, blocker)    # retry the careful way
            blocker = None
        await safe_goto(page, GEMINI_URL)
        await page.wait_for_timeout(5000)
        if not await click_section(page, SECTION):
            await ask(ui, f"Couldn't find '{SECTION}' in the Gemini sidebar. Click '{SECTION}' in the "
                          "browser window, then continue.")
        await page.wait_for_timeout(6000)

    # ---- checking pictures against what is already in the folder ----
    global FULL_LIMIT
    FULL_LIMIT = asyncio.Semaphore(CONCURRENCY)
    lock, sem = asyncio.Lock(), asyncio.Semaphore(LOOKUP_CONCURRENCY)
    stats, per_shape = Counter(), defaultdict(Counter)
    results = {}          # picture key -> (status, how, file name), for every Library entry checked so far

    def key_of(src):
        return image_key(src)[0]

    async def work(src):
        async with sem:
            ui.check()
            try:
                return src, await save_image(page, ctx, src, out_dir, index, lock, ui), None
            except Cancelled:
                raise
            except Exception as e:
                return src, None, e

    async def process(cands, total_hint, done_before):
        if not cands:
            return
        t0 = time.monotonic()
        tasks = [asyncio.ensure_future(work(c)) for c in cands]
        try:
            for n, fut in enumerate(asyncio.as_completed(tasks), 1):
                src, result, err = await fut
                shape = url_shape(src)
                if err is not None:
                    stats["failed"] += 1
                    per_shape[shape]["failed"] += 1
                    if DEBUG or stats["failed"] <= 3:
                        ui.log(f"  could not save one image: {err}")
                else:
                    status, how, name = result
                    results[key_of(src)] = (status, how, name)
                    stats[how] += 1
                    per_shape[shape][how] += 1
                    if status == "saved" and stats["new"] % 50 == 0:
                        index.save()
                done = done_before + n
                ui.progress(done, total_hint)
                eta = ""
                if 50 <= n < len(cands):
                    eta = f" (about {fmt_dur((time.monotonic() - t0) / n * (len(cands) - n))} left)"
                ui.status(f"Saving images... {done}/{total_hint}{eta}")
                if done % 50 == 0 or n == len(cands):
                    ui.log(f"  {done}/{total_hint} done ({stats['new']} new, "
                           f"{stats['address'] + stats['tail'] + stats['content']} already in folder, "
                           f"{stats['failed']} failed)")
        except BaseException:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        finally:
            TIMING["Saving images"] += time.monotonic() - t0

    # ---- which kind of scan? ----
    mode = "full"
    if OPTS.new_only:
        why = index.new_only_blocker(OPTS.full_days) if expected else "there is no earlier scan to compare with."
        if why is None:
            mode = "new"
        else:
            ui.log(f"Doing a full scan: {why}")
    candidates = []

    # ---- new pictures only: look at the top of the Library, stop when we reach pictures we already have ----
    if mode == "new":
        ui.log(f"New pictures only: checking the newest part of your Library (the first {TOP_WINDOW}, "
               "and further down if new pictures run close to the end of that)...")
        target, reloads = TOP_WINDOW, 0
        while True:
            ui.check()
            candidates = await collect_images(ui, page, expected, best, passes=1, target=target)
            wanted = min(target, expected)
            if len(candidates) < 0.95 * wanted and reloads < 2:
                reloads += 1
                ui.log(f"Found {len(candidates)} pictures but expected at least {wanted}. Gemini may not have "
                       f"finished loading, so reloading the Library to look again ({reloads} of 2)...")
                await reload_library()
                continue
            await process([c for c in candidates if key_of(c) not in results], len(candidates), len(results))
            tail_keys = [key_of(c) for c in candidates[-TOP_KNOWN_TAIL:]]
            tail_has_new = any(results.get(k, ("",))[0] == "saved" for k in tail_keys)
            if len(candidates) < target or not tail_has_new:
                break
            target += TOP_WINDOW
            ui.log("New pictures run close to the end of that part of the Library, so looking further down...")
        known = [results[k][2] for k in map(key_of, candidates)
                 if k in results and results[k][0] == "skipped" and results[k][2]]
        agreement = index.rank_consistency(known)
        if agreement is not None and agreement < 0.95:
            ui.log(f"The Library's order no longer matches the last full scan ({agreement:.0%} of neighbouring "
                   "pictures in order), so doing a full scan now...")
            mode = "full"
            await scroll_to_top(page)
            await page.wait_for_timeout(1500)
        else:
            added = [results[k][2] for k in map(key_of, candidates) if k in results and results[k][0] == "saved"]
            index.assign_new_ranks(added)
            index.library_total += len(added)
            index.dirty = True

    # ---- full scan: list the whole Library ----
    complete = True
    if mode == "full":
        ui.log("Loading your images (this can take a few minutes if you have a lot)...")
        ui.progress(None, None)
        candidates = None
        use_quick = OPTS.quick and expected and index.jump_failures < 2
        quick_ok = False
        if use_quick:
            ui.log("Trying a quicker way to list the Library (jumping to the bottom to load it all)...")
            candidates = await collect_images(ui, page, expected, best, jump=True)
            quick_ok = count_distinct(best) >= 0.99 * expected
            if not quick_ok:
                # Gemini often loads only the first ~600 pictures the first time; a reload fixes that
                ui.log(f"  The quick listing found only {count_distinct(best)} of about {expected}. Gemini may not "
                       "have finished loading, so reloading the Library and trying again...")
                await reload_library()
                candidates = await collect_images(ui, page, expected, best, jump=True)
                quick_ok = count_distinct(best) >= 0.99 * expected
                if not quick_ok:
                    ui.log(f"  The quick listing found {count_distinct(best)} of about {expected}, "
                           "so listing the Library the careful way...")
                    candidates = None
                    await scroll_to_top(page)
                    await page.wait_for_timeout(1500)
            if quick_ok:
                index.jump_failures = 0
                index.dirty = True
        elif OPTS.quick and expected:
            ui.log("Listing the Library the careful way (the quick way didn't work on the last runs).")
        if candidates is None:
            candidates = await collect_images(ui, page, expected, best)
        counts = [count_distinct(best)]
        if use_quick and not quick_ok and counts[0] >= 0.95 * expected:
            # the page was healthy (the careful way found everything) but the quick way still failed
            index.jump_failures += 1
            index.jump_failed_version = VERSION
            index.dirty = True
        for attempt in (1, 2):
            if not expected or counts[-1] >= 0.95 * expected:
                break
            if len(counts) > 1 and counts[-1] == counts[-2]:
                break                                  # the last reload found nothing new
            ui.log(f"Found {counts[-1]} images but expected about {expected}. Gemini may not have "
                   f"finished loading, so reloading the Library to look again carefully ({attempt} of 2)...")
            await reload_library()
            candidates = await collect_images(ui, page, expected, best, passes=1, careful=True)
            counts.append(count_distinct(best))

        if blocker:
            await unblock_library_images(page, blocker)
        if fast_tried:
            if counts[0] >= 0.95 * expected:
                index.fast_failures = 0
            else:
                index.fast_failures += 1
                index.fast_failed_version = VERSION
            index.dirty = True
        found = counts[-1]
        complete = not (expected and found < 0.95 * expected)
        if not complete:
            ui.log(f"Only {found} images found (expected about {expected}). If you haven't deleted images "
                   "from Gemini, keep the browser window visible and click Start again.")
            if index.last_short == found:
                ui.log(f"That's the same count as the last short run, so {found} will be treated as the "
                       "size of your Library from now on.")
                index.library_total, index.last_short = found, 0
            else:
                index.last_short = found
            index.dirty = True
        elif candidates:
            index.library_total, index.last_short = len(candidates), 0
            index.dirty = True

        if not candidates:
            shot = BASE / "no_images_found.png"
            try:
                await page.screenshot(path=str(shot))
            except Exception:
                pass
            ui.alert("No images were found on this page.\n\nEither your Library is empty, or Gemini's "
                     f"layout has changed since this tool was made.\nA screenshot was saved to:\n{shot}")
            return None

        try:
            ui.log(f"The page holds {await page.evaluate('document.images.length')} picture elements "
                   f"for {len(candidates)} Library pictures.")
        except Exception:
            pass
        ui.log(f"Found {len(candidates)} images in your Library. Saving new ones to: {out_dir}")
        await process([c for c in candidates if key_of(c) not in results], len(candidates), len(results))

        if complete and stats["failed"] == 0:
            # remember where each picture sits in the list, and check where new pictures appear
            keys = [key_of(c) for c in candidates]
            index.set_ranks_full([results[k][2] for k in keys if k in results and results[k][2]])
            new_positions = [i for i, k in enumerate(keys) if results.get(k, ("",))[0] == "saved"]
            if new_positions and len(new_positions) <= 0.2 * len(candidates):
                at_top = all(p < max(30, 2 * len(new_positions)) for p in new_positions)
                index.newest_first = at_top
                ui.log("Library order check: " + (
                    f"the {len(new_positions)} new pictures were at the top of the list, so new-pictures-only "
                    "mode can be used from now on." if at_top else
                    f"{len(new_positions)} new pictures were NOT all at the top of the list, so "
                    "new-pictures-only mode can't be used."))
            index.last_full_scan = time.time()
            index.dirty = True

    TIMING["Scanning your Library"] += ((time.monotonic() - t_scan) - (TIMING["Waiting for you"] - w_scan)
                                        - (TIMING["Saving images"] - s_scan0))
    ui.log("Image address types (private parts hidden):")
    for shape, c in sorted(per_shape.items(), key=lambda kv: -sum(kv[1].values()))[:6]:
        ui.log(f"  {sum(c.values())} x {shape}: {c['address']} matched by address, {c['tail']} by file end, "
               f"{c['content']} by picture, {c['new']} new")
    return {"saved": stats["new"], "skipped": stats["address"] + stats["tail"] + stats["content"],
            "by_address": stats["address"], "by_tail": stats["tail"], "by_content": stats["content"],
            "failed": stats["failed"], "total": len(candidates), "mode": mode}


async def run(ui, out_dir, opts=None):
    if opts is not None:
        OPTS.new_only, OPTS.quick, OPTS.full_days = opts.new_only, opts.quick, opts.full_days
    t_run = time.monotonic()
    TIMING.clear()
    COUNTS.clear()
    ui.banner()
    out_dir.mkdir(parents=True, exist_ok=True)
    PROFILES.mkdir(exist_ok=True)
    runlog.start(out_dir)

    index = ImageIndex(out_dir)
    t_index = time.monotonic()
    index.load(ui)
    TIMING["Checking existing images"] += time.monotonic() - t_index
    ui.progress(0, 1)

    try:
        async with async_playwright() as p:
            ctx = await launch_browser(p, ui)
            if ctx is None:
                ui.alert("Couldn't find Google Chrome or Microsoft Edge on this computer.\n"
                         "Please install Google Chrome (https://www.google.com/chrome) and try again.")
                return
            try:
                result = await session(ui, ctx, out_dir, index)
            finally:
                try:
                    await ctx.close()
                except Exception:
                    pass
    finally:
        index.save()

    if result:
        ui.log("")
        ui.log("All done!")
        if result["mode"] == "new":
            ui.log(f"  Checked the newest {result['total']} pictures of your Library (new pictures only).")
        else:
            ui.log(f"  Images in your Library:       {result['total']}")
        ui.log(f"  New images saved:             {result['saved']}")
        ui.log(f"  Already in your folder:       {result['skipped']}  "
               f"({result['by_address']} by address, {result['by_tail']} by file end, "
               f"{result['by_content']} by picture)")
        if result["failed"]:
            ui.log(f"  Failed:                       {result['failed']}  (run it again to retry these)")
        ui.log(f"Your images are in: {out_dir}")
        took = time.monotonic() - t_run
        ui.log(f"Time taken: {fmt_dur(took)}")
        for name in ("Checking existing images", "Opening Gemini", "Waiting for you",
                     "Scanning your Library", "Saving images"):
            if TIMING[name] >= 1:
                ui.log(f"   {name + ':':<28}{fmt_dur(TIMING[name])}")
        if COUNTS["downloads"] and TIMING["Saving images"] >= 1:
            mb = COUNTS["bytes"] / 1e6
            ui.log(f"   Downloaded {COUNTS['downloads']} images ({mb:,.0f} MB, "
                   f"{mb / TIMING['Saving images']:.1f} MB/s while saving)")
        if COUNTS["tail_tried"]:
            recognised = COUNTS["tail_hits"] + COUNTS["tail_verified"]
            ui.log(f"   Checked {COUNTS['tail_tried']} pictures from just their file end "
                   f"({COUNTS['tail_bytes'] / 1e6:,.0f} MB fetched): {recognised} recognised, "
                   f"{COUNTS['tail_verified']} spot-checked against the full picture, "
                   f"{COUNTS['tail_mismatch']} mismatches")
            by_type = ", ".join(f"{k} {COUNTS['matched_' + k]}/{COUNTS['tried_' + k]}"
                                for k in ("png", "jpg", "webp") if COUNTS["tried_" + k])
            if by_type:
                ui.log(f"   File-end matches by picture type: {by_type}")
        if COUNTS["tail_shared"]:
            ui.log(f"   {COUNTS['tail_shared']} Library entries matched a file that another entry had already "
                   f"matched; each was checked in full: {COUNTS['tail_shared_same']} were the identical picture, "
                   f"{COUNTS['tail_shared'] - COUNTS['tail_shared_same']} were different")
        ui.status(f"Done in {fmt_dur(took)}.")
        try:
            os.startfile(out_dir)    # open the folder (Windows)
        except Exception:
            pass


def report_crash(ui):
    text = traceback.format_exc()
    log = BASE / "error_log.txt"
    try:
        log.write_text(text, encoding="utf-8")
    except Exception:
        pass
    ui.alert("Something went wrong.\n\nCommon fixes:\n"
             "- Make sure you're online and signed in to Gemini.\n"
             "- Don't close the browser window while the tool is running.\n"
             "- Start it again; images that were already saved are skipped.\n\n"
             f"Technical details were saved to:\n{log}")
    if DEBUG:
        ui.log(text)


# ======================================================================
# Console version (--cli)
# ======================================================================

class ConsoleUI:
    def banner(self): console_banner()
    def log(self, msg):
        line = stamped(msg)
        runlog.write(line)
        print(line)
    def status(self, text): pass
    def progress(self, done, total): pass
    def alert(self, msg):
        print("\n" + msg)
        runlog.write(stamped(msg))
    def check(self): pass

    def ask(self, msg):
        input(f"\n{msg}\nPress Enter here to continue... ")


def main_cli():
    ui = ConsoleUI()
    try:
        asyncio.run(run(ui, DEFAULT_OUT))
    except KeyboardInterrupt:
        print("\nStopped.")
    except Exception:
        report_crash(ui)
    input("\nPress Enter to close this window...")


# ======================================================================
# Window version (default) - built with CustomTkinter
# ======================================================================

SETTINGS_FILE = BASE / "settings.json"
APPEARANCE = {"Auto": "System", "Light": "Light", "Dark": "Dark"}

# Colours are (light mode, dark mode) pairs
ACCENT = ("#4F46E5", "#6366F1")
ACCENT_HOVER = ("#4338CA", "#4F46E5")
DANGER = ("#DC2626", "#EF4444")
DANGER_HOVER = ("#B91C1C", "#DC2626")
TEXT = ("#111827", "#F3F4F6")
MUTED = ("#6B7280", "#9CA3AF")
BORDER = ("#D1D5DB", "#374151")
SUBTLE_HOVER = ("#F3F4F6", "#1F2937")
NOTICE_BG = ("#FEF3C7", "#3B2F0B")
NOTICE_BORDER = ("#F59E0B", "#B45309")
NOTICE_TEXT = ("#1F2937", "#FDE68A")


def load_settings():
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_settings(data):
    try:
        SETTINGS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


NEW_ONLY_TEXT = "New pictures only (faster)"
NEW_ONLY_TIP = ("Checks only the newest part of your Library and stops once it reaches pictures you already have. "
                "Usually a few minutes instead of about 15.\n\n"
                "It relies on Gemini listing new pictures first. The tool confirms this itself during a full scan, "
                f"and does a full scan at least every {FULL_CHECK_DAYS} days. Untick this for a complete scan now.")
QUICK_TEXT = "Quick listing (experimental)"
QUICK_TIP = ("Experimental, and only used for full scans. Jumps straight to the bottom of the Library to load it "
             "all at once.\n\n"
             "Disclaimer: in testing it was no faster than the normal scan and sometimes stopped short, so a few "
             "pictures could be missed. Leave it off unless you want to experiment.")


class Tooltip:
    """A small hover note for a widget (the window toolkit has none built in)."""

    def __init__(self, widget, text, delay=350):
        self.widget, self.text, self.delay = widget, text, delay
        self.tip, self.job = None, None
        try:
            widget.bind("<Enter>", self.schedule, add="+")
            widget.bind("<Leave>", self.hide, add="+")
            widget.bind("<ButtonPress>", self.hide, add="+")
        except Exception:
            pass

    def schedule(self, event=None):
        self.cancel()
        try:
            self.job = self.widget.after(self.delay, self.show)
        except Exception:
            self.job = None

    def cancel(self):
        if self.job is not None:
            try:
                self.widget.after_cancel(self.job)
            except Exception:
                pass
            self.job = None

    def show(self):
        self.job = None
        if self.tip is not None:
            return
        try:
            import tkinter as tk
            x = self.widget.winfo_rootx() + 24
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 8
            tip = self.tip = tk.Toplevel(self.widget)
            tip.wm_overrideredirect(True)
            tip.wm_attributes("-topmost", True)
            tip.wm_geometry(f"+{x}+{y}")
            tk.Label(tip, text=self.text, justify="left", wraplength=340, bg="#1F2937", fg="#F9FAFB",
                     padx=12, pady=9, font=("Segoe UI", 10), relief="flat").pack()
        except Exception:
            self.tip = None

    def hide(self, event=None):
        self.cancel()
        if self.tip is not None:
            try:
                self.tip.destroy()
            except Exception:
                pass
            self.tip = None


class App:
    def __init__(self, ctk):
        self.ctk = ctk
        self.settings = load_settings()
        mode_label = self.settings.get("appearance", "Auto")
        if mode_label not in APPEARANCE:
            mode_label = "Auto"
        ctk.set_appearance_mode(APPEARANCE[mode_label])
        ctk.set_default_color_theme("blue")

        self.q = queue.Queue()
        self.cancelled = False
        self.ask_event = None
        self.thread = None
        self.bar_indeterminate = False
        self.run_started = None
        self.run_done = True

        root = self.root = ctk.CTk()
        root.title(f"Gemini Image Downloader v{VERSION}")
        root.geometry("780x680")
        root.minsize(640, 600)
        # CustomTkinter applies its own icon shortly after start-up, so set ours after that
        root.after(250, self.set_icon)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.grid_columnconfigure(0, weight=1)
        root.grid_rowconfigure(4, weight=1)

        font = lambda size, weight="normal": ctk.CTkFont(family="Segoe UI", size=size, weight=weight)

        # ---- header ----
        header = ctk.CTkFrame(root, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=26, pady=(22, 6))
        header.grid_columnconfigure(0, weight=1)
        titles = ctk.CTkFrame(header, fg_color="transparent")
        titles.grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(titles, text="Gemini Image Downloader", font=font(24, "bold"),
                     text_color=TEXT).pack(anchor="w")
        ctk.CTkLabel(titles, text="Save your Gemini Library images to this computer",
                     font=font(13), text_color=MUTED).pack(anchor="w", pady=(2, 0))
        self.theme_btn = ctk.CTkSegmentedButton(
            header, values=list(APPEARANCE), command=self.on_theme, height=30,
            selected_color=ACCENT, selected_hover_color=ACCENT_HOVER)
        self.theme_btn.set(mode_label)
        self.theme_btn.grid(row=0, column=1, sticky="e")

        # ---- settings card ----
        card = ctk.CTkFrame(root, corner_radius=14)
        card.grid(row=1, column=0, sticky="ew", padx=26, pady=(14, 8))
        card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(card, text="Save images to", font=font(12, "bold"),
                     text_color=MUTED).grid(row=0, column=0, sticky="w", padx=18, pady=(14, 4), columnspan=2)
        self.folder_var = ctk.StringVar(value=self.settings.get("folder") or str(DEFAULT_OUT))
        ctk.CTkEntry(card, textvariable=self.folder_var, height=36, corner_radius=8,
                     font=font(13)).grid(row=1, column=0, sticky="ew", padx=(18, 8), pady=(0, 12))
        ctk.CTkButton(card, text="Browse", width=96, height=36, corner_radius=8, command=self.browse,
                      fg_color="transparent", border_width=1, border_color=BORDER, text_color=TEXT,
                      hover_color=SUBTLE_HOVER, font=font(13)).grid(row=1, column=1, padx=(0, 18), pady=(0, 12))

        self.new_only_var = ctk.BooleanVar(value=bool(self.settings.get("new_only", True)))
        self.quick_var = ctk.BooleanVar(value=bool(self.settings.get("quick", False)))
        opts = ctk.CTkFrame(card, fg_color="transparent")
        opts.grid(row=2, column=0, columnspan=2, sticky="w", padx=18, pady=(0, 6))
        self.option_boxes = []
        for text, var, tip in ((NEW_ONLY_TEXT, self.new_only_var, NEW_ONLY_TIP),
                               (QUICK_TEXT, self.quick_var, QUICK_TIP)):
            line = ctk.CTkFrame(opts, fg_color="transparent")
            line.pack(anchor="w", pady=2)
            box = ctk.CTkCheckBox(line, text=text, variable=var, command=self.on_options, font=font(13),
                                  checkbox_width=20, checkbox_height=20, corner_radius=5,
                                  fg_color=ACCENT, hover_color=ACCENT_HOVER)
            box.pack(side="left")
            hint = ctk.CTkLabel(line, text="?", width=18, height=18, corner_radius=9, fg_color=BORDER,
                                text_color=TEXT, font=font(11, "bold"))
            hint.pack(side="left", padx=(8, 0))
            Tooltip(box, tip)
            Tooltip(hint, tip)
            self.option_boxes.append(box)

        row = ctk.CTkFrame(card, fg_color="transparent")
        row.grid(row=3, column=0, columnspan=2, sticky="w", padx=18, pady=(2, 16))
        self.start_btn = ctk.CTkButton(row, text="Start download", width=150, height=40, corner_radius=10,
                                       command=self.start, fg_color=ACCENT, hover_color=ACCENT_HOVER,
                                       font=font(14, "bold"))
        self.start_btn.pack(side="left")
        self.stop_btn = ctk.CTkButton(row, text="Stop", width=84, height=40, corner_radius=10,
                                      command=self.stop, fg_color=DANGER, hover_color=DANGER_HOVER,
                                      font=font(14, "bold"), state="disabled")
        self.stop_btn.pack(side="left", padx=10)
        ctk.CTkButton(row, text="Open folder", width=110, height=40, corner_radius=10,
                      command=self.open_folder, fg_color="transparent", border_width=1,
                      border_color=BORDER, text_color=TEXT, hover_color=SUBTLE_HOVER,
                      font=font(13)).pack(side="left")
        ctk.CTkButton(row, text="Open log", width=96, height=40, corner_radius=10,
                      command=self.open_log, fg_color="transparent", border_width=1,
                      border_color=BORDER, text_color=TEXT, hover_color=SUBTLE_HOVER,
                      font=font(13)).pack(side="left", padx=(10, 0))

        # ---- notice box (shown only when you need to do something in the browser) ----
        self.prompt_frame = ctk.CTkFrame(root, corner_radius=12, fg_color=NOTICE_BG,
                                         border_width=1, border_color=NOTICE_BORDER)
        self.prompt_frame.grid(row=2, column=0, sticky="ew", padx=26, pady=(4, 8))
        self.prompt_frame.grid_columnconfigure(0, weight=1)
        self.prompt_label = ctk.CTkLabel(self.prompt_frame, text="", text_color=NOTICE_TEXT, font=font(13),
                                         wraplength=560, justify="left", anchor="w")
        self.prompt_label.grid(row=0, column=0, sticky="ew", padx=16, pady=14)
        ctk.CTkButton(self.prompt_frame, text="Continue", width=110, height=36, corner_radius=8,
                      command=self.on_continue, fg_color=ACCENT, hover_color=ACCENT_HOVER,
                      font=font(13, "bold")).grid(row=0, column=1, padx=(0, 16), pady=14)
        self.prompt_frame.grid_remove()

        # ---- status and progress ----
        status = ctk.CTkFrame(root, fg_color="transparent")
        status.grid(row=3, column=0, sticky="ew", padx=26, pady=(4, 6))
        status.grid_columnconfigure(0, weight=1)
        self.status_var = ctk.StringVar(value="Ready. Click Start download.")
        ctk.CTkLabel(status, textvariable=self.status_var, font=font(13), text_color=TEXT,
                     anchor="w").grid(row=0, column=0, sticky="ew", pady=(0, 6))
        self.elapsed_var = ctk.StringVar(value="")
        ctk.CTkLabel(status, textvariable=self.elapsed_var, font=font(13), text_color=MUTED,
                     anchor="e").grid(row=0, column=1, sticky="e", padx=(12, 0), pady=(0, 6))
        self.bar = ctk.CTkProgressBar(status, height=10, corner_radius=5, progress_color=ACCENT)
        self.bar.set(0)
        self.bar.grid(row=1, column=0, columnspan=2, sticky="ew")

        # ---- log ----
        self.log_box = ctk.CTkTextbox(root, corner_radius=12, font=("Consolas", 12), wrap="word")
        self.log_box.grid(row=4, column=0, sticky="nsew", padx=26, pady=(8, 8))
        self.log_box.configure(state="disabled")

        ctk.CTkLabel(root, font=font(11), text_color=MUTED,
                     text="Unofficial tool, not affiliated with Google. Everything stays on your computer."
                     ).grid(row=5, column=0, pady=(0, 14))

        root.after(100, self.poll)

    # ----- helpers -----
    def set_icon(self):
        try:
            self.root.iconbitmap(str(resource_path("icon.ico")))
        except Exception:
            pass

    def dialog(self, title, text, buttons=("OK",), wait=False):
        """A small themed pop-up. Returns the index of the button clicked (if wait=True)."""
        ctk = self.ctk
        win = ctk.CTkToplevel(self.root)
        win.title(title)
        win.resizable(False, False)
        result = {"value": None}

        def choose(i):
            result["value"] = i
            win.destroy()

        ctk.CTkLabel(win, text=text, wraplength=420, justify="left",
                     font=ctk.CTkFont(family="Segoe UI", size=13)).pack(padx=26, pady=(24, 16))
        btns = ctk.CTkFrame(win, fg_color="transparent")
        btns.pack(pady=(0, 20))
        for i, label in enumerate(buttons):
            ctk.CTkButton(btns, text=label, width=110, height=34, corner_radius=8,
                          fg_color=ACCENT, hover_color=ACCENT_HOVER,
                          command=lambda i=i: choose(i)).pack(side="left", padx=6)
        try:
            win.update_idletasks()
            x = self.root.winfo_x() + (self.root.winfo_width() - win.winfo_width()) // 2
            y = self.root.winfo_y() + (self.root.winfo_height() - win.winfo_height()) // 2
            win.geometry(f"+{max(x, 0)}+{max(y, 0)}")
            win.after(50, win.lift)
            win.after(100, win.grab_set)
        except Exception:
            pass
        if wait:
            self.root.wait_window(win)
        return result["value"]

    # ----- calls made by the worker thread (thread-safe via the queue) -----
    def banner(self): pass
    def log(self, msg):
        line = stamped(msg)
        runlog.write(line)
        self.q.put(("log", line))
    def status(self, text): self.q.put(("status", text))
    def progress(self, done, total): self.q.put(("progress", done, total))
    def alert(self, msg):
        line = stamped(msg)
        runlog.write(line)
        self.q.put(("alert", msg, line))

    def check(self):
        if self.cancelled:
            raise Cancelled()

    def ask(self, msg):
        self.check()
        ev = threading.Event()
        self.ask_event = ev
        self.q.put(("ask", msg))
        ev.wait()
        self.ask_event = None
        self.check()

    # ----- button handlers (main thread) -----
    def on_theme(self, label):
        self.ctk.set_appearance_mode(APPEARANCE.get(label, "System"))
        self.settings["appearance"] = label
        save_settings(self.settings)

    def on_options(self):
        self.settings["new_only"] = bool(self.new_only_var.get())
        self.settings["quick"] = bool(self.quick_var.get())
        save_settings(self.settings)

    def browse(self):
        from tkinter import filedialog
        d = filedialog.askdirectory(title="Choose where to save your images", mustexist=False)
        if d:
            self.folder_var.set(str(Path(d)))

    def open_folder(self):
        try:
            Path(self.folder_var.get()).mkdir(parents=True, exist_ok=True)
            os.startfile(self.folder_var.get())
        except Exception:
            pass

    def start(self):
        out = Path(self.folder_var.get().strip() or str(DEFAULT_OUT))
        try:
            out.mkdir(parents=True, exist_ok=True)
            probe = out / ".write_test"
            probe.write_text("x")
            probe.unlink()
        except OSError as e:
            self.dialog("Can't use that folder", f"Couldn't write to:\n{out}\n\n{e}")
            return
        self.settings["folder"] = str(out)
        save_settings(self.settings)
        self.cancelled = False
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.status_var.set("Starting...")
        self.run_started, self.run_done = time.monotonic(), False
        self.elapsed_var.set("Elapsed 0:00")
        opts = Options(new_only=bool(self.new_only_var.get()), quick=bool(self.quick_var.get()))
        for box in self.option_boxes:
            box.configure(state="disabled")
        self.thread = threading.Thread(target=self.work, args=(out, opts), daemon=True)
        self.thread.start()

    def stop(self):
        self.cancelled = True
        self.stop_btn.configure(state="disabled")
        self.status_var.set("Stopping...")
        if self.ask_event:
            self.ask_event.set()

    def on_continue(self):
        self.prompt_frame.grid_remove()
        if self.ask_event:
            self.ask_event.set()

    def on_close(self):
        if self.thread and self.thread.is_alive():
            choice = self.dialog("Quit?", "A download is still running. Stop it and quit?",
                                 buttons=("Keep running", "Stop and quit"), wait=True)
            if choice != 1:
                return
            self.stop()
            self.close_when_done(50)
        else:
            self.root.destroy()

    def close_when_done(self, tries):
        if not (self.thread and self.thread.is_alive()) or tries <= 0:
            self.root.destroy()
        else:
            self.root.after(200, self.close_when_done, tries - 1)

    # ----- worker thread -----
    def work(self, out, opts):
        try:
            asyncio.run(run(self, out, opts))
        except Cancelled:
            self.log("Stopped.")
            self.status("Stopped.")
        except Exception:
            report_crash(self)
            self.status("Something went wrong.")
        finally:
            self.q.put(("finished",))

    # ----- main-thread queue pump -----
    def append_log(self, text):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def poll(self):
        if self.run_started is not None and not self.run_done:
            self.elapsed_var.set("Elapsed " + fmt_clock(time.monotonic() - self.run_started))
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]
                if kind == "log":
                    self.append_log(item[1])
                elif kind == "status":
                    self.status_var.set(item[1])
                elif kind == "progress":
                    self.set_progress(item[1], item[2])
                elif kind == "ask":
                    self.status_var.set("Waiting for you... see the yellow box.")
                    self.prompt_label.configure(text=item[1])
                    self.prompt_frame.grid()
                    self.root.bell()
                elif kind == "alert":
                    self.append_log(item[2])
                    self.dialog("Gemini Image Downloader", item[1])
                elif kind == "finished":
                    self.finished()
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def set_progress(self, done, total):
        if total is None:
            if not self.bar_indeterminate:
                self.bar.configure(mode="indeterminate")
                self.bar.start()
                self.bar_indeterminate = True
        else:
            if self.bar_indeterminate:
                self.bar.stop()
                self.bar.configure(mode="determinate")
                self.bar_indeterminate = False
            self.bar.set(done / max(total, 1))

    def open_log(self):
        try:
            os.startfile(runlog.path)
        except Exception:
            self.dialog("No log yet", "The log file is created when you start a download.")

    def finished(self):
        if self.run_started is not None and not self.run_done:
            self.elapsed_var.set("Took " + fmt_clock(time.monotonic() - self.run_started))
        self.run_done = True
        for box in self.option_boxes:
            box.configure(state="normal")
        if self.bar_indeterminate:
            self.bar.stop()
            self.bar.configure(mode="determinate")
            self.bar.set(0)
            self.bar_indeterminate = False
        self.prompt_frame.grid_remove()
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")

    def mainloop(self):
        self.root.mainloop()


def main_gui():
    try:
        import customtkinter as ctk
    except ImportError:
        print("The window version needs the 'customtkinter' package:\n"
              "  python -m pip install -r requirements.txt\n"
              "Starting the console version instead.\n")
        return main_cli()
    App(ctk).mainloop()


if __name__ == "__main__":
    main_cli() if CLI else main_gui()
