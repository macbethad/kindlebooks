"""Minimal Flibusta client built on the site's OPDS (Atom) catalog."""

import html
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urljoin, unquote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

NS = {
    "a": "http://www.w3.org/2005/Atom",
    "dc": "http://purl.org/dc/terms/",
}
ACQUISITION = "http://opds-spec.org/acquisition"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) FlibustaKindle/1.0"


class FlibustaError(Exception):
    pass


@dataclass
class Book:
    id: str
    title: str
    authors: list
    year: str = ""
    language: str = ""
    genres: list = field(default_factory=list)
    annotation: str = ""
    formats: dict = field(default_factory=dict)  # "epub" -> absolute-path URL ("/b/123/epub")

    @property
    def author_text(self):
        return ", ".join(self.authors)


@dataclass
class Author:
    id: str
    name: str
    count: str


@dataclass
class Page:
    items: list
    next_url: str | None


def normalize_proxy(proxy):
    proxy = (proxy or "").strip()
    if not proxy:
        return None
    if "://" not in proxy:
        proxy = "http://" + proxy
    # socks5h resolves host names through the proxy, which .onion addresses need.
    if proxy.startswith("socks5://"):
        proxy = "socks5h://" + proxy[len("socks5://"):]
    return proxy


def _text(el, path):
    found = el.find(path, NS)
    return (found.text or "").strip() if found is not None else ""


def _strip_html(s):
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s).strip()


def _surname_first(name):
    """The website writes "Дмитрий Глуховский"; OPDS (and the rest of the app) "Глуховский Дмитрий"."""
    words = name.split()
    if 2 <= len(words) <= 3 and "(" not in name:
        return " ".join([words[-1], *words[:-1]])
    return name


def _format_key(href, mime):
    tail = href.rstrip("/").rsplit("/", 1)[-1]
    if tail != "download":
        return tail
    # "/b/123/download" serves the original file; the MIME type names its format.
    sub = mime.split("/", 1)[-1]
    return {"x-mobipocket-ebook": "mobi"}.get(sub, sub)


def _parse_book(entry):
    formats = {}
    book_id = ""
    for link in entry.findall("a:link", NS):
        rel = link.get("rel", "")
        href = link.get("href", "")
        if rel.startswith(ACQUISITION):
            formats.setdefault(_format_key(href, link.get("type", "")), href)
        m = re.match(r"/b/(\d+)", href)
        if m and not book_id:
            book_id = m.group(1)
    if not book_id:
        return None
    return Book(
        id=book_id,
        title=re.sub(r"\s+", " ", _text(entry, "a:title")),
        authors=[_text(a, "a:name") for a in entry.findall("a:author", NS)],
        year=_text(entry, "dc:issued"),
        language=_text(entry, "dc:language"),
        genres=[c.get("label") or c.get("term", "") for c in entry.findall("a:category", NS)],
        annotation=_strip_html(_text(entry, "a:content")),
        formats=formats,
    )


def _parse_author(entry):
    m = re.search(r"tag:author:(\d+)", _text(entry, "a:id"))
    if not m:
        return None
    return Author(id=m.group(1), name=_text(entry, "a:title"), count=_text(entry, "a:content"))


class Flibusta:
    def __init__(self, base_url, proxy=None):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        # The site and its book converter often return a transient 502/503.
        retry = Retry(total=4, backoff_factor=2, status_forcelist=(500, 502, 503, 504),
                      allowed_methods=("GET",), raise_on_status=False)
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        proxy = normalize_proxy(proxy)
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}

    def _url(self, path):
        return urljoin(self.base_url + "/", path.lstrip("/"))

    def _get(self, path, **kw):
        kw.setdefault("timeout", (30, 120))
        try:
            r = self.session.get(self._url(path), **kw)
            if r.status_code >= 500:
                raise FlibustaError(f"Сайт временно не отвечает (ошибка {r.status_code}). Попробуйте через минуту.")
            r.raise_for_status()
        except requests.RequestException as e:
            raise FlibustaError(f"Не удалось связаться с сайтом: {e}") from e
        return r

    def _feed(self, path, parse):
        r = self._get(path)
        try:
            root = ET.fromstring(r.content)
        except ET.ParseError as e:
            raise FlibustaError("Сайт вернул что-то непонятное (возможно, страницу блокировки).") from e
        items = [x for x in map(parse, root.findall("a:entry", NS)) if x]
        next_url = None
        for link in root.findall("a:link", NS):
            if link.get("rel") == "next":
                next_url = link.get("href")
        return Page(items, next_url)

    def check(self):
        self._feed("/opds", lambda e: e)

    def search_books(self, query):
        return self._feed(f"/opds/search?searchType=books&searchTerm={quote(query)}", _parse_book)

    def search_authors(self, query):
        return self._feed(f"/opds/search?searchType=authors&searchTerm={quote(query)}", _parse_author)

    def search_authors_site(self, query):
        """Author search via the website, which (unlike OPDS) matches several words in any order:
        "глуховский дмитрий" finds nothing in OPDS but finds the author here."""
        r = self._get(f"/booksearch?ask={quote(query)}&cha=on")
        section = re.search(r"Найденные писатели.*?<ul>(.*?)</ul>", r.text, re.S)
        if not section:
            return []
        return [
            Author(id=m.group(1), name=_surname_first(_strip_html(m.group(2))), count=(m.group(3) or "").strip())
            for m in re.finditer(r'<li><a href="/a/(\d+)">(.*?)</a>\s*(?:\(([^)]*)\))?', section.group(1))
        ]

    def author_books_site(self, author_id, author_name=""):
        """All of an author's books from the website's author page: one fast request, unlike paging
        through OPDS. It has no genres, year or language, only title, size and formats."""
        text = self._get(f"/a/{author_id}").text
        books = {}
        for m in re.finditer(r'<a href="/b/(\d+)">([^<]+)</a>\s*<span style=size>(\d+)K[^<]*</span>(.*?)<br', text, re.S):
            book_id = m.group(1)
            if book_id in books:
                continue
            formats = {f: f"/b/{book_id}/{f}" for f in re.findall(rf'href="/b/{book_id}/(\w+)">\(', m.group(4))
                       if f != "read"}
            books[book_id] = Book(
                id=book_id,
                title=html.unescape(m.group(2)).strip(),
                authors=[author_name] if author_name else [],
                annotation=f"Размер: {m.group(3)} Kb",  # same shape as the OPDS annotation line
                formats=formats,
            )
        return list(books.values())

    def author_books(self, author_id, page=0):
        """One page (20 books) of an author's books, alphabetically."""
        suffix = f"/{page}" if page else ""
        return self._feed(f"/opds/author/{author_id}/alphabet{suffix}", _parse_book)

    def next_page(self, next_url, kind):
        return self._feed(next_url, _parse_book if kind == "books" else _parse_author)

    def download(self, book, fmt, dest_dir, progress=None):
        """Download `book` in `fmt` into `dest_dir`; returns the file path.

        `progress(done_bytes, total_bytes_or_0)` is called while downloading.
        """
        href = book.formats.get(fmt)
        if not href:
            raise FlibustaError(f"Книга недоступна в формате {fmt}.")
        # The converter can take a while for big books before the first byte arrives.
        r = self._get(href, stream=True, timeout=(30, 300))
        if "text/html" in r.headers.get("Content-Type", ""):
            raise FlibustaError("Вместо файла сайт вернул веб-страницу. Попробуйте позже или другой формат.")

        name = _filename_from_disposition(r.headers.get("Content-Disposition", "")) or f"{book.id}.{fmt}"
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / name
        tmp = path.with_name(path.name + ".part")
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        try:
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(64 * 1024):
                    f.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
        except requests.RequestException as e:
            tmp.unlink(missing_ok=True)
            raise FlibustaError(f"Загрузка прервалась: {e}") from e
        tmp.replace(path)
        return path


def _filename_from_disposition(cd):
    m = re.search(r"filename\*=(?:UTF-8'')?([^;]+)", cd, re.I)
    if m:
        name = unquote(m.group(1).strip().strip('"'))
    else:
        m = re.search(r'filename="?([^";]+)"?', cd, re.I)
        if not m:
            return None
        name = m.group(1).strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    # "Author_Title.680251.fb2.epub" -> "Author_Title.680251.epub"
    return re.sub(r"\.fb2\.(epub|mobi|azw3)$", r".\1", name, flags=re.I) or None
