# Felix's Home Assistant Add-ons

A small collection of [Home Assistant](https://www.home-assistant.io/) add-ons.

## Installation

**Quick add** — click to open Home Assistant and add this repository:

[![Open your Home Assistant instance and show the add add-on repository dialog with this repository URL pre-filled.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Ffelixmusil%2Fhaos-addons)

Or add it manually:

1. In Home Assistant, go to **Settings → Add-ons → Add-on Store**.
2. Click the **⋮** menu (top-right) → **Repositories**.
3. Add this URL:

   ```
   https://github.com/felixmusil/haos-addons
   ```

4. The add-ons below will appear in the store.

## Add-ons

| Add-on | Description |
| ------ | ----------- |
| [Qobuz Proxy](./qobuz-proxy) | Headless Qobuz Connect player that bridges to a DLNA renderer (Sonos, HEOS, …). |
| [TTS Coordinator](./tts-coordinator) | WuxiaWorld → per-chapter Audiobookshelf audiobooks: queue + web UI here, synthesis on pull-based `tts-worker` machines (laptop over Tailscale). |
| [Transmission (NordVPN)](./transmission-nordvpn) | Transmission 4 BitTorrent client that only talks to the internet through NordVPN (OpenVPN, fail-closed); Flood UI and an "Add from AudioBookBay" page in the sidebar, downloads into an Audiobookshelf library folder. |

## Architectures

Add-ons here target **`aarch64`** (64-bit ARM, e.g. Raspberry Pi 4 on a 64-bit OS) and
**`amd64`**. 32-bit ARM is not supported.

## Development & testing

A typical loop, fastest to most realistic. The first three steps don't need Home Assistant.

### 1. Lint the add-on config

Every add-on's `config.yaml` is validated automatically on every push/PR by
[`.github/workflows/lint.yml`](.github/workflows/lint.yml) (a matrix over the add-on directories
running the [`frenck/action-addon-linter`](https://github.com/frenck/action-addon-linter)). The
same workflow also runs the repo's own tests: each add-on's `tests/run_sh_test.sh` drives its
`run.sh` through fake binaries (options.json → env → exec), `transmission-nordvpn/tests/*_test.py`
exercises the AudioBookBay helper against loopback fakes, and `tests/test_workflows.py` checks
that the release workflows push exactly the `image:version` each `config.yaml` declares.

To run the same linter locally (swap the add-on directory as needed):

```bash
docker run --rm -e INPUT_PATH=/addon -e INPUT_COMMUNITY=false -v "$PWD/qobuz-proxy":/addon \
  $(docker build -q https://github.com/frenck/action-addon-linter.git#v2:src)
```

Quick checks without Docker:

```bash
python3 -c "import yaml; yaml.safe_load(open('tts-coordinator/config.yaml')); print('OK')"
bash tts-coordinator/tests/run_sh_test.sh            # needs jq
bash transmission-nordvpn/tests/run_sh_test.sh       # needs jq
python3 -m unittest discover -s transmission-nordvpn/tests -p '*_test.py'
python3 -m pytest tests -q                           # needs pytest + pyyaml
```

### 2. Run the add-on image directly (fast inner loop)

Each add-on is just a container that reads its user options from `/data/options.json`. You can
exercise the real image without Home Assistant — this validates the `run.sh` option→env mapping
and that the app boots. (It does **not** test ingress, the sidebar panel, or the config/log
tabs — those need the Supervisor; see step 3.)

```bash
cd qobuz-proxy

# Build the thin add-on image from its Dockerfile.
docker buildx build --build-arg BUILD_FROM=ghcr.io/felixmusil/qobuz-proxy:latest -t addon-test .

# Provide the options Home Assistant would normally write, then run with host networking.
mkdir -p data
cat > data/options.json <<'EOF'
{"device_name":"QobuzProxy","dlna_ip":"","dlna_port":1400,"dlna_fixed_volume":false,"max_quality":"auto","log_level":"info"}
EOF
docker run --rm --network host -v "$PWD/data":/data addon-test
# In another shell: curl http://localhost:8689/api/status
```

If the `FROM` image is private, run `docker login ghcr.io` first.

### 3. Run a real Supervisor in the devcontainer

This repo ships a [`.devcontainer.json`](.devcontainer.json) (the official Home Assistant
add-ons devcontainer). It's the only way to properly test **ingress**, the **sidebar panel**,
and the **Configuration / Log tabs**.

1. Open the repo in VS Code → **Reopen in Container** (requires Docker + the Dev Containers
   extension).
2. Run the **Start Home Assistant** task (or `supervisor_run` in the terminal).
3. Open Home Assistant at <http://localhost:7123>. This repo is auto-mounted as a local add-on
   store, so the add-ons appear under **Settings → Add-ons** and you can install/start them.

> **Note:** because each add-on sets `image:` (pre-built strategy), the Supervisor will *pull*
> the published image rather than build your local `Dockerfile`. To test **local** Dockerfile /
> `run.sh` changes, temporarily comment out the `image:` line in the add-on's `config.yaml` so
> the Supervisor builds from source.

### 4. Install on a real Home Assistant instance

The final, fully faithful test — and the only place mDNS discovery, DLNA control, and the audio
proxy are genuinely exercised (host networking can't be emulated in the devcontainer). Push to
GitHub, add this repo URL under **Settings → Add-ons → Add-on Store → ⋮ → Repositories**
(see [Installation](#installation)), then install and start the add-on.

Because of the pre-built-image strategy, the add-on image must be published to GHCR **before**
Home Assistant can install it, and its tag must match the `version` in `config.yaml` (the
build workflow reads the version from `config.yaml`, so `version: "1.3.8"` → image `:1.3.8`).

### Releasing

The add-ons are versioned independently, so a release tag names the add-on it publishes:

| Tag | Builds |
| --- | ------ |
| `tts-coordinator-v0.1.0` | `tts-coordinator` |
| `qobuz-proxy-v1.4.0` | `qobuz-proxy` |
| `transmission-nordvpn-v5.5.2.0` | `transmission-nordvpn` |
| `v1.4.0` | `qobuz-proxy` (legacy namespace, used by `scripts/release.sh`) |

Pushing such a tag triggers [`.github/workflows/build.yml`](.github/workflows/build.yml), which
builds and pushes the multi-arch (`amd64` + `arm64`) image for that add-on only, tagged with the
`image:version` from its `config.yaml`. The run fails before pushing anything if the tag's
version does not equal the `config.yaml` version, so bump `config.yaml` and `CHANGELOG.md`
first. [`sync-upstream.yml`](.github/workflows/sync-upstream.yml) opens that bump PR
automatically (hourly) whenever the upstream app (`qobuz-proxy`, `tts-server`) publishes a
GitHub release; its body names the tag to push after merging.

The **Update** button on a user's add-on page only appears once the higher `version` *and* its
matching `:<version>` image are published — i.e. after this workflow has run. Users then click
**Update** (or **Add-on Store → ⋮ → Check for updates** to refresh); no uninstall/reinstall is
needed and `/data` is preserved.
