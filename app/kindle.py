"""Getting books onto a Kindle: Calibre conversion, Send-to-Kindle e-mail, USB copy."""

import ctypes
import os
import shutil
import smtplib
import ssl
import string
import subprocess
import tempfile
from email.message import EmailMessage
from pathlib import Path

CREATE_NO_WINDOW = 0x08000000

# Amazon accepts up to 50 MB, but most mail providers cap attachments at ~25 MB.
EMAIL_SIZE_LIMIT = 25 * 1024 * 1024

SMTP_PRESETS = {
    "gmail.com": ("smtp.gmail.com", 587),
    "googlemail.com": ("smtp.gmail.com", 587),
    "yandex.ru": ("smtp.yandex.ru", 465),
    "ya.ru": ("smtp.yandex.ru", 465),
    "yandex.com": ("smtp.yandex.ru", 465),
    "mail.ru": ("smtp.mail.ru", 465),
    "bk.ru": ("smtp.mail.ru", 465),
    "inbox.ru": ("smtp.mail.ru", 465),
    "list.ru": ("smtp.mail.ru", 465),
    "internet.ru": ("smtp.mail.ru", 465),
    "rambler.ru": ("smtp.rambler.ru", 465),
    "outlook.com": ("smtp-mail.outlook.com", 587),
    "hotmail.com": ("smtp-mail.outlook.com", 587),
    "live.com": ("smtp-mail.outlook.com", 587),
    "icloud.com": ("smtp.mail.me.com", 587),
    "me.com": ("smtp.mail.me.com", 587),
}


class KindleError(Exception):
    pass


# --- Calibre ---------------------------------------------------------------

def find_calibre(configured=""):
    candidates = [configured, shutil.which("ebook-convert") or ""]
    for base in (os.environ.get("ProgramFiles", ""), os.environ.get("ProgramFiles(x86)", "")):
        if base:
            candidates.append(os.path.join(base, "Calibre2", "ebook-convert.exe"))
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


def convert(calibre, src, ext):
    """Convert `src` to `ext` with Calibre; returns the new file path."""
    src = Path(src)
    dst = src.with_suffix(f".{ext}")
    if dst == src:
        dst = src.with_name(f"{src.stem}.kindle.{ext}")
    try:
        r = subprocess.run(
            [calibre, str(src), str(dst)],
            capture_output=True, timeout=900, creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise KindleError(f"Calibre не смог конвертировать книгу: {e}") from e
    if r.returncode != 0 or not dst.exists():
        tail = r.stderr.decode("utf-8", "replace").strip().splitlines()[-3:]
        raise KindleError("Calibre не смог конвертировать книгу.\n" + "\n".join(tail))
    return dst


# --- E-mail ----------------------------------------------------------------

def smtp_preset(sender_email):
    domain = sender_email.rsplit("@", 1)[-1].lower().strip()
    return SMTP_PRESETS.get(domain)


def _smtp_connect(cfg):
    host, port = cfg["smtp_host"], int(cfg["smtp_port"])
    ctx = ssl.create_default_context()
    try:
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, context=ctx, timeout=60)
        else:
            server = smtplib.SMTP(host, port, timeout=60)
            server.starttls(context=ctx)
        server.login(cfg["sender_email"], cfg["sender_password"])
    except smtplib.SMTPAuthenticationError as e:
        raise KindleError(
            "Почта не приняла логин или пароль.\n"
            "Для Gmail, Яндекса и Mail.ru нужен специальный «пароль приложения», "
            "а не обычный пароль от почты."
        ) from e
    except (OSError, smtplib.SMTPException) as e:
        raise KindleError(f"Не удалось подключиться к почтовому серверу {host}:{port}: {e}") from e
    return server


def test_email(cfg):
    _smtp_connect(cfg).quit()


def send_email(cfg, path, title=""):
    path = Path(path)
    if path.stat().st_size > EMAIL_SIZE_LIMIT:
        raise KindleError("Файл больше 25 МБ — почта его не пропустит. Используйте кабель.")
    msg = EmailMessage()
    msg["From"] = cfg["sender_email"]
    msg["To"] = cfg["kindle_email"]
    msg["Subject"] = title or path.stem
    msg.set_content("Книга во вложении.")
    subtype = {".epub": "epub+zip", ".pdf": "pdf"}.get(path.suffix.lower(), "octet-stream")
    msg.add_attachment(path.read_bytes(), maintype="application", subtype=subtype, filename=path.name)
    server = _smtp_connect(cfg)
    try:
        server.send_message(msg)
    except smtplib.SMTPException as e:
        raise KindleError(f"Почта не отправилась: {e}") from e
    finally:
        try:
            server.quit()
        except smtplib.SMTPException:
            pass


# --- USB -------------------------------------------------------------------

DRIVE_REMOVABLE = 2


def _volume_label(root):
    buf = ctypes.create_unicode_buffer(261)
    ok = ctypes.windll.kernel32.GetVolumeInformationW(root, buf, len(buf), None, None, None, None, 0)
    return buf.value if ok else ""


def find_kindle_drive():
    """Older Kindles show up as a USB drive with a `documents` folder."""
    mask = ctypes.windll.kernel32.GetLogicalDrives()
    for i, letter in enumerate(string.ascii_uppercase):
        if not mask & (1 << i):
            continue
        root = f"{letter}:\\"
        if ctypes.windll.kernel32.GetDriveTypeW(root) != DRIVE_REMOVABLE:
            continue
        docs = os.path.join(root, "documents")
        if os.path.isdir(docs) and (
            "kindle" in _volume_label(root).lower() or os.path.isdir(os.path.join(root, "system"))
        ):
            return docs
    return None


# Newer Kindles (2021+) connect over MTP and have no drive letter; the Windows
# Shell is the only built-in way to copy to them.
_MTP_SCRIPT = r"""
param([string]$Src = "", [switch]$Detect)
$shell = New-Object -ComObject Shell.Application
$kindle = $shell.NameSpace(17).Items() | Where-Object { $_.Name -like '*Kindle*' } | Select-Object -First 1
if (-not $kindle) { 'NOKINDLE'; exit }
$docs = $null
$roots = @($kindle) + @($kindle.GetFolder.Items() | Where-Object { $_.IsFolder })
foreach ($r in $roots) {
    $d = $r.GetFolder.Items() | Where-Object { $_.Name -eq 'documents' } | Select-Object -First 1
    if ($d) { $docs = $d.GetFolder; break }
}
if (-not $docs) { 'NODOCS'; exit }
if ($Detect) { 'OK'; exit }

$name = [IO.Path]::GetFileName($Src)
$bare = [IO.Path]::GetFileNameWithoutExtension($Src)
$size = (Get-Item -LiteralPath $Src).Length
# Explorer may hide extensions, so match the name with or without one.
function Find-Copy { $docs.Items() | Where-Object { $_.Name -eq $name -or $_.Name -eq $bare } | Select-Object -First 1 }
if (Find-Copy) { 'EXISTS'; exit }
$docs.CopyHere($Src, 4 + 16)
# CopyHere is asynchronous and the copy dies with this process, so wait for the
# file to show up, then give the transfer generous extra time to finish.
$seen = 0
for ($i = 0; $i -lt 1200; $i++) {
    Start-Sleep -Milliseconds 500
    $item = Find-Copy
    if (-not $item) { continue }
    $seen++
    # Some devices never report a size, so fall back to "visible for 10 s".
    if ($item.Size -ge $size -or $seen -ge 20) {
        Start-Sleep -Seconds (3 + [math]::Ceiling($size / 1MB))
        'OK'; exit
    }
}
'TIMEOUT'
"""


def _run_mtp(*args):
    fd, script = tempfile.mkstemp(suffix=".ps1")
    try:
        with os.fdopen(fd, "w", encoding="utf-8-sig") as f:
            f.write(_MTP_SCRIPT)
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", script, *args],
            capture_output=True, timeout=700, creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise KindleError(f"Не удалось обратиться к Kindle: {e}") from e
    finally:
        os.unlink(script)
    out = r.stdout.decode("utf-8", "replace").strip().splitlines()
    return out[-1].strip() if out else "ERROR"


def kindle_connected():
    """Returns "drive", "mtp" or None."""
    if find_kindle_drive():
        return "drive"
    if _run_mtp("-Detect") == "OK":
        return "mtp"
    return None


def copy_to_kindle(path):
    """Copy a file into the Kindle's documents folder. Returns True if copied, False if it was already there."""
    path = Path(path)
    docs = find_kindle_drive()
    if docs:
        dst = Path(docs) / path.name
        if dst.exists() and dst.stat().st_size == path.stat().st_size:
            return False
        shutil.copyfile(path, dst)
        return True

    status = _run_mtp("-Src", str(path))
    if status == "OK":
        return True
    if status == "EXISTS":
        return False
    raise KindleError({
        "NOKINDLE": "Kindle не найден. Подключите его кабелем к компьютеру и разблокируйте экран.",
        "NODOCS": "Kindle найден, но папка documents не видна. Разблокируйте экран Kindle и попробуйте ещё раз.",
        "TIMEOUT": "Копирование на Kindle заняло слишком много времени. Проверьте, появилась ли книга.",
    }.get(status, f"Не удалось скопировать книгу на Kindle ({status})."))
