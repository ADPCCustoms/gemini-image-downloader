## Gemini Image Downloader v1.3.0

### Faster re-checks (new, experimental)
- **Pictures you already have are recognised from just the end of the file.** When the image addresses from your last run have expired, the tool used to download every picture in full just to compare it, which took about 13 minutes for 3,700 images. It now asks Gemini for only the last 32 KB of each file. Two downloads of the same picture differ only in a few bytes of metadata near the start, so their ends are identical (99.7% of the repeat copies tested matched). That is about a hundredth of the data.
- **It checks itself.** One in every 50 matches is confirmed by downloading the full picture and comparing the pictures. If a confirmation ever disagrees, the shortcut switches itself off for that version and every picture is checked in full again.
- If Gemini's server doesn't support asking for part of a file, the tool carries on exactly as before.
- The summary shows how many pictures were recognised this way, how many were spot-checked, any mismatches, and the results by file type (PNG/JPG).

### Notes
- Most testing so far was on JPEG pictures. PNG results will show in the summary line.
- `tools/` now includes the test scripts used to check this idea.
- Unofficial tool, not affiliated with Google.
