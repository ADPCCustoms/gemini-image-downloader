## Gemini Image Downloader v1.3.1

### Faster
- **Many more small checks at once.** In v1.3.0, recognising a picture from the end of its file worked perfectly on a real Library (3,733 of 3,733 recognised, 0 mismatches), but the checks were limited to 6 at a time and each takes a moment to come back, so saving still took about 10 minutes. The tool now makes up to 24 of these small checks at once. Full-size downloads stay limited to 6 at a time.
- If the server pushes back on the small requests (HTTP 403 or 429), the tool waits and tries again before falling back to a full download.

### Notes
- Unofficial tool, not affiliated with Google.
