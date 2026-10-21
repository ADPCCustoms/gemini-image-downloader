## Gemini Image Downloader v1.3.2

### Safer
- **Library entries that share a file are now checked in full.** If two Library entries are recognised as the same saved file (for example the same picture listed twice, or a new picture wrongly matched), each one after the first is now downloaded in full and compared picture to picture. If it turns out to be a different picture, it is saved as new and the file-end shortcut switches itself off. The summary shows how many entries shared a file, and how many were identical or different.

### Faster (experimental)
- **Quick listing.** Scanning the Library took about 13 of the 15 minutes of a full run. The tool now first tries jumping straight to the bottom of the Library over and over, which makes Gemini load the whole list quickly, and reads all the picture addresses in one go. It is only accepted if it finds at least 99% of the expected number of pictures; otherwise the tool lists the Library the careful way as before. If the quick listing comes up short on two runs in a row, the tool stops trying it until you update to a newer version.
- Run the program with `--no-quick-listing` to turn it off.

### Added
- The log shows how many picture elements the page holds for the number of Library pictures found, which tells us whether the page keeps every picture loaded.

### Notes
- Unofficial tool, not affiliated with Google.
