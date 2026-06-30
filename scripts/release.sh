#!/usr/bin/env bash
#
# Cut an add-on release that tracks qobuz-proxy.
#
# Reads the latest *published* release version of the qobuz-proxy repo, bumps the
# add-on's qobuz-proxy/config.yaml `version` to match, commits the bump, then tags
# the commit as `v<version>` and pushes it — which triggers
# .github/workflows/build.yml (on: push: tags: v*) to build and publish the
# multi-arch add-on image — and creates the matching GitHub release.
#
# The add-on bundles `ghcr.io/felixmusil/qobuz-proxy:latest`, so versioning it off
# the latest *published* qobuz-proxy release keeps the add-on version aligned with
# the image it actually ships. Release qobuz-proxy first, then run this.
#
# Idempotent: an existing matching config version / tag / release is reused.
#
# Usage:  scripts/release.sh
#   QP_REPO   qobuz-proxy GitHub repo (default: felixmusil/qobuz-proxy)
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

QP_REPO="${QP_REPO:-felixmusil/qobuz-proxy}"
CONFIG="qobuz-proxy/config.yaml"

# 1. Latest published qobuz-proxy release version (source of truth).
qp_tag="$(gh release view --repo "${QP_REPO}" --json tagName -q .tagName)"
if [ -z "${qp_tag}" ]; then
  echo "ERROR: could not read latest release tag from ${QP_REPO}" >&2
  exit 1
fi
version="${qp_tag#v}"
tag="v${version}"
echo "qobuz-proxy latest release: ${qp_tag}  ->  add-on tag ${tag}"

# 2. Refuse to start from a dirty tree (so the bump commit is isolated).
if [ -n "$(git status --porcelain)" ]; then
  echo "ERROR: working tree is dirty — commit or stash changes before releasing." >&2
  exit 1
fi

# 3. Bump the add-on config version to match (commit only if it changed).
current="$(grep -m1 -E '^version:' "${CONFIG}" | sed -E 's/^version: *"?([^"]+)"?.*/\1/')"
if [ "${current}" != "${version}" ]; then
  sed -i.bak -E "s/^version:.*/version: \"${version}\"/" "${CONFIG}" && rm -f "${CONFIG}.bak"
  git add "${CONFIG}"
  git commit -m "release: add-on v${version} (bundles qobuz-proxy ${version})"
  echo "Bumped ${CONFIG} ${current} -> ${version}"
  git push origin HEAD
else
  echo "${CONFIG} already at ${version}."
fi

# 4. Create the annotated tag at HEAD if it doesn't exist yet, then push it.
if git rev-parse -q --verify "refs/tags/${tag}" >/dev/null; then
  existing="$(git rev-parse --short "${tag}^{commit}")"
  head="$(git rev-parse --short HEAD)"
  if [ "${existing}" != "${head}" ]; then
    echo "NOTE: tag ${tag} already exists at ${existing} (HEAD is ${head}); using the existing tag." >&2
  else
    echo "Tag ${tag} already exists at HEAD (${head})."
  fi
else
  git tag -a "${tag}" -m "Add-on ${tag} (bundles qobuz-proxy ${version})"
  echo "Created tag ${tag} at $(git rev-parse --short HEAD)."
fi
git push origin "refs/tags/${tag}"

# 5. Publish the GitHub release (skip if it already exists).
if gh release view "${tag}" >/dev/null 2>&1; then
  echo "GitHub release ${tag} already exists — leaving it as-is."
else
  gh release create "${tag}" --verify-tag --title "${tag}" \
    --notes "Add-on ${tag} — bundles qobuz-proxy ${version}."
  echo "Published GitHub release ${tag}."
fi
