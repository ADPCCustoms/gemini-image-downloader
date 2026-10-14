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
import asyncio, base64, hashlib, json, os, queue, re, sys, threading, traceback
from pathlib import Path
from urllib.parse import urlparse

# Windowed (no-console) builds have no stdin/stdout; give them harmless stand-ins
for _name in ("stdin", "stdout", "stderr"):
    if getattr(sys, _name) is None:
        setattr(sys, _name, open(os.devnull, "r" if _name == "stdin" else "w"))

from playwright.async_api import async_playwright

VERSION = "1.2.0"

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

        row = ctk.CTkFrame(card, fg_color="transparent")
        row.grid(row=2, column=0, columnspan=2, sticky="w", padx=18, pady=(2, 16))
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
        self.bar = ctk.CTkProgressBar(status, height=10, corner_radius=5, progress_color=ACCENT)
        self.bar.set(0)
        self.bar.grid(row=1, column=0, sticky="ew")

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
    def on_theme(self, label):
        self.ctk.set_appearance_mode(APPEARANCE.get(label, "System"))
        self.settings["appearance"] = label
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
        self.thread = threading.Thread(target=self.work, args=(out,), daemon=True)
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
    def append_log(self, text):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def poll(self):
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
                    self.append_log(item[1])
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

    def finished(self):
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
