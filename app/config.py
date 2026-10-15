"""Settings stored as JSON in %APPDATA%\\FlibustaKindle\\config.json."""

import json
import os
from pathlib import Path

APP_NAME = "FlibustaKindle"

CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULTS = {
    # Site access
    "base_url": "https://flibusta.is",
    "proxy": "",
    # Send to Kindle by e-mail
    "kindle_email": "",
    "sender_email": "",
    "sender_password": "",
    "smtp_host": "",
    "smtp_port": 0,
    # Misc
    "download_dir": str(Path.home() / "Documents" / "Книги Flibusta"),
    "calibre_path": "",
}

MIRRORS = [
    "https://flibusta.is",
    "http://flibustaongezhld6dibs2dps6vm4nvqg2kp7vgowbu76tzopgnhazqd.onion",
]

TOR_BROWSER_PROXY = "socks5h://127.0.0.1:9150"


def load():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cfg


def save(cfg):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_FILE)


def email_configured(cfg):
    return all(cfg.get(k) for k in ("kindle_email", "sender_email", "sender_password", "smtp_host", "smtp_port"))
