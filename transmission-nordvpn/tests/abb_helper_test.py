"""Tests for rootfs/usr/local/bin/abb_helper.py — stdlib unittest, no network beyond 127.0.0.1.

WHY this shape: a pasted book URL is the one unit of work; it hops nginx-injected token →
classify → AbbClient (login POST, MozillaCookieJar, redirect-to-login detection, bounded
re-login) → parse_book_page → build_magnet → TransmissionRpc (409 handshake, duplicate) →
JSON reply/log line. The pure functions get unit tests with hand-derived values from the real
fixture; everything else runs against two loopback fakes (http.server FakeAbb + FakeTransmission),
with the helper launched as a real subprocess through its env interface exactly as run.sh does.
The fakes record every request so assertions land on what crossed the wire, not on internals.

Run from the repo root:
    python3 -m unittest discover -s transmission-nordvpn/tests -p '*_test.py'
"""

from __future__ import annotations

import ast
import http.client
import importlib.util
import json
import os
import py_compile
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ADDON_DIR = os.path.dirname(HERE)
HELPER_PATH = os.path.join(ADDON_DIR, "rootfs", "usr", "local", "bin", "abb_helper.py")
FIXTURES = os.path.join(HERE, "fixtures")

HASH = "47bb9fc714466829b17f309a78a1b8a9ebf3a14d"
TITLE = "Predictably Irrational - Dan Ariely"
BOOK_PATH = "/abss/predictably-irrational-dan-ariely/"
MEMBER_PATH = "/abss/member-view/"
MEMBER_MAGNET = f"magnet:?xt=urn:btih:{HASH}&dn=Predictably+Irrational"
MEMBER_ANCHOR = f'<a href="{MEMBER_MAGNET}">Magnet</a>'
TEN_TRACKERS = [
    "udp://tracker.openbittorrent.com:80/announce",
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://bittorrent-tracker.e-n-c-r-y-p-t.net:1337/announce",
    "udp://retracker01-msk-virt.corbina.net:80/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.dler.org:6969/announce",
    "http://ipv4announce.sktorrent.eu:6969/announce",
    "http://tracker.bt4g.com:2095/announce",
    "http://tracker.mywaifu.best:6969/announce",
]
UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)
ABB_USER = "felix"
ABB_PASS = "s3cretPW!"
RPC_USER = "transmission"
RPC_PASS = "rpcTok3n"
RPC_BASIC = "Basic dHJhbnNtaXNzaW9uOnJwY1RvazNu"  # base64("transmission:rpcTok3n"), hand-derived
SESSION_COOKIE = "abbsess=tok42"

LOGIN_HTML = """<html><body>
<form name="entryform" method="post" action="/member/login.php">
<input type="text" name="username"><input type="password" name="password">
</form></body></html>"""


def read_fixture(name: str) -> str:
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


def load_helper():
    spec = importlib.util.spec_from_file_location("abb_helper", HELPER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses need the module registered on 3.10
    spec.loader.exec_module(module)
    return module


abb = load_helper()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def closed_port() -> int:
    """A loopback port nothing listens on (bound then released)."""
    return free_port()


# --------------------------------------------------------------------------- fakes


class _RecordingServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, handler):
        super().__init__(("127.0.0.1", 0), handler)
        self.requests: list[dict] = []
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def record(self, handler, body: str) -> None:
        with self.lock:
            self.requests.append(
                {
                    "method": handler.command,
                    "path": handler.path.split("?", 1)[0],
                    "headers": {k.lower(): v for k, v in handler.headers.items()},
                    "body": body,
                }
            )

    def reset(self) -> None:
        with self.lock:
            self.requests = []

    def count(self, method: str, path: str) -> int:
        return sum(1 for r in self.requests if r["method"] == method and r["path"] == path)

    def stop(self) -> None:
        self.shutdown()
        self.server_close()


class _QuietHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # keep test output clean
        pass

    def _body(self) -> str:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n).decode("utf-8") if n else ""

    def _reply(self, status: int, body: bytes = b"", ctype: str = "text/html", extra=()):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)


class FakeAbbHandler(_QuietHandler):
    """Anonymous GET of a book page → 302 /member/login; with the session cookie the page is
    served. POST /member/login.php with the right form fields sets a cookie WITHOUT Expires (as
    ABB does) and redirects to the member index; a wrong password redirects back to the login."""

    def _has_cookie(self) -> bool:
        return SESSION_COOKIE in (self.headers.get("Cookie") or "")

    def do_GET(self):
        srv: FakeAbb = self.server  # type: ignore[assignment]
        body = self._body()
        srv.record(self, body)
        path = self.path.split("?", 1)[0]
        if path == "/member/login":
            self._reply(200, LOGIN_HTML.encode())
        elif path == "/member/index.php":
            self._reply(200, b"<html><body>Welcome, member</body></html>")
        elif path in srv.member_pages and self._has_cookie():
            self._reply(200, srv.member_pages[path].encode("utf-8"))  # member view (magnet link)
        elif path in srv.public_pages:
            self._reply(200, srv.public_pages[path].encode("utf-8"))
        elif path in srv.pages:
            if self._has_cookie():
                self._reply(200, srv.pages[path].encode("utf-8"))
            else:
                self._reply(302, b"", extra=[("Location", "/member/login")])
        else:
            self._reply(404, b"<html>not found</html>")

    def do_POST(self):
        srv: FakeAbb = self.server  # type: ignore[assignment]
        body = self._body()
        srv.record(self, body)
        path = self.path.split("?", 1)[0]
        if path != "/member/login.php":
            self._reply(404, b"not found")
            return
        form = urllib.parse.parse_qs(body)
        if form.get("username") == [ABB_USER] and form.get("password") == [ABB_PASS]:
            self._reply(
                302,
                b"",
                extra=[("Set-Cookie", SESSION_COOKIE + "; Path=/"), ("Location", "/member/index.php")],
            )
        else:
            self._reply(302, b"", extra=[("Location", "/member/login")])


class FakeAbb(_RecordingServer):
    def __init__(self):
        self.pages = {
            BOOK_PATH: read_fixture("abb_book.html"),
            MEMBER_PATH: read_fixture("abb_book_member.html"),
        }
        self.public_pages: dict[str, str] = {}  # served with 200 even without the cookie
        # Real-site shape: the same path answers the anonymous page to everyone and the member page
        # (with the magnet anchor) only when the session cookie is present.
        self.member_pages: dict[str, str] = {}
        super().__init__(FakeAbbHandler)


class FakeTransmissionHandler(_QuietHandler):
    def do_POST(self):
        srv: FakeTransmission = self.server  # type: ignore[assignment]
        body = self._body()
        srv.record(self, body)
        if self.headers.get("Authorization") != RPC_BASIC:
            self._reply(401, b"Unauthorized User", "text/plain")
            return
        if self.headers.get("X-Transmission-Session-Id") != srv.session_id:
            self._reply(
                409,
                b"<h1>409: Conflict</h1>",
                extra=[("X-Transmission-Session-Id", srv.session_id)],
            )
            return
        try:
            req = json.loads(body)
        except ValueError:
            self._reply(400, b"bad json", "text/plain")
            return
        method = req.get("method")
        if method == "session-get":
            out = {"result": "success", "arguments": {"version": "4.1.3", "rpc-version": 18}}
        elif method == "torrent-add":
            filename = req.get("arguments", {}).get("filename", "")
            m = re.search(r"urn:btih:([0-9a-fA-F]{40})", filename)
            if not m:
                out = {"result": "invalid or corrupt torrent file", "arguments": {}}
            else:
                h = m.group(1).lower()
                torrent = {"id": len(srv.seen) + 1, "name": TITLE, "hashString": h}
                key = "torrent-duplicate" if h in srv.seen else "torrent-added"
                srv.seen.add(h)
                out = {"result": "success", "arguments": {key: torrent}}
        else:
            out = {"result": "method name not recognized", "arguments": {}}
        self._reply(200, json.dumps(out).encode(), "application/json")


class FakeTransmission(_RecordingServer):
    def __init__(self):
        self.session_id = "sessA"
        self.seen: set[str] = set()
        super().__init__(FakeTransmissionHandler)

    def rotate_session_id(self) -> None:
        self.session_id = "sess" + str(time.time_ns())

    def torrent_add_bodies(self) -> list[dict]:
        out = []
        for r in self.requests:
            try:
                j = json.loads(r["body"])
            except ValueError:
                continue
            if j.get("method") == "torrent-add":
                out.append(j)
        return out


# --------------------------------------------------------------------------- helper subprocess


class Helper:
    """abb_helper.py as a subprocess, driven only by env vars — the run.sh ↔ helper seam."""

    def __init__(self, tmp: str, fake_abb: FakeAbb, fake_tr: FakeTransmission, **overrides):
        self.port = free_port()
        self.jar_path = overrides.pop("jar_path", os.path.join(tmp, "abb", "cookies.txt"))
        env = {
            "PATH": os.environ.get("PATH", ""),
            "ABB_LISTEN": f"127.0.0.1:{self.port}",
            "ABB_TOKEN": RPC_PASS,
            "ABB_BASE_URL": fake_abb.url,
            "ABB_USERNAME": ABB_USER,
            "ABB_PASSWORD": ABB_PASS,
            "ABB_COOKIE_JAR": self.jar_path,
            "TR_RPC_URL": fake_tr.url + "/transmission/rpc",
            "TR_RPC_USER": RPC_USER,
            "TR_RPC_PASSWORD": RPC_PASS,
            "DEFAULT_DOWNLOAD_DIR": "/share/audiobooks",
        }
        env.update(overrides)
        self.env = env
        self.proc = subprocess.Popen(
            [sys.executable, HELPER_PATH],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        self.output: list[bytes] = []
        self._drain = threading.Thread(target=self._pump, daemon=True)
        self._drain.start()
        deadline = time.time() + 20
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("helper exited early:\n" + self.text())
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                    return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("helper did not start listening:\n" + self.text())

    def _pump(self):
        for line in self.proc.stdout:
            self.output.append(line)

    def text(self) -> str:
        return b"".join(self.output).decode("utf-8", errors="replace")

    def request(self, method: str, path: str, body=None, token=RPC_PASS, raw_body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {}
        if token is not None:
            headers["X-Abb-Token"] = token
        data = None
        if raw_body is not None:
            data = raw_body
            headers["Content-Type"] = "application/json"
        elif body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        payload = resp.read()
        ctype = resp.getheader("Content-Type") or ""
        conn.close()
        parsed = None
        if ctype.startswith("application/json"):
            parsed = json.loads(payload.decode("utf-8"))
        return resp.status, ctype, payload.decode("utf-8", errors="replace"), parsed

    def add(self, input_text: str, download_dir: str | None = None, token=RPC_PASS):
        body = {"input": input_text}
        if download_dir is not None:
            body["download_dir"] = download_dir
        return self.request("POST", "/abb/add", body=body, token=token)

    def stop(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=5)
        self._drain.join(timeout=5)
        self.proc.stdout.close()


# =========================================================================== unit tests


class ClassifyTest(unittest.TestCase):
    def test_classify_inputs(self):
        # WHY: classification is the routing decision; phone pastes carry whitespace/newlines,
        # hashes arrive upper-case, and old ABB domains or foreign hosts must never be fetched.
        base = "https://audiobookbay.lu"
        cases = [
            (f"magnet:?xt=urn:btih:{HASH.upper()}&dn=x", "magnet"),
            (f"magnet:?xt=urn:btih:{HASH}", "magnet"),
            (HASH, "hash"),
            (f"  {HASH.upper()}\n", "hash"),
            ("https://audiobookbay.lu/abss/predictably-irrational-dan-ariely/", "url"),
            ("http://audiobookbay.lu/abss/predictably-irrational-dan-ariely/", "url"),
            ("https://audiobookbay.lu/abss/predictably-irrational-dan-ariely/\n", "url"),
            ("https://audiobookbay.is/abss/x/", None),
            ("https://example.com/abss/x/", None),
            ("hello world", None),
            ("", None),
            (HASH[:39], None),
            (HASH + "a", None),
            (f"urn:btih:{HASH}", None),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(abb.classify(text, base), expected)


class ParseBookPageTest(unittest.TestCase):
    def test_parse_book_page_uses_table_structure_not_first_hex(self):
        # WHY: the real page holds the hash once, so a lazy first-40-hex regex passes on the raw
        # fixture; a decoy planted before the Info Hash row pins the "label cell → next cell" rule.
        html = read_fixture("abb_book.html")
        page = abb.parse_book_page(html)
        self.assertEqual(page.title, TITLE)  # the h1, not the keyword <h2>s
        self.assertEqual(page.info_hash, HASH)
        self.assertEqual(list(page.trackers), TEN_TRACKERS)  # Announce URL row adds no 11th
        self.assertIsNone(page.magnet)
        for t in page.trackers:
            for decoy in ("downld0", "dodl3", "dedl4"):
                self.assertNotIn(decoy, t)

        decoy_hash = "0123456789abcdef0123456789abcdef01234567"
        decoyed = html.replace(
            "<td>Info Hash:</td>",
            f"<tr><td>Comment:</td><td>{decoy_hash}</td></tr><td>Info Hash:</td>",
        ).replace(
            "/downld0?downfs=22Predictably_Irrational_by_Dan_Ariely",
            "/downld0?downfs=deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
        )
        self.assertIn(decoy_hash, decoyed)
        self.assertEqual(abb.parse_book_page(decoyed).info_hash, HASH)

        without_hash = html.replace("<td>Info Hash:</td>", "<td>Something else:</td>")
        self.assertIsNone(abb.parse_book_page(without_hash).info_hash)

    def test_parse_unescapes_html_entities(self):
        # WHY: WordPress emits &#8217; and &amp;; an unescaped title garbles dn and an unescaped
        # '&amp;' in a tracker/magnet href gives Transmission a URI it rejects.
        html = read_fixture("abb_book.html")
        a = html.replace(
            f'<h1 itemprop="name">{TITLE}</h1>',
            '<h1 itemprop="name">Isn&#8217;t It Obvious &amp; More</h1>',
        )
        self.assertEqual(abb.parse_book_page(a).title, "Isn’t It Obvious & More")

        b = html.replace(
            "<td>http://tracker.bt4g.com:2095/announce</td>",
            "<td>http://t.example/announce?a=1&amp;b=2</td>",
        )
        self.assertIn("http://t.example/announce?a=1&b=2", abb.parse_book_page(b).trackers)

        member = read_fixture("abb_book_member.html")
        c = member.replace(MEMBER_ANCHOR, MEMBER_ANCHOR.replace("&dn=", "&amp;dn="))
        self.assertNotEqual(c, member)
        self.assertEqual(abb.parse_book_page(c).magnet, MEMBER_MAGNET)

    def test_member_fixture_prefers_page_magnet_and_stays_in_sync(self):
        # WHY: pins "prefer the page's magnet over a rebuilt one" and guards the derived fixture:
        # abb_book_member.html must differ from abb_book.html only by the inserted anchor.
        anon = read_fixture("abb_book.html")
        member = read_fixture("abb_book_member.html")
        self.assertEqual(member.count(MEMBER_ANCHOR), 1)
        self.assertEqual(member.replace(MEMBER_ANCHOR, ""), anon)
        self.assertRegex(
            member,
            re.compile(r"Torrent Download</td>.*?display:none;'>" + re.escape(MEMBER_ANCHOR), re.S),
        )
        page = abb.parse_book_page(member)
        self.assertEqual(page.magnet, MEMBER_MAGNET)
        self.assertEqual(page.info_hash, HASH)
        self.assertEqual(page.title, TITLE)
        self.assertEqual(len(page.trackers), 10)


class BuildMagnetTest(unittest.TestCase):
    def test_build_magnet_encoding_and_tracker_fallback(self):
        # WHY: a raw space or '&' in dn splits the magnet's query; a bare-hash add has no page
        # trackers so the built-in list is its only path to peers.
        m = abb.build_magnet(HASH, TITLE, TEN_TRACKERS)
        self.assertTrue(m.startswith(f"magnet:?xt=urn:btih:{HASH}"))
        q = urllib.parse.parse_qs(urllib.parse.urlparse(m).query, keep_blank_values=True)
        self.assertEqual(q["xt"], [f"urn:btih:{HASH}"])
        self.assertEqual(q["dn"], [TITLE])
        self.assertEqual(q["tr"], TEN_TRACKERS)
        self.assertNotIn(" ", m)

        m2 = abb.build_magnet(HASH, "Tom & Jerry #1", TEN_TRACKERS)
        q2 = urllib.parse.parse_qs(urllib.parse.urlparse(m2).query)
        self.assertEqual(q2["dn"], ["Tom & Jerry #1"])
        self.assertEqual(m2.count("&dn="), 1)
        self.assertNotIn("#", m2)

        m3 = abb.build_magnet(HASH, TITLE, [])
        q3 = urllib.parse.parse_qs(urllib.parse.urlparse(m3).query)
        self.assertEqual(len(q3["tr"]), 6)
        self.assertEqual(len(set(q3["tr"])), 6)
        for t in q3["tr"]:
            self.assertTrue(t.startswith("udp://") or t.startswith("http"), t)

        m4 = abb.build_magnet(HASH.upper(), TITLE, [])
        self.assertIn(HASH, m4.lower())
        self.assertEqual(abb.classify(m4), "magnet")


# =========================================================================== client tests


class AbbClientTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeAbb()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(self.fake.stop)

    def test_abb_client_logs_in_once_and_reuses_persisted_session_cookie(self):
        # WHY: ABB's session cookie has no Expires; MozillaCookieJar.save()/load() DROP such
        # cookies unless ignore_discard=True, which would make the helper re-login on every add.
        jar_path = os.path.join(self.tmp, "nested", "dir", "cookies.txt")
        book_url = self.fake.url + BOOK_PATH
        c1 = abb.AbbClient(self.fake.url, ABB_USER, ABB_PASS, jar_path, UA)
        page = c1.fetch_book(book_url)
        self.assertEqual(page.info_hash, HASH)
        self.assertEqual(page.title, TITLE)

        reqs = self.fake.requests
        logins = [r for r in reqs if r["method"] == "POST" and r["path"] == "/member/login.php"]
        self.assertEqual(len(logins), 1)
        self.assertTrue(logins[0]["headers"]["content-type"].startswith("application/x-www-form-urlencoded"))
        self.assertEqual(
            urllib.parse.parse_qs(logins[0]["body"]),
            {"username": [ABB_USER], "password": [ABB_PASS]},
        )
        for r in reqs:
            self.assertEqual(r["headers"].get("user-agent"), UA)
        book_gets = [r for r in reqs if r["method"] == "GET" and r["path"] == BOOK_PATH]
        self.assertIn(SESSION_COOKIE, book_gets[-1]["headers"].get("cookie", ""))
        self.assertLessEqual(len(reqs), 5)  # book→302→login page (2) + login POST→302→member index (2) + book again (1)
        self.assertTrue(os.path.isfile(jar_path))
        with open(jar_path) as f:
            self.assertIn("abbsess", f.read())

        self.fake.reset()
        c2 = abb.AbbClient(self.fake.url, ABB_USER, ABB_PASS, jar_path, UA)
        page2 = c2.fetch_book(book_url)
        self.assertEqual(page2.info_hash, HASH)
        self.assertEqual(self.fake.count("POST", "/member/login.php"), 0)
        self.assertEqual(self.fake.count("GET", "/member/login"), 0)


class TransmissionRpcTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeTransmission()
        self.addCleanup(self.fake.stop)

    def test_abb_client_logs_in_eagerly_on_the_real_site_shape(self):
        # WHY: real AudioBookBay book pages show the Info Hash to anonymous visitors and reserve the
        # magnet link for members. A lazy "login only when bounced" client would never log in, so the
        # configured credentials and the member magnet preference would be dead code (spec-drift
        # finding). Eager login must fire once per fresh jar, be skipped when a session is stored,
        # and never happen without credentials.
        real = FakeAbb()
        real.public_pages[BOOK_PATH] = read_fixture("abb_book.html")
        real.member_pages[BOOK_PATH] = read_fixture("abb_book_member.html")
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        try:
            jar = os.path.join(tmp, "eager", "cookies.txt")
            member = abb.AbbClient(real.url, ABB_USER, ABB_PASS, jar, UA).fetch_book(real.url + BOOK_PATH)
            self.assertEqual(member.magnet, MEMBER_MAGNET)
            self.assertEqual(real.count("POST", "/member/login.php"), 1)

            again = abb.AbbClient(real.url, ABB_USER, ABB_PASS, jar, UA).fetch_book(real.url + BOOK_PATH)
            self.assertEqual(again.magnet, MEMBER_MAGNET)
            self.assertEqual(real.count("POST", "/member/login.php"), 1)  # stored session reused

            anon_jar = os.path.join(tmp, "eager", "anon.txt")
            anon = abb.AbbClient(real.url, "", "", anon_jar, UA).fetch_book(real.url + BOOK_PATH)
            self.assertIsNone(anon.magnet)
            self.assertEqual(anon.info_hash, HASH)
            self.assertEqual(real.count("POST", "/member/login.php"), 1)
        finally:
            real.stop()

    def test_transmission_rpc_handshake_duplicate_and_down(self):
        # WHY: the 409 session-id dance, the hyphenated JSON keys and the Basic header are typo
        # hotspots no type checker sees; duplicate must be a normal outcome and a dead RPC must
        # surface as TransmissionDown, not a traceback.
        magnet = abb.build_magnet(HASH, TITLE, TEN_TRACKERS)
        rpc = abb.TransmissionRpc(self.fake.url + "/transmission/rpc", RPC_USER, RPC_PASS)
        self.assertEqual(rpc.add(magnet, "/share/audiobooks"), ("added", TITLE, HASH))
        self.assertEqual(len(self.fake.requests), 2)  # 409 then success
        self.assertEqual(
            json.loads(self.fake.requests[1]["body"]),
            {
                "method": "torrent-add",
                "arguments": {"filename": magnet, "download-dir": "/share/audiobooks"},
            },
        )
        self.assertEqual(self.fake.requests[1]["headers"]["authorization"], RPC_BASIC)

        self.assertEqual(rpc.add(magnet, "/share/audiobooks")[0], "duplicate")

        self.fake.rotate_session_id()
        other = abb.build_magnet("0123456789abcdef0123456789abcdef01234567", "Other", [])
        self.assertEqual(rpc.add(other, "/share/other")[0], "added")

        with self.assertRaises(abb.HelperError) as ctx:
            rpc.add("magnet:?xt=urn:btih:bad", "/share/audiobooks")
        self.assertNotIsInstance(ctx.exception, (KeyError, TypeError))

        dead = abb.TransmissionRpc(f"http://127.0.0.1:{closed_port()}/transmission/rpc", RPC_USER, RPC_PASS)
        with self.assertRaises(abb.TransmissionDown):
            dead.add(magnet, "/share/audiobooks")


# =========================================================================== end-to-end


class E2EBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.abb_fake = FakeAbb()
        cls.tr_fake = FakeTransmission()
        cls.tmp = tempfile.mkdtemp()

    @classmethod
    def tearDownClass(cls):
        cls.abb_fake.stop()
        cls.tr_fake.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.abb_fake.reset()
        self.tr_fake.reset()
        self.abb_fake.public_pages.clear()

    def spawn(self, **overrides) -> Helper:
        h = Helper(self.tmp, self.abb_fake, self.tr_fake, **overrides)
        self.addCleanup(h.stop)
        return h


class E2EHappyPathTest(E2EBase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.helper = Helper(cls.tmp, cls.abb_fake, cls.tr_fake)

    @classmethod
    def tearDownClass(cls):
        cls.helper.stop()
        super().tearDownClass()

    def test_e2e_add_from_abb_url_then_duplicate_then_hash_override_then_magnet_passthrough(self):
        # WHY: the mandatory happy path through the real entrypoint and the run.sh ↔ helper env
        # seam; the only place a field flowing page → magnet → RPC → reply is seen to arrive intact.
        h = self.helper
        book_url = self.abb_fake.url + BOOK_PATH
        self.tr_fake.seen.discard(HASH)

        status, _, _, body = h.add(book_url)
        self.assertEqual(status, 200, body)
        self.assertEqual(set(body), {"name", "hash", "magnet", "result"})
        self.assertEqual(body["name"], TITLE)
        self.assertEqual(body["hash"], HASH)
        self.assertEqual(body["result"], "added")
        add_body = self.tr_fake.torrent_add_bodies()[-1]
        self.assertEqual(add_body["arguments"]["filename"], body["magnet"])
        self.assertEqual(add_body["arguments"]["download-dir"], "/share/audiobooks")
        q = urllib.parse.parse_qs(urllib.parse.urlparse(body["magnet"]).query)
        self.assertEqual(q["dn"], [TITLE])
        self.assertEqual(q["tr"], TEN_TRACKERS)
        self.assertEqual(self.abb_fake.count("POST", "/member/login.php"), 1)
        for r in self.abb_fake.requests:
            self.assertTrue(r["headers"].get("user-agent", "").startswith("Mozilla/5.0"), r)

        status, _, _, body2 = h.add(book_url)
        self.assertEqual(status, 200, body2)
        self.assertEqual(body2["result"], "duplicate")
        self.assertEqual(body2["hash"], HASH)

        self.abb_fake.reset()
        self.tr_fake.seen.discard(HASH)
        status, _, _, body3 = h.add(HASH.upper(), download_dir="/share/other")
        self.assertEqual(status, 200, body3)
        add_body = self.tr_fake.torrent_add_bodies()[-1]
        self.assertEqual(add_body["arguments"]["download-dir"], "/share/other")
        q = urllib.parse.parse_qs(urllib.parse.urlparse(add_body["arguments"]["filename"]).query)
        self.assertEqual(q["xt"][0].lower(), f"urn:btih:{HASH}")
        self.assertEqual(len(q["tr"]), 6)
        self.assertEqual(self.abb_fake.requests, [])

        custom = (
            "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567"
            "&dn=Custom%20Name&tr=udp%3A%2F%2Ft.example%3A1%2Fannounce"
        )
        status, _, _, body4 = h.add(custom)
        self.assertEqual(status, 200, body4)
        self.assertEqual(self.tr_fake.torrent_add_bodies()[-1]["arguments"]["filename"], custom)

        self.tr_fake.seen.discard(HASH)
        status, _, _, body5 = h.add(self.abb_fake.url + MEMBER_PATH)
        self.assertEqual(status, 200, body5)
        self.assertEqual(body5["magnet"], MEMBER_MAGNET)
        self.assertNotIn("tr=", body5["magnet"])
        self.assertEqual(self.tr_fake.torrent_add_bodies()[-1]["arguments"]["filename"], MEMBER_MAGNET)

        time.sleep(0.2)  # let the log drain
        out = h.text()
        self.assertIn(TITLE, out)
        self.assertIn(HASH, out)
        for secret in (ABB_PASS, RPC_PASS, "dHJhbnNtaXNzaW9uOnJwY1RvazNu"):
            self.assertNotIn(secret, out)

    def test_e2e_token_gate_blocks_every_route_before_any_upstream_call(self):
        # WHY: the token is all that stands between any container on the Supervisor /23 and
        # "add arbitrary torrents / fetch through the VPN"; it must run first on every route.
        h = self.helper
        book_url = self.abb_fake.url + BOOK_PATH
        for token in (None, "wrong"):
            for method, path, body in (
                ("GET", "/abb/", None),
                ("POST", "/abb/add", {"input": book_url}),
                ("GET", "/abb/health", None),
            ):
                with self.subTest(token=token, path=path):
                    status, ctype, _, parsed = h.request(method, path, body=body, token=token)
                    self.assertEqual(status, 401)
                    self.assertTrue(ctype.startswith("application/json"), ctype)
                    self.assertIsInstance(parsed.get("error"), str)
        self.assertEqual(self.abb_fake.requests, [])
        self.assertEqual(self.tr_fake.requests, [])

        self.tr_fake.rotate_session_id()  # health must do the 409 handshake, not read 409 as down
        status, _, _, parsed = h.request("GET", "/abb/health")
        self.assertEqual(status, 200, parsed)
        status, _, _, parsed = h.request("GET", "/somewhere-else")
        self.assertEqual(status, 404)

    def test_e2e_bad_input_is_400_and_never_fetches_or_adds(self):
        # WHY: a foreign-host URL must be rejected before any network call (rationale: classify() docstring).
        h = self.helper
        cases = [
            ({"input": "hello world"}, None),
            ({"input": ""}, None),
            ({}, None),
            (None, b"not json{"),
            ({"input": "https://audiobookbay.is/abss/predictably-irrational-dan-ariely/"}, None),
            ({"input": "https://example.com/"}, None),
            ({"input": f"urn:btih:{HASH}"}, None),
        ]
        for body, raw in cases:
            with self.subTest(body=body, raw=raw):
                status, ctype, _, parsed = h.request("POST", "/abb/add", body=body, raw_body=raw)
                self.assertEqual(status, 400, parsed)
                self.assertTrue(ctype.startswith("application/json"))
                self.assertTrue(parsed.get("error"))
        self.assertEqual(self.abb_fake.requests, [])
        self.assertEqual(self.tr_fake.requests, [])
        self.assertIsNone(h.proc.poll())
        status, _, _, _ = h.request("GET", "/abb/health")
        self.assertEqual(status, 200)

    def test_e2e_html_page_is_ingress_relative_and_carries_no_token(self):
        # WHY: nginx injects the token for browser traffic, so the page must not embed the header
        # or its value; HA ingress prefixes the path, so absolute URLs break the sidebar.
        status, ctype, body, _ = self.helper.request("GET", "/abb/")
        self.assertEqual(status, 200)
        self.assertTrue(ctype.startswith("text/html"), ctype)
        self.assertIn("../web/", body)
        self.assertRegex(body, r"(name|id)=[\"']input[\"']")
        self.assertRegex(body, r"(name|id)=[\"']download_dir[\"']")
        self.assertRegex(body, r"fetch\(\s*['\"]add['\"]")
        self.assertIn("POST", body)
        self.assertIn("application/json", body)
        self.assertNotIn("x-abb-token", body.lower())
        self.assertNotIn(RPC_PASS, body)
        self.assertIsNone(re.search(r"(href|src|action)\s*=\s*['\"]/", body))
        self.assertIsNone(re.search(r"fetch\(\s*['\"]/", body))
        self.assertRegex(body, r"<meta[^>]+name=[\"']viewport[\"']")
        self.assertIn("<script", body)


class E2EFailureTest(E2EBase):
    def test_e2e_upstream_failures_map_to_401_404_502_503(self):
        # WHY: the status is what the phone UI and HA rest_command see; four distinct upstream
        # failures must stay distinguishable, and re-login must be bounded.
        book_url = self.abb_fake.url + BOOK_PATH

        with self.subTest("wrong ABB password -> 401"):
            h = self.spawn(ABB_PASSWORD="wrong", jar_path=os.path.join(self.tmp, "j1", "c.txt"))
            status, _, _, parsed = h.add(book_url)
            self.assertEqual(status, 401, parsed)
            self.assertIn("login", parsed["error"].lower())
            self.assertLessEqual(self.abb_fake.count("POST", "/member/login.php"), 2)
            self.assertLessEqual(len(self.abb_fake.requests), 6)
            self.assertEqual(self.tr_fake.requests, [])
            h.stop()

        self.abb_fake.reset()
        with self.subTest("page without hash -> 404"):
            self.abb_fake.public_pages["/abss/no-hash-here/"] = LOGIN_HTML
            h = self.spawn(jar_path=os.path.join(self.tmp, "j2", "c.txt"))
            status, _, _, parsed = h.add(self.abb_fake.url + "/abss/no-hash-here/")
            self.assertEqual(status, 404, parsed)
            self.assertIn("hash", parsed["error"].lower())
            self.assertLessEqual(self.abb_fake.count("POST", "/member/login.php"), 2)
            self.assertEqual(self.tr_fake.requests, [])
            h.stop()

        with self.subTest("ABB unreachable -> 502"):
            dead = f"http://127.0.0.1:{closed_port()}"
            h = self.spawn(ABB_BASE_URL=dead, jar_path=os.path.join(self.tmp, "j3", "c.txt"))
            status, _, _, parsed = h.add(dead + "/abss/x/")
            self.assertEqual(status, 502, parsed)
            self.assertTrue(parsed["error"])
            h.stop()

        with self.subTest("Transmission down -> 503"):
            h = self.spawn(
                TR_RPC_URL=f"http://127.0.0.1:{closed_port()}/transmission/rpc",
                jar_path=os.path.join(self.tmp, "j4", "c.txt"),
            )
            status, _, _, parsed = h.add(HASH)
            self.assertEqual(status, 503, parsed)
            self.assertIn("transmission", parsed["error"].lower())
            status, _, _, _ = h.request("GET", "/abb/health")
            self.assertEqual(status, 503)
            h.stop()
            ok = self.spawn(jar_path=os.path.join(self.tmp, "j5", "c.txt"))
            status, _, _, _ = ok.request("GET", "/abb/health")
            self.assertEqual(status, 200)
            ok.stop()

        with self.subTest("wrong RPC password -> non-2xx JSON error"):
            h = self.spawn(TR_RPC_PASSWORD="nope", jar_path=os.path.join(self.tmp, "j6", "c.txt"))
            status, ctype, _, parsed = h.add(HASH)
            self.assertTrue(500 <= status < 600, (status, parsed))
            self.assertTrue(ctype.startswith("application/json"))
            self.assertTrue(parsed.get("error"))
            self.assertNotEqual(parsed.get("result"), "added")
            h.stop()

    def test_e2e_cookie_jar_survives_helper_restart(self):
        # WHY: the jar at ABB_COOKIE_JAR is the single source of the ABB session across add-on
        # restarts; a jar written by one process must be loadable by a fresh one, and deleting it
        # must trigger one transparent re-login.
        jar = os.path.join(self.tmp, "restart", "abb", "cookies.txt")
        book_url = self.abb_fake.url + BOOK_PATH
        self.tr_fake.seen.discard(HASH)

        a = self.spawn(jar_path=jar)
        status, _, _, parsed = a.add(book_url)
        self.assertEqual(status, 200, parsed)
        self.assertEqual(parsed["result"], "added")
        a.stop()
        self.assertTrue(os.path.isfile(jar))

        self.abb_fake.reset()
        self.tr_fake.seen.discard(HASH)
        b = self.spawn(jar_path=jar)
        status, _, _, parsed = b.add(book_url)
        self.assertEqual(status, 200, parsed)
        self.assertEqual(parsed["result"], "added")
        self.assertEqual(self.abb_fake.count("POST", "/member/login.php"), 0)
        self.assertEqual(self.abb_fake.count("GET", "/member/login"), 0)

        # Delete the jar between restarts → exactly one transparent re-login.
        b.stop()
        os.remove(jar)
        self.abb_fake.reset()
        self.tr_fake.seen.discard(HASH)
        c = self.spawn(jar_path=jar)
        status, _, _, parsed = c.add(book_url)
        self.assertEqual(status, 200, parsed)
        self.assertEqual(self.abb_fake.count("POST", "/member/login.php"), 1)
        self.assertTrue(os.path.isfile(jar))


# =========================================================================== static checks


class StaticTest(unittest.TestCase):
    def test_helper_compiles_and_avoids_modules_removed_before_python_3_14(self):
        # WHY: the image ships python 3.14, the host 3.10 and CI 3.12; unittest on the host proves
        # 3.10 syntax but cannot see a stdlib module removed in 3.13/3.14 that fails only in the
        # container at first request.
        py_compile.compile(HELPER_PATH, doraise=True)
        with open(HELPER_PATH, encoding="utf-8") as f:
            source = f.read()
        self.assertTrue(
            source.startswith("#!/usr/bin/env python3") or source.startswith("#!/usr/bin/python3")
        )
        tree = ast.parse(source)
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names.add(node.module.split(".")[0])
        removed = {
            "cgi", "cgitb", "asyncore", "asynchat", "imp", "distutils", "telnetlib", "pipes",
            "crypt", "nntplib", "smtpd", "sndhdr", "uu", "xdrlib", "lib2to3",
        }
        self.assertTrue(names.isdisjoint(removed), names & removed)
        stdlib = set(sys.stdlib_module_names)
        self.assertTrue(names <= stdlib, names - stdlib)


if __name__ == "__main__":
    unittest.main()
