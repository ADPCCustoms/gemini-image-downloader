## Gemini Image Downloader v1.2.2

### Fixed
- **No more repeat copies.** Pictures are now recognised by the picture itself, not just the file. A later run can no longer save the same image again when the download differs only in hidden metadata or the web address changed.
- A new run also checks existing images by picture, so folders that already contain images (including from older versions) are recognised.

### Faster
- Downloads run six at a time, so saving a large Library is several times quicker.

### Added
- The log now shows the *types* of image address it saw (with the private parts hidden) and how each type was recognised. This helps diagnose why some images are skipped quickly and others need re-checking.
- `tools/find_duplicates.py` cleans up folders that already contain repeat copies (it moves them aside, never deletes).

### Notes
- The first run after updating re-checks the images already in your folder, which takes a few minutes for thousands of images. The result is remembered.
- Requires Pillow, which is bundled in the .exe.
- Unofficial tool, not affiliated with Google.
