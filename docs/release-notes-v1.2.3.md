## Gemini Image Downloader v1.2.3

### Fixed
- **Sometimes only part of the Library was found.** The tool now remembers how many images your Library had last time. If a run finds far fewer (under 95%), it waits and looks again more slowly, up to three times, and tells you if the count still looks short.
- The browser is now started in a way that stops it pausing page loading when its window is covered by other windows.
- **Temporary download errors** (such as HTTP 403 or 429, or a network hiccup) are retried automatically instead of counting as failed straight away.

### Notes
- The first run after updating just records your Library size; the check works from the next run on.
- If you've deleted lots of images from your Library, the tool may look again unnecessarily once; it then remembers the new size.
- Unofficial tool, not affiliated with Google.
