# Transmission (NordVPN)

A [Transmission](https://transmissionbt.com/) 4 BitTorrent client whose traffic leaves the Pi
**only through your NordVPN subscription**. The OpenVPN client and Transmission run in one
container (built from [`haugene/transmission-openvpn`](https://github.com/haugene/docker-transmission-openvpn));
Transmission is started when the tunnel comes up and stopped when it goes down, and the
container's normal default route is deleted once the tunnel is up, so a dropped tunnel stops
traffic instead of leaking it.

The Home Assistant sidebar entry (**Torrents**) opens a small "Add from AudioBookBay" page; one
link away is the bundled **Flood for Transmission** web UI. Finished downloads land in a folder
you can point an [Audiobookshelf](https://www.audiobookshelf.org/) library at.

```
 phone / browser ──► HA ingress ──► nginx :8099 ──┬──► /abb/  helper :8098  (AudioBookBay → magnet → RPC)
                                                  └──► /      Transmission :9091  (Flood UI + RPC)
                                                          │
                                        OpenVPN tun0 ─────┘  ◄── the only route to the internet
```

## Requirements

- A 64-bit Home Assistant OS install (`aarch64`, e.g. Raspberry Pi 4/5 on a 64-bit OS, or `amd64`).
- A NordVPN subscription. NordVPN's OpenVPN servers are used (NordLynx/WireGuard is not available
  in this image).
- Enough free space on the data disk for your downloads (see [Data, backups and updates](#data-backups-and-updates)).

## Setup

### 1. NordVPN service credentials

The OpenVPN client does **not** log in with your Nord Account e-mail and password. Open the Nord
Account dashboard → **NordVPN** → **Manual setup** (also called "Set up NordVPN manually") and
copy the **service credentials** shown there (a generated username and password). Put them into
`nordvpn_username` and `nordvpn_password`.

Leave `nordvpn_category` at `legacy_p2p`: it selects NordVPN's P2P server group, which is the one
that allows BitTorrent. `nordvpn_country` picks the country (`CH` by default; a two-letter code or
the country name), `nordvpn_protocol` defaults to `udp`. A concrete server is chosen through the
NordVPN recommendations API at every start; set `nordvpn_server` (e.g. `ch123.nordvpn.com`) to
pin one instead — the country and category filters are then skipped.

### 2. Start the add-on and read the RPC password

Start the add-on and open its **Log** tab. The first start takes a little longer (the OpenVPN
config is downloaded and the tunnel negotiated); the log ends with Transmission starting.

Transmission's RPC interface requires a login: user **`transmission`**, password from
`rpc_password`. When `rpc_password` is empty (the default) the add-on generates one on the first
start, stores it under `/data/rpc-password`, reuses it on later starts, and prints it once per
start in the Log tab as a line beginning with `RPC password:`. You need this password for the
[HA `transmission` integration](#home-assistant-transmission-integration), for the
[one-tap recipe](#one-tap-from-the-phone-home-assistant-rest_command), and for
[direct access on port 9091](#direct-access-on-port-9091-and-local_networks). You do **not**
need it for the sidebar panel: there, nginx adds the credentials for you.

### 3. Open the panel

Click **Torrents** in the sidebar (or **Open Web UI** on the add-on page). The panel lands on the
"Add from AudioBookBay" page; the **Open Flood** link on that page opens the torrent list (`../web/`). Right after a
(re)start the panel answers with an error for roughly 10–30 s while the tunnel is being
established — reload once the log shows Transmission started.

### 4. Optional: AudioBookBay member login

Set `abb_username` and `abb_password` to your AudioBookBay member login. Anonymous book pages
show no magnet link and no `.torrent`, only an info hash and a tracker list; the add-on can build
a magnet from those, but a logged-in session sees the page's own magnet link and the add-on
prefers it. The session cookie is kept under `/data/abb/` so the login happens once, not on every
add. If AudioBookBay moves to a new domain, change `abb_base_url`.

## How it works

- **One container, one tunnel.** OpenVPN is the container's main process. When the tunnel is up
  it runs the image's `tunnelUp.sh`, which starts `transmission-daemon` bound to the tunnel
  address; when the tunnel goes down `tunnelDown.sh` stops Transmission. Transmission therefore
  exists **only while the tunnel is up**.
- **Fail-closed routing.** After the tunnel is up the add-on deletes the container's original
  default route (`DROP_DEFAULT_ROUTE=true`), so the only path to the internet is `tun0`. The
  Supervisor network (the container's own `/23`) stays reachable through its connected route, which
  is how ingress and the HA integration keep working. Anything else on your LAN or tailnet is
  reachable only if you list it in `local_networks` (see below).
- **Dead tunnel = restart.** OpenVPN exits when the server stops answering for 60 s
  (`ping-exit 60`) and any OpenVPN-internal restart is turned into an exit (`remap-usr1 SIGTERM`).
  When OpenVPN exits, the container exits and the Supervisor starts it again (keep the add-on's
  **Watchdog** toggle on); the image's health check (OpenVPN and Transmission running, the
  network reachable through the tunnel) also marks a stuck container unhealthy so it is
  restarted. The whole sequence — pick a server, connect, drop the route, start Transmission —
  then runs afresh.
- **Seeding is limited.** NordVPN offers no port forwarding, so no peer can open a connection to
  you; you upload only to peers you connected to. Ratios stay low by design, and there is no
  peer-port option because it could not carry traffic.
- **Settings are re-applied at every start.** The image rewrites Transmission's `settings.json`
  from the add-on's options on every start: download/incomplete/watch directories, RPC
  authentication, log level and the web UI always follow the options, and changes you make to
  those in Flood's settings dialog are overwritten at the next start. Other Transmission settings
  changed in the UI (speed limits, queue sizes, ...) are kept.
- **RPC is not open to neighbours.** Transmission listens on `9091` inside the container with
  authentication required and no IP whitelist; other add-ons on the Supervisor network get a
  `401` without the password. The ingress nginx accepts connections from the Supervisor's ingress
  proxy only and injects the credentials, so the browser never sees the RPC password.

## Audiobookshelf hand-off

`download_dir` defaults to `/share/audiobooks`. Point an Audiobookshelf library (type
**Book**, folder watcher enabled — the default) at the same folder: with the Audiobookshelf
add-on that is `/share/audiobooks` as seen from its container. Each finished torrent is one
folder, which Audiobookshelf treats as one book and imports within about a minute, while
Transmission keeps seeding the same files in place — nothing is moved or copied.

`incomplete_dir` (`/share/torrents/incomplete`) is deliberately a different folder, so partially
downloaded books never show up in the library. When a torrent finishes, Transmission moves it to
`download_dir`.

For a download that is not an audiobook, pick another destination in Flood's **Add Torrent**
dialog (the **Destination** field) or in the `download_dir` box of the AudioBookBay page. There
is no label-based sorting or hard-linking in this add-on.

## AudioBookBay

### Paste flow

The Home Assistant companion app shows the panel in a WebView that cannot receive `magnet:` links
from other apps and cannot write to the clipboard, so torrents are added by pasting: on an
AudioBookBay book page (`https://audiobookbay.lu/abss/<slug>/`) tap **Share → Copy link** in the
browser, open the **Torrents** panel, paste the link, optionally change the destination, and
press **Add**. For any other torrent, copy its magnet link in the browser and paste it into the
same box, or use Flood → Add Torrent → By URL. The add-on fetches the page through the tunnel,
takes the page's magnet link (member view) or builds one from its `Info Hash:` and `Tracker:`
cells, and hands it to Transmission. The result line reads **Added** or **Already in
Transmission** followed by the book's title. The big "Torrent Free Downloads" buttons on those
pages are advertisements and are ignored.

What can go wrong, and the message you get:

| Message | Cause |
| ------- | ----- |
| `400` bad input | Not a URL on the `abb_base_url` host, not a magnet, not a 40-character hash. |
| `401` login failed | `abb_username`/`abb_password` rejected by AudioBookBay. |
| `404` hash not found | The page has no `Info Hash:` cell (wrong URL, or the page layout changed). |
| `502` unreachable / refused | AudioBookBay did not answer (the tunnel is not up yet, or the domain moved — `abb_base_url`), or Transmission rejected the torrent. |
| `503` Transmission not up | The tunnel is still connecting; retry in a few seconds. |

### One tap from the phone (Home Assistant `rest_command`)

The helper behind the panel also listens for Home Assistant itself, so a share-sheet action can
add a book without opening the panel. Home Assistant reaches the add-on at its **hostname** (shown
on the add-on's Info page, of the form `<id>-transmission-nordvpn`) on port **8098**; every call
must carry the RPC password in the `X-Abb-Token` header. Port 8098 is not published on the host
and the panel's nginx port (8099) only accepts the ingress proxy, so use exactly this URL.

Add to `configuration.yaml` (replace the hostname and the token; `!secret` works for the token):

```yaml
rest_command:
  abb_add:
    url: "http://REPLACE-WITH-ADDON-HOSTNAME:8098/abb/add"
    method: post
    headers:
      X-Abb-Token: REPLACE-WITH-RPC-PASSWORD
    content_type: "application/json; charset=utf-8"
    payload: '{"input": "{{ url }}"}'
    timeout: 60
```

The `timeout` is above Home Assistant's default because the add-on may have to log in to
AudioBookBay and fetch the page through the VPN before it answers. `input` may be a book page
URL, a magnet link or a bare info hash. To send the torrent somewhere else than `download_dir`,
add `"download_dir": "/share/other"` to the JSON payload.

Then a script with a `url` field, so anything that can run a script can pass a link in:

```yaml
script:
  abb_add_url:
    alias: Add audiobook from a link
    fields:
      url:
        name: URL
        description: AudioBookBay book page, magnet link or info hash
        required: true
        selector:
          text:
    sequence:
      - action: rest_command.abb_add
        data:
          url: "{{ url }}"
        response_variable: result
      - action: persistent_notification.create
        data:
          title: Torrent added
          message: "{{ result.content.name }} ({{ result.content.result }})"
```

Reload the YAML configuration (or restart Home Assistant), then test the script from
**Settings → Automations & scenes → Scripts → Run** with a book URL. The add-on log shows the
title and hash of each add.

**iOS Shortcut.** In the Shortcuts app create a new shortcut and set it to **Receive URLs from
Share Sheet** (shortcut details → "Show in Share Sheet"). Add the Home Assistant app's
**Perform Action** action (called **Call Service** in older app versions) with domain `script`,
service `abb_add_url`, and payload `{"url": "<Shortcut Input>"}` — insert the *Shortcut Input*
variable inside the quotes. In Safari on a book page, **Share** → the shortcut. It sends the page
URL to `script.abb_add_url`, which calls `rest_command.abb_add`.

**Android.** The Home Assistant companion app is a share target: sharing a page to it fires a
`mobile_app.share` event in Home Assistant. An automation forwards the shared link:

```yaml
automation:
  - alias: AudioBookBay share to Transmission
    triggers:
      - trigger: event
        event_type: mobile_app.share
    actions:
      - action: script.abb_add_url
        data:
          url: "{{ trigger.event.data.url | default(trigger.event.data.text) }}"
```

Share **only the link** (not "title + link" text); the helper rejects anything that is not a URL,
magnet or hash.

## Direct access on port 9091 and `local_networks`

Port `9091` (Transmission RPC and web UI) is **not published by default**: the sidebar is the
intended way in. If you want Flood or the RPC API directly from a laptop on the LAN, or over
Tailscale, map `9091/tcp` in the add-on's **Network** section *and* set `local_networks` to the
client networks, e.g. `192.168.2.0/24,100.64.0.0/10` (comma-separated CIDRs). Without those
routes the replies to such clients have nowhere to go once the default route is dropped, and the
connection hangs. Never put the Supervisor network (`172.30.32.0/23`) there — it already has a
route. Browsers then prompt for user `transmission` and the RPC password.

## Home Assistant `transmission` integration

The core [`transmission`](https://www.home-assistant.io/integrations/transmission/) integration
gives you sensors (download/upload speed, counts), switches (turtle mode) and the
`transmission.add_torrent` action. It connects from Home Assistant core across the Supervisor
network, so **no port mapping and no `local_networks` entry are needed**.

**Settings → Devices & services → Add integration → Transmission**:

| Field | Value |
| ----- | ----- |
| Host | the add-on's hostname from its Info page (`<id>-transmission-nordvpn`) |
| Port | `9091` |
| Path | leave the default, `/transmission/rpc` |
| Username | `transmission` |
| Password | the RPC password (`rpc_password`, or the `RPC password:` line in the log) |

Set it up while the add-on is running (the tunnel must be up for RPC to answer). A dashboard
paste box that adds a magnet through the integration — independent of the AudioBookBay helper:

```yaml
input_text:
  torrent_magnet:
    name: Magnet link or torrent URL
    max: 255
```

```yaml
script:
  add_torrent_from_dashboard:
    alias: Add torrent from dashboard
    sequence:
      - action: transmission.add_torrent
        data:
          entry_id: REPLACE-WITH-TRANSMISSION-ENTRY-ID
          torrent: "{{ states('input_text.torrent_magnet') }}"
      - action: input_text.set_value
        target:
          entity_id: input_text.torrent_magnet
        data:
          value: ""
```

Put `input_text.torrent_magnet` and a button that runs `script.add_torrent_from_dashboard` on a
dashboard. The `entry_id` is the integration's config entry; the easiest way to fill it in is to
build this action once in the script editor's visual mode (it offers the Transmission entry in a
dropdown) and switch to YAML. Torrents added this way go to `download_dir`.

## Watch folder

Every `.torrent` file dropped into `watch_dir` (`/share/torrents/watch`) is added automatically
and the file is consumed. With the Samba add-on the folder is `\\<ha-host>\share\torrents\watch`.

## Options

```yaml
nordvpn_username: ""
nordvpn_password: ""
nordvpn_country: CH
nordvpn_category: legacy_p2p
nordvpn_protocol: udp
nordvpn_server: ""
openvpn_custom_config: ""
rpc_password: ""
download_dir: /share/audiobooks
incomplete_dir: /share/torrents/incomplete
watch_dir: /share/torrents/watch
web_ui: flood-for-transmission
local_networks: ""
health_check_host: 1.1.1.1
log_level: info
abb_base_url: https://audiobookbay.lu
abb_username: ""
abb_password: ""
```

| Option | Description |
| ------ | ----------- |
| `nordvpn_username` | NordVPN **service credentials** username (Nord Account → NordVPN → Manual setup), not your account e-mail. Required unless `openvpn_custom_config` is set. |
| `nordvpn_password` | NordVPN service credentials password. |
| `nordvpn_country` | Country to connect to: a code (`CH`) or a name (`Switzerland`). Ignored when `nordvpn_server` is set. |
| `nordvpn_category` | NordVPN server group; `legacy_p2p` is the group that allows BitTorrent. Ignored when `nordvpn_server` is set. |
| `nordvpn_protocol` | `udp` (default, faster) or `tcp` (try this when UDP is blocked or the connection is unstable). |
| `nordvpn_server` | Pin one server by hostname, e.g. `ch123.nordvpn.com`. Blank = best recommended server for the country/category at each start. |
| `openvpn_custom_config` | Escape hatch: the name of a config file you placed at `/share/transmission/openvpn/<name>.ovpn` (`name` or `name.ovpn` both work). Switches the provider to `custom`; the NordVPN selection options are then unused. The file is **copied** into the container at every start, so edit the copy under `/share` and restart. The `nordvpn_*` credentials are still required and written into the profile's `auth-user-pass` unless the profile authenticates with a certificate. Blank = built-in NordVPN provider. |
| `rpc_password` | Password of the RPC user `transmission`. Blank = generated on first start, stored in `/data/rpc-password`, printed in the log as `RPC password: …`; a value set here wins over the stored one. Also the token the AudioBookBay helper expects in `X-Abb-Token`. Must not contain a double quote, backslash, dollar sign, backtick or whitespace (the add-on refuses to start and says so). |
| `download_dir` | Where finished torrents are moved. Default `/share/audiobooks` — point your Audiobookshelf library here. |
| `incomplete_dir` | Where running downloads live until they finish. Keep it outside the Audiobookshelf library. |
| `watch_dir` | Folder scanned for `.torrent` files. |
| `web_ui` | Web UI served at `/web/`: `flood-for-transmission` (default), `stock` (Transmission's built-in UI), `combustion`, `kettu`, `transmission-web-control`, `shift` or `transmissionic`. |
| `local_networks` | Comma-separated CIDRs that must be able to reach a mapped `9091` port directly, e.g. `192.168.2.0/24,100.64.0.0/10`. Routed via the LAN gateway, outside the tunnel. Only needed when you map the 9091 port; leave blank otherwise. |
| `health_check_host` | Host the image's health check resolves and pings through the tunnel (`1.1.1.1` by default; the upstream default `google.com` gave false negatives). |
| `log_level` | Transmission log level: `trace`, `debug`, `info`, `warn`, `error` or `critical`. |
| `abb_base_url` | AudioBookBay base URL, `https://audiobookbay.lu` today. Change it when the site moves domain; only links on this host are treated as book pages. |
| `abb_username` | AudioBookBay member username. Blank = anonymous fetches (magnets are built from the page's info hash and trackers). |
| `abb_password` | AudioBookBay member password. |

## Data, backups and updates

Under the add-on's `/data` (kept across restarts and updates, deleted on uninstall):

- `transmission-home/` — Transmission's `settings.json`, resume files and torrent files.
- `rpc-password` — the generated RPC password (only when `rpc_password` is blank).
- `abb/cookies.txt` — the AudioBookBay session cookie.

Your downloads are under `/share`, which is **included in Home Assistant full backups** and can
make them very large. Either exclude the *Share* folder in the backup settings / use partial
backups, or keep `download_dir` and `incomplete_dir` where you accept them being backed up. Watch
the free space on the data disk (**Settings → System → Storage**): a full data disk takes Home
Assistant itself down, not only this add-on.

Update from the **Update** button on the add-on page like any other add-on; do not
uninstall/reinstall. The add-on version is `<upstream image version>.<wrapper revision>`; see
CHANGELOG.md for what each version bundles.

## Troubleshooting

Read the **Log** tab first; every failure below leaves a clear line there.

- **`AUTH_FAILED` from OpenVPN.** Wrong or missing service credentials (they are not your account
  login), or a NordVPN-side spell of refusals on some servers. Re-copy the credentials; try
  `nordvpn_protocol: tcp`; pin a different `nordvpn_server`. If NordVPN's own client works but
  this keeps failing, download a `.ovpn` for one server from NordVPN's manual-setup page, save it
  as `/share/transmission/openvpn/<name>.ovpn` and set `openvpn_custom_config: <name>`.
- **"Cannot open TUN/TAP dev /dev/net/tun" or "TUNSETIFF … Operation not permitted".** The TUN
  device or `NET_ADMIN` is missing. Both are requested by the add-on itself; on Home Assistant OS
  this indicates a Supervisor/host problem — check that the host has the `tun` module and
  reinstall the add-on.
- **"Network is down" / "DNS resolution failed" lines and repeated restarts.** The image's health
  check pings `health_check_host` through the tunnel. If that host is unreachable from the VPN
  exit for your region, set another always-on address.
- **The panel shows an error page.** For 10–30 s after every start the tunnel is not up yet and
  nginx has nothing to forward to. If it persists, the log shows why OpenVPN did not connect.
- **"WARNING: TRANSMISSION_HOME is not set to the default /config/transmission-home" followed by
  "WARNING: Deprecated. Found old default transmission-home folder at /data/transmission-home".**
  Both are expected at every start — the add-on keeps Transmission's state in `/data` on purpose,
  and the image then adopts that path itself.
- **Nothing downloads, peers stay at 0.** Confirm the tunnel is up (log), then that the torrent
  has seeders; with no port forwarding you depend on peers accepting your outgoing connections.
- **Cannot reach `http://<ha-host>:9091`.** The port is not mapped by default; map it and set
  `local_networks` to your client's network (see above).
- **AudioBookBay adds fail.** The status code in the result line maps to a cause in the
  [table above](#paste-flow).
