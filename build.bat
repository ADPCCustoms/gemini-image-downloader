@echo off
REM Builds GeminiImageDownloader.exe (run this on Windows, with Python installed)
REM To use a custom icon, put a file named icon.ico in this same folder.

REM Always work from the folder this file is in
cd /d "%~dp0"

echo Working in: %cd%
echo.

python -m pip install --upgrade playwright customtkinter pillow pyinstaller

REM Remove old build output so an old icon can't be reused
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist GeminiImageDownloader.spec del /q GeminiImageDownloader.spec

if exist "%~dp0icon.ico" (
    echo.
    echo *** Using icon: %~dp0icon.ico ***
    python -m PyInstaller --clean --onefile --collect-all playwright --collect-all customtkinter --windowed --add-data "icon.ico;." --icon "%~dp0icon.ico" --name GeminiImageDownloader gemini_image_downloader.py
) else (
    echo.
    echo *** icon.ico NOT FOUND in %~dp0 - building with the default icon ***
    python -m PyInstaller --clean --onefile --windowed --collect-all playwright --collect-all customtkinter --name GeminiImageDownloader gemini_image_downloader.py
)

echo.
echo Done. Your program is in the "dist" folder: dist\GeminiImageDownloader.exe
pause
