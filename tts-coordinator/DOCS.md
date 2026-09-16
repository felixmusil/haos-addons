# TTS Coordinator

Turns a [WuxiaWorld](https://www.wuxiaworld.com/) novel you have access to into a per-chapter
audiobook in [Audiobookshelf](https://www.audiobookshelf.org/) (ABS), a few chapters at a time,
from your phone.

This add-on is the **coordinator**: the web UI, the persistent chapter queue, the WuxiaWorld
chapter fetcher and the ABS publisher. It does **no speech synthesis itself** — the image ships
without torch or any TTS engine, so it is light enough for a Raspberry Pi. Synthesis runs on one
or more **workers** (`tts-worker`, typically your laptop) that pull chapters from the add-on over
the network, convert them, and upload the audio back. Workers only make outbound HTTP calls; the
laptop needs no open ports.

```
 phone / browser ──► TTS Coordinator (this add-on)  ◄── pull ── tts-worker (laptop, Kokoro/…)
   ingress or :8880    queue in /data · WuxiaWorld text · Audiobookshelf upload
```

## Requirements

- A 64-bit Home Assistant OS install (`aarch64`, e.g. Raspberry Pi 4/5 on a 64-bit OS, or `amd64`).
- An Audiobookshelf server the add-on can reach (the ABS add-on on the same Home Assistant works).
- A machine with the [`tts-server`](https://github.com/felixmusil/tts-server) engines installed
  to run the worker (`uv sync --extra kokoro`), reachable to the add-on over LAN or Tailscale.
- Optionally a WuxiaWorld subscription for premium chapters (free chapters need no token).

## Setup

### 1. Audiobookshelf token and library

Create (or pick) a **non-admin** ABS user for the add-on and give it the **Upload** and **Update**
permissions — those are the only ones the publisher needs (adding files and rewriting the
chapter list). Copy the user's API token from its profile page into `abs_token`, and put the
ABS base URL (as seen from the add-on, e.g. `http://homeassistant.local:13378` or the ABS
add-on's internal hostname) into `abs_url`.

`abs_library` is the **name** of the ABS library to publish into (leave blank for the first
library). The library must be of type **Book**, and its **folder watcher must be enabled** (the
default): the add-on uploads files and then waits for the watcher to pick them up, because a
non-admin user cannot trigger a rescan.

### 2. WuxiaWorld token (premium chapters only)

Log in at wuxiaworld.com in a desktop browser, open DevTools → Application → Local Storage →
`https://www.wuxiaworld.com`, and copy the **value** of the key
`oidc.user:https://identity.wuxiaworld.com:wuxiaworld_spa` (a JSON blob with `access_token` and
`refresh_token`). Paste the whole blob, as a single line, into `wuxiaworld_token`.

The add-on refreshes the access token itself and stores the rotated token under
`/data/wuxiaworld-token.json`, so one paste keeps working across restarts and updates for as
long as WuxiaWorld honours the refresh token.

### 3. API token

Set `api_token` to a long random string. Every `/api/*` call on port 8880 — from the web UI **and**
from workers — must then send it as `Authorization: Bearer <token>`. The UI asks for it once and
remembers it in the browser. Without it, anyone on your LAN who can reach port 8880 can enqueue
and cancel conversions.

### 4. Start the add-on

Start it, then open the UI from the **TTS Library** sidebar entry (ingress) or **Open Web UI**.
Paste a WuxiaWorld novel URL (e.g. `https://www.wuxiaworld.com/novel/desolate-era`) to register
it. The book card shows how many chapters ABS already holds, the queued range, and buttons to
enqueue the next `+10 / +25 / custom…` chapters.

### 5. Run a worker on your laptop

On the machine with the engines installed:

```bash
cd tts-server
uv sync --extra kokoro                      # or --all-extras
TTS_WORKER_COORDINATOR_URL=http://<ha-host>.<tailnet>.ts.net:8880 \
TTS_WORKER_TOKEN=<the api_token above> \
uv run tts-worker
```

With Tailscale on Home Assistant OS the add-on is reachable from anywhere at
`http://<ha-host>.<tailnet>.ts.net:8880`; on the home LAN `http://homeassistant.local:8880` works
too. The worker registers itself (it shows up in the **Nodes** strip of the UI), leases one
chapter at a time, synthesizes it, and uploads the finished `.m4a`. Close the laptop and the
lease simply expires; the chapter is re-done later. You can run several workers.

Optional worker settings: `TTS_WORKER_NAME` (default hostname), `TTS_WORKER_ENGINES`
(comma-separated, default all installed), `TTS_WORKER_POLL_S`, `TTS_WORKER_HEARTBEAT_S`.

### 6. Articles from Le Grand Continent (optional)

The **Articles** box in the UI turns a [Le Grand Continent](https://legrandcontinent.eu/fr/)
article into one episode of a podcast called *Le Grand Continent* in Audiobookshelf. It needs:

1. A **second ABS library of type Podcast** (ABS keeps books and podcasts apart), e.g. named
   `LeGrandContinent`, with its folder on the USB drive and the folder watcher on. Give the
   add-on's non-admin ABS user access to it. Put the name in `abs_podcast_library` (blank = the
   first podcast library on the server).
2. For subscriber-only articles, your **login cookie**: on the laptop run
   `uv sync --extra scraper-login && playwright install chromium && uv run lgc-epub login`,
   log in in the browser window that opens, and paste the `Cookie` header value the command
   prints into `lgc_cookie`. A cookie copied from DevTools lacks the httpOnly
   `wordpress_logged_in_*` cookie and only reaches free articles.
3. A worker with the French engine: `uv sync --extra kyutai` on the laptop, then run
   `tts-worker` as usual (it advertises every engine it has). `article_engine` /
   `article_voice` pick what articles are converted with (`kyutai` is the quality choice).

Episodes are titled after the article, dated with its publication date and carry the byline and
URL. The feed card cannot be deleted (numbering would restart while ABS keeps the old episodes);
use **Retry failed** for articles that failed.

## Ingress vs. port 8880

| Access | Auth | Use for |
| ------ | ---- | ------- |
| **Sidebar / ingress** (`TTS Library`) | Home Assistant login | Everyday use from the phone or Nabu Casa remote UI. |
| **`http://<ha-host>:8880`** | `api_token` (bearer) | Workers, and the UI over Tailscale/LAN without HA in front. |

Ingress never exposes the worker API, so port 8880 stays published for the workers. Keep
`api_token` set whenever that port is reachable by more than yourself.

## Data and updates

Everything the add-on needs to survive a restart or update lives in `/data`, which Home Assistant
preserves across updates but **deletes on uninstall**:

- `library.sqlite3` — books, chapters, tasks, units and their states;
- `units/` — finished chapter audio waiting to be published to ABS;
- `library/` — the fallback directory publisher output when ABS is not configured;
- `wuxiaworld-token.json` — the rotated WuxiaWorld token.

Update the add-on from the **Update** button on its page like any other add-on; do not
uninstall/reinstall.

## Options

```yaml
abs_url: ""
abs_token: ""
abs_library: ""
wuxiaworld_token: ""
api_token: ""
ntfy_topic: ""
default_engine: kokoro
default_voice: af_heart
lgc_cookie: ""
abs_podcast_library: ""
article_engine: kyutai
article_voice: cml-tts/fr/10177_10625_000134-0003_enhanced.wav
log_level: info
```

| Option | Description |
| ------ | ----------- |
| `abs_url` | Audiobookshelf base URL as seen from the add-on. Blank disables publishing to ABS: finished chapters are written under `/data/library/<book>/<volume>/` instead. |
| `abs_token` | API token of a **non-admin** ABS user with Upload + Update permissions. |
| `abs_library` | Name of the ABS library to publish into. Blank = the first library. |
| `wuxiaworld_token` | The `oidc.user:…` JSON blob from the browser (see Setup). Blank = free chapters only. |
| `api_token` | Bearer token required on every `/api/*` call on port 8880 (UI and workers). Blank = open. |
| `ntfy_topic` | [ntfy](https://ntfy.sh) topic for one push per scheduled batch, sent when its last chapter has been published (or failed). Blank = off. |
| `default_engine` | Engine stamped on newly registered books; a worker must advertise it (`kokoro` by default). Not installed in this add-on — it names what the *workers* run. |
| `default_voice` | Default voice for new books (`af_heart` for Kokoro). |
| `lgc_cookie` | Le Grand Continent login cookie (the `Cookie` header captured by `lgc-epub login`). Blank = free articles only. |
| `abs_podcast_library` | Name of the **Podcast-type** ABS library articles are published into. Blank = the first podcast library. |
| `article_engine` | Engine for articles (`kyutai` = best French quality; workers must have it installed). |
| `article_voice` | Voice id for articles; Kyutai voices are paths in the `kyutai/tts-voices` repo. |
| `log_level` | `debug`, `info`, `warning`, or `error`. |

## How chapters land in Audiobookshelf

Each WuxiaWorld **volume** becomes one ABS book, `<Author>/<Novel>/<N - Volume title>/`, with one
track per chapter named `NNNN - Chapter title.m4a`; the novel is the ABS **series**. ABS
auto-generates one chapter per file, so chapters are tappable in the player, and the add-on
rewrites the chapter list after each publish so newly added files always appear. Publishing is
idempotent: re-publishing a chapter overwrites the same file.

## Limitations

- **The ABS library's folder watcher must be enabled.** Uploads do not trigger a scan, and a
  non-admin token cannot request one. If the watcher is off, files land on disk but the add-on
  waits (about two minutes per chapter) and the chapter stays in the `uploaded` state until you
  scan the library yourself from an admin account.
- **Pasting a new WuxiaWorld token after the old one died** is ignored while a cached token
  exists: the add-on prefers `/data/wuxiaworld-token.json` over the `wuxiaworld_token` option.
  Delete the cache first, then paste and restart. From the *Advanced SSH & Web Terminal* add-on
  with protection mode off:
  `find /mnt/data/supervisor/addons/data -path '*tts_coordinator*' -name wuxiaworld-token.json -delete`.
  (Uninstall/reinstall also clears it, but wipes the library queue as well.)
- With no worker online, chapters stay queued (their text is prefetched meanwhile) until one
  connects.
- Articles need a Podcast-type ABS library with its own folder watcher on; a podcast uploaded
  into a Book library is refused with `library … is a book library` in the log. The Le Grand
  Continent cookie expires when the site logs you out; re-run `lgc-epub login` and paste again.
- Chapters numbered 10000 and above are rejected (ABS sorts tracks by the first 1-4 digit run).
- The add-on does not mirror progress/bookmarks; ABS keeps one position per volume-book.
