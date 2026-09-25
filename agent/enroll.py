"""Device enrollment (design 15 §3, §6) — the device's credential lifecycle.

The Pi generates its own secret and trades a one-time enrollment token for
it at ``POST {DAM_ENDPOINT}/enroll``. The secret is written to the
credential file BEFORE the first call, so a lost answer or a crash is
simply retried with the same secret (the signer makes that idempotent).

Credential file (JSON, mode 0600)::

    {"device_id": "dam-...", "device_secret": "...",   ← the live credential
     "pending_secret": "..."}                          ← an enrollment in flight

First enrollment has no ``device_secret`` yet; a re-enrollment keeps the
current one working until the new secret is accepted.

CLI:  ``STAGE=dev python -m agent.enroll <enrollment-token>``
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import socket
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from agent import __version__

log = logging.getLogger(__name__)

ENROLL_TIMEOUT_S = 15
# first retry after 5 s, doubling to at most 5 min while offline
ENROLL_BACKOFF_START_S = 5.0
ENROLL_BACKOFF_MAX_S = 300.0
SECRET_BYTES = 32
ENROLLMENT_TOKEN_KEY = "ENROLLMENT_TOKEN"
CPUINFO = Path("/proc/cpuinfo")


class EnrollError(Exception):
    """The signer refused the enrollment (unknown, used, expired, revoked
    token) — retrying the same token cannot succeed."""


@dataclass(frozen=True)
class Credential:
    device_id: str
    device_secret: str


def _read_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(path: Path, state: dict) -> None:
    """Atomic write, owner-only permissions (the file holds the secret)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(state, handle)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:  # e.g. a filesystem without POSIX modes (tests on Windows)
        pass


def load_credential(path: Path) -> Credential | None:
    state = _read_state(path)
    if state.get("device_id") and state.get("device_secret"):
        return Credential(str(state["device_id"]), str(state["device_secret"]))
    return None


def hardware_facts() -> dict[str, str]:
    """Informational facts sent at enroll (never an auth factor)."""
    facts = {"hostname": socket.gethostname(), "agent_version": __version__}
    try:
        for line in CPUINFO.read_text(encoding="utf-8").splitlines():
            if line.startswith("Serial"):
                facts["cpu_serial"] = line.split(":", 1)[1].strip()
    except OSError:
        pass
    return facts


def enroll(
    endpoint: str,
    token: str,
    path: Path,
    *,
    urlopen: Callable = urllib.request.urlopen,
    sleep: Callable[[float], None] = time.sleep,
    max_attempts: int | None = None,
    facts: dict[str, str] | None = None,
) -> Credential:
    """Trade ``token`` for a device credential and persist it.

    Transport failures and 5xx/429 are retried with backoff (forever unless
    ``max_attempts``) — the Wi-Fi may come up minutes after boot. A 4xx
    other than 429 raises ``EnrollError`` and drops the pending secret.
    """
    state = _read_state(path)
    if not state.get("pending_secret"):
        state["pending_secret"] = secrets.token_urlsafe(SECRET_BYTES)
        _write_state(path, state)  # persisted BEFORE the first call
    body = json.dumps({
        "enrollment_token": token,
        "device_secret": state["pending_secret"],
        **(facts if facts is not None else hardware_facts()),
    }).encode("utf-8")

    delay = ENROLL_BACKOFF_START_S
    attempt = 0
    while True:
        attempt += 1
        request = urllib.request.Request(
            endpoint.rstrip("/") + "/enroll",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=ENROLL_TIMEOUT_S) as response:
                answer = json.loads(response.read())
            break
        except urllib.error.HTTPError as exc:
            if exc.code < 500 and exc.code != 429:
                state.pop("pending_secret", None)
                _write_state(path, state)
                raise EnrollError(f"enrollment refused (HTTP {exc.code})") from exc
            reason: object = f"HTTP {exc.code}"
        except (OSError, ValueError) as exc:  # URLError, timeouts, bad JSON
            reason = exc
        if max_attempts is not None and attempt >= max_attempts:
            raise ConnectionError(f"enrollment not reached after {attempt} attempts")
        log.warning(
            "enroll attempt %d failed (%s) — retry in %.0fs", attempt, reason, delay
        )
        sleep(delay)
        delay = min(delay * 2, ENROLL_BACKOFF_MAX_S)

    credential = Credential(str(answer["device_id"]), state["pending_secret"])
    _write_state(path, {
        "device_id": credential.device_id,
        "device_secret": credential.device_secret,
    })
    log.info("enrolled as %s", credential.device_id)
    return credential


def scrub_token(env_file: Path) -> None:
    """Remove the spent ENROLLMENT_TOKEN line from an env file. Best-effort:
    the token is single-use, so a read-only file is only untidy."""
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines(keepends=True)
        prefix = f"{ENROLLMENT_TOKEN_KEY}="
        kept = [ln for ln in lines if not ln.lstrip().startswith(prefix)]
        if kept != lines:
            env_file.write_text("".join(kept), encoding="utf-8")
    except OSError as exc:
        log.warning(
            "could not remove the spent enrollment token from %s: %s", env_file, exc
        )


def resolve_identity(settings, *, urlopen: Callable = urllib.request.urlopen,
                     sleep: Callable[[float], None] = time.sleep):
    """Settings with ``device_id``/``device_token`` filled (design 15 §6):
    credential file → ENROLLMENT_TOKEN (stage env, then the boot-partition
    file) → legacy DEVICE_ID + DEVICE_TOKEN. Raises ConfigError when none."""
    from agent.config import ConfigError, read_boot_enrollment_token

    path = Path(settings.credential_file)
    credential = load_credential(path)
    if credential is None:
        token = settings.enrollment_token
        source = Path(settings.env_file) if settings.env_file else None
        if not token:
            token = read_boot_enrollment_token(Path(settings.boot_env_file))
            source = Path(settings.boot_env_file) if token else None
        if token:
            log.info("not enrolled yet — enrolling at %s", settings.upload_signer_url)
            credential = enroll(settings.upload_signer_url, token, path,
                                urlopen=urlopen, sleep=sleep)
            if source is not None:
                scrub_token(source)
    if credential is not None:
        return replace(settings, device_id=credential.device_id,
                       device_token=credential.device_secret, enrollment_token=None)
    if settings.device_id and settings.device_token:
        return settings  # legacy fleet: long-lived token from the env file
    raise ConfigError(
        "not enrolled: set ENROLLMENT_TOKEN (or run `python -m agent.enroll "
        "<token>`), or legacy DEVICE_ID + DEVICE_TOKEN"
    )


def main(argv: list[str] | None = None) -> int:
    """``python -m agent.enroll <token>`` — enroll now, interactively."""
    from agent.config import load_settings

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print(
            "usage: STAGE=<stage> python -m agent.enroll <enrollment-token>",
            file=sys.stderr,
        )
        return 2
    settings = load_settings()
    try:
        credential = enroll(settings.upload_signer_url, args[0],
                            Path(settings.credential_file), max_attempts=3)
    except (EnrollError, ConnectionError) as exc:
        print(f"enrollment failed: {exc}", file=sys.stderr)
        return 1
    print(f"enrolled as {credential.device_id} - restart the service: "
          "sudo systemctl restart dam-agent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
