# Changelog

Each entry records the bundled upstream **`qobuz-proxy`** application version (the add-on image
is built `FROM ghcr.io/felixmusil/qobuz-proxy`).

| Add-on version | Bundles `qobuz-proxy` |
| -------------- | --------------------- |
| 1.4.2          | 1.4.2                 |
| 1.4.1          | 1.4.1                 |
| 1.4.0          | 1.4.0                 |
| 1.3.11         | 1.3.9                 |
| 1.3.10         | 1.3.8                 |
| 1.3.9          | 1.3.7                 |
| 1.3.8          | 1.3.7                 |
| 1.3.5          | 1.3.5                 |

## 1.4.2

_Bundles `qobuz-proxy` v1.4.2._

- Fix the hourly pause introduced by 1.4.1's token renewal. After reconnecting, the Qobuz server
  replays its last session state ("paused"), which the add-on obeyed, pausing the speaker and
  sometimes making the app move playback to the phone. The replay is now ignored and the real
  playing state is reported immediately.

## 1.4.1

_Bundles `qobuz-proxy` v1.4.1._

- Fix playback stopping about an hour into a session. The Qobuz Connect token expires after 60
  minutes and the app only sends a new one when the device is re-selected, so the proxy lost its
  connection, played one more track and stopped. It now renews the token itself from the stored
  login, falling back to waiting for the app (with backoff) if Qobuz refuses.
- Poll DLNA renderers far less often. Playback state and position are served from the 2-second
  poll loop instead of a fresh SOAP connection every 0.5 s, cutting load on the renderer.

## 1.4.0

_Bundles `qobuz-proxy` v1.4.0._

- Report plays to Qobuz, so tracks played through the add-on show up in Last.fm scrobbles.
- Sanitize the ingress path before reflecting it in the web UI.

## 1.3.11

_Bundles `qobuz-proxy` v1.3.9._

- Fix track-switch reliability. Rapidly skipping tracks in the Qobuz app no longer wedges the
  DLNA renderer (which previously required restarting both the add-on and the device). Playback
  commands are now serialized so overlapping SOAP control requests can't pile up, a stuck renderer
  recovers on its own via a session and transport reset, and the audio proxy retries with a fresh
  stream URL before sending response headers. SOAP errors are also logged with their exception
  type instead of an empty message.

## 1.3.10

_Bundles `qobuz-proxy` v1.3.8._

- Fix Qobuz login from the sidebar panel. Qobuz (and providers like Google) refuse to be framed,
  so login now opens in a new browser tab; the panel switches to "connected" automatically once
  login completes.

## 1.3.9

_Bundles `qobuz-proxy` v1.3.7._

- Actually ships the no-`dlna_ip` start-up fix. The `v1.3.8` tag was placed on a pre-fix commit,
  so the published `:1.3.8` image still failed with "DLNA IP address is required"; this release
  is built from the fixed `run.sh`.

## 1.3.8

_Bundles `qobuz-proxy` v1.3.7._

- Intended to fix the "DLNA IP address is required" start-up failure, but the released image was
  built from a pre-fix commit and did not contain the fix. Superseded by 1.3.9.

## 1.3.5

_Bundles `qobuz-proxy` v1.3.5._

- Initial release of the Qobuz Proxy Home Assistant add-on.
- Wraps the `qobuz-proxy` application image (DLNA backend only).
- Host networking for Qobuz Connect (mDNS) and the DLNA audio proxy.
- Options for device name, DLNA IP/port, fixed volume, max quality, and log level.
