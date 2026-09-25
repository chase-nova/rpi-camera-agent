"""Tests for agent.enroll — device enrollment and identity resolution
(design 15 §3, §6) plus the uploader's relayed re-enrollment (§3a)."""

import io
import json
import os
import sys
import urllib.error
from dataclasses import replace

import pytest

from agent.config import ConfigError, Settings
from agent.enroll import (
    Credential,
    EnrollError,
    enroll,
    load_credential,
    resolve_identity,
)
from agent.uploader import Uploader

FACTS = {"hostname": "pi-test", "agent_version": "0.1.0"}


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeSigner:
    """urlopen stand-in for POST /enroll: a queue of outcomes."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes) or [{"device_id": "dam-new01"}]
        self.bodies = []

    def __call__(self, request, timeout=None):
        assert request.full_url.endswith("/enroll")
        self.bodies.append(json.loads(request.data))
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, int):
            raise urllib.error.HTTPError(
                request.full_url, outcome, "x", None, io.BytesIO(b"{}")
            )
        if isinstance(outcome, Exception):
            raise outcome
        return FakeResponse(json.dumps(outcome).encode())


def _enroll(tmp_path, signer, **kw):
    return enroll("https://dam.example", "dame_tok", tmp_path / "cred.json",
                  urlopen=signer, sleep=kw.pop("sleep", lambda s: None),
                  facts=FACTS, **kw)


def test_enroll_writes_the_credential_with_the_secret_it_sent(tmp_path):
    signer = FakeSigner()
    credential = _enroll(tmp_path, signer)
    sent = signer.bodies[0]
    assert sent["enrollment_token"] == "dame_tok" and sent["hostname"] == "pi-test"
    assert len(sent["device_secret"]) >= 43
    assert credential == Credential("dam-new01", sent["device_secret"])
    state = json.loads((tmp_path / "cred.json").read_text())
    assert state == {"device_id": "dam-new01", "device_secret": sent["device_secret"]}
    if sys.platform != "win32":
        assert (os.stat(tmp_path / "cred.json").st_mode & 0o777) == 0o600


def test_secret_is_persisted_before_the_call_and_reused_on_retry(tmp_path):
    sleeps = []
    signer = FakeSigner(urllib.error.URLError("no wifi yet"), 503, {"device_id": "dam-new01"})
    _enroll(tmp_path, signer, sleep=sleeps.append)
    secrets_sent = {body["device_secret"] for body in signer.bodies}
    assert len(signer.bodies) == 3 and len(secrets_sent) == 1  # same secret each time
    assert sleeps == [5.0, 10.0]  # doubling backoff


def test_a_crash_after_the_first_call_resumes_with_the_same_secret(tmp_path):
    with pytest.raises(ConnectionError):
        _enroll(tmp_path, FakeSigner(OSError("reset")), max_attempts=1)
    pending = json.loads((tmp_path / "cred.json").read_text())["pending_secret"]
    signer = FakeSigner()
    _enroll(tmp_path, signer)  # "after reboot"
    assert signer.bodies[0]["device_secret"] == pending


def test_refused_token_raises_and_drops_the_pending_secret(tmp_path):
    with pytest.raises(EnrollError):
        _enroll(tmp_path, FakeSigner(401))
    assert "pending_secret" not in json.loads((tmp_path / "cred.json").read_text())


def test_reenroll_keeps_the_current_credential_until_success(tmp_path):
    path = tmp_path / "cred.json"
    path.write_text(json.dumps({"device_id": "dam-a", "device_secret": "OLD" * 15}))
    with pytest.raises(ConnectionError):
        _enroll(tmp_path, FakeSigner(OSError("down")), max_attempts=1)
    assert load_credential(path) == Credential("dam-a", "OLD" * 15)  # still usable
    new = _enroll(tmp_path, FakeSigner({"device_id": "dam-a"}))
    assert new.device_secret != "OLD" * 15 and load_credential(path) == new


# ── resolve_identity: credential file → enrollment token → legacy ───────────

def _settings(tmp_path, **kw):
    return Settings(
        stage="test", device_id=kw.pop("device_id", ""), timezone="Asia/Seoul",
        upload_signer_url="https://dam.example", device_token=kw.pop("device_token", ""),
        credential_file=str(tmp_path / "cred.json"),
        boot_env_file=str(tmp_path / "boot-dam-agent.env"), **kw,
    )


def test_credential_file_wins_over_legacy_env(tmp_path):
    (tmp_path / "cred.json").write_text(json.dumps({"device_id": "dam-a", "device_secret": "S" * 43}))
    settings = resolve_identity(_settings(tmp_path, device_id="dam-a", device_token="legacy"))
    assert (settings.device_id, settings.device_token) == ("dam-a", "S" * 43)


def test_enrollment_token_in_the_stage_env_enrolls_and_is_scrubbed(tmp_path):
    env = tmp_path / ".env.test"
    env.write_text("TIMEZONE=Asia/Seoul\nENROLLMENT_TOKEN=dame_tok\n")
    base = replace(_settings(tmp_path, enrollment_token="dame_tok"), env_file=str(env))
    settings = resolve_identity(base, urlopen=FakeSigner(), sleep=lambda s: None)
    assert settings.device_id == "dam-new01" and settings.device_token
    assert "ENROLLMENT_TOKEN" not in env.read_text()
    assert "TIMEZONE" in env.read_text()


def test_boot_partition_token_is_used_when_the_stage_env_has_none(tmp_path):
    boot = tmp_path / "boot-dam-agent.env"
    boot.write_text("ENROLLMENT_TOKEN=dame_boot\n")
    signer = FakeSigner()
    settings = resolve_identity(_settings(tmp_path), urlopen=signer, sleep=lambda s: None)
    assert signer.bodies[0]["enrollment_token"] == "dame_boot"
    assert settings.device_id == "dam-new01"
    assert "ENROLLMENT_TOKEN" not in boot.read_text()


def test_legacy_identity_still_works(tmp_path):
    settings = resolve_identity(_settings(tmp_path, device_id="dam-x", device_token="tok"))
    assert (settings.device_id, settings.device_token) == ("dam-x", "tok")


def test_no_identity_at_all_is_a_clear_error(tmp_path):
    with pytest.raises(ConfigError, match="not enrolled"):
        resolve_identity(_settings(tmp_path))


# ── uploader: re-enrollment relayed in the /sign answer ─────────────────────

def _uploader(tmp_path, reenroll_fn):
    uploader = Uploader(_settings(tmp_path, device_id="dam-x", device_token="legacy"),
                        urlopen=lambda *a, **k: None, sleep=lambda s: None)
    uploader.reenroll_fn = reenroll_fn
    return uploader


def test_relayed_token_switches_the_credential(tmp_path):
    calls = []
    uploader = _uploader(tmp_path, lambda t: calls.append(t) or Credential("dam-x", "NEW"))
    uploader._stash_wifi({"status": "ok", "reenroll": {"id": "r1", "token": "dame_r"}})
    uploader.tick(poll_s=0)
    assert calls == ["dame_r"] and uploader.device_token == "NEW"
    uploader._stash_wifi({"reenroll": {"id": "r1", "token": "dame_r"}})  # relayed again
    uploader.tick(poll_s=0)
    assert calls == ["dame_r"]  # already done: not repeated


def test_refused_relay_is_not_retried_but_a_transport_failure_is(tmp_path):
    outcomes = [EnrollError("401"), ConnectionError("down"), Credential("dam-x", "NEW")]
    calls = []

    def reenroll(token):
        calls.append(token)
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    uploader = _uploader(tmp_path, reenroll)
    for request_id in ("r1", "r1"):  # refused, then relayed again → skipped
        uploader._stash_wifi({"reenroll": {"id": request_id, "token": "dame_a"}})
        uploader.tick(poll_s=0)
    assert calls == ["dame_a"] and uploader.device_token == "legacy"
    for _ in range(2):  # a new request: transport failure, then success
        uploader._stash_wifi({"reenroll": {"id": "r2", "token": "dame_b"}})
        uploader.tick(poll_s=0)
    assert calls == ["dame_a", "dame_b", "dame_b"] and uploader.device_token == "NEW"
