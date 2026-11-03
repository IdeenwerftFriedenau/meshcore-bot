"""Configuration, HTTP protection and packaged offline UI checks."""

import json
import shutil
import threading
from pathlib import Path

import httpx
import pytest

from meshcore_ai_bot.config import load_config
from meshcore_ai_bot.dashboard import make_server, runtime
from meshcore_ai_bot.settings import digest, read_settings, save_settings


@pytest.fixture
def config_path(tmp_path):
    target = tmp_path / "config.toml"
    shutil.copyfile(Path(__file__).parents[1] / "config.example.toml", target)
    return target


def test_settings_preserve_private_values_and_write_only_key(config_path):
    env = config_path.parent / ".env"
    env.write_text("PRIVATE_CHANNEL_KEY=do-not-lose\nMESHCORE_AI_API_KEY=old-value\n")
    initial = read_settings(config_path)
    assert initial["api_key_configured"]
    assert "old-value" not in json.dumps(initial)
    result = save_settings(
        config_path,
        {
            "revision": initial["revision"],
            "settings": {"station": {"location": "Bayern"}, "broadcasts": {"bbk_regions": ["BY"]}},
            "api_key": "new-secret",
        },
    )
    assert result["settings"]["broadcasts"]["bbk_regions"] == ["BY"]
    assert "new-secret" not in json.dumps(result)
    assert "do-not-lose" in env.read_text()
    assert "new-secret" in env.read_text()
    assert config_path.stat().st_mode & 0o777 == 0o600
    assert config_path.with_suffix(".toml.bak").exists()
    save_settings(config_path, {"revision": result["revision"], "settings": {}, "api_key": ""})
    assert "new-secret" in env.read_text()


@pytest.mark.parametrize(
    "settings",
    [
        {"dashboard": {"host": "0.0.0.0"}},
        {"storage": {"path": "/tmp/other.sqlite"}},
        {"ai": {"api_key_env": "OTHER"}},
        {"radio": {"send_interval_seconds": 1}},
        {"broadcasts": {"warning_regions": ["unknown"]}},
        {"asa": {"inbox": "/etc"}},
        {"asa": {"device_index": -1}},
        {"asa": {"dab_channel": "bad"}},
        {"station": {"name": 12}},
        {"broadcasts": {"dwd_enabled": "true"}},
    ],
)
def test_reject_invalid_without_modifying_files(config_path, settings):
    before = config_path.read_bytes()
    with pytest.raises(ValueError):
        save_settings(config_path, {"revision": digest(config_path), "settings": settings})
    assert config_path.read_bytes() == before


def test_conflict_preserves_newer_settings(config_path):
    revision = digest(config_path)
    config_path.write_text(config_path.read_text() + "\n# external edit\n")
    with pytest.raises(FileExistsError):
        save_settings(config_path, {"revision": revision, "settings": {}})


def test_public_and_question_channels_locked(config_path):
    initial = read_settings(config_path)
    for name, changes in [
        ("Public", {"mode": "all"}),
        ("#bot", {"mode": "all"}),
        ("#fragdenbot", {"broadcasts": ["warnings"]}),
    ]:
        channels = initial["settings"]["channels"]
        for channel in channels:
            if channel["name"] == name:
                prior = channel.copy()
                channel.update(changes)
                with pytest.raises(ValueError):
                    save_settings(
                        config_path, {"revision": initial["revision"], "settings": {"channels": channels}}
                    )
                channel.update(prior)


@pytest.fixture
def server(config_path):
    server = make_server(config_path, port=0)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    yield "http://127.0.0.1:" + str(server.server_port)
    server.shutdown()
    server.server_close()
    worker.join()


def test_real_http_save_and_csrf_host_defenses(server, config_path):
    with httpx.Client(base_url=server, trust_env=False) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "script-src 'self'" in response.headers["Content-Security-Policy"]
        state = client.get("/api/state").json()
        assert len(state["states"]) == 16
        payload = {"revision": state["revision"], "settings": {"station": {"location": "München"}}}
        assert client.post("/api/settings", json=payload).status_code == 403
        headers = {"Origin": server, "X-CSRF-Token": state["csrf"]}
        assert (
            client.post(
                "/api/settings", json=payload, headers={**headers, "Origin": "https://bad.example"}
            ).status_code
            == 403
        )
        assert client.get("/api/state", headers={"Host": "bad.example"}).status_code == 403
        assert client.post("/api/settings", json=payload, headers=headers).status_code == 200
        assert load_config(config_path).station.location == "München"
        assert client.post("/api/settings", json=payload, headers=headers).status_code == 409
        assert client.get("/../.env").status_code == 404
        for asset in ["app.js", "app.css"]:
            assert client.get("/" + asset).status_code == 200


def test_no_fake_runtime_or_created_database(config_path):
    config = load_config(config_path)
    assert not Path(config.storage.path).exists()
    status = runtime(config)
    assert status["connected"] is False and status["repeaters"] is None
    assert not Path(config.storage.path).exists()


def test_lan_requires_long_token(config_path, monkeypatch):
    monkeypatch.delenv("MESHCORE_DASHBOARD_TOKEN", raising=False)
    with pytest.raises(ValueError):
        make_server(config_path, host="0.0.0.0", port=0)


def test_optional_token_authenticates(config_path, monkeypatch):
    monkeypatch.setenv("MESHCORE_DASHBOARD_TOKEN", "test-token-which-is-long-enough")
    server = make_server(config_path, port=0)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        url = "http://127.0.0.1:" + str(server.server_port)
        with httpx.Client(trust_env=False) as client:
            assert client.get(url + "/api/state").status_code == 401
            assert (
                client.get(
                    url + "/api/state", headers={"Authorization": "Bearer test-token-which-is-long-enough"}
                ).status_code
                == 200
            )
    finally:
        server.shutdown()
        server.server_close()
        worker.join()
