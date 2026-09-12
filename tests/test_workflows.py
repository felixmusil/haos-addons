"""Contract tests between each add-on's config.yaml and the release/lint/sync workflows.

The Supervisor installs ``{config.yaml image}:{config.yaml version}``; nothing else in CI checks
that the workflows push exactly that string for every add-on. Run from the repo root with
``python3 -m pytest tests -q`` (needs pytest + PyYAML; bash + jq on PATH).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
TTS_SERVER = Path(os.environ.get("TTS_SERVER_DIR", ROOT.parent / "tts-server"))


def _addons() -> list[str]:
    return sorted(d.name for d in ROOT.iterdir() if (d / "config.yaml").is_file())


def _config(addon: str) -> dict[str, Any]:
    data = yaml.safe_load((ROOT / addon / "config.yaml").read_text())
    assert isinstance(data, dict)
    return data


def _workflow(name: str) -> dict[Any, Any]:
    data = yaml.safe_load((WORKFLOWS / name).read_text())
    assert isinstance(data, dict)
    return data


def _triggers(wf: dict[Any, Any]) -> dict[str, Any]:
    # PyYAML 1.1 reads the bare `on:` key as boolean True.
    return wf.get("on") or wf[True]


def _step(wf: dict[str, Any], job: str, name: str) -> dict[str, Any]:
    for step in wf["jobs"][job]["steps"]:
        if step.get("name") == name:
            return step
    raise AssertionError(f"step {name!r} missing from job {job!r}")


def _run_step(
    script: str, env: dict[str, str], cwd: Path, tmp_path: Path
) -> tuple[int, dict[str, str], str]:
    """Run a workflow ``run:`` script under bash the way Actions does; return (rc, outputs, log)."""
    out_file = tmp_path / f"gh-output-{abs(hash(script))}"
    out_file.write_text("")
    full_env = {
        "PATH": env.pop("PATH", os.environ["PATH"]),
        "HOME": str(tmp_path),
        "GITHUB_OUTPUT": str(out_file),
        **env,
    }
    proc = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
        cwd=cwd,
        env=full_env,
        capture_output=True,
        text=True,
    )
    outputs: dict[str, str] = {}
    for line in out_file.read_text().splitlines():
        key, _, value = line.partition("=")
        outputs[key] = value
    return proc.returncode, outputs, proc.stdout + proc.stderr


def _engine_literal_from_tts_server() -> set[str] | None:
    config_py = TTS_SERVER / "src" / "tts_server" / "config.py"
    if not config_py.is_file():
        return None
    match = re.search(r"EngineName\s*=\s*Literal\[([^\]]+)\]", config_py.read_text())
    assert match, "EngineName Literal not found in tts_server/config.py"
    return set(re.findall(r'"([a-z0-9_]+)"', match.group(1)))


def test_every_addon_directory_is_covered_by_lint_and_build_matrices_and_image_names_match_config(
    tmp_path: Path,
) -> None:
    # WHY: an add-on missing from the lint matrix ships an invalid config unnoticed; an image name
    # or version in build.yml that differs from config.yaml can never be installed (Supervisor
    # pulls `image:version`), and nothing turns red. Discover add-ons from the filesystem so the
    # next one is covered automatically.
    addons = _addons()
    assert addons == ["qobuz-proxy", "tts-coordinator"]

    lint = _workflow("lint.yml")
    linter_job = lint["jobs"]["addon-linter"]
    matrix_addons = linter_job["strategy"]["matrix"]["addon"]
    linter_step = _step(lint, "addon-linter", "Run Home Assistant Add-on Lint")
    assert linter_step["uses"].startswith("frenck/action-addon-linter@")
    path_template = linter_step["with"]["path"]
    resolved = {path_template.replace("${{ matrix.addon }}", a) for a in matrix_addons}
    assert resolved == {f"./{a}" for a in addons}

    build = _workflow("build.yml")
    select = _step(build, "plan", "Select add-ons")
    rc, outputs, log = _run_step(
        select["run"],
        {"GITHUB_EVENT_NAME": "workflow_dispatch", "INPUT_ADDON": "all"},
        ROOT,
        tmp_path,
    )
    assert rc == 0, log
    assert sorted(yaml.safe_load(outputs["matrix"])) == addons

    compute = _step(build, "build", "Compute version")
    push_step = next(
        s
        for s in build["jobs"]["build"]["steps"]
        if str(s.get("uses", "")).startswith("docker/build-push-action@")
    )
    assert push_step["with"]["context"] == "./${{ matrix.addon }}"
    tag_lines = [t.strip() for t in push_step["with"]["tags"].splitlines() if t.strip()]
    for addon in addons:
        cfg = _config(addon)
        rc, outputs, log = _run_step(compute["run"], {"ADDON": addon}, ROOT, tmp_path)
        assert rc == 0, log
        assert outputs["image"] == cfg["image"]
        assert outputs["version"] == str(cfg["version"])
        pushed = {
            t.replace("${{ steps.meta.outputs.image }}", outputs["image"]).replace(
                "${{ steps.meta.outputs.version }}", outputs["version"]
            )
            for t in tag_lines
        }
        assert f"{cfg['image']}:{cfg['version']}" in pushed, pushed

    cfg = _config("tts-coordinator")
    assert set(cfg["options"]) == set(cfg["schema"])
    for secret in ("abs_token", "wuxiaworld_token", "api_token"):
        assert cfg["schema"][secret].rstrip("?") == "password", secret
    engines = cfg["schema"]["default_engine"]
    assert engines.startswith("list(") and engines.endswith(")")
    offered = set(engines[len("list(") : -1].split("|"))
    assert offered == {"fake", "kokoro", "piper", "kyutai", "f5"}
    real = _engine_literal_from_tts_server()
    if real is not None:
        # A name outside Settings.engine's Literal makes the coordinator crash at boot.
        assert offered <= real, offered - real
    assert cfg["options"]["default_engine"] in offered
    assert cfg["ingress"] is True
    assert cfg["ingress_port"] == 8880
    assert "8880/tcp" in cfg["ports"]
    assert cfg["init"] is False


def test_pushed_image_version_tag_equals_each_addons_config_version(tmp_path: Path) -> None:
    # WHY: two independently versioned add-ons cannot share one `v*` namespace with
    # `version=${GITHUB_REF_NAME#v}` — pushing v1.4.0 would publish tts-coordinator:1.4.0 while
    # its config says 0.1.0 and the Supervisor pulls :0.1.0 forever. The chosen design: the tag
    # names the add-on (`<addon>-vX.Y.Z`, legacy `vX.Y.Z` = qobuz-proxy), the version pushed is
    # always config.yaml's, and a tag that disagrees with config.yaml fails before any push.
    build = _workflow("build.yml")
    patterns = _triggers(build)["push"]["tags"]
    assert "v*" in patterns and "*-v*" in patterns
    select = _step(build, "plan", "Select add-ons")["run"]

    def plan(tag: str) -> tuple[int, list[str] | None, str]:
        rc, outputs, log = _run_step(
            select, {"GITHUB_EVENT_NAME": "push", "GITHUB_REF_NAME": tag}, ROOT, tmp_path
        )
        matrix = yaml.safe_load(outputs["matrix"]) if "matrix" in outputs else None
        return rc, matrix, log

    for addon in _addons():
        version = str(_config(addon)["version"])
        rc, matrix, log = plan(f"{addon}-v{version}")
        assert rc == 0, log
        assert matrix == [addon], f"tag for {addon} must build only {addon}: {matrix}"

    qobuz_version = str(_config("qobuz-proxy")["version"])
    rc, matrix, log = plan(f"v{qobuz_version}")
    assert rc == 0 and matrix == ["qobuz-proxy"], log

    # A tag whose version disagrees with config.yaml must refuse to build anything.
    for bad in ("tts-coordinator-v9.9.9", "v9.9.9", "nonsense"):
        rc, matrix, log = plan(bad)
        assert rc != 0 and not matrix, (bad, log)


def test_sync_upstream_bump_applies_to_tts_coordinator_and_pins_a_tag_tts_server_actually_pushes(
    tmp_path: Path,
) -> None:
    # WHY: the bump script rewrites three files by regex; a CHANGELOG table of a different shape
    # makes it "succeed" and insert nothing, and a BUILD_FROM pin (`:vX.Y.Z`) that the upstream
    # release never pushes makes the next add-on build unpullable. A missing upstream release
    # (tts-server had none when this was written) must be "nothing to sync", not a job failure.
    sync = _workflow("sync-upstream.yml")
    includes = sync["jobs"]["sync"]["strategy"]["matrix"]["include"]
    by_addon = {row["addon"]: row["upstream"] for row in includes}
    assert set(by_addon) == set(_addons())
    assert by_addon["tts-coordinator"] == "tts-server"

    work = tmp_path / "repo"
    shutil.copytree(ROOT / "tts-coordinator", work / "tts-coordinator")
    before = (work / "tts-coordinator" / "CHANGELOG.md").read_text()
    env = {"V": "0.2.0", "ADDON": "tts-coordinator", "UPSTREAM": "tts-server"}
    rc, _, log = _run_step(_step(sync, "sync", "Apply bump")["run"], env, work, tmp_path)
    assert rc == 0, log

    cfg = yaml.safe_load((work / "tts-coordinator" / "config.yaml").read_text())
    assert cfg["version"] == "0.2.0"
    dockerfile = (work / "tts-coordinator" / "Dockerfile").read_text().splitlines()
    froms = [ln for ln in dockerfile if ln.startswith("ARG BUILD_FROM=")]
    assert froms == ["ARG BUILD_FROM=ghcr.io/felixmusil/tts-server:v0.2.0"]
    after = (work / "tts-coordinator" / "CHANGELOG.md").read_text()
    sep = re.search(r"\| -+ \| -+ \|\n", after)
    assert sep, "CHANGELOG table separator row missing"
    assert after[sep.end() :].startswith("| 0.2.0 "), after[sep.end() : sep.end() + 40]
    headings = re.findall(r"(?m)^## (.+)$", after)
    assert headings[:2] == ["0.2.0", "0.1.0"]
    assert len(headings) == len(re.findall(r"(?m)^## ", before)) + 1
    assert "_Bundles `tts-server` v0.2.0._" in after

    if (TTS_SERVER / ".github" / "workflows" / "build.yml").is_file():
        upstream = yaml.safe_load((TTS_SERVER / ".github" / "workflows" / "build.yml").read_text())
        push_step = next(
            s
            for s in upstream["jobs"]["docker"]["steps"]
            if str(s.get("uses", "")).startswith("docker/build-push-action@")
        )
        tags = {t.strip() for t in push_step["with"]["tags"].splitlines() if t.strip()}
        assert "ghcr.io/${{ github.repository_owner }}/tts-server:${{ github.ref_name }}" in tags
        assert "github-release" in upstream["jobs"], "sync discovers versions via gh release view"
    else:
        pytest.skip(f"sibling tts-server checkout not found at {TTS_SERVER}")

    # Resolve step with a `gh` that has no release to report.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text("#!/usr/bin/env bash\necho 'release not found' >&2\nexit 1\n")
    gh.chmod(0o755)
    resolve = _step(sync, "sync", "Resolve versions")["run"]
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "GH_TOKEN": "x",
        "ADDON": "tts-coordinator",
        "UPSTREAM": "tts-server",
    }
    rc, outputs, log = _run_step(resolve, env, ROOT, tmp_path)
    assert rc == 0, log
    assert outputs["changed"] == "false"

    gh.write_text("#!/usr/bin/env bash\necho v0.5.0\n")
    env["PATH"] = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
    rc, outputs, log = _run_step(resolve, env, ROOT, tmp_path)
    assert rc == 0, log
    assert outputs["changed"] == "true"
    assert outputs["latest"] == "0.5.0"
    assert outputs["release_tag"] == "tts-coordinator-v0.5.0"
