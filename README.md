# KindleBooks

A minimal Windows app for Flibusta: search books and authors, download them, or send them to a Kindle.
The interface is in Russian and always uses a light theme.

**Search:** one box finds books and authors. Tabs switch between «Всё», «Книги» and «Авторы»; each book
shows its genres, language, file size and download count. Results can be sorted by relevance, popularity,
year or title (sorting applies to the results loaded so far; «Показать ещё» loads the next 20).

**EPUB only:** results show only books available in EPUB, the format that works everywhere (including
Kindle by e-mail). The ↓ button saves the EPUB with a readable name like
`Булгаков Михаил Афанасьевич - Мастер и Маргарита.epub` to `Documents\Книги Flibusta` (changeable in
settings → «Дополнительно»); the folder button in the top bar opens it.

**How books are delivered:** if a Kindle is plugged in by USB, the book is copied to it. Otherwise it is
e-mailed to the Kindle's `@kindle.com` address. There's no choice to make.

## Give it to someone

Copy `dist\KindleBooks.exe` to their PC and run it. Nothing else needs to be installed: it uses the Edge
WebView2 engine built into Windows 10/11. Windows SmartScreen may warn about an unknown app: click
"More info" → "Run anyway".

On first launch the settings panel opens. Settings are saved to `%APPDATA%\FlibustaKindle\config.json`
(the mail app password is stored there in plain text).

## One-time setup (⚙ in the top-right corner)

**E-mail delivery.** You can skip this if the Kindle will always be connected by cable.

1. Amazon → *Manage Your Content and Devices* → *Preferences* → *Personal Document Settings*:
   copy the Kindle's address (`…@kindle.com`) and add the sender address to the *Approved Personal Document E-mail List*.
2. Create an **app password** for the sender mailbox. The normal password won't work:
   - Gmail: <https://myaccount.google.com/apppasswords> (2-step verification must be on)
   - Yandex: id.yandex.ru → Безопасность → Пароли приложений
   - Mail.ru: Настройки → Безопасность → Пароли для внешних приложений
3. Enter the Kindle address, the sender address and the app password. The SMTP server is detected
   automatically for common providers (override it under «Дополнительно»).

Books are sent as EPUB, which Amazon converts. The Kindle needs Wi-Fi to receive them. Attachment limit: 25 MB.

**Site access (if flibusta.is is blocked)**, under «Дополнительно». Pick one:

- Turn on a VPN and leave the proxy empty.
- Install [Tor Browser](https://www.torproject.org/), start it and let it connect (in Russia, use the
  built-in "Connection Assist" / bridges). Then click «Через Tor Browser». This switches to the `.onion`
  address through `socks5h://127.0.0.1:9150`. Tor Browser must be running while the app is in use.
- Any other HTTP or SOCKS5 proxy in the «Прокси» field.

«Проверить» tests site access and the mail login.

**Cable delivery**: plug the Kindle in and unlock its screen. Both old Kindles (which appear as a drive
letter) and new ones (MTP, no drive letter) are supported.

**Calibre (optional)**: if [Calibre](https://calibre-ebook.com/) is installed, books for the cable are
converted to AZW3 (nicer than Flibusta's MOBI). EPUBs are also re-packed before e-mailing, which avoids
Amazon occasionally rejecting them. It's detected automatically.

Books sent to the Kindle are also kept in the download folder.

## Updates

The app checks GitHub Releases (`UPDATE_REPO` in `app/version.py`) on every start. When a newer version
exists it shows «Вышла новая версия — Обновить»; one click downloads the new exe, swaps it in place of the
running one and restarts. Settings are kept. The version and «Проверить обновления» are at the bottom of the
settings panel. The exe must live in a folder the user can write to (Documents, Desktop — not Program Files).

To publish an update:

1. Raise `VERSION` in `app/version.py` (e.g. `1.0.0` → `1.1.0`) and commit.
2. Run `.elease.ps1 "Что нового — одной строкой"`: it builds the exe, pushes, and creates release `v1.1.0`
   with `KindleBooks.exe` attached. The note is what the user sees under the banner.

Needs the GitHub CLI (`gh`) logged in to the account that owns the repository.

## Development

```bat
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python main.py      # run
build.bat                         # build dist\KindleBooks.exe
```

- `app/ui/index.html`: the whole UI (HTML/CSS/JS, rendered with pywebview)
- `app/api.py`: Python methods the page calls (`pywebview.api.*`)
- `app/flibusta.py`: OPDS client (search, author pages, downloads, retries on the site's frequent 502/503 errors)
- `app/kindle.py`: Calibre conversion, SMTP sending, USB copying (drive letter or MTP via the Windows Shell)
- `app/config.py`: settings file, mirror list

Errors that aren't expected are logged to `%APPDATA%\FlibustaKindle\error.log`.
