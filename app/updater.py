"""Self-update from GitHub Releases.

A release is tagged "v1.2.0" and carries KindleBooks.exe as an asset. A running exe can't overwrite itself,
but Windows lets it rename itself: so the new build is downloaded next to it, the running file is renamed to
*.old.exe, the new one takes its name, and the app restarts. The old file is deleted on the next launch.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

import requests

from .flibusta import normalize_proxy
from .version import UPDATE_REPO, VERSION

ASSET_NAME = "KindleBooks.exe"
# Lets a test point the app at a local feed instead of GitHub.
FEED_OVERRIDE = os.environ.get("KINDLEBOOKS_UPDATE_FEED")

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200


class UpdateError(Exception):
    pass


def _version_tuple(text):
    return tuple(int(x) for x in re.findall(r"\d+", text)[:3])


def _get(url, proxy, **kwargs):
    """GitHub is usually reachable directly; fall back to the user's proxy if it isn't."""
    attempts = [None]
    if normalize_proxy(proxy):
        attempts.append(normalize_proxy(proxy))
    error = None
    for p in attempts:
        try:
            return requests.get(url, proxies={"http": p, "https": p} if p else None, **kwargs)
        except requests.RequestException as e:
            error = e
    raise UpdateError(f"Не удалось проверить обновления: {error}")


def check(proxy=None):
    """Return {"version", "notes", "url", "size"} if a newer release exists, else None."""
    feed = FEED_OVERRIDE or (UPDATE_REPO and f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest")
    if not feed:
        return None
    r = _get(feed, proxy, timeout=20, headers={"Accept": "application/vnd.github+json"})
    if r.status_code == 404:
        return None  # no releases published yet
    if r.status_code != 200:
        raise UpdateError(f"Не удалось проверить обновления (ошибка {r.status_code}).")
    data = r.json()
    tag = data.get("tag_name", "")
    if _version_tuple(tag) <= _version_tuple(VERSION):
        return None
    asset = next((a for a in data.get("assets", []) if a.get("name") == ASSET_NAME), None)
    if not asset:
        return None
    return {
        "version": tag.lstrip("vV"),
        "notes": (data.get("body") or "").strip(),
        "url": asset["browser_download_url"],
        "size": asset.get("size") or 0,
    }


def can_install():
    return getattr(sys, "frozen", False)


def install(info, proxy=None, progress=None):
    """Download the new exe, swap it in for the running one and start it."""
    if not can_install():
        raise UpdateError("Обновление работает только в собранной программе (KindleBooks.exe).")
    exe = Path(sys.executable)
    new = exe.with_name(exe.stem + ".new.exe")
    old = exe.with_name(exe.stem + ".old.exe")

    r = _get(info["url"], proxy, stream=True, timeout=(20, 120))
    if r.status_code != 200:
        raise UpdateError(f"Не удалось скачать обновление (ошибка {r.status_code}).")
    total = info["size"] or int(r.headers.get("Content-Length") or 0)
    done = 0
    try:
        with open(new, "wb") as f:
            for chunk in r.iter_content(256 * 1024):
                f.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
    except PermissionError as e:
        raise UpdateError(_no_write_access(exe)) from e
    except (OSError, requests.RequestException) as e:
        new.unlink(missing_ok=True)
        raise UpdateError(f"Загрузка обновления прервалась: {e}") from e

    with open(new, "rb") as f:
        is_exe = f.read(2) == b"MZ"
    if not is_exe or (total and new.stat().st_size != total):
        new.unlink(missing_ok=True)
        raise UpdateError("Обновление скачалось с ошибкой. Попробуйте ещё раз.")

    try:
        try:
            old.unlink(missing_ok=True)
        except OSError:
            old = exe.with_name(f"{exe.stem}.old{os.getpid()}.exe")  # a previous old copy is still locked
        exe.replace(old)  # allowed even while running
        new.replace(exe)
    except OSError as e:
        if old.exists() and not exe.exists():
            old.replace(exe)
        new.unlink(missing_ok=True)
        raise UpdateError(_no_write_access(exe)) from e

    # A onefile app starting a copy of itself must ask PyInstaller for a clean environment,
    # otherwise the new process tries to reuse this one's unpacked files.
    env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1")
    subprocess.Popen([str(exe)], cwd=str(exe.parent), env=env, close_fds=True,
                     creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP)


def cleanup_old():
    """Delete the previous version left behind by an update (it was still running back then)."""
    if not can_install():
        return
    exe = Path(sys.executable)
    for leftover in [*exe.parent.glob(exe.stem + ".old*.exe"), exe.with_name(exe.stem + ".new.exe")]:
        try:
            leftover.unlink()
        except OSError:
            pass


def _no_write_access(exe):
    return (f"Нет прав на запись в папку {exe.parent}. "
            "Переместите программу, например, в «Документы» и обновите ещё раз.")
