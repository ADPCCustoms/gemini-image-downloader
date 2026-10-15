## Gemini Image Downloader v1.2.1

### Fixed
- **More accurate image counts.** The tool now scrolls your Library more patiently and re-checks it (up to three passes) until nothing new turns up, so one run finds everything. Previously a second run could find extra images.
- **Better recognition of images you already have.** Images are now matched by their content, not just their web address. Pictures saved earlier (including by older versions or under different file names) are skipped instead of downloaded again.

### Notes
- Choose the folder that already holds your images as the save folder. The first run checks what's in it, which takes a minute for large folders; the result is remembered in a hidden `.gemini_index.json` file in that folder.
- The final summary now shows how many images are in your Library, how many were new, and how many you already had.
- Scanning takes a little longer than before because of the extra checking pass.
