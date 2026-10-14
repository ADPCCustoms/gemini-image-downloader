## Gemini Image Downloader v1.0.0

Download all the images in your Gemini **Library** to your PC in one go, no need to open every chat.

### What it does
- Opens Gemini in your own Chrome or Edge, goes to your Library and saves your images in full size
- Runs entirely on your computer; you sign in yourself and nothing is uploaded anywhere
- Safe to re-run: it skips images you've already downloaded

### How to use
1. Download **GeminiImageDownloader.exe** below and put it in its own folder.
2. Double-click it. If Windows says "Windows protected your PC", click **More info → Run anyway** (the exe isn't code-signed).
3. Sign in to Gemini in the browser window, wait for your chats to appear, then press **Enter** in the console window.
4. Your images are saved in the `gemini_images` folder next to the program.

Requires Windows 10/11 and Google Chrome or Microsoft Edge.

### Verify the download
This exe is built automatically from the source in this repository by GitHub Actions. The SHA-256 checksum is in `GeminiImageDownloader.exe.sha256`. To check yours in PowerShell:
`Get-FileHash .\GeminiImageDownloader.exe -Algorithm SHA256`

### Notes
- Unofficial tool, not affiliated with Google. It reads the Gemini web page, so it may stop working if Google changes the layout; please [open an issue](../../issues/new/choose) if it does.
- Never share your `browser_profile` folder; it contains your Google login session.
