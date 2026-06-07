# Qobuz Proxy

Headless [Qobuz Connect](https://www.qobuz.com/) player. It appears as a Qobuz Connect device
in the official Qobuz app and streams the audio to a **DLNA renderer** on your network (Sonos,
Denon HEOS, and other UPnP/DLNA speakers).

## Requirements

- A 64-bit Home Assistant OS install (`aarch64`, e.g. Raspberry Pi 4 on a 64-bit OS, or `amd64`).
- A Qobuz subscription.
- A DLNA/UPnP renderer reachable on the same network as Home Assistant.

## How it works / networking

This add-on runs with **host networking** — this is required because:

- Qobuz Connect discovery uses **mDNS**, which needs to be on the host LAN.
- The DLNA renderer fetches audio directly from the add-on's **audio proxy on port 7120**.

The Web UI is available three ways:

- **Sidebar panel** — the add-on is exposed through Home Assistant **ingress**, so it shows up
  in the HA sidebar (icon: a music circle, title "Qobuz") and embeds the UI directly in HA.
- **"Open Web UI"** button on the add-on's Info tab.
- **Directly** at `http://<your-ha-host>:8689` (host networking exposes the port).

The add-on page also gives you, for free:

- a **Configuration** tab to edit all options below, and
- a **Log** tab showing the live add-on output.

| Port | Purpose |
| ---- | ------- |
| 8689 | Web UI + Qobuz Connect discovery |
| 7120 | DLNA audio proxy (renderer pulls audio from here) |

## Installation

1. Install and **Start** the add-on. (It starts fine with no speaker and no login configured.)
2. Open the UI from the **Qobuz** entry in the HA sidebar (or the **Open Web UI** button).
3. Click **Log in to Qobuz**. Because Qobuz (and providers like Google on its sign-in page)
   refuse to be shown inside Home Assistant's panel, login **opens in a new browser tab** that
   talks to the add-on directly at `http://<your-ha-host>:8689` — allow pop-ups for Home
   Assistant if your browser blocks it. Complete the login there; this panel switches to
   "connected" automatically within a few seconds, and you can close the tab. The token is
   stored in the add-on's `/data` and persists across restarts — you only do this once.

   > **Do the one-time login from your local network.** It needs to reach `<ha-host>:8689`,
   > which works on the LAN (e.g. `homeassistant.local` or the HA IP) but **not** through a
   > Nabu Casa remote URL (port 8689 isn't proxied remotely).
4. Either set `dlna_ip` in the **Configuration** tab (see below) or add a speaker from the
   Web UI (it can discover renderers on your network).
5. Open the Qobuz app on your phone/desktop, pick the device from the Connect menu, and play.

## Updating

Home Assistant updates this add-on the same way as any other — there is **no "update" option in
the add-on configuration**, and you should **not** uninstall/reinstall (that deletes `/data`,
losing your Qobuz login and saved speakers).

- When a newer version is published, an **Update** button appears on the add-on page. Click it.
  Your login and speakers are preserved (they live in `/data`, which survives updates).
- If you don't see the button yet, force a refresh:
  **Settings → Add-ons → Add-on Store → ⋮ → Check for updates**, then reopen the add-on page.

The Update button only appears once a higher `version` (with a matching published image) is
available in the repository — so a new release has to be pushed first.

## Options

```yaml
device_name: QobuzProxy
dlna_ip: ""
dlna_port: 1400
dlna_fixed_volume: false
max_quality: auto
log_level: info
```

| Option | Description |
| ------ | ----------- |
| `device_name` | Name shown in the Qobuz app's Connect device list. |
| `dlna_ip` | IP of your DLNA renderer. **Optional** — if set, that renderer is configured automatically on start. If left **blank** (the default), the add-on starts unconfigured so you can add a speaker from the Web UI. Note: once you add a speaker in the Web UI it is saved to `/data`, and that saved speaker takes priority over this option on later restarts. |
| `dlna_port` | DLNA control port (Sonos uses `1400`). |
| `dlna_fixed_volume` | `true` to ignore Qobuz volume commands (use when an external amp controls volume). |
| `max_quality` | `auto` (detect from the renderer), or one of `5` (MP3), `6` (CD), `7` (Hi-Res 96k), `27` (Hi-Res 192k). |
| `log_level` | `debug`, `info`, `warning`, or `error`. |

## Notes

- Credentials and any speaker configuration are persisted under the add-on's `/data` directory,
  so they survive restarts and updates.
- This add-on supports the **DLNA backend only**. Local PortAudio output is not included.
