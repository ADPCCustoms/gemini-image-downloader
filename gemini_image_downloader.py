"""
Gemini Image Downloader
-----------------------
Downloads the images in your Gemini "Library" to a folder on your computer.

Everything runs locally. You log in to your own Google account in a normal
browser window; nothing is sent to anyone else.

This is an unofficial tool and is not affiliated with Google. It depends on
Gemini's web page layout, so it may stop working if Google changes the site.
Run with --debug for extra diagnostic output.
"""
import asyncio, base64, hashlib, os, re, sys, traceback
from pathlib import Path
from playwright.async_api import async_playwright

VERSION = "1.0.1"

# ---------------- SETTINGS ----------------
SECTION = "Library"      # Sidebar item that holds your generated images
MIN_SIZE = 120           # Ignore images smaller than this (icons, avatars)
DEBUG = "--debug" in sys.argv
# ------------------------------------------

# Save everything next to the .exe (or next to this script when run normally)
BASE = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
OUT = BASE / "gemini_images"
PROFILES = BASE / "browser_profile"      # keeps you logged in between runs

BROWSERS = [("chrome", "Google Chrome"), ("msedge", "Microsoft Edge")]

COLLECT_JS = """() => [...document.querySelectorAll('img')]
    .map(e => ({src: e.currentSrc || e.src, w: e.naturalWidth, h: e.naturalHeight}))"""


GEMINI_URL = "https://gemini.google.com/app"


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
    from urllib.parse import urlparse
    u = urlparse(url)
    if "accounts.google.com" in u.netloc:
        return True
    return u.netloc == "gemini.google.com" and u.path in ("", "/", "/welcome")


def banner():
    print("=" * 60)
    print(f" Gemini Image Downloader v{VERSION}")
    print("=" * 60)
    print(" This tool opens a browser window, goes to your Gemini")
    print(" Library and saves your images into a folder next to this")
    print(" program. You log in yourself; everything stays on your")
    print(" computer and nothing is sent anywhere else.")
    print(" (Unofficial tool, not affiliated with Google.)")
    print("=" * 60)
    print()


def is_ui_image(src):
    return ("gstatic.com" in src or "/ogw/" in src
            or "googleusercontent.com/a/" in src or src.endswith(".svg"))


async def launch_browser(p):
    """Try Chrome first, then Edge. Each browser gets its own profile folder."""
    for channel, label in BROWSERS:
        try:
            ctx = await p.chromium.launch_persistent_context(
                str(PROFILES / channel), headless=False, channel=channel,
                args=["--disable-blink-features=AutomationControlled"],
            )
            print(f"Using {label}.\n")
            return ctx
        except Exception as e:
            if DEBUG:
                print(f"({label} not available: {e})")
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


async def scroll_everything_down(page):
    """Scroll every scrollable element down a bit; return how far anything moved."""
    return await page.evaluate("""() => {
        let moved = 0;
        const els = [document.scrollingElement, ...document.querySelectorAll('*')];
        for (const el of els) {
            if (el && el.scrollHeight > el.clientHeight + 50) {
                const before = el.scrollTop;
                el.scrollTop = before + el.clientHeight * 0.9;
                moved += el.scrollTop - before;
            }
        }
        return moved;
    }""")


def image_key(src):
    """Stable name for an image so re-runs can skip ones already saved."""
    full = re.sub(r"=[sw]\d+.*$", "=s0", src)
    return hashlib.md5(full.encode()).hexdigest()[:16], full


async def save_image(page, ctx, src):
    """Returns 'saved', or 'skipped' if this image is already in the folder."""
    key, full = image_key(src)
    if any(OUT.glob(f"gemini_{key}.*")):
        return "skipped"

    if src.startswith("blob:") or src.startswith("data:"):
        b64 = await page.evaluate("""async (u) => {
            const b = await (await fetch(u)).blob();
            return await new Promise(r => {
                const fr = new FileReader();
                fr.onload = () => r(fr.result.split(',')[1]);
                fr.readAsDataURL(b);
            });
        }""", src)
        body, ctype = base64.b64decode(b64), ""
    else:
        r = await ctx.request.get(full)
        if not r.ok:
            raise RuntimeError(f"download failed (HTTP {r.status})")
        body, ctype = await r.body(), r.headers.get("content-type", "")

    ext = {"image/jpeg": "jpg", "image/webp": "webp"}.get(ctype)
    if not ext:
        ext = "jpg" if body[:3] == b"\xff\xd8\xff" else "webp" if body[8:12] == b"WEBP" else "png"
    (OUT / f"gemini_{key}.{ext}").write_bytes(body)
    return "saved"


async def run():
    banner()
    OUT.mkdir(exist_ok=True)
    PROFILES.mkdir(exist_ok=True)

    async with async_playwright() as p:
        ctx = await launch_browser(p)
        if ctx is None:
            print("Couldn't find Google Chrome or Microsoft Edge on this computer.")
            print("Please install Google Chrome (https://www.google.com/chrome) and run this again.")
            return

        page = await ctx.new_page()
        await safe_goto(page, GEMINI_URL)
        print("A browser window has opened on Gemini.")
        print("If you're asked to sign in, sign in to the Google account whose images you want.")
        input("When Gemini has fully loaded and you can see your chats, press Enter here... ")

        # Reload Gemini so the page has settled, and make sure you're really signed in.
        signed_in = False
        for attempt in range(4):
            await safe_goto(page, GEMINI_URL)
            await page.wait_for_timeout(5000)
            if not looks_signed_out(page.url):
                signed_in = True
                break
            if attempt == 3:
                break
            print("\nIt looks like you're not signed in to Gemini yet.")
            print("Sign in in the browser window and wait until you can see your chats.")
            input("Then press Enter here... ")
        if not signed_in:
            print("\nCouldn't confirm that you're signed in to Gemini.")
            print("Please run the tool again and sign in when the browser opens.")
            await ctx.close()
            return

        print(f"\nOpening '{SECTION}'...")
        if not await click_section(page, SECTION):
            print(f"Couldn't find '{SECTION}' in the Gemini sidebar.")
            print(f"Please click '{SECTION}' (or wherever your images are shown) yourself in the browser window.")
            input("When the images are showing, press Enter here... ")
        await page.wait_for_timeout(6000)

        if DEBUG:
            print("Page:", page.url, "|", await page.title())

        # ---- Scroll to load everything ----
        print("Loading your images (this can take a few minutes if you have a lot)...")
        found, idle = {}, 0
        for step in range(3000):
            for im in await page.evaluate(COLLECT_JS):
                found[im["src"]] = im
            moved = await scroll_everything_down(page)
            await page.wait_for_timeout(900)
            idle = idle + 1 if moved < 5 else 0
            if idle >= 3:
                break
            if step % 10 == 0 and step:
                real = sum(1 for im in found.values()
                           if im["w"] >= MIN_SIZE and im["h"] >= MIN_SIZE and not is_ui_image(im["src"]))
                print(f"  ...found {real} images so far")

        candidates = [im["src"] for im in found.values()
                      if im["w"] >= MIN_SIZE and im["h"] >= MIN_SIZE and not is_ui_image(im["src"])]

        if not candidates:
            shot = BASE / "no_images_found.png"
            await page.screenshot(path=str(shot))
            print("\nNo images were found on this page.")
            print("Either your Library is empty, or Gemini's layout has changed since this tool was made.")
            print(f"A screenshot was saved to: {shot}")
            await ctx.close()
            return

        # ---- Save ----
        total = len(candidates)
        print(f"\nFound {total} images. Saving to:\n  {OUT}\n")
        saved = skipped = failed = 0
        for n, src in enumerate(candidates, 1):
            try:
                result = await save_image(page, ctx, src)
                if result == "saved":
                    saved += 1
                else:
                    skipped += 1
            except Exception as e:
                failed += 1
                if DEBUG or failed <= 3:
                    print(f"  could not save one image: {e}")
            if n % 25 == 0 or n == total:
                print(f"  {n}/{total} done ({saved} saved, {skipped} already had, {failed} failed)")

        await ctx.close()

    print("\nAll done!")
    print(f"  New images saved:      {saved}")
    print(f"  Already downloaded:    {skipped}")
    if failed:
        print(f"  Failed:                {failed}  (run it again to retry these)")
    print(f"\nYour images are in:\n  {OUT}")
    try:
        os.startfile(OUT)    # open the folder (Windows)
    except Exception:
        pass


def main():
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\nStopped.")
    except Exception:
        log = BASE / "error_log.txt"
        try:
            log.write_text(traceback.format_exc(), encoding="utf-8")
        except Exception:
            pass
        print("\nSomething went wrong. Common fixes:")
        print("  - Make sure you're connected to the internet and signed in to Gemini.")
        print("  - Don't close the browser window while the tool is running.")
        print("  - Run the tool again; it skips images it has already saved.")
        print(f"\nTechnical details were saved to:\n  {log}")
        if DEBUG:
            traceback.print_exc()
    input("\nPress Enter to close this window...")


if __name__ == "__main__":
    main()
