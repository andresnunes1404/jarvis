"""Regression tests: project_templates_path must co-locate with the actual
config.json in use (not always the OS default config dir), and the default
location must be seeded with the app's real template set on first run
rather than silently staying empty.

Without this, a fresh install (or any run with JARVIS_CONFIG_PATH pointed
elsewhere, e.g. tests) resolves project_templates_path against the wrong
directory, project_intake.py's load_templates() finds nothing there, and
every intake silently degrades to the 4-question generic fallback — the
rich per-category templates (website, app, marketing, branding) never
load. See project_intake.spec.md "Templates" and "Config keys".
"""

import json
from pathlib import Path

from jarvis.config import load_settings


def _write_config(tmp_path, monkeypatch, values):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(values))
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(cfg_path))
    return cfg_path


def test_default_project_templates_path_colocates_with_config_dir(tmp_path, monkeypatch):
    """With no explicit project_templates_path, the default must live next
    to the config.json actually being used — not the OS default config
    dir — so JARVIS_CONFIG_PATH-based isolation (portable installs, tests)
    doesn't silently point at a different directory's templates file."""
    cfg_path = _write_config(tmp_path, monkeypatch, {})

    cfg = load_settings()

    assert Path(cfg.project_templates_path).parent == cfg_path.parent


def test_default_project_templates_path_is_seeded_with_real_templates(tmp_path, monkeypatch):
    """The default project_templates.json must be created with the app's
    real template set (website/app/marketing/branding/other) on first run,
    not left missing until the user manually copies one in."""
    _write_config(tmp_path, monkeypatch, {})

    cfg = load_settings()

    seeded_path = Path(cfg.project_templates_path)
    assert seeded_path.exists(), (
        "Default project_templates.json was not seeded on first run — "
        "project intake would silently fall back to the minimal built-in "
        "template until a user manually places a file there."
    )
    data = json.loads(seeded_path.read_text(encoding="utf-8"))
    assert "website" in data, "Seeded templates must include the real 'website' template"
    assert "other" in data and data["other"]["keywords"] == []


def test_explicit_project_templates_path_is_never_auto_seeded(tmp_path, monkeypatch):
    """A user-specified project_templates_path must be respected as-is —
    if it doesn't exist, that's the user's own path, and Jarvis must not
    silently create a file there; the tool's own load_templates() fallback
    already handles the missing-file case at runtime."""
    custom_path = tmp_path / "custom" / "my_templates.json"
    _write_config(tmp_path, monkeypatch, {"project_templates_path": str(custom_path)})

    cfg = load_settings()

    assert cfg.project_templates_path == str(custom_path)
    assert not custom_path.exists()


def test_existing_default_project_templates_file_is_not_overwritten(tmp_path, monkeypatch):
    """Seeding is first-run-only — an already-existing default file (e.g.
    one the user has customised) must never be clobbered on a later
    startup."""
    cfg_path = _write_config(tmp_path, monkeypatch, {})
    existing = cfg_path.parent / "project_templates.json"
    existing.write_text(json.dumps({"other": {"label": "custom", "keywords": [], "questions": []}}))

    cfg = load_settings()

    data = json.loads(Path(cfg.project_templates_path).read_text(encoding="utf-8"))
    assert data == {"other": {"label": "custom", "keywords": [], "questions": []}}
