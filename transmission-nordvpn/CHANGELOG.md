# Changelog

Each entry records the bundled upstream **`transmission-openvpn`** image version (the add-on image
is built `FROM haugene/transmission-openvpn`). The add-on version is
`<upstream version>.<wrapper revision>`: `5.5.2.0` is wrapper revision 0 of upstream `5.5.2`.

| Add-on version | Bundles `transmission-openvpn` |
| -------------- | ------------------------------ |
| 5.5.2.0        | 5.5.2                          |

## 5.5.2.0

_Bundles `transmission-openvpn` 5.5.2._

- Initial release of the Transmission (NordVPN) Home Assistant add-on.
- Wraps `haugene/transmission-openvpn` 5.5.2 (Transmission 4.1.3, OpenVPN): NordVPN provider with
  service credentials, country/category/protocol/server selection, and a custom `.ovpn` escape
  hatch from `/share/transmission/openvpn/`.
- Fail-closed tunnel: Transmission runs only while the tunnel is up, the container's default route
  is dropped once connected, and the container exits with OpenVPN so the Supervisor watchdog
  restarts the add-on.
- Ingress panel: nginx in front of Transmission with RPC credentials injected, Flood for
  Transmission as the default web UI (`web_ui` option), landing on an "Add from AudioBookBay"
  page.
- AudioBookBay helper: accepts a book page URL, a magnet link or an info hash; optional member
  login with the session cookie kept under `/data/abb/`; builds a magnet from the page's info hash
  and trackers when the page shows none.
- RPC user `transmission` with a password from `rpc_password` or generated once into
  `/data/rpc-password` and printed in the log. Port 9091 is unmapped by default; `local_networks`
  adds routes for direct LAN/Tailscale access.
- Default `download_dir` `/share/audiobooks` for an Audiobookshelf library hand-off; separate
  incomplete and watch directories under `/share/torrents/`.
