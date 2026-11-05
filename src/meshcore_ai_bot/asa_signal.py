"""Forward actual decoder signalling, without inventing an official warning text."""

import hashlib
import json

from .dab_control import PRESETS, fresh_signal
from .protocol import allows_send, split_text


def forward_signal(config, store, mux, channel, settings, now):
    if not settings["forward_asa"] or not config.asa.enabled:
        return "disabled"
    if channel not in PRESETS or not fresh_signal(mux, now):
        return "stale"
    asa = mux.get("asa", {})
    if type(asa.get("active")) is not bool:
        return "missing"
    if asa.get("is_test") is not False or str(asa.get("status", "")).casefold() != "actual":
        return "test_or_unconfirmed"
    if not allows_send(config, "#warnings", "warnings"):
        return "blocked"
    ensemble = str(mux.get("ensemble", {}).get("label", {}).get("label", "")).strip()[:80]
    if not ensemble:
        return "missing"
    # Track episodes per ensemble, so tuning elsewhere cannot terminate a signal.
    scope = hashlib.sha256(f"{channel}:{ensemble}".encode()).hexdigest()
    key = "asa:signal:" + scope
    prior = json.loads(store.meta_get(key) or '{"active":false,"episode":0}')
    active = asa["active"] or asa.get("emergency_warning") is True
    if not active:
        if prior["active"]:
            prior["active"] = False
            store.meta_set(key, json.dumps(prior))
        return "idle"
    details = {
        k: asa.get(k) for k in ("iid", "level", "region_zone", "region_cc", "region_id", "emergency_warning")
    }
    revision = hashlib.sha256(json.dumps(details, sort_keys=True).encode()).hexdigest()
    episode = prior["episode"] + (not prior["active"])
    identity = f"asa-signal:{scope}:{episode}"
    text = (
        f"DAB+ ASA: {'Aktualisiertes Warnsignal' if prior['active'] else 'Warnsignal empfangen'} "
        f"auf {channel} ({ensemble}). Kein amtlicher Warntext im Decoder vorhanden. "
        "Amtliche Hinweise prüfen."
    )
    dropped = store.meta_get("queue:dropped")
    queued = store.broadcast(
        identity,
        revision,
        now + 300,
        {"#warnings": split_text(text, config.radio.packet_bytes)},
        "warnings",
        now,
        priority=100,
    )
    if store.meta_get("queue:dropped") != dropped:
        return "queue_full"
    # A crash before this update repeats the same identity/revision, which the
    # durable broadcast table deduplicates, including after uncertain radio ACKs.
    store.meta_set(key, json.dumps({"active": True, "episode": episode}))
    if queued:
        store.meta_set("asa:signal_queued_at", now)
    return "queued" if queued else "duplicate"
