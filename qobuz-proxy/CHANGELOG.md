# Changelog

## 1.3.8

- Fix start-up failure ("DLNA IP address is required") when no `dlna_ip` is configured. The
  add-on now starts unconfigured with a blank `dlna_ip`, so you can log in and add a speaker
  from the Web UI / sidebar panel.

## 1.3.5

- Initial release of the Qobuz Proxy Home Assistant add-on.
- Wraps the `qobuz-proxy` application image (DLNA backend only).
- Host networking for Qobuz Connect (mDNS) and the DLNA audio proxy.
- Options for device name, DLNA IP/port, fixed volume, max quality, and log level.
