# Changelog

Each entry records the bundled upstream **`qobuz-proxy`** application version (the add-on image
is built `FROM ghcr.io/felixmusil/qobuz-proxy`).

| Add-on version | Bundles `qobuz-proxy` |
| -------------- | --------------------- |
| 1.3.10         | 1.3.8                 |
| 1.3.9          | 1.3.7                 |
| 1.3.8          | 1.3.7                 |
| 1.3.5          | 1.3.5                 |

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
