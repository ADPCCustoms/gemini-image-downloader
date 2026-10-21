## Gemini Image Downloader v1.3.3

### Changed
- **The quick listing now gets a fair test.** On real runs, Gemini's Library often loads only the first 500 to 600 pictures the first time it is opened in a session, whichever way you scroll, and a reload fixes it. The tool now reloads the Library when the quick listing comes up short and tries the quick listing again on the reloaded page, before falling back to the careful way.
- A stalled first load is no longer counted against the quick listing. It only counts as a failure if the careful way then finds everything on the same page.
- When a listing is far below the expected size, the tool no longer spends time on a second pass over the stalled page. It goes straight to the reload.
- The small-preview scan from 1.2.8 is now off by default. Measured over several runs it made no real difference to scan time. (`--preview-scan` turns it back on.)

### Notes
- Library entries that share a file are checked in full (added in 1.3.2). On a real Library, 12 such entries were found and all 12 were confirmed to be the identical picture, so the Library lists a few pictures more than once.
- Unofficial tool, not affiliated with Google.
