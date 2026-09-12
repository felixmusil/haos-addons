#!/usr/bin/env python3
"""abb_helper — "Add from AudioBookBay" companion for the transmission-nordvpn add-on.

A tiny stdlib-only HTTP service (default 0.0.0.0:8098) that turns a pasted AudioBookBay book
page URL, a bare info hash or a magnet link into a Transmission ``torrent-add`` RPC call.

Every request must carry ``X-Abb-Token`` equal to ``ABB_TOKEN`` (the Transmission RPC
password). For ingress traffic nginx injects that header; HA ``rest_command`` sends it itself.
The served HTML page therefore never embeds the token.

Runs on the image's python3 (3.14) and on 3.10+ test hosts; stdlib only — no pip in the image.
"""

from __future__ import annotations

import base64
import html
import json
import logging
import os
import re
import socket
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser
from http.cookiejar import MozillaCookieJar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("abb_helper")

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

# Used for bare-hash adds (no page to read trackers from) and for pages without a tracker list.
DEFAULT_TRACKERS = (
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "http://tracker.openbittorrent.com:80/announce",
)

HASH_RE = re.compile(r"^[0-9a-fA-F]{40}$")
MAGNET_HASH_RE = re.compile(r"urn:btih:([0-9a-fA-F]{40})")
ABB_TIMEOUT = 25
RPC_TIMEOUT = 10


# --------------------------------------------------------------------------- errors


class HelperError(Exception):
    """Base class; ``status`` is the HTTP status the handler replies with."""

    status = 500


class BadInput(HelperError):
    status = 400


class LoginFailed(HelperError):
    status = 401


class HashNotFound(HelperError):
    status = 404


class AbbUnreachable(HelperError):
    status = 502


class RpcError(HelperError):
    """Transmission answered, but not with a usable result (bad credentials, bad torrent)."""

    status = 502


class TransmissionDown(HelperError):
    status = 503


# --------------------------------------------------------------------------- pure functions


def classify(text: str, base_url: str | None = None) -> str | None:
    """Return "magnet" | "hash" | "url" for a pasted input, or None when it is unusable.

    A URL counts only when its host equals the host of ``base_url`` (default: ABB_BASE_URL);
    anything else would make the helper an open fetch proxy whose egress is the VPN tunnel.
    """
    if text is None:
        return None
    s = text.strip()
    if not s:
        return None
    if s.lower().startswith("magnet:?"):
        return "magnet" if MAGNET_HASH_RE.search(s) else None
    if HASH_RE.match(s):
        return "hash"
    if "://" not in s:
        return None
    try:
        parsed = urllib.parse.urlsplit(s)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    base = base_url if base_url is not None else os.environ.get("ABB_BASE_URL", "https://audiobookbay.lu")
    base_host = (urllib.parse.urlsplit(base).hostname or "").lower()
    if not base_host:
        return None
    return "url" if parsed.hostname.lower() == base_host else None


@dataclass
class BookPage:
    title: str | None = None
    info_hash: str | None = None
    trackers: list[str] = field(default_factory=list)
    magnet: str | None = None


class _BookPageParser(HTMLParser):
    """Collects the text of every <td> in document order, the <h1> inside div.postTitle and
    every href="magnet:…". HTMLParser unescapes entities in data and attributes for us."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.cells: list[str] = []
        self.magnets: list[str] = []
        self.title: str | None = None
        self._cell: list[str] | None = None
        self._h1: list[str] | None = None
        self._div_depth = 0
        self._post_title_depth: int | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "a":
            href = (a.get("href") or "").strip()
            if href.lower().startswith("magnet:"):
                self.magnets.append(href)
        elif tag == "td":
            self._cell = []
        elif tag == "div":
            self._div_depth += 1
            classes = (a.get("class") or "").split()
            if "postTitle" in classes and self._post_title_depth is None:
                self._post_title_depth = self._div_depth
        elif tag == "h1" and self._post_title_depth is not None and self.title is None:
            self._h1 = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "td" and self._cell is not None:
            self.cells.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "h1" and self._h1 is not None:
            self.title = " ".join("".join(self._h1).split())
            self._h1 = None
        elif tag == "div":
            if self._post_title_depth == self._div_depth:
                self._post_title_depth = None
            self._div_depth = max(0, self._div_depth - 1)

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)
        if self._h1 is not None:
            self._h1.append(data)


def parse_book_page(html_text: str) -> BookPage:
    """Parse an ABB book page: the cell after "Info Hash:", the cell after every "Tracker:",
    the <h1> in div.postTitle and any magnet href (member view). Decoy download links
    (/downld0, /dodl3-no, /dedl4-now) are plain anchors and never match."""
    p = _BookPageParser()
    p.feed(html_text)
    p.close()
    page = BookPage(title=p.title or None)
    cells = p.cells
    seen: set[str] = set()
    for i, cell in enumerate(cells[:-1]):
        label = cell.rstrip(":").strip().lower()
        nxt = cells[i + 1].strip()
        if label == "info hash" and page.info_hash is None and HASH_RE.match(nxt):
            page.info_hash = nxt.lower()
        elif label == "tracker" and "://" in nxt and nxt not in seen:
            seen.add(nxt)
            page.trackers.append(nxt)
    if p.magnets:
        page.magnet = p.magnets[0]
        if page.info_hash is None:
            m = MAGNET_HASH_RE.search(page.magnet)
            if m:
                page.info_hash = m.group(1).lower()
    return page


def build_magnet(info_hash: str, title: str | None, trackers: list[str] | tuple[str, ...]) -> str:
    """magnet:?xt=urn:btih:<hash>[&dn=<encoded title>]&tr=<encoded tracker>… — page trackers
    when given (deduplicated, order kept), else the built-in public list."""
    h = info_hash.strip().lower()
    if not HASH_RE.match(h):
        raise BadInput("Not a valid 40-character info hash")
    parts = ["magnet:?xt=urn:btih:" + h]
    if title:
        parts.append("dn=" + urllib.parse.quote(title, safe=""))
    seen: set[str] = set()
    for t in trackers or DEFAULT_TRACKERS:
        if t and t not in seen:
            seen.add(t)
            parts.append("tr=" + urllib.parse.quote(t, safe=""))
    return "&".join(parts)


# --------------------------------------------------------------------------- ABB client


def _path_of(url: str) -> str:
    return urllib.parse.urlsplit(url).path.rstrip("/") or "/"


class AbbClient:
    """Fetches book pages with a browser UA and a persistent MozillaCookieJar.

    With credentials configured it logs in eagerly whenever the jar holds no session cookie for
    the site (anonymous ABB pages do carry the Info Hash but not the member-only magnet link, so
    a lazy login would never fire); a stale session still gets one transparent re-login."""

    def __init__(
        self,
        base_url: str,
        username: str | None,
        password: str | None,
        jar_path: str,
        user_agent: str = DEFAULT_USER_AGENT,
        opener: urllib.request.OpenerDirector | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username or ""
        self.password = password or ""
        self.jar_path = jar_path
        self.user_agent = user_agent or DEFAULT_USER_AGENT
        self.jar = MozillaCookieJar(jar_path)
        if os.path.isfile(jar_path):
            try:
                # ABB's session cookie has no Expires → it is a "discard" cookie that load()
                # would silently skip without ignore_discard=True.
                self.jar.load(ignore_discard=True, ignore_expires=True)
            except Exception as exc:  # corrupt jar → start fresh
                log.warning("cookie jar %s unreadable (%s); starting fresh", jar_path, exc)
        self.opener = opener or urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )
        self._lock = threading.Lock()

    # -- low level

    def _request(self, url: str, data: bytes | None = None, content_type: str | None = None):
        req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
        req.add_header("User-Agent", self.user_agent)
        req.add_header("Accept", "text/html,application/xhtml+xml,*/*;q=0.8")
        req.add_header("Accept-Language", "en-US,en;q=0.9")
        if content_type:
            req.add_header("Content-Type", content_type)
        try:
            resp = self.opener.open(req, timeout=ABB_TIMEOUT)
            body = resp.read().decode("utf-8", errors="replace")
            return resp.geturl(), resp.status, body
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise HashNotFound("AudioBookBay says this page does not exist (HTTP 404)") from exc
            raise AbbUnreachable(f"AudioBookBay answered HTTP {exc.code}") from exc
        except (urllib.error.URLError, socket.timeout, OSError, ValueError) as exc:
            reason = getattr(exc, "reason", exc)
            raise AbbUnreachable(
                f"Could not reach AudioBookBay ({reason}); is the VPN tunnel up?"
            ) from exc

    def _save_jar(self) -> None:
        d = os.path.dirname(self.jar_path)
        if d:
            os.makedirs(d, exist_ok=True)
        self.jar.save(ignore_discard=True, ignore_expires=True)

    @staticmethod
    def _is_login_page(final_url: str, body: str) -> bool:
        path = _path_of(final_url)
        if path == "/member/login":
            return True
        return path == "/member/login.php" and 'name="entryform"' in body

    # -- public

    def login(self) -> None:
        if not self.username:
            raise LoginFailed(
                "AudioBookBay login required: set abb_username / abb_password in the add-on options"
            )
        form = urllib.parse.urlencode({"username": self.username, "password": self.password})
        final_url, _status, body = self._request(
            self.base_url + "/member/login.php",
            data=form.encode("utf-8"),
            content_type="application/x-www-form-urlencoded",
        )
        if self._is_login_page(final_url, body):
            raise LoginFailed("AudioBookBay login failed: check abb_username / abb_password")
        self._save_jar()
        log.info("logged in to AudioBookBay as %s", self.username)

    def fetch_book(self, url: str) -> BookPage:
        with self._lock:
            return self._fetch_book(url)

    def _has_session(self) -> bool:
        host = (urllib.parse.urlparse(self.base_url).hostname or "").lower()
        return any(host.endswith(c.domain.lstrip(".").lower()) for c in self.jar if c.domain)

    def _fetch_book(self, url: str) -> BookPage:
        logged_in = False
        if self.username and not self._has_session():
            self.login()
            logged_in = True
        final_url, _status, body = self._request(url)
        if self._is_login_page(final_url, body):
            self.login()
            logged_in = True
            final_url, _status, body = self._request(url)
            if self._is_login_page(final_url, body):
                raise LoginFailed("AudioBookBay keeps asking for a login")
        page = parse_book_page(body)
        if page.info_hash is None and not logged_in and self.username:
            # Anonymous view without the hash cell (or a stale session) → one re-login.
            self.login()
            final_url, _status, body = self._request(url)
            page = parse_book_page(body)
        if page.info_hash is None:
            raise HashNotFound("No Info Hash found on that page — is it an AudioBookBay book page?")
        return page


# --------------------------------------------------------------------------- Transmission RPC


class TransmissionRpc:
    """Minimal Transmission RPC client with the 409 X-Transmission-Session-Id handshake."""

    def __init__(
        self,
        url: str,
        user: str,
        password: str,
        opener: urllib.request.OpenerDirector | None = None,
    ) -> None:
        self.url = url
        self._auth = "Basic " + base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
        self.opener = opener or urllib.request.build_opener()
        self.session_id: str | None = None
        self._lock = threading.Lock()

    def _post_once(self, payload: bytes):
        req = urllib.request.Request(self.url, data=payload, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", self._auth)
        if self.session_id:
            req.add_header("X-Transmission-Session-Id", self.session_id)
        return self.opener.open(req, timeout=RPC_TIMEOUT)

    def call(self, method: str, arguments: dict) -> dict:
        payload = json.dumps({"method": method, "arguments": arguments}).encode("utf-8")
        with self._lock:
            for attempt in (1, 2):
                try:
                    resp = self._post_once(payload)
                    raw = resp.read()
                    break
                except urllib.error.HTTPError as exc:
                    if exc.code == 409 and attempt == 1:
                        self.session_id = exc.headers.get("X-Transmission-Session-Id")
                        if self.session_id:
                            continue
                    if exc.code == 401:
                        raise RpcError("Transmission rejected the RPC credentials") from exc
                    if exc.code >= 500 or exc.code == 409:
                        raise TransmissionDown(f"Transmission RPC answered HTTP {exc.code}") from exc
                    raise RpcError(f"Transmission RPC answered HTTP {exc.code}") from exc
                except (urllib.error.URLError, socket.timeout, OSError) as exc:
                    raise TransmissionDown(
                        "Transmission is not reachable yet (it starts once the VPN tunnel is up)"
                    ) from exc
            else:  # pragma: no cover — loop always breaks or raises
                raise TransmissionDown("Transmission RPC handshake failed")
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise TransmissionDown("Transmission RPC returned a non-JSON reply") from exc
        if data.get("result") != "success":
            raise RpcError(f"Transmission refused the torrent: {data.get('result', 'unknown error')}")
        args = data.get("arguments")
        return args if isinstance(args, dict) else {}

    def add(self, magnet: str, download_dir: str | None) -> tuple[str, str, str]:
        arguments: dict = {"filename": magnet}
        if download_dir:
            arguments["download-dir"] = download_dir
        args = self.call("torrent-add", arguments)
        for key, outcome in (("torrent-added", "added"), ("torrent-duplicate", "duplicate")):
            t = args.get(key)
            if isinstance(t, dict):
                return outcome, str(t.get("name") or ""), str(t.get("hashString") or "").lower()
        raise RpcError("Transmission returned neither torrent-added nor torrent-duplicate")

    def health(self) -> bool:
        self.call("session-get", {})
        return True


# --------------------------------------------------------------------------- HTTP service

PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Add from AudioBookBay</title>
<style>
  :root { color-scheme: light dark; }
  body { font: 16px/1.4 -apple-system, system-ui, sans-serif; margin: 0; padding: 1rem; max-width: 40rem; }
  h1 { font-size: 1.25rem; margin: 0 0 1rem; }
  label { display: block; margin: .75rem 0 .25rem; font-weight: 600; }
  textarea, input, button { width: 100%; box-sizing: border-box; font: inherit; padding: .6rem; border-radius: .5rem; border: 1px solid #8884; }
  textarea { min-height: 5.5rem; }
  button { margin-top: 1rem; background: #2563eb; color: #fff; border: 0; font-weight: 600; }
  button[disabled] { opacity: .6; }
  #result { margin-top: 1rem; padding: .75rem; border-radius: .5rem; background: #8882; white-space: pre-wrap; word-break: break-all; }
  #result.ok { background: #16a34a33; }
  #result.err { background: #dc262633; }
  nav { margin-top: 1.5rem; }
</style>
</head>
<body>
<h1>Add from AudioBookBay</h1>
<form id="addform">
  <label for="input">Book page URL, info hash, or magnet link</label>
  <textarea id="input" name="input" placeholder="https://audiobookbay.lu/abss/… or 40-hex hash or magnet:?…" autocomplete="off" autocapitalize="off" spellcheck="false"></textarea>
  <label for="download_dir">Destination folder (optional)</label>
  <input id="download_dir" name="download_dir" placeholder="%%DEFAULT_DOWNLOAD_DIR%%" autocomplete="off" autocapitalize="off">
  <button id="addbtn" type="submit">Add to Transmission</button>
</form>
<div id="result" hidden></div>
<nav><a href="../web/">Open Flood (torrent list)</a></nav>
<script>
(function () {
  var form = document.getElementById('addform');
  var btn = document.getElementById('addbtn');
  var out = document.getElementById('result');
  function show(text, cls) { out.hidden = false; out.className = cls; out.textContent = text; }
  form.addEventListener('submit', function (ev) {
    ev.preventDefault();
    var input = document.getElementById('input').value.trim();
    var dir = document.getElementById('download_dir').value.trim();
    if (!input) { show('Paste a book page URL, an info hash or a magnet link first.', 'err'); return; }
    var body = { input: input };
    if (dir) { body.download_dir = dir; }
    btn.disabled = true;
    show('Adding…', '');
    fetch('add', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
      .then(function (r) { return r.json().then(function (j) { return { status: r.status, json: j }; }); })
      .then(function (res) {
        if (res.status >= 200 && res.status < 300) {
          var label = res.json.result === 'duplicate' ? 'Already in Transmission' : 'Added';
          show(label + ': ' + (res.json.name || res.json.hash), 'ok');
          document.getElementById('input').value = '';
        } else {
          show('Error: ' + (res.json.error || ('HTTP ' + res.status)), 'err');
        }
      })
      .catch(function (e) { show('Request failed: ' + e, 'err'); })
      .then(function () { btn.disabled = false; });
  });
})();
</script>
</body>
</html>
"""


@dataclass
class Settings:
    listen: str = "0.0.0.0:8098"
    token: str = ""
    abb_base_url: str = "https://audiobookbay.lu"
    abb_username: str = ""
    abb_password: str = ""
    cookie_jar: str = "/data/abb/cookies.txt"
    user_agent: str = DEFAULT_USER_AGENT
    rpc_url: str = "http://127.0.0.1:9091/transmission/rpc"
    rpc_user: str = "transmission"
    rpc_password: str = ""
    default_download_dir: str = ""

    @classmethod
    def from_env(cls, env=os.environ) -> "Settings":
        s = cls()
        s.listen = env.get("ABB_LISTEN", s.listen)
        s.token = env.get("ABB_TOKEN", "")
        s.abb_base_url = env.get("ABB_BASE_URL", s.abb_base_url) or s.abb_base_url
        s.abb_username = env.get("ABB_USERNAME", "")
        s.abb_password = env.get("ABB_PASSWORD", "")
        s.cookie_jar = env.get("ABB_COOKIE_JAR", s.cookie_jar) or s.cookie_jar
        s.user_agent = env.get("ABB_USER_AGENT", s.user_agent) or s.user_agent
        s.rpc_url = env.get("TR_RPC_URL", s.rpc_url) or s.rpc_url
        s.rpc_user = env.get("TR_RPC_USER", s.rpc_user) or s.rpc_user
        s.rpc_password = env.get("TR_RPC_PASSWORD", s.token)
        s.default_download_dir = env.get("DEFAULT_DOWNLOAD_DIR", "")
        return s


class Service:
    """The work behind the routes, independent of http.server so it can be tested in-process."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.abb = AbbClient(
            settings.abb_base_url,
            settings.abb_username,
            settings.abb_password,
            settings.cookie_jar,
            settings.user_agent,
        )
        self.rpc = TransmissionRpc(settings.rpc_url, settings.rpc_user, settings.rpc_password)

    def resolve(self, text: str) -> tuple[str, str | None, str | None]:
        """Pasted input → (magnet, name, hash) without touching Transmission."""
        kind = classify(text, self.settings.abb_base_url)
        s = (text or "").strip()
        if kind == "magnet":
            m = MAGNET_HASH_RE.search(s)
            return s, None, m.group(1).lower() if m else None
        if kind == "hash":
            h = s.lower()
            return build_magnet(h, None, []), None, h
        if kind == "url":
            page = self.abb.fetch_book(s)
            assert page.info_hash is not None
            magnet = page.magnet or build_magnet(page.info_hash, page.title, page.trackers)
            return magnet, page.title, page.info_hash
        raise BadInput(
            "Paste an AudioBookBay book page URL (host %s), a 40-character info hash or a magnet link"
            % (urllib.parse.urlsplit(self.settings.abb_base_url).hostname or "?")
        )

    def add(self, text: str, download_dir: str | None) -> dict:
        magnet, name, info_hash = self.resolve(text)
        target = (download_dir or "").strip() or self.settings.default_download_dir
        result, rpc_name, rpc_hash = self.rpc.add(magnet, target)
        name = rpc_name or name or ""
        info_hash = rpc_hash or info_hash or ""
        log.info("%s: '%s' hash=%s dir=%s", result, name or "(no name yet)", info_hash, target or "(default)")
        return {"name": name, "hash": info_hash, "magnet": magnet, "result": result}

    def page(self) -> str:
        return PAGE_HTML.replace(
            "%%DEFAULT_DOWNLOAD_DIR%%", html.escape(self.settings.default_download_dir, quote=True)
        )


class Handler(BaseHTTPRequestHandler):
    server_version = "abb-helper/1.0"
    service: Service  # set on the server instance
    protocol_version = "HTTP/1.1"

    # -- helpers

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, obj: dict) -> None:
        self._send(status, json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8")

    def _authorized(self) -> bool:
        token = self.server.service.settings.token  # type: ignore[attr-defined]
        return bool(token) and self.headers.get("X-Abb-Token", "") == token

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length < 0 or length > 1_000_000:
            raise BadInput("Request body too large")
        raw = self.rfile.read(length) if length else b""
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        text = raw.decode("utf-8", errors="replace")
        if ctype == "application/x-www-form-urlencoded":
            q = urllib.parse.parse_qs(text, keep_blank_values=True)
            return {k: v[0] for k, v in q.items()}
        if not text.strip():
            raise BadInput("Empty request body; send JSON {\"input\": …}")
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise BadInput("Request body is not valid JSON") from exc
        if not isinstance(data, dict):
            raise BadInput("JSON body must be an object")
        return data

    def log_message(self, fmt: str, *args) -> None:  # only method + path, never bodies/headers
        log.debug("%s %s", self.command, self.path.split("?", 1)[0])

    # -- routes

    def _route(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if not self._authorized():
            self._json(401, {"error": "Missing or invalid X-Abb-Token"})
            return
        svc: Service = self.server.service  # type: ignore[attr-defined]
        try:
            if self.command in ("GET", "HEAD") and path == "/abb":
                self._send(200, svc.page().encode("utf-8"), "text/html; charset=utf-8")
            elif self.command in ("GET", "HEAD") and path == "/abb/health":
                svc.rpc.health()
                self._json(200, {"status": "ok"})
            elif self.command == "POST" and path == "/abb/add":
                data = self._read_body()
                text = data.get("input")
                if not isinstance(text, str) or not text.strip():
                    raise BadInput("Missing 'input' (book page URL, info hash or magnet link)")
                download_dir = data.get("download_dir")
                if download_dir is not None and not isinstance(download_dir, str):
                    raise BadInput("'download_dir' must be a string")
                self._json(200, svc.add(text, download_dir))
            else:
                self._json(404, {"error": "Not found"})
        except HelperError as exc:
            log.warning("%s %s -> %s: %s", self.command, path, exc.status, exc)
            self._json(exc.status, {"error": str(exc)})
        except Exception as exc:  # never let a traceback kill the request thread silently
            log.exception("unhandled error on %s %s", self.command, path)
            self._json(500, {"error": f"Internal error: {exc.__class__.__name__}"})

    def do_GET(self) -> None:
        self._route()

    def do_HEAD(self) -> None:
        self._route()

    def do_POST(self) -> None:
        self._route()


def make_server(settings: Settings) -> ThreadingHTTPServer:
    host, _, port = settings.listen.rpartition(":")
    host = host.strip("[]") or "0.0.0.0"
    server = ThreadingHTTPServer((host, int(port or 8098)), Handler)
    server.daemon_threads = True
    server.service = Service(settings)  # type: ignore[attr-defined]
    return server


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("ABB_LOG_LEVEL", "INFO").upper(),
        format="[abb-helper] %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    settings = Settings.from_env()
    if not settings.token:
        log.error("ABB_TOKEN is required (the Transmission RPC password); refusing to start")
        return 2
    server = make_server(settings)
    log.info(
        "listening on %s (ABB %s, member login %s, Transmission %s)",
        settings.listen,
        settings.abb_base_url,
        "configured" if settings.abb_username else "not configured",
        settings.rpc_url,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
