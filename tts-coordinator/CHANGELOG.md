# Changelog

Each entry records the bundled upstream **`tts-server`** application version (the add-on image
is built `FROM ghcr.io/felixmusil/tts-server`).

| Add-on version | Bundles `tts-server`  |
| -------------- | --------------------- |
| 0.2.0          | 0.2.0                 |
| 0.1.0          | 0.2.0                 |

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
