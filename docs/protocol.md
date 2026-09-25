# Device protocol

Everything a device needs to know about the service: two HTTPS endpoints
under `DAM_ENDPOINT` (default `https://device.chase-nova.com`). Bodies and
answers are JSON (`Content-Type: application/json`). Requests are
rate-limited per endpoint; a `429` means "back off and retry".

## `POST /enroll` — once per device (and per re-enrollment)

Trades a one-time **enrollment token** (from the web app's *Register
device*) for the device's own secret.

```json
{
  "enrollment_token": "dame_…",
  "device_secret": "<43+ chars base64url, generated on the device>",
  "cpu_serial": "10000000abcdef01",
  "hostname": "raspberrypi",
  "agent_version": "0.1.0"
}
```

`cpu_serial`, `hostname` and `agent_version` are optional and
informational (shown to the device's owner; never used for auth).

| Answer | Meaning |
| --- | --- |
| `200 {"device_id": "dam-…"}` | enrolled; from now on sign with `device_secret` |
| `401 {"error": "invalid enrollment"}` | unknown, used, expired or revoked token — the same answer for all of them; do not retry this token |
| `400` | missing token or a weak/malformed secret |

**Retries are safe.** Generate the secret once and store it *before* the
first call; if the answer is lost, send the same token with the same
secret again and you get the same `200`. A different secret with an
already-used token gets `401`, so a copied token works on one device only.

## `POST /sign` — per frame; also the heartbeat

```json
{
  "token": "<device_secret>",
  "date": "2026-09-25",
  "filename": "143059123.jpg",
  "content_type": "image/jpeg",
  "metadata": {"ulid": "01J…", "device-id": "dam-…",
               "captured-utc": "2026-09-25T05:30:59.123Z", "timezone": "Europe/Berlin"},
  "status": { "uploaded": 120, "temp_c": 61.2, "…": "…" },
  "sidecar": true
}
```

- `date` is the device-local capture date `YYYY-MM-DD`; `filename` is the
  local time `hhmmssfff.jpg` (9 digits). The service decides the storage
  location from the device's assigned Location.
- `metadata` keys are limited to `ulid`, `device-id`, `captured-utc`,
  `timezone`; they are stored as object metadata.
- `status` is the heartbeat (whitelisted keys, e.g. `hostname`,
  `agent_version`, `uploaded`, `queue_depth`, `interval_s`, `timezone`,
  `pi_model`, `camera`, `temp_c`, `throttled`, `thermal_state`,
  `net_state`, `cache_frames`, `replay_pending`, `wifi_ssid`,
  `wifi_scan`, `wifi_applied`, `dawn_at`, `dusk_at`).
- `sidecar: true` asks for a second URL for a `.json` file next to the
  frame.

### Answers

| Answer | Meaning / what to do |
| --- | --- |
| `200 {"status": "ok", "url", "key", "expires_in", "window", …}` | `PUT` the JPEG to `url` (header `Content-Type: image/jpeg`) within `expires_in` seconds; `sidecar_url` too if requested |
| `200 {"status": "paused", "window"}` | operator stopped capture — skip this frame |
| `200 {"status": "shutdown", "window"}` | the device's Location was closed — power off |
| `409 {"error": "unassigned"}` | no Location yet — skip, keep heartbeating |
| `401 {"error": "unknown token"}` | not a valid device credential (an enrollment token here is also `401`) |
| `403 {"error": "device disabled"}` | the operator disabled this device |
| `400` / `404` / `405` | malformed request / wrong path / not POST |

Any answer may also carry desired configuration, which the device applies:

| Field | Meaning |
| --- | --- |
| `window: {"start", "end"}` | daily capture window, `HH:MM` or `"dawn"` / `"dusk"`; equal = all day; start > end crosses midnight |
| `coords: {"latitude", "longitude"}` | the Location's position, for on-device dawn/dusk times |
| `boost_dawn`, `boost_dusk` | ×4 capture density around dawn / dusk |
| `cache_enabled` | `false` = do not spill frames to the SD card while offline |
| `wifi: {"id", "scan": true}` / `{"id", "apply": {"ssid", "psk", "hidden"}}` | scan and report networks / switch network; answer in `status.wifi_scan` / `status.wifi_applied` with the same `id` |
| `reenroll: {"id", "token", "expires_at"}` | enroll again (`POST /enroll`) with this token and a **new** secret, then switch to it — the old secret stops working |

The response's `Date` header is a trusted clock for devices without NTP.
