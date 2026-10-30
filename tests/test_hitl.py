import hashlib

import pytest

from perun.hitl import HITLConfig, HITLTokenEngine


def test_issue_and_validate():
    engine = HITLTokenEngine(HITLConfig(secret_key=b"test-secret"))
    details = {"command": "echo hello"}
    issued = engine.issue("prop_001", details)
    assert issued["proposal_id"] == "prop_001"
    assert issued["token"]
    assert issued["nonce"]

    ok, reason = engine.validate(
        token=issued["token"],
        proposal_id="prop_001",
        execution_details=details,
        expiry_epoch=issued["expiry_epoch"],
        nonce=issued["nonce"],
    )
    assert ok
    assert reason is None


def test_validate_fails_after_expiry():
    engine = HITLTokenEngine(HITLConfig(secret_key=b"test-secret", token_ttl_seconds=-1))
    issued = engine.issue("prop_002", {"x": 1})
    ok, reason = engine.validate(
        token=issued["token"],
        proposal_id="prop_002",
        execution_details={"x": 1},
        expiry_epoch=issued["expiry_epoch"],
        nonce=issued["nonce"],
    )
    assert not ok
    assert reason == "expired"


def test_replay_fails():
    engine = HITLTokenEngine(HITLConfig(secret_key=b"test-secret"))
    issued = engine.issue("prop_003", {"x": 1})
    engine.validate(
        token=issued["token"],
        proposal_id="prop_003",
        execution_details={"x": 1},
        expiry_epoch=issued["expiry_epoch"],
        nonce=issued["nonce"],
    )
    ok, reason = engine.validate(
        token=issued["token"],
        proposal_id="prop_003",
        execution_details={"x": 1},
        expiry_epoch=issued["expiry_epoch"],
        nonce=issued["nonce"],
    )
    assert not ok
    assert reason == "nonce_replay"


def test_tampered_details_fails():
    engine = HITLTokenEngine(HITLConfig(secret_key=b"test-secret"))
    issued = engine.issue("prop_004", {"command": "echo hello"})
    ok, reason = engine.validate(
        token=issued["token"],
        proposal_id="prop_004",
        execution_details={"command": "rm -rf /"},
        expiry_epoch=issued["expiry_epoch"],
        nonce=issued["nonce"],
    )
    assert not ok
    assert reason == "invalid_signature"


def test_replay_fails_across_engine_instances():
    """Consumed nonces must persist in the database, not just in memory."""
    cfg = HITLConfig(secret_key=b"test-secret")
    issued = HITLTokenEngine(cfg).issue("prop_005", {"x": 1})
    HITLTokenEngine(cfg).validate(
        token=issued["token"],
        proposal_id="prop_005",
        execution_details={"x": 1},
        expiry_epoch=issued["expiry_epoch"],
        nonce=issued["nonce"],
    )
    # A fresh engine instance should still see the nonce as consumed.
    ok, reason = HITLTokenEngine(cfg).validate(
        token=issued["token"],
        proposal_id="prop_005",
        execution_details={"x": 1},
        expiry_epoch=issued["expiry_epoch"],
        nonce=issued["nonce"],
    )
    assert not ok
    assert reason == "nonce_replay"


def test_tokens_are_secret_bound():
    """A token issued under one signing key must not validate under another."""
    cfg_a = HITLConfig(secret_key=b"secret-a")
    cfg_b = HITLConfig(secret_key=b"secret-b")
    issued = HITLTokenEngine(cfg_a).issue("prop_006", {"x": 1})
    ok, reason = HITLTokenEngine(cfg_b).validate(
        token=issued["token"],
        proposal_id="prop_006",
        execution_details={"x": 1},
        expiry_epoch=issued["expiry_epoch"],
        nonce=issued["nonce"],
    )
    assert not ok
    assert reason == "invalid_signature"


def test_default_secret_resolution(monkeypatch, tmp_path):
    """Resolution order: PERUN_HITL_SECRET env > setting > PERUN_HOME fallback."""
    from perun import config, hitl

    monkeypatch.delenv("PERUN_HITL_SECRET", raising=False)
    config.CONFIG_PATH = tmp_path / "config.json"
    config.Settings().save()

    home_key = hitl._default_secret()

    s = config.Settings()
    s.perun_hitl_secret = "shared-secret"
    s.save()
    setting_key = hitl._default_secret()
    assert setting_key != home_key

    monkeypatch.setenv("PERUN_HITL_SECRET", "env-secret")
    env_key = hitl._default_secret()
    assert env_key != setting_key


def test_default_secret_decrypts_encrypted_setting(monkeypatch, tmp_path):
    """An encrypted perun_hitl_secret is decrypted when PERUN_PASSWORD is set."""
    from perun import config, hitl, secrets

    monkeypatch.delenv("PERUN_HITL_SECRET", raising=False)
    monkeypatch.setenv("PERUN_PASSWORD", "test-password")
    config.CONFIG_PATH = tmp_path / "config.json"

    encrypted = secrets.encrypt_value("hidden-secret", "test-password")
    s = config.Settings()
    s.perun_hitl_secret = encrypted
    s.save()

    assert hitl._default_secret() == hashlib.sha256(b"hidden-secret").digest()

def test_bad_signature_does_not_consume_nonce():
    engine = HITLTokenEngine(HITLConfig(secret_key=b"test-secret"))
    issued = engine.issue("prop_101", {"x": 1})
    args = dict(
        proposal_id="prop_101",
        execution_details={"x": 1},
        expiry_epoch=issued["expiry_epoch"],
        nonce=issued["nonce"],
    )
    assert engine.validate(token="0" * 64, **args) == (False, "invalid_signature")
    assert engine.validate(token=issued["token"], **args) == (True, None)


def test_action_hash_ignores_nested_key_order():
    digest = HITLTokenEngine.action_hash
    assert digest({"a": {"x": 1, "y": 2}}) == digest({"a": {"y": 2, "x": 1}})


@pytest.mark.parametrize("bad", [123, None, "é"])
def test_non_string_token_is_rejected(bad):
    engine = HITLTokenEngine(HITLConfig(secret_key=b"test-secret"))
    issued = engine.issue("prop_102", {"x": 1})
    ok, reason = engine.validate(
        token=bad,
        proposal_id="prop_102",
        execution_details={"x": 1},
        expiry_epoch=issued["expiry_epoch"],
        nonce=issued["nonce"],
    )
    assert ok is False
    assert reason == "invalid_signature"


def test_config_rejects_short_nonce_window():
    with pytest.raises(ValueError):
        HITLConfig(secret_key=b"k" * 32, token_ttl_seconds=600, nonce_ttl_seconds=60)
