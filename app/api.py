"""Python side of the UI: methods here are callable from JavaScript as `pywebview.api.<name>()`.

pywebview runs every call on its own thread, so slow network work is fine here.
Errors are returned as {"error": "..."} so the page can show them inline.
"""

import json
import os
import re
import subprocess
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import webview

from . import config, kindle, updater
from .flibusta import Flibusta, FlibustaError
from .kindle import KindleError
from .updater import UpdateError
from .version import VERSION

# Flibusta appends these to every annotation; the UI doesn't need them.
_META_LINE = re.compile(r"^(Год издания|Формат|Язык|Размер|Скачиваний):")


_PLACEHOLDER = "Введите сюда краткую аннотацию"


def _clean_annotation(text):
    text = "\n".join(l for l in text.splitlines() if not _META_LINE.match(l.strip())).strip()
    return "" if text.startswith(_PLACEHOLDER) else text


def _email_problem(cfg):
    """What's wrong with the e-mail settings, in words the user can act on (None if nothing)."""
    sender = cfg["sender_email"].lower()
    if not cfg["kindle_email"]:
        return "Укажите адрес Kindle (…@kindle.com)."
    if not sender:
        return "Укажите свою почту (Gmail, Яндекс, Mail.ru) — с неё книги отправляются на Kindle."
    if sender.endswith("kindle.com"):
        return ("В поле «Ваша почта» указан адрес Kindle. Нужна ваша обычная почта (Gmail, Яндекс, Mail.ru): "
                "приложение входит в неё и отправляет книгу на адрес Kindle.")
    if not cfg["sender_password"]:
        return "Укажите пароль приложения для своей почты."
    if not cfg["smtp_host"] or not cfg["smtp_port"]:
        domain = sender.rsplit("@", 1)[-1]
        return f"Не знаю почтовый сервер для @{domain}. Укажите SMTP-сервер и порт в разделе «Дополнительно»."
    return None


def _normalize(text):
    return text.lower().replace("ё", "е")


def _words(query):
    return [w for w in re.findall(r"\w+", _normalize(query)) if len(w) >= 2]


def _stem(word):
    """Crude Russian stem: drop a two-letter ending from longer words ("собачье" -> "собач")."""
    return word[:-2] if len(word) > 5 and word.isalpha() else word


def _leading_number(text):
    m = re.match(r"\d+", text or "")
    return int(m.group()) if m else 0


def _meta_number(text, pattern):
    m = re.search(pattern, text)
    return int(re.sub(r"\s", "", m.group(1))) if m else None


def _format_size(kb):
    """5846 -> "5,7 МБ"; small files stay in kilobytes."""
    if kb < 100:
        return f"{kb} КБ"
    return f"{kb / 1024:.1f}".replace(".", ",") + " МБ"


def _format_downloads(n):
    if n >= 1000:
        return f"{round(n / 1000)} тыс. скачиваний"
    if n % 10 == 1 and n % 100 != 11:
        word = "скачивание"
    elif 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        word = "скачивания"
    else:
        word = "скачиваний"
    return f"{n} {word}"


def _guard(fn):
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (FlibustaError, KindleError) as e:
            return {"error": str(e)}
        except Exception as e:  # noqa: BLE001 - shown to the user, details go to the log
            _log_error(e)
            return {"error": f"Непредвиденная ошибка: {e}"}
    wrapper.__name__ = fn.__name__
    return wrapper


class Api:
    def __init__(self):
        self._window = None
        self._cfg = config.load()
        self._client = None
        self._books = {}
        self._send_lock = threading.Lock()
        self._update = None  # the newer release found by check_update()

    # --- helpers (underscore names are not exposed to JS) --------------------

    def _flibusta(self):
        if self._client is None:
            self._client = Flibusta(self._cfg["base_url"], self._cfg["proxy"])
        return self._client

    def _js(self, fn, *args):
        if self._window:
            self._window.evaluate_js(f"{fn}({', '.join(json.dumps(a, ensure_ascii=False) for a in args)})")

    def _books_json(self, books):
        out = []
        for b in books:
            # EPUB is the one format that works everywhere, Kindle e-mail included.
            if "epub" not in b.formats:
                continue
            self._books[b.id] = b
            size_kb = _meta_number(b.annotation, r"Размер:\s*([\d\s]+)\s*Kb")
            downloads = _meta_number(b.annotation, r"Скачиваний:\s*([\d\s]+)")
            out.append({
                "id": b.id,
                "title": b.title,
                "authors": b.author_text,
                "year": b.year,
                "annotation": _clean_annotation(b.annotation),
                "genres": b.genres[:3],
                "downloads": downloads or 0,
                "facts": " · ".join(x for x in (
                    b.language.upper(),
                    _format_size(size_kb) if size_kb else "",
                    _format_downloads(downloads) if downloads is not None else "",
                ) if x),
            })
        return out

    @staticmethod
    def _authors_json(authors):
        return [{"id": a.id, "name": a.name, "count": a.count} for a in authors]

    # --- browsing ------------------------------------------------------------

    @_guard
    def search(self, query):
        query = query.strip()
        client = self._flibusta()
        words = _words(query)
        ex = ThreadPoolExecutor(4)
        try:
            books_f = ex.submit(client.search_books, query)
            authors_f = ex.submit(client.search_authors, query)
            site_f = ex.submit(client.search_authors_site, query)
            # Started right away so "глуховский метро" doesn't wait for the plain search to come back empty.
            mixed_f = ex.submit(self._author_and_title_search, words) if len(words) >= 2 else None
            books, authors = books_f.result(), authors_f.result()
            try:
                site_authors = site_f.result()
            except FlibustaError:
                site_authors = []  # OPDS results alone are still useful
            # OPDS matches the query as one phrase; the website also finds "имя фамилия" in any order.
            seen = {a.id for a in authors.items}
            merged = authors.items + [a for a in site_authors if a.id not in seen]
            found_books, books_next = books.items, books.next_url

            if not found_books and mixed_f:
                mixed_authors, found_books = mixed_f.result()
                seen = {a.id for a in mixed_authors}
                merged = mixed_authors + [a for a in merged if a.id not in seen]
                books_next = None
        finally:
            # Don't wait for the author+title search when the plain search already found books.
            ex.shutdown(wait=False, cancel_futures=True)

        return {
            "books": self._books_json(found_books),
            "booksNext": books_next,
            "authors": self._authors_json(merged),
            "authorsNext": authors.next_url,
        }

    def _author_and_title_search(self, words):
        """Neither OPDS nor the website understands "глуховский метро". Try each word as an author's
        name and look for the remaining words in that author's titles."""
        client = self._flibusta()

        def authors_for(word):
            # Stems let "стругацкие" find "Стругацкий" and "булгакова" find "Булгаков".
            stem = _stem(word)
            try:
                found = client.search_authors_site(stem)
            except FlibustaError:
                return []
            found = [a for a in found if stem in _normalize(a.name)]
            # The best-known authors first: "глуховский" means Dmitry, not a namesake with one book.
            return sorted(found, key=lambda a: -_leading_number(a.count))[:2]

        def matching_books(author, rest):
            # The website's author page lists every book in one request; paging OPDS takes minutes.
            try:
                books = client.author_books_site(author.id, author.name)
            except FlibustaError:
                return author, []
            return author, [b for b in books if all(w in _normalize(b.title) for w in rest)]

        name_words = [w for w in words if len(w) >= 3]
        with ThreadPoolExecutor(max(len(name_words), 1)) as ex:
            candidates = [(a, [_stem(x) for x in words if x != w])
                          for w, found in zip(name_words, ex.map(authors_for, name_words)) for a in found][:4]
        if not candidates:
            return [], []
        with ThreadPoolExecutor(len(candidates)) as ex:
            results = list(ex.map(lambda c: matching_books(*c), candidates))

        authors, books, seen = [], [], set()
        for author, matches in results:
            if matches:
                authors.append(author)
                for b in matches:
                    if b.id not in seen:
                        seen.add(b.id)
                        books.append(b)
        return authors, books

    @_guard
    def author_books(self, author_id):
        page = self._flibusta().author_books(author_id)
        return {"books": self._books_json(page.items), "next": page.next_url}

    @_guard
    def more_books(self, next_url):
        page = self._flibusta().next_page(next_url, "books")
        return {"books": self._books_json(page.items), "next": page.next_url}

    @_guard
    def more_authors(self, next_url):
        page = self._flibusta().next_page(next_url, "authors")
        return {"authors": self._authors_json(page.items), "next": page.next_url}

    # --- delivery ------------------------------------------------------------

    @_guard
    def send(self, book_id):
        book = self._books.get(book_id)
        if not book:
            return {"error": "Книга не найдена. Повторите поиск."}

        def status(text, pct=None):
            self._js("onProgress", book_id, text, pct)

        if not self._send_lock.acquire(blocking=False):
            status("В очереди…")
            self._send_lock.acquire()
        try:
            status("Ищу Kindle…")
            if kindle.kindle_connected():
                return self._send_usb(book, status)
            if config.email_configured(self._cfg):
                return self._send_email(book, status)
            if self._cfg["kindle_email"]:
                return {"error": _email_problem(self._cfg), "setup": True}
            return {"error": "Подключите Kindle кабелем или укажите почту в настройках.", "setup": True}
        finally:
            self._send_lock.release()

    def _download(self, book, fmt, status):
        def progress(done, total):
            status("Скачиваю…", round(done * 100 / total) if total else None)
        status("Скачиваю…")
        return self._flibusta().download(book, fmt, self._cfg["download_dir"], progress=progress)

    def _send_usb(self, book, status):
        calibre = kindle.find_calibre(self._cfg["calibre_path"])
        if calibre:
            path = self._download(book, "epub", status)
            status("Готовлю для Kindle…")
            path = kindle.convert(calibre, path, "azw3")
        elif "mobi" in book.formats:
            path = self._download(book, "mobi", status)
        else:
            return {"error": "Для записи этой книги по кабелю нужен Calibre (calibre-ebook.com)."}
        status("Записываю на Kindle…")
        if kindle.copy_to_kindle(path):
            return {"ok": "Записано на Kindle. Отключите кабель — книга появится в библиотеке."}
        return {"ok": "Эта книга уже есть на Kindle."}

    def _send_email(self, book, status):
        path = self._download(book, "epub", status)
        to_send = path
        calibre = kindle.find_calibre(self._cfg["calibre_path"])
        if calibre:
            status("Готовлю для Kindle…")
            try:
                to_send = kindle.convert(calibre, path, "epub")
            except KindleError:
                pass  # Flibusta's own EPUB usually works too
        status("Отправляю…")
        try:
            kindle.send_email(self._cfg, to_send, title=book.title)
        finally:
            if to_send != path:
                to_send.unlink(missing_ok=True)
        return {"ok": "Отправлено. Книга появится на Kindle через пару минут."}

    # --- downloading -----------------------------------------------------------

    @_guard
    def download(self, book_id):
        book = self._books.get(book_id)
        if not book:
            return {"error": "Книга не найдена. Повторите поиск."}
        path = self._download(book, "epub", lambda text, pct=None: self._js("onProgress", book_id, text, pct))
        path = _readable_name(path, book)
        return {"ok": f"Сохранено: {path.name}", "path": str(path)}

    def reveal(self, path):
        """Open Explorer with the file selected (or the download folder if no file is given)."""
        if path and os.path.exists(path):
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        else:
            folder = Path(self._cfg["download_dir"])
            folder.mkdir(parents=True, exist_ok=True)
            os.startfile(folder)

    def choose_folder(self, current=""):
        result = self._window.create_file_dialog(webview.FileDialog.FOLDER, directory=current or "")
        return result[0] if result else None

    # --- updates -------------------------------------------------------------

    @_guard
    def check_update(self):
        try:
            self._update = updater.check(self._cfg["proxy"])
        except UpdateError as e:
            return {"error": str(e)}
        if not self._update:
            return {"available": False, "current": VERSION}
        return {"available": True, "current": VERSION,
                "version": self._update["version"], "notes": self._update["notes"]}

    @_guard
    def install_update(self):
        info = self._update
        if not info:
            return {"error": "Обновление не найдено. Проверьте ещё раз."}

        def progress(done, total):
            self._js("onUpdateProgress", round(done * 100 / total) if total else None)

        try:
            updater.install(info, self._cfg["proxy"], progress=progress)
        except UpdateError as e:
            return {"error": str(e)}
        # The new version is starting; close this one.
        threading.Timer(0.8, self._window.destroy).start()
        return {"ok": True}

    # --- settings ------------------------------------------------------------

    def get_settings(self):
        keys = ("kindle_email", "sender_email", "sender_password", "smtp_host", "smtp_port", "base_url", "proxy",
                "download_dir")
        s = {k: self._cfg[k] for k in keys}
        s["smtp_port"] = s["smtp_port"] or ""
        s["first_run"] = not config.CONFIG_FILE.exists()
        s["mirrors"] = config.MIRRORS
        s["tor_proxy"] = config.TOR_BROWSER_PROXY
        s["version"] = VERSION
        return s

    def _normalize(self, s):
        cfg = dict(self._cfg)
        for k in ("kindle_email", "sender_email", "smtp_host", "base_url", "proxy", "download_dir"):
            cfg[k] = str(s.get(k, "")).strip()
        cfg["download_dir"] = cfg["download_dir"] or config.DEFAULTS["download_dir"]
        cfg["sender_password"] = str(s.get("sender_password", "")).replace(" ", "")
        cfg["base_url"] = cfg["base_url"].rstrip("/") or config.DEFAULTS["base_url"]
        try:
            cfg["smtp_port"] = int(s.get("smtp_port") or 0)
        except ValueError:
            cfg["smtp_port"] = 0
        if cfg["sender_email"] and not cfg["smtp_host"]:
            preset = kindle.smtp_preset(cfg["sender_email"])
            if preset:
                cfg["smtp_host"], cfg["smtp_port"] = preset
        return cfg

    def save_settings(self, s):
        self._cfg = self._normalize(s)
        config.save(self._cfg)
        self._client = None
        return self.get_settings()

    def check_settings(self, s):
        """Check site access and (if filled in) e-mail login with the unsaved values."""
        cfg = self._normalize(s)
        result = {}
        try:
            Flibusta(cfg["base_url"], cfg["proxy"]).check()
            result["site"] = {"ok": "Сайт открывается"}
        except FlibustaError as e:
            result["site"] = {"error": str(e)}
        if any(cfg[k] for k in ("kindle_email", "sender_email", "sender_password")):
            problem = _email_problem(cfg)
            if problem:
                result["mail"] = {"error": problem}
            else:
                try:
                    kindle.test_email(cfg)
                    result["mail"] = {"ok": "Почта работает"}
                except KindleError as e:
                    result["mail"] = {"error": str(e)}
        return result


def _readable_name(path, book):
    """"Karter_Sbornik.680251.epub" -> "Картер Ник - Сборник.epub"."""
    m = re.search(rf"\.{book.id}\.(.+)$", path.name)
    ext = m.group(1) if m else path.suffix.lstrip(".")
    author = book.authors[0] if book.authors else ""
    stem = f"{author} - {book.title}" if author else book.title
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", stem)[:150].strip(" .,;") or book.id
    target = path.with_name(f"{stem}.{ext}")
    n = 2
    # Same name and size is the same book downloaded again: just replace it.
    while target.exists() and target != path and target.stat().st_size != path.stat().st_size:
        target = path.with_name(f"{stem} ({n}).{ext}")
        n += 1
    try:
        path.replace(target)
    except OSError:
        return path
    return target


def _log_error(err):
    try:
        config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(config.CONFIG_DIR / "error.log", "a", encoding="utf-8") as f:
            f.write(f"\n--- {datetime.now():%Y-%m-%d %H:%M:%S}\n")
            f.write("".join(traceback.format_exception(err)))
    except OSError:
        pass
