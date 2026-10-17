## Gemini Image Downloader v1.2.7

### Faster
- **Scanning no longer downloads every picture twice.** The v1.2.6 time summary showed that a scan of a 3,700-image Library took about 14 minutes, mostly because the page itself was loading thousands of full pictures just so the tool could read their addresses. The tool now stops the page loading Library pictures while it scrolls, and downloads only the ones it needs afterwards.
- This faster scan is used when the tool knows how big your Library should be (from your last run or the images already in your folder). A first run on an empty folder scans the careful way.

### Safety net
- If the faster scan finds fewer images than expected, the tool turns the blocking off, reloads the Library and scans again the careful way, as before.

### Notes
- Unofficial tool, not affiliated with Google.
