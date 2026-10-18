## Gemini Image Downloader v1.2.8

### Fixed
- **The faster scan in 1.2.7 didn't work on the real page.** Blocking the Library pictures made Gemini list only the first 30, so the tool noticed, fell back to the careful scan and still got everything, but wasted about a minute.

### Changed
- **Preview-size scan.** While scrolling, the tool now makes the page load small previews of each Library picture instead of the full-size ones, rather than blocking them. The pictures still load, so Gemini behaves normally, but far less data is transferred. The tool only needs each picture's address from this scan.
- If the faster scan still comes up short, the tool falls back to the careful scan as before. After two such runs in a row it stops trying the fast scan until you update to a newer version.

### Notes
- Unofficial tool, not affiliated with Google.
