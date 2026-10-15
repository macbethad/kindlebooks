@echo off
rem Builds dist\KindleBooks.exe (a single file you can copy to any Windows PC).
cd /d "%~dp0"
if not exist .venv (
    python -m venv .venv || goto :error
    .venv\Scripts\python -m pip install -r requirements.txt || goto :error
)
.venv\Scripts\python -m PyInstaller --noconfirm --onefile --windowed --name KindleBooks --icon app\icon.ico --add-data "app\ui;app\ui" main.py || goto :error
echo.
echo Done: dist\KindleBooks.exe
goto :eof
:error
echo Build failed.
exit /b 1
