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


def _step_env(step: dict[str, Any], row: dict[str, Any], outputs: dict[str, str]) -> dict[str, str]:
    """Resolve a step's ``env:`` block the way Actions does for one matrix row.

    ``${{ matrix.X }}`` → the row's value ("" when the row lacks the key — Actions passes a missing
    matrix key as an empty string, which `set -u` scripts rely on), ``${{ steps.v.outputs.X }}`` →
    the resolve step's recorded outputs, secrets → "x". Tests thus exercise the real resolve→apply
    wiring instead of hardcoding whatever variable names the step happens to use.
    """
    env: dict[str, str] = {}
    for key, raw in step.get("env", {}).items():
        value = re.sub(r"\$\{\{\s*matrix\.(\w+)\s*\}\}", lambda m: str(row.get(m[1], "")), str(raw))
        value = re.sub(r"\$\{\{\s*steps\.v\.outputs\.(\w+)\s*\}\}", lambda m: outputs[m[1]], value)
        value = re.sub(r"\$\{\{\s*secrets\.\w+\s*\}\}", "x", value)
        # An unresolved expression means this model of the wiring is stale — fail loudly rather
        # than hand an expression string to bash.
        assert "${{" not in value, (key, raw)
        env[key] = value
    return env


def _sync_rows() -> dict[str, dict[str, Any]]:
    sync = _workflow("sync-upstream.yml")
    return {row["addon"]: row for row in sync["jobs"]["sync"]["strategy"]["matrix"]["include"]}


def _fake_gh(tmp_path: Path) -> Path:
    """A `gh` that records its argv to $HOME/gh-args and prints $FAKE_TAG (fails when empty)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        'printf \'%s\\n\' "$*" > "$HOME/gh-args"\n'
        "if [ -z \"${FAKE_TAG:-}\" ]; then echo 'release not found' >&2; exit 1; fi\n"
        'echo "$FAKE_TAG"\n'
    )
    gh.chmod(0o755)
    return bin_dir


def _resolve(
    tmp_path: Path, row: dict[str, Any], fake_tag: str, cwd: Path = ROOT
) -> tuple[int, dict[str, str], str, str]:
    """Run the sync 'Resolve versions' step for one matrix row; return (rc, outputs, log, gh argv)."""
    sync = _workflow("sync-upstream.yml")
    step = _step(sync, "sync", "Resolve versions")
    env = _step_env(step, row, {})
    env["PATH"] = f"{_fake_gh(tmp_path)}{os.pathsep}{os.environ['PATH']}"
    env["FAKE_TAG"] = fake_tag
    rc, outputs, log = _run_step(step["run"], env, cwd, tmp_path)
    gh_args_file = tmp_path / "gh-args"
    gh_args = gh_args_file.read_text().strip() if gh_args_file.is_file() else ""
    return rc, outputs, log, gh_args


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
    assert addons == ["qobuz-proxy", "transmission-nordvpn", "tts-coordinator"]

    lint = _workflow("lint.yml")
    linter_job = lint["jobs"]["addon-linter"]
    matrix_addons = linter_job["strategy"]["matrix"]["addon"]
    linter_step = _step(lint, "addon-linter", "Run Home Assistant Add-on Lint")
    assert linter_step["uses"].startswith("frenck/action-addon-linter@")
    path_template = linter_step["with"]["path"]
    resolved = {path_template.replace("${{ matrix.addon }}", a) for a in matrix_addons}
    assert resolved == {f"./{a}" for a in addons}

    build = _workflow("build.yml")
    # The workflow_dispatch choice list is the one part of build.yml that does not auto-discover
    # add-ons: an add-on missing from it can never be built by hand and nothing turns red.
    options = _triggers(build)["workflow_dispatch"]["inputs"]["addon"]["options"]
    assert options[0] == "all"
    assert set(options) == {"all", *addons}, options
    select = _step(build, "plan", "Select add-ons")
    rc, outputs, log = _run_step(
        select["run"],
        {"GITHUB_EVENT_NAME": "workflow_dispatch", "INPUT_ADDON": "all"},
        ROOT,
        tmp_path,
    )
    assert rc == 0, log
    assert sorted(yaml.safe_load(outputs["matrix"])) == addons
    rc, outputs, log = _run_step(
        select["run"],
        {"GITHUB_EVENT_NAME": "workflow_dispatch", "INPUT_ADDON": "transmission-nordvpn"},
        ROOT,
        tmp_path,
    )
    assert rc == 0, log
    assert yaml.safe_load(outputs["matrix"]) == ["transmission-nordvpn"]

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
        assert isinstance(cfg["version"], str), f"{addon}: version must be quoted in config.yaml"
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
    rows = _sync_rows()
    assert set(rows) == set(_addons())
    assert rows["tts-coordinator"]["upstream"] == "tts-server"

    work = tmp_path / "repo"
    shutil.copytree(ROOT / "tts-coordinator", work / "tts-coordinator")
    before = (work / "tts-coordinator" / "CHANGELOG.md").read_text()
    # Feed Apply bump exactly what the resolve step would have produced for a 0.2.0 release, through
    # the step's own `env:` wiring (a hardcoded env would keep passing after the step is rewired).
    rc, resolved, log, _ = _resolve(tmp_path, rows["tts-coordinator"], "v0.2.0")
    assert rc == 0 and resolved["changed"] == "true", log
    apply = _step(sync, "sync", "Apply bump")
    env = _step_env(apply, rows["tts-coordinator"], resolved)
    rc, _, log = _run_step(apply["run"], env, work, tmp_path)
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
    rc, outputs, log, _ = _resolve(tmp_path, rows["tts-coordinator"], "")
    assert rc == 0, log
    assert outputs["changed"] == "false"

    rc, outputs, log, gh_args = _resolve(tmp_path, rows["tts-coordinator"], "v0.5.0")
    assert rc == 0, log
    assert outputs["changed"] == "true"
    assert outputs["latest"] == "0.5.0"
    assert outputs["release_tag"] == "tts-coordinator-v0.5.0"
    assert "--repo felixmusil/tts-server" in gh_args, gh_args


def test_lint_executes_every_test_suite_an_addon_ships() -> None:
    # WHY: a test file that exists on disk but is never invoked by CI is "green but proves
    # nothing" one step earlier. Pin the two commands the transmission-nordvpn add-on requires,
    # then check both directions: every shipped suite is run, and every script lint.yml runs
    # exists (a renamed test script would otherwise fail only on GitHub).
    lint = _workflow("lint.yml")
    joined = "\n".join(str(s.get("run", "")) for s in lint["jobs"]["tests"]["steps"])
    assert re.search(r"(?m)^\s*bash transmission-nordvpn/tests/run_sh_test\.sh\s*$", joined), joined
    assert "python3 -m unittest discover -s transmission-nordvpn/tests -p '*_test.py'" in joined
    assert "bash tts-coordinator/tests/run_sh_test.sh" in joined
    assert re.search(r"python3 -m pytest tests", joined)

    for addon in _addons():
        tests_dir = ROOT / addon / "tests"
        if (tests_dir / "run_sh_test.sh").is_file():
            assert f"bash {addon}/tests/run_sh_test.sh" in joined, addon
        if list(tests_dir.glob("*_test.py")):
            assert f"-s {addon}/tests" in joined, addon
    for path in re.findall(r"bash (\S+/tests/run_sh_test\.sh)", joined):
        assert (ROOT / path).is_file(), path


def test_sync_matrix_row_for_transmission_pins_haugene_repo_and_wrapper_version_suffix() -> None:
    # WHY: this row's upstream is NOT under felixmusil/ and the add-on version scheme is
    # `<upstream>.<wrapper revision>`; an unquoted `version_suffix: .0` parses as the float 0.0 and
    # Actions would render "5.5.30". The base-version ↔ Dockerfile-tag invariant is what makes the
    # CHANGELOG's "bundles 5.5.2" claim true.
    rows = _sync_rows()
    assert set(rows) == set(_addons())
    assert rows["transmission-nordvpn"] == {
        "addon": "transmission-nordvpn",
        "upstream": "transmission-openvpn",
        "repo": "haugene/docker-transmission-openvpn",
        "version_suffix": ".0",
    }
    for addon in ("qobuz-proxy", "tts-coordinator"):
        row = rows[addon]
        assert row.get("repo", f"felixmusil/{row['upstream']}") == f"felixmusil/{row['upstream']}"
        assert row.get("version_suffix", "") == ""

    cfg = _config("transmission-nordvpn")
    version = str(cfg["version"])
    assert re.fullmatch(r"\d+\.\d+\.\d+\.\d+", version), version
    base = version.rsplit(".", 1)[0]
    dockerfile = (ROOT / "transmission-nordvpn" / "Dockerfile").read_text().splitlines()
    froms = [ln for ln in dockerfile if ln.startswith("ARG BUILD_FROM=")]
    assert froms == [f"ARG BUILD_FROM=haugene/transmission-openvpn:{base}"]
    assert cfg["image"] == "ghcr.io/felixmusil/transmission-nordvpn-haos-addon"


def test_sync_resolve_maps_upstream_latest_onto_wrapper_version_with_suffix(tmp_path: Path) -> None:
    # WHY: a naive compare of 5.5.3 against 5.5.2.0 without stripping the wrapper revision happens
    # to get `changed` right but emits the wrong new version / release tag, and a wrapper-only bump
    # (5.5.2.3) must still read as "same upstream". The recorded gh argv proves `repo` is what gh
    # is asked about.
    row = _sync_rows()["transmission-nordvpn"]

    for tag in ("5.5.3", "v5.5.3"):  # haugene tags carry no `v`; the `#v` strip must stay harmless
        rc, out, log, gh_args = _resolve(tmp_path, row, tag)
        assert rc == 0, log
        assert out["changed"] == "true", (tag, out)
        assert out["latest"] == "5.5.3"
        assert out["current"] == "5.5.2.0"
        assert out["version"] == "5.5.3.0"
        assert out["release_tag"] == "transmission-nordvpn-v5.5.3.0"
        assert "--repo haugene/docker-transmission-openvpn" in gh_args, gh_args
        assert "felixmusil/" not in gh_args

    for tag in ("5.5.2", "5.5.1"):  # same upstream / downgrade → nothing to sync
        rc, out, log, _ = _resolve(tmp_path, row, tag)
        assert rc == 0, log
        assert out["changed"] == "false", (tag, out)

    rc, out, log, _ = _resolve(tmp_path, row, "")  # no release / gh failure is not a job failure
    assert rc == 0, log
    assert out["changed"] == "false"

    # Wrapper revision 3 of the same upstream: unchanged for 5.5.2, and a real upstream bump
    # resets the revision to 0 (not 5.5.3.3).
    work = tmp_path / "rev3"
    shutil.copytree(ROOT / "transmission-nordvpn", work / "transmission-nordvpn")
    cfg_path = work / "transmission-nordvpn" / "config.yaml"
    cfg_path.write_text(
        re.sub(r'(?m)^version: "5\.5\.2\.0"', 'version: "5.5.2.3"', cfg_path.read_text(), count=1)
    )
    assert yaml.safe_load(cfg_path.read_text())["version"] == "5.5.2.3"
    rc, out, log, _ = _resolve(tmp_path, row, "5.5.2", cwd=work)
    assert rc == 0 and out["changed"] == "false", (out, log)
    rc, out, log, _ = _resolve(tmp_path, row, "5.5.3", cwd=work)
    assert rc == 0 and out["changed"] == "true", (out, log)
    assert out["current"] == "5.5.2.3"
    assert out["version"] == "5.5.3.0"


def test_sync_resolve_for_existing_rows_is_unchanged_by_generalisation(tmp_path: Path) -> None:
    # WHY: a row without `repo`/`version_suffix` must still ask gh about felixmusil/<upstream>, must
    # not get "" or ".0" appended to its version, and must not trip `set -u` on the now-optional
    # matrix keys (Actions passes a missing key as an empty string, which _step_env mirrors).
    row = _sync_rows()["qobuz-proxy"]
    assert "repo" not in row and "version_suffix" not in row
    current = str(_config("qobuz-proxy")["version"])

    rc, out, log, gh_args = _resolve(tmp_path, row, "v1.5.0")
    assert rc == 0, log
    assert out["changed"] == "true", out
    assert out["latest"] == "1.5.0"
    assert out["current"] == current
    assert out["version"] == "1.5.0"
    assert out["release_tag"] == "qobuz-proxy-v1.5.0"
    assert "--repo felixmusil/qobuz-proxy" in gh_args, gh_args

    rc, out, log, _ = _resolve(tmp_path, row, f"v{current}")
    assert rc == 0, log
    assert out["changed"] == "false", out


def test_sync_to_build_e2e_transmission_release_flows_from_gh_to_supervisor_pull_tag(
    tmp_path: Path,
) -> None:
    # WHY: the seam nobody tests otherwise — resolve outputs feed Apply bump via
    # `${{ steps.v.outputs.* }}`, the PR's suggested release tag feeds build.yml's tag==config
    # check, and build.yml's Compute version feeds the pushed image tag. If release_tag lacks the
    # ".0", or Apply bump writes 5.5.3 into config.yaml while the Dockerfile gets 5.5.3.0 (or
    # `:v5.5.3`, which haugene never pushes), every step still exits 0 and the add-on becomes
    # unpullable after merge.
    sync = _workflow("sync-upstream.yml")
    build = _workflow("build.yml")
    row = _sync_rows()["transmission-nordvpn"]
    work = tmp_path / "repo"
    shutil.copytree(ROOT / "transmission-nordvpn", work / "transmission-nordvpn")
    before = (work / "transmission-nordvpn" / "CHANGELOG.md").read_text()

    rc, resolved, log, _ = _resolve(tmp_path, row, "5.5.3", cwd=work)
    assert rc == 0 and resolved["changed"] == "true", (resolved, log)

    apply = _step(sync, "sync", "Apply bump")
    rc, _, log = _run_step(apply["run"], _step_env(apply, row, resolved), work, tmp_path)
    assert rc == 0, log

    cfg = yaml.safe_load((work / "transmission-nordvpn" / "config.yaml").read_text())
    assert cfg["version"] == "5.5.3.0" and isinstance(cfg["version"], str)
    dockerfile = (work / "transmission-nordvpn" / "Dockerfile").read_text().splitlines()
    froms = [ln for ln in dockerfile if ln.startswith("ARG BUILD_FROM=")]
    assert froms == ["ARG BUILD_FROM=haugene/transmission-openvpn:5.5.3"]
    assert "FROM ${BUILD_FROM}" in dockerfile
    after = (work / "transmission-nordvpn" / "CHANGELOG.md").read_text()
    sep = re.search(r"\| -+ \| -+ \|\n", after)
    assert sep, "CHANGELOG table separator row missing"
    assert after[sep.end() :].startswith("| 5.5.3.0 "), after[sep.end() : sep.end() + 40]
    headings = re.findall(r"(?m)^## (.+)$", after)
    assert headings[:2] == ["5.5.3.0", "5.5.2.0"]
    # The stub names the bundled image the way the hand-written entries do (bare haugene version).
    assert "_Bundles `transmission-openvpn` 5.5.3._" in after, after[:400]
    assert len(headings) == len(re.findall(r"(?m)^## ", before)) + 1

    # The suggested release tag must route build.yml to this add-on against the bumped config...
    select = _step(build, "plan", "Select add-ons")["run"]
    rc, outputs, log = _run_step(
        select,
        {"GITHUB_EVENT_NAME": "push", "GITHUB_REF_NAME": resolved["release_tag"]},
        work,
        tmp_path,
    )
    assert rc == 0, log
    assert yaml.safe_load(outputs["matrix"]) == ["transmission-nordvpn"]
    # ...and a tag without the wrapper suffix is refused (documents why release_tag carries it).
    rc, outputs, log = _run_step(
        select,
        {"GITHUB_EVENT_NAME": "push", "GITHUB_REF_NAME": "transmission-nordvpn-v5.5.3"},
        work,
        tmp_path,
    )
    assert rc != 0 and "matrix" not in outputs, log

    compute = _step(build, "build", "Compute version")
    rc, outputs, log = _run_step(compute["run"], {"ADDON": "transmission-nordvpn"}, work, tmp_path)
    assert rc == 0, log
    assert outputs["version"] == "5.5.3.0"
    assert outputs["image"] == "ghcr.io/felixmusil/transmission-nordvpn-haos-addon"
    push_step = next(
        s
        for s in build["jobs"]["build"]["steps"]
        if str(s.get("uses", "")).startswith("docker/build-push-action@")
    )
    pushed = {
        t.strip()
        .replace("${{ steps.meta.outputs.image }}", outputs["image"])
        .replace("${{ steps.meta.outputs.version }}", outputs["version"])
        for t in push_step["with"]["tags"].splitlines()
        if t.strip()
    }
    assert "ghcr.io/felixmusil/transmission-nordvpn-haos-addon:5.5.3.0" in pushed, pushed
