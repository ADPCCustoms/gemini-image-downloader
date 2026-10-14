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
import asyncio, base64, hashlib, os, queue, re, sys, threading, traceback
from pathlib import Path
from urllib.parse import urlparse

# Windowed (no-console) builds have no stdin/stdout; give them harmless stand-ins
for _name in ("stdin", "stdout", "stderr"):
    if getattr(sys, _name) is None:
        setattr(sys, _name, open(os.devnull, "r" if _name == "stdin" else "w"))

from playwright.async_api import async_playwright

VERSION = "1.1.0"

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

BROWSERS = [("chrome", "Google Chrome"), ("msedge", "Microsoft Edge")]

COLLECT_JS = """() => [...document.querySelectorAll('img')]
    .map(e => ({src: e.currentSrc || e.src, w: e.naturalWidth, h: e.naturalHeight}))"""


class Cancelled(Exception):
    """Raised when the user presses Stop."""


def resource_path(name):
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / name


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
    await asyncio.get_running_loop().run_in_executor(None, ui.ask, msg)


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
                args=["--disable-blink-features=AutomationControlled"],
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


async def save_image(page, ctx, src, out_dir):
    """Returns 'saved', or 'skipped' if this image is already in the folder."""
    key, full = image_key(src)
    if any(out_dir.glob(f"gemini_{key}.*")):
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
    (out_dir / f"gemini_{key}.{ext}").write_bytes(body)
    return "saved"


async def session(ui, ctx, out_dir):
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

    # ---- Scroll to load everything ----
    ui.log("Loading your images (this can take a few minutes if you have a lot)...")
    ui.progress(None, None)
    found, idle = {}, 0
    for step in range(3000):
        ui.check()
        for im in await page.evaluate(COLLECT_JS):
            found[im["src"]] = im
        moved = await scroll_everything_down(page)
        await page.wait_for_timeout(900)
        idle = idle + 1 if moved < 5 else 0
        if idle >= 3:
            break
        if step % 5 == 0:
            real = sum(1 for im in found.values()
                       if im["w"] >= MIN_SIZE and im["h"] >= MIN_SIZE and not is_ui_image(im["src"]))
            ui.status(f"Loading your images... found {real} so far")

    candidates = [im["src"] for im in found.values()
                  if im["w"] >= MIN_SIZE and im["h"] >= MIN_SIZE and not is_ui_image(im["src"])]

    if not candidates:
        shot = BASE / "no_images_found.png"
        try:
            await page.screenshot(path=str(shot))
        except Exception:
            pass
        ui.alert("No images were found on this page.\n\nEither your Library is empty, or Gemini's "
                 f"layout has changed since this tool was made.\nA screenshot was saved to:\n{shot}")
        return None

    # ---- Save ----
    total = len(candidates)
    ui.log(f"Found {total} images. Saving to: {out_dir}")
    saved = skipped = failed = 0
    for n, src in enumerate(candidates, 1):
        ui.check()
        try:
            result = await save_image(page, ctx, src, out_dir)
            if result == "saved":
                saved += 1
            else:
                skipped += 1
        except Cancelled:
            raise
        except Exception as e:
            failed += 1
            if DEBUG or failed <= 3:
                ui.log(f"  could not save one image: {e}")
        ui.progress(n, total)
        ui.status(f"Saving images... {n}/{total}")
        if n % 50 == 0 or n == total:
            ui.log(f"  {n}/{total} done ({saved} saved, {skipped} already had, {failed} failed)")
    return {"saved": saved, "skipped": skipped, "failed": failed}


async def run(ui, out_dir):
    ui.banner()
    out_dir.mkdir(parents=True, exist_ok=True)
    PROFILES.mkdir(exist_ok=True)

    async with async_playwright() as p:
        ctx = await launch_browser(p, ui)
        if ctx is None:
            ui.alert("Couldn't find Google Chrome or Microsoft Edge on this computer.\n"
                     "Please install Google Chrome (https://www.google.com/chrome) and try again.")
            return
        try:
            result = await session(ui, ctx, out_dir)
        finally:
            try:
                await ctx.close()
            except Exception:
                pass

    if result:
        ui.log("")
        ui.log("All done!")
        ui.log(f"  New images saved:      {result['saved']}")
        ui.log(f"  Already downloaded:    {result['skipped']}")
        if result["failed"]:
            ui.log(f"  Failed:                {result['failed']}  (run it again to retry these)")
        ui.log(f"Your images are in: {out_dir}")
        ui.status("Done.")
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
    def log(self, msg): print(msg)
    def status(self, text): pass
    def progress(self, done, total): pass
    def alert(self, msg): print("\n" + msg)
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
# Window version (default)
# ======================================================================

class App:
    def __init__(self):
        import tkinter as tk
        from tkinter import ttk, scrolledtext
        self.tk, self.ttk = tk, ttk

        try:    # crisp text on high-DPI Windows screens
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

        self.q = queue.Queue()
        self.cancelled = False
        self.ask_event = None
        self.thread = None
        self.bar_indeterminate = False

        root = self.root = tk.Tk()
        root.title(f"Gemini Image Downloader v{VERSION}")
        root.geometry("700x600")
        root.minsize(600, 520)
        try:
            root.iconbitmap(str(resource_path("icon.ico")))
        except Exception:
            pass
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        pad = {"padx": 14}
        ttk.Label(root, text="Gemini Image Downloader",
                  font=("Segoe UI", 15, "bold")).pack(anchor="w", pady=(12, 2), **pad)
        ttk.Label(root, wraplength=660, justify="left", text=(
            "Saves the images from your Gemini Library to a folder on this computer. "
            "You sign in yourself in a browser window that opens; everything stays on your "
            "computer and nothing is uploaded anywhere. Unofficial tool, not affiliated with Google."
        )).pack(anchor="w", pady=(0, 10), **pad)

        folder = ttk.Frame(root)
        folder.pack(fill="x", pady=(0, 8), **pad)
        ttk.Label(folder, text="Save images to:").pack(side="left")
        self.folder_var = tk.StringVar(value=str(DEFAULT_OUT))
        ttk.Entry(folder, textvariable=self.folder_var).pack(side="left", fill="x", expand=True, padx=8)
        ttk.Button(folder, text="Browse...", command=self.browse).pack(side="left")

        buttons = ttk.Frame(root)
        buttons.pack(fill="x", pady=(0, 8), **pad)
        self.start_btn = ttk.Button(buttons, text="Start", command=self.start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(buttons, text="Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=8)
        ttk.Button(buttons, text="Open folder", command=self.open_folder).pack(side="left")

        # Prompt box: shown when the tool needs you to do something in the browser
        self.prompt_frame = tk.Frame(root, bg="#FFF4CE", highlightbackground="#E0B84C", highlightthickness=1)
        self.prompt_label = tk.Label(self.prompt_frame, bg="#FFF4CE", fg="#222222", wraplength=520,
                                     justify="left", font=("Segoe UI", 10))
        self.prompt_label.pack(side="left", padx=10, pady=10, fill="x", expand=True)
        self.continue_btn = ttk.Button(self.prompt_frame, text="Continue", command=self.on_continue)
        self.continue_btn.pack(side="right", padx=10)

        self.status_var = tk.StringVar(value="Ready. Click Start.")
        ttk.Label(root, textvariable=self.status_var).pack(anchor="w", pady=(8, 2), **pad)
        self.bar = ttk.Progressbar(root, mode="determinate", maximum=100)
        self.bar.pack(fill="x", pady=(0, 8), **pad)

        self.log_box = scrolledtext.ScrolledText(root, height=12, state="disabled", wrap="word",
                                                 font=("Consolas", 9))
        self.log_box.pack(fill="both", expand=True, pady=(0, 12), **pad)

        root.after(100, self.poll)

    # ----- calls made by the worker thread (thread-safe via the queue) -----
    def banner(self): pass
    def log(self, msg): self.q.put(("log", msg))
    def status(self, text): self.q.put(("status", text))
    def progress(self, done, total): self.q.put(("progress", done, total))
    def alert(self, msg): self.q.put(("alert", msg))

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
        from tkinter import messagebox
        out = Path(self.folder_var.get().strip() or str(DEFAULT_OUT))
        try:
            out.mkdir(parents=True, exist_ok=True)
            probe = out / ".write_test"
            probe.write_text("x")
            probe.unlink()
        except OSError as e:
            messagebox.showerror("Can't use that folder", f"Couldn't write to:\n{out}\n\n{e}")
            return
        self.cancelled = False
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.status_var.set("Starting...")
        self.thread = threading.Thread(target=self.work, args=(out,), daemon=True)
        self.thread.start()

    def stop(self):
        self.cancelled = True
        self.stop_btn.config(state="disabled")
        self.status_var.set("Stopping...")
        if self.ask_event:
            self.ask_event.set()

    def on_continue(self):
        self.prompt_frame.pack_forget()
        if self.ask_event:
            self.ask_event.set()

    def on_close(self):
        from tkinter import messagebox
        if self.thread and self.thread.is_alive():
            if not messagebox.askyesno("Quit", "A download is still running. Stop it and quit?"):
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
    def work(self, out):
        try:
            asyncio.run(run(self, out))
        except Cancelled:
            self.log("Stopped.")
            self.status("Stopped.")
        except Exception:
            report_crash(self)
            self.status("Something went wrong.")
        finally:
            self.q.put(("finished",))

    # ----- main-thread queue pump -----
    def poll(self):
        from tkinter import messagebox
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]
                if kind == "log":
                    self.log_box.config(state="normal")
                    self.log_box.insert("end", item[1] + "\n")
                    self.log_box.see("end")
                    self.log_box.config(state="disabled")
                elif kind == "status":
                    self.status_var.set(item[1])
                elif kind == "progress":
                    self.set_progress(item[1], item[2])
                elif kind == "ask":
                    self.status_var.set("Waiting for you... see the yellow box.")
                    self.prompt_label.config(text=item[1])
                    self.prompt_frame.pack(fill="x", padx=14, pady=(0, 8), after=self.stop_btn.master)
                    self.root.bell()
                elif kind == "alert":
                    self.log_box.config(state="normal")
                    self.log_box.insert("end", item[1] + "\n")
                    self.log_box.see("end")
                    self.log_box.config(state="disabled")
                    messagebox.showwarning("Gemini Image Downloader", item[1])
                elif kind == "finished":
                    self.finished()
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def set_progress(self, done, total):
        if total is None:
            if not self.bar_indeterminate:
                self.bar.config(mode="indeterminate")
                self.bar.start(12)
                self.bar_indeterminate = True
        else:
            if self.bar_indeterminate:
                self.bar.stop()
                self.bar.config(mode="determinate")
                self.bar_indeterminate = False
            self.bar.config(maximum=max(total, 1), value=done)

    def finished(self):
        if self.bar_indeterminate:
            self.bar.stop()
            self.bar.config(mode="determinate", value=0)
            self.bar_indeterminate = False
        self.prompt_frame.pack_forget()
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")

    def mainloop(self):
        self.root.mainloop()


def main_gui():
    App().mainloop()


if __name__ == "__main__":
    main_cli() if CLI else main_gui()
