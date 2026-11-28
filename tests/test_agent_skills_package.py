"""Packaged ``kct`` skills and ``kct skills install`` (issue #5950).

The skills ship as package data in ``src/kicad_tools/agent_skills/kct/`` so a
``pip install kicad-tools`` carries them. These tests pin:

* the repo copy ``.claude/commands/kct/`` stays byte-identical to the package
  data (one source of truth);
* the wheel actually contains the skills;
* ``kct skills install`` round-trips: install, ``--check`` clean, idempotent
  re-run, drift and stale-file detection, ``--prune``, and the codex and
  opencode layouts (#5951).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from kicad_tools.agent_skills import (
    LAYOUTS,
    check_installed,
    install_skills,
    packaged_skill_sources,
    planned_files,
    resolve_target,
    split_frontmatter,
)
from kicad_tools.agent_surfaces import PLACEHOLDER_RE, render
from kicad_tools.cli import main

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = REPO_ROOT / "src" / "kicad_tools" / "agent_skills" / "kct"
REPO_COPY_DIR = REPO_ROOT / ".claude" / "commands" / "kct"

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import sync_agent_skills  # noqa: E402


def _repo_skill_names() -> set[str]:
    return {p.name for p in REPO_COPY_DIR.glob("*.md")}


# --- one source of truth -----------------------------------------------------


def test_repo_copy_matches_package_data() -> None:
    drift = sync_agent_skills.find_drift(PACKAGE_DIR, REPO_COPY_DIR)
    assert drift == [], (
        "`.claude/commands/kct/` drifted from src/kicad_tools/agent_skills/kct/: "
        f"{drift}. Edit the package data, then run "
        "`uv run python scripts/sync_agent_skills.py --write`."
    )


def test_sync_script_write_repairs_drift(tmp_path: Path) -> None:
    copy = tmp_path / "kct"
    copy.mkdir()
    (copy / "stale.md").write_text("old", encoding="utf-8")
    (copy / "help.md").write_text("edited", encoding="utf-8")
    assert sync_agent_skills.find_drift(PACKAGE_DIR, copy)
    sync_agent_skills.write_copy(PACKAGE_DIR, copy)
    assert sync_agent_skills.find_drift(PACKAGE_DIR, copy) == []


def test_packaged_sources_load_from_package() -> None:
    sources = packaged_skill_sources()
    assert {s.filename for s in sources} == _repo_skill_names()
    assert any(s.is_readme for s in sources)
    assert "help" in {s.name for s in sources}


def test_packaged_text_has_no_demo_board_paths() -> None:
    for source in packaged_skill_sources():
        assert not re.search(r"\bboards/\d{2}-", source.text), source.filename


def _skill_bodies() -> list[tuple[str, str]]:
    """``(filename, body)`` for every packaged skill, frontmatter removed."""
    return [
        (source.filename, split_frontmatter(source.text)[1])
        for source in packaged_skill_sources()
        if not source.is_readme
    ]


def _prose_sentences(body: str) -> list[str]:
    """Sentences of *body* outside fenced code blocks, line wraps joined."""
    prose = re.sub(r"```.*?```", "", body, flags=re.S)
    sentences: list[str] = []
    for paragraph in re.split(r"\n\s*\n", prose):
        sentences.extend(re.split(r"(?<=[.!?])\s+", " ".join(paragraph.split())))
    return sentences


@pytest.mark.parametrize(
    "body", [pytest.param(body, id=filename) for filename, body in _skill_bodies()]
)
def test_body_scopes_model_hint_to_installs_that_carry_it(body: str) -> None:
    """No sentence presents ``suggestedModel`` as a field the reader holds (#10703).

    The Codex CLI and opencode renderers drop ``suggestedModel``, so prose such
    as `` `suggestedModel: sonnet`. `` opening a paragraph or "this doc's
    frontmatter ``suggestedModel``" describes frontmatter two of the three
    installs do not have. The string is not banned: the house wording scopes it
    with "when the installed frontmatter includes ...".
    """
    unscoped = [
        sentence
        for sentence in _prose_sentences(body)
        if "suggestedModel" in sentence
        and not re.search(r"\bwhen the installed\b", sentence, re.IGNORECASE)
    ]
    assert unscoped == [], f"states the model hint unconditionally: {unscoped}"


# --- wheel contents -----------------------------------------------------------


def test_skill_files_are_not_gitignored() -> None:
    """Hatchling drops gitignored files from the wheel."""
    if shutil.which("git") is None:
        pytest.skip("git not available")
    files = [str(p.relative_to(REPO_ROOT)) for p in PACKAGE_DIR.glob("*.md")]
    result = subprocess.run(
        ["git", "check-ignore", *files], cwd=REPO_ROOT, capture_output=True, text=True
    )
    assert result.stdout.strip() == "", f"gitignored skill files: {result.stdout}"


def test_built_wheel_contains_skills_and_installs_from_it(tmp_path: Path) -> None:
    """Build the real wheel, then run ``kct skills install`` from it.

    This is acceptance criterion 1 without a network ``pip install``: the
    wheel's own ``kicad_tools`` is imported (dependencies come from the test
    environment), so the skills must be reachable as package data.
    """
    if shutil.which("uv") is None:
        pytest.skip("uv not available to build a wheel")
    dist = tmp_path / "dist"
    result = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(dist)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    (wheel,) = dist.glob("*.whl")
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        archive.extractall(tmp_path / "site")
    shipped = {n.rsplit("/", 1)[1] for n in names if n.startswith("kicad_tools/agent_skills/kct/")}
    assert shipped == _repo_skill_names()
    assert "kicad_tools/agent_skills/__init__.py" in names
    # The agent primer behind `kct agent-guide` ships beside the skills (#5960).
    assert "kicad_tools/agent_skills/AGENT_GUIDE.md" in names

    code = (
        "import sys, kicad_tools; from kicad_tools.cli import main; "
        "print(kicad_tools.__file__, file=sys.stderr); "
        "sys.exit(main(sys.argv[1:]))"
    )
    env = {**os.environ, "PYTHONPATH": str(tmp_path / "site")}
    for harness, review_path in (
        ("claude-code", "kct/ee-review.md"),
        ("codex", "kct-ee-review/SKILL.md"),
        ("opencode", "kct/ee-review.md"),
    ):
        target = tmp_path / harness
        args = ["skills", "install", "--harness", harness, "--target", str(target)]
        run = subprocess.run(
            [sys.executable, "-c", code, *args],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )
        assert run.returncode == 0, run.stderr[-2000:]
        assert str(tmp_path / "site") in run.stderr, "imported kicad_tools from outside the wheel"
        assert check_installed(target, harness).in_sync
        fields, body = split_frontmatter((target / review_path).read_text(encoding="utf-8"))
        assert fields.get("suggestedModel") == ("opus" if harness == "claude-code" else None)
        if harness == "claude-code":
            assert {p.name for p in (target / "kct").glob("*.md")} == _repo_skill_names()
        else:
            selection = body.split("## Model selection", 1)[1].split("## ", 1)[0]
            assert "is set in the frontmatter above" not in selection, harness
            assert re.search(r"Codex CLI and opencode[^.]*session[^.]*model", selection), harness

    guide = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from kicad_tools.cli import main; "
            "sys.exit(main(['agent-guide', '--harness', 'codex']))",
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert guide.returncode == 0, guide.stderr[-2000:]
    assert guide.stdout.startswith("# kicad-tools agent guide")


# --- claude-code round trip ---------------------------------------------------


def test_claude_code_install_round_trip(tmp_path: Path) -> None:
    target = tmp_path / "x"
    report = install_skills(target)
    expected = {f"kct/{name}" for name in _repo_skill_names()}
    assert set(report.written) == expected
    assert {p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()} == expected

    for name in _repo_skill_names():
        installed = (target / "kct" / name).read_text(encoding="utf-8")
        source = (REPO_COPY_DIR / name).read_text(encoding="utf-8")
        assert installed == render(source, "claude-code")
        assert not PLACEHOLDER_RE.search(installed), name

    assert check_installed(target).in_sync
    again = install_skills(target)
    assert again.written == [] and set(again.unchanged) == expected


def test_check_reports_drift_missing_and_stale(tmp_path: Path) -> None:
    install_skills(tmp_path)
    (tmp_path / "kct" / "tapeout.md").write_text("hand edit\n", encoding="utf-8")
    (tmp_path / "kct" / "help.md").unlink()
    (tmp_path / "kct" / "retired-skill.md").write_text("old\n", encoding="utf-8")

    report = check_installed(tmp_path)
    assert not report.in_sync
    assert report.drifted == ["kct/tapeout.md"]
    assert report.missing == ["kct/help.md"]
    assert report.stale == ["kct/retired-skill.md"]

    fixed = install_skills(tmp_path, prune=True)
    assert set(fixed.written) == {"kct/tapeout.md", "kct/help.md"}
    assert fixed.removed == ["kct/retired-skill.md"]
    assert check_installed(tmp_path).in_sync


def test_install_leaves_other_namespaces_alone(tmp_path: Path) -> None:
    loom = tmp_path / "loom" / "builder.md"
    loom.parent.mkdir()
    loom.write_text("loom\n", encoding="utf-8")
    install_skills(tmp_path, prune=True)
    assert loom.read_text(encoding="utf-8") == "loom\n"
    assert check_installed(tmp_path).in_sync


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    report = install_skills(tmp_path / "x", dry_run=True)
    assert report.written
    assert not (tmp_path / "x").exists()


# --- codex layout -------------------------------------------------------------


def test_codex_layout(tmp_path: Path) -> None:
    install_skills(tmp_path, "codex")
    skills = {s.name for s in packaged_skill_sources() if not s.is_readme}
    files = {p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file()}
    assert files == {f"kct-{n}/SKILL.md" for n in skills} | {"kct-help/README.md"}

    for source in packaged_skill_sources():
        if source.is_readme:
            continue
        text = (tmp_path / f"kct-{source.name}" / "SKILL.md").read_text(encoding="utf-8")
        fields, _ = split_frontmatter(text)
        assert fields["name"] == f"kct-{source.name}"
        assert "invocation" not in fields and "suggestedModel" not in fields
        _, source_body = split_frontmatter(source.text)
        assert render(source_body, "codex").strip("\n") in text
        assert not PLACEHOLDER_RE.search(text)
    assert check_installed(tmp_path, "codex").in_sync


# --- opencode layout (#5951) ---------------------------------------------------


def test_opencode_layout(tmp_path: Path) -> None:
    """opencode gets ``kct/<name>.md`` commands (``/kct/<name>``) plus the README."""
    report = install_skills(tmp_path, "opencode")
    expected = {f"kct/{name}" for name in _repo_skill_names()}
    files = {p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file()}
    assert files == expected == set(report.written)

    readme = (tmp_path / "kct" / "README.md").read_text(encoding="utf-8")
    assert readme == render((REPO_COPY_DIR / "README.md").read_text(encoding="utf-8"), "opencode")

    for source in packaged_skill_sources():
        if source.is_readme:
            continue
        text = (tmp_path / "kct" / source.filename).read_text(encoding="utf-8")
        fields, body = split_frontmatter(text)
        # Only `description` survives: opencode derives the command name from
        # the path, and `suggestedModel` is not a provider/model id.
        assert set(fields) == {"description"}, fields
        assert text.startswith("---\ndescription: >-\n  ")
        source_fields, source_body = split_frontmatter(source.text)
        assert render(source_fields["description"], "opencode") in text
        assert render(source_body, "opencode").strip("\n") in body
        assert not PLACEHOLDER_RE.search(text), source.filename
        assert "/kct:" not in text and ".claude/" not in text, source.filename
        assert f"/kct/{source.name}" in text, source.filename
        assert "$ARGUMENTS" in text, source.filename

    assert check_installed(tmp_path, "opencode").in_sync
    again = install_skills(tmp_path, "opencode")
    assert again.written == [] and set(again.unchanged) == expected


def test_opencode_help_does_not_require_frontmatter_name(tmp_path: Path) -> None:
    """The rendered help command must not demand a `name` opencode files omit."""
    help_text = (planned_files("opencode"))["kct/help.md"]
    fields, _ = split_frontmatter(help_text)
    assert "name" not in fields
    # It must still tell the agent how to identify a skill with no `name`.
    assert "where it is absent" in help_text and "filename `<name>.md`" in help_text
    assert "identify it from frontmatter, not the basename" not in help_text


def test_opencode_frontmatter_is_valid_yaml(tmp_path: Path) -> None:
    yaml = pytest.importorskip("yaml")
    for rel, text in planned_files("opencode").items():
        if rel.endswith("README.md"):
            continue
        front = text.split("---\n", 2)[1]
        data = yaml.safe_load(front)
        assert set(data) == {"description"} and data["description"].strip(), rel


def test_opencode_stale_and_prune(tmp_path: Path) -> None:
    install_skills(tmp_path, "opencode")
    (tmp_path / "kct" / "retired-skill.md").write_text("old\n", encoding="utf-8")
    other = tmp_path / "review.md"
    other.write_text("user command\n", encoding="utf-8")
    assert check_installed(tmp_path, "opencode").stale == ["kct/retired-skill.md"]
    assert install_skills(tmp_path, "opencode", prune=True).removed == ["kct/retired-skill.md"]
    assert other.read_text(encoding="utf-8") == "user command\n"
    assert check_installed(tmp_path, "opencode").in_sync


@pytest.mark.parametrize("snippet", ["Run !`git status` first.", "awk '{print $1}'"])
def test_opencode_refuses_template_hazards(snippet: str) -> None:
    from kicad_tools.agent_skills import SkillSource

    source = SkillSource("probe.md", f"---\nname: probe\ndescription: d\n---\n{snippet}\n")
    with pytest.raises(ValueError, match="opencode"):
        LAYOUTS["opencode"].files([source])
    # The other harnesses do not interpret either token.
    assert LAYOUTS["claude-code"].files([source])


def test_opencode_user_target_follows_opencode_config_dir(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert resolve_target("opencode", user=True, home=home, env={}) == (
        home / ".config" / "opencode" / "commands"
    )
    xdg = tmp_path / "xdg"
    assert resolve_target("opencode", user=True, home=home, env={"XDG_CONFIG_HOME": str(xdg)}) == (
        xdg / "opencode" / "commands"
    )
    custom = tmp_path / "oc"
    env = {"OPENCODE_CONFIG_DIR": str(custom), "XDG_CONFIG_HOME": str(xdg)}
    assert resolve_target("opencode", user=True, home=home, env=env) == custom / "commands"
    # A relative value is ignored, as for XDG; other harnesses ignore both vars.
    assert resolve_target("opencode", user=True, home=home, env={"XDG_CONFIG_HOME": "rel"}) == (
        home / ".config" / "opencode" / "commands"
    )
    assert resolve_target("claude-code", user=True, home=home, env=env) == (
        home / ".claude" / "commands"
    )
    assert resolve_target("opencode", cwd=tmp_path) == tmp_path / ".opencode" / "commands"


# --- target resolution --------------------------------------------------------


def test_resolve_target(tmp_path: Path) -> None:
    assert resolve_target(cwd=tmp_path) == tmp_path / ".claude" / "commands"
    assert resolve_target(user=True, home=tmp_path) == tmp_path / ".claude" / "commands"
    assert resolve_target("codex", cwd=tmp_path) == tmp_path / ".agents" / "skills"
    assert resolve_target(target=tmp_path / "t") == tmp_path / "t"
    with pytest.raises(ValueError):
        resolve_target(target=tmp_path, user=True)
    with pytest.raises(ValueError):
        planned_files("no-such-harness")


def test_layouts_cover_render_harnesses() -> None:
    from kicad_tools.agent_surfaces import HARNESSES

    assert set(LAYOUTS) <= set(HARNESSES)
    assert "claude-code" in LAYOUTS


# --- CLI ----------------------------------------------------------------------


def test_cli_install_check_list(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = tmp_path / "x"
    assert main(["skills", "install", "--target", str(target), "--check"]) == 1
    assert main(["skills", "install", "--target", str(target)]) == 0
    assert {p.name for p in (target / "kct").glob("*.md")} == _repo_skill_names()
    assert main(["skills", "install", "--target", str(target), "--check"]) == 0

    capsys.readouterr()
    assert main(["skills", "install", "--target", str(target), "--list", "--format", "json"]) == 0
    listing = json.loads(capsys.readouterr().out)
    assert listing["harness"] == "claude-code"
    invocations = {s["invocation"] for s in listing["skills"]}
    assert "/kct:tapeout" in invocations and "/kct:help" in invocations
    assert {s["status"] for s in listing["skills"]} == {"installed"}

    (target / "kct" / "tapeout.md").write_text("x\n", encoding="utf-8")
    assert main(["skills", "install", "--target", str(target), "--check", "--format", "json"]) == 1
    assert json.loads(capsys.readouterr().out)["drifted"] == ["kct/tapeout.md"]


def test_cli_user_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert main(["skills", "install", "--user"]) == 0
    assert (tmp_path / ".claude" / "commands" / "kct" / "help.md").is_file()


def test_cli_default_target_is_project_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["skills", "install", "--harness", "codex"]) == 0
    assert (tmp_path / ".agents" / "skills" / "kct-help" / "SKILL.md").is_file()


def test_cli_opencode_project_user_and_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["skills", "install", "--harness", "opencode"]) == 0
    assert (tmp_path / ".opencode" / "commands" / "kct" / "tapeout.md").is_file()

    capsys.readouterr()
    assert main(["skills", "install", "--harness", "opencode", "--list", "--format", "json"]) == 0
    listing = json.loads(capsys.readouterr().out)
    assert listing["harness"] == "opencode"
    invocations = {s["invocation"] for s in listing["skills"]}
    assert "/kct/tapeout" in invocations and "/kct/help" in invocations
    assert {s["status"] for s in listing["skills"]} == {"installed"}

    xdg = tmp_path / "xdg"
    monkeypatch.delenv("OPENCODE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    assert main(["skills", "install", "--harness", "opencode", "--user"]) == 0
    assert (xdg / "opencode" / "commands" / "kct" / "help.md").is_file()
    assert main(["skills", "install", "--harness", "opencode", "--user", "--check"]) == 0


def test_cli_rejects_target_with_user(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["skills", "install", "--target", str(tmp_path), "--user"])
    assert exc.value.code == 2
