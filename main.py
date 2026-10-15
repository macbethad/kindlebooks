import sys
from pathlib import Path

import webview

from app import updater
from app.api import Api

BASE = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))


def force_light_title_bar():
    """The app is light-only; stop pywebview from darkening the title bar on dark-mode Windows."""
    from webview.platforms.winforms import BrowserView
    BrowserView.BrowserForm.is_dark_theme = lambda self: False


if __name__ == "__main__":
    force_light_title_bar()
    updater.cleanup_old()
    api = Api()
    window = webview.create_window(
        "Книги на Kindle",
        url=str(BASE / "app" / "ui" / "index.html"),
        js_api=api,
        width=880,
        height=760,
        min_size=(560, 520),
        background_color="#FBFBFA",
    )
    api._window = window
    webview.start()
