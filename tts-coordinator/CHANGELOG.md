# Changelog

Each entry records the bundled upstream **`tts-server`** application version (the add-on image
is built `FROM ghcr.io/felixmusil/tts-server`).

| Add-on version | Bundles `tts-server`  |
| -------------- | --------------------- |
| 0.3.1          | 0.3.1                 |
| 0.2.1          | 0.2.1                 |
| 0.2.0          | 0.2.0                 |
| 0.1.0          | 0.2.0                 |

## 0.3.1

_Bundles `tts-server` v0.3.1._

- TODO: summarize upstream changes (auto-generated stub — edit before merging).

## Unreleased

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
