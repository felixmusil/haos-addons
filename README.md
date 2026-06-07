# Felix's Home Assistant Add-ons

A small collection of [Home Assistant](https://www.home-assistant.io/) add-ons.

## Installation

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

## Architectures

Add-ons here target **`aarch64`** (64-bit ARM, e.g. Raspberry Pi 4 on a 64-bit OS) and
**`amd64`**. 32-bit ARM is not supported.

## Development & testing

A typical loop, fastest to most realistic. The first three steps don't need Home Assistant.

### 1. Lint the add-on config

`config.yaml` is validated automatically on every push/PR by
[`.github/workflows/lint.yml`](.github/workflows/lint.yml) (the
[`frenck/action-addon-linter`](https://github.com/frenck/action-addon-linter)).

To run the same linter locally:

```bash
docker run --rm -e INPUT_PATH=/addon -v "$PWD/qobuz-proxy":/addon \
  $(docker build -q https://github.com/frenck/action-addon-linter.git#v2:src)
```

A quick YAML sanity check without Docker:

```bash
python3 -c "import yaml; yaml.safe_load(open('qobuz-proxy/config.yaml')); print('OK')"
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
release workflow strips the leading `v`, so tag `v1.3.8` → image `:1.3.8` → `version: "1.3.8"`).

### Releasing

Pushing a `v*` tag triggers [`.github/workflows/build.yml`](.github/workflows/build.yml), which
builds and pushes the multi-arch (`amd64` + `arm64`) add-on image to GHCR. Bump the add-on's
`config.yaml` `version` and `CHANGELOG.md` to match the tag.

The **Update** button on a user's add-on page only appears once the higher `version` *and* its
matching `:<version>` image are published — i.e. after this workflow has run. Users then click
**Update** (or **Add-on Store → ⋮ → Check for updates** to refresh); no uninstall/reinstall is
needed and `/data` is preserved.
