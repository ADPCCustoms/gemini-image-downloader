## Gemini Image Downloader v1.2.6

### Faster
- **Scanning your Library is quicker.** The time summary from v1.2.5 showed that scanning, not downloading, took about two thirds of a full run. Now:
  - After scrolling, the tool moves on as soon as the new images have loaded, instead of always waiting a fixed time. It still waits the full time when the page stalls.
  - The page reports only images it hasn't reported before, and the scrollable areas are remembered between steps, so each step does much less work on a big Library.
  - The second "check for missed images" pass is skipped when the first pass finds exactly the number of images expected from your last run. It still runs on a first run, or when the count has changed.

### New
- Each scan pass now shows how long it took, and the end summary shows how many images were downloaded, how many MB, and the speed.

### Notes
- If a faster scan ever finds fewer images than expected, the tool reloads the Library and re-scans at the old, slower, careful speed.
- Unofficial tool, not affiliated with Google.
