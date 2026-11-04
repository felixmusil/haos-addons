# Changelog

Each entry records the bundled upstream **`tts-server`** application version (the add-on image
is built `FROM ghcr.io/felixmusil/tts-server`).

| Add-on version | Bundles `tts-server`  |
| -------------- | --------------------- |
| 0.4.0          | 0.4.0                 |
| 0.3.1          | 0.3.1                 |
| 0.2.1          | 0.2.1                 |
| 0.2.0          | 0.2.0                 |
| 0.1.0          | 0.2.0                 |

## 0.4.0

_Bundles `tts-server` v0.4.0._

- Fixed: publishing stopped for every book once one unit had been failing for about three
  weeks (for example an article with no Podcast-type library in ABS). Chapters stayed
  "uploaded" and never reached Audiobookshelf; a restart only reset the clock.
- `default_engine` and `article_engine` accept `pocket` (Kyutai Pocket TTS: small, CPU-only,
  English and French voices such as `en/alba` and `fr/estelle`). Workers need
  `uv sync --extra pocket`.
- **Reconvert…** on a book card re-queues a chapter range whose audio in ABS is only the
  early-access teaser; the new file overwrites the stub.
- **Retry failed** also fills cancelled holes and re-checks chapters parked as early-access
  teasers; cards show "N skipped". Failed or cancelled articles get a **Retry** button.
- A worker that is stopped or restarted hands its chapter back at once instead of holding it
  until the lease expires.

## 0.3.1

_Bundles `tts-server` v0.3.1._

- Articles from Le Grand Continent: paste a link in the new **Articles** box, the article is
  converted (Kyutai by default) and published as an episode of one podcast in a Podcast-type ABS
  library. New options `lgc_cookie`, `abs_podcast_library`, `article_engine`, `article_voice`.

## 0.2.1

_Bundles `tts-server` v0.2.1._

- TODO: summarize upstream changes (auto-generated stub — edit before merging).

## 0.2.0

_Bundles `tts-server` v0.2.0._

- TODO: summarize upstream changes (auto-generated stub — edit before merging).

## 0.1.0

_Bundles `tts-server` v0.2.0._

- Initial release of the TTS Coordinator Home Assistant add-on.
- Wraps the `tts-server` coordinator image: library web UI, SQLite chapter queue under `/data`,
  WuxiaWorld chapter prefetcher, per-chapter Audiobookshelf publisher, and the node API that
  pull-based `tts-worker` processes (laptop, over Tailscale) call.
- No synthesis engines in the image — conversions run on workers.
- Options for Audiobookshelf URL/token/library, the WuxiaWorld token, the shared API token, an
  ntfy topic, default engine/voice, and log level.
- Persists the rotated WuxiaWorld refresh token under `/data` so premium chapters keep working
  across restarts.
