"""Exercise deployment checkpoints in temporary directories, never the installed services."""

import json
import os
from pathlib import Path

import pytest

from meshcore_ai_bot import deployment
from meshcore_ai_bot.config import Config
from meshcore_ai_bot.health import doctor
from meshcore_ai_bot.store import Store


@pytest.fixture
def installation(tmp_path):
    app, config, state, units = (tmp_path / name for name in ["app", "config", "state", "units"])
    for path in (app, config, state, units):
        path.mkdir()
    (config / ".env").write_text("SYNTHETIC_KEY=not-a-real-key")
    (config / ".env").chmod(0o600)
    (state / "data").write_text("old database checkpoint")
    (units / "meshcore-ai-bot.service").write_text("old service")
    old = app / "release-old"
    old.mkdir()
    deployment.atomic_link(old, app / "current")
    return app, config, state, units, tmp_path / "backup"


def test_failed_update_restore_includes_code_data_secrets_units_and_permissions(installation):
    app, config, state, units, checkpoint = installation
    deployment.backup(app, config, state, units, checkpoint)
    assert checkpoint.stat().st_mode & 0o777 == 0o700
    assert (app / "last-backup").resolve() == checkpoint
    new = app / "release-new"
    new.mkdir()
    deployment.atomic_link(new, app / "current")
    (config / ".env").write_text("changed")
    (state / "data").write_text("migrated and incompatible")
    (state / "new").write_text("new only")
    (units / "meshcore-ai-bot.service").write_text("new service")
    (units / "meshcore-dashboard.service").write_text("new dashboard")
    deployment.restore(app, config, state, units, checkpoint)
    assert (app / "current").resolve() == app / "release-old"
    assert (config / ".env").read_text() == "SYNTHETIC_KEY=not-a-real-key"
    assert (config / ".env").stat().st_mode & 0o777 == 0o600
    assert (state / "data").read_text() == "old database checkpoint"
    assert not (state / "new").exists()
    assert (units / "meshcore-ai-bot.service").read_text() == "old service"
    assert not (units / "meshcore-dashboard.service").exists()


def test_legacy_installation_restores_without_a_current_link(installation):
    app, config, state, units, checkpoint = installation
    (app / "current").unlink()
    (app / ".venv").mkdir()
    deployment.backup(app, config, state, units, checkpoint)
    deployment.atomic_link(app / "new", app / "current")
    deployment.restore(app, config, state, units, checkpoint)
    assert not (app / "current").is_symlink()
    assert (app / ".venv").is_dir()


def test_failed_release_switch_keeps_previous_selection(installation, monkeypatch):
    app, *_ = installation

    def failed_replace(*_):
        raise OSError("simulated interruption")

    monkeypatch.setattr(os, "replace", failed_replace)
    with pytest.raises(OSError):
        deployment.atomic_link(app / "new", app / "current")
    assert (app / "current").resolve() == app / "release-old"
    assert not list(app.glob(".link-*"))


def test_wrong_backup_paths_are_rejected_before_any_changes(installation):
    app, config, state, units, checkpoint = installation
    deployment.backup(app, config, state, units, checkpoint)
    manifest = checkpoint / "manifest.json"
    data = json.loads(manifest.read_text())
    data["state_dir"] = "/another/installation"
    manifest.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        deployment.restore(app, config, state, units, checkpoint)
    assert (state / "data").read_text() == "old database checkpoint"


def test_doctor_reports_real_integrity_and_disk_space_without_rf(tmp_path, monkeypatch):
    config = Config()
    config.storage.path = str(tmp_path / "state.db")
    store = Store(config.storage.path)
    store.close()
    result = doctor(config)
    assert result["ok"] and not result["radio_opened"] and not result["network_used"]
    from types import SimpleNamespace

    monkeypatch.setattr("meshcore_ai_bot.health.shutil.disk_usage", lambda _: SimpleNamespace(free=0))
    assert not doctor(config)["ok"]
    Path(config.storage.path).write_bytes(b"not a database")
    assert not doctor(config)["checks"]["database_integrity"]


def test_backup_rejects_external_state_links_before_switching(installation, tmp_path):
    app, config, state, units, checkpoint = installation
    external = tmp_path / "external-secret"
    external.write_text("synthetic external value")
    (config / "linked-secret").symlink_to(external)
    with pytest.raises(ValueError):
        deployment.backup(app, config, state, units, checkpoint)
    assert (app / "current").resolve() == app / "release-old"
    assert not (checkpoint / "manifest.json").exists()
    assert not (app / "last-backup").is_symlink()
