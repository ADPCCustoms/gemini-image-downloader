## Gemini Image Downloader v1.4.0

### New: New pictures only (faster)
- A new tick box, **New pictures only (faster)**, is on by default. Instead of listing the whole Library (about 12 to 14 minutes for 3,700 pictures, however it is scrolled), the tool looks at the newest part of it and stops once it reaches pictures you already have. It should take a few minutes.
- **It checks its own assumption.** It relies on Gemini listing new pictures first. During a full scan the tool records where each picture sits in the Library list, and confirms that new pictures show up at the top. Until that is confirmed, runs are full scans. If new pictures turn out not to be at the top, the mode switches itself off.
- **A full scan is still done at least every 7 days,** and automatically whenever the order of your Library no longer matches what was recorded. Untick the box for a complete scan any time.
- If new pictures run close to the end of the part it looked at, it keeps looking further down.

### Changed
- The **Quick listing (experimental)** option is now a tick box (off by default) with a note explaining that in testing it was no faster than the normal scan and sometimes stopped short. Hover over the **?** next to either option to read its note.
- Your two choices are remembered between runs.

### Notes
- Command line: `--full` turns new-pictures-only off; `--quick-listing` turns the quick listing on.
- Unofficial tool, not affiliated with Google.
