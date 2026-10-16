## Gemini Image Downloader v1.2.4

### Fixed
- **Sometimes only part of the Library was found** (for example 570 of 3,732 images), and a second run was needed. The tool now knows roughly how many images to expect, from your last run or from the images already in your folder. If a run finds under 95% of that, it **reloads the Library and looks again** (up to twice), keeping everything seen so far.
- When a page stalls at the bottom, the tool now also nudges it with real mouse-wheel and End-key presses, in case it only loads more on those.
- If your Library really is smaller because you deleted images, it reloads once, says so, and treats the new size as normal when the same smaller count shows up again. After that there is no extra work.

### Changed
- Replaces the "slow look" added in 1.2.3, which didn't help when the page had simply stopped loading.

### Notes
- The check uses the images already in your save folder, so it works from the first run after updating.
- Unofficial tool, not affiliated with Google.
