# rpi-camera-agent

The capture agent for **DAM — days in a minute**: a Raspberry Pi with a
camera photographs its view every few seconds, and the service at
[chase-nova.com](https://chase-nova.com) turns each day into a one-minute
time-lapse video.

This repository is the part that runs **on the Pi**. It captures frames,
uploads them through short-lived presigned URLs (the device never holds
cloud credentials), keeps frames on the SD card while the network is down
and replays them later, follows the operator's schedule (including
dawn/dusk windows computed on the device), protects itself from heat, and
can switch Wi-Fi networks when asked remotely.

## Requirements

- Raspberry Pi 3, 4, 5 or Zero 2 W
- Raspberry Pi OS Lite, 64-bit (Bookworm or Trixie)
- A CSI camera supported by libcamera (Raspberry Pi Camera Module / HQ
  Camera, most Arducam modules)
- Network access (Wi-Fi or Ethernet)

## Joining DAM

Only registered devices can upload. Registering needs an account that an
administrator has granted device registration (a *device limit*); ask the
operator of the DAM instance you want to join.

1. Sign in, open **Manage → Devices** and click **Register device**.
2. Copy the **enrollment token**. It is shown once, works once, and
   expires after 48 hours.
3. Install the agent (below) and give it the token. On its first
   connection the Pi generates its own secret, exchanges the token for
   it, and the token stops working.
4. Assign the device to one of your Locations on its device page.

## Install

On a freshly imaged Pi (enable SSH and Wi-Fi in Raspberry Pi Imager), as
the user the agent should run as:

```bash
sudo apt-get install -y git
git clone https://github.com/chase-nova/rpi-camera-agent.git
cd rpi-camera-agent
./scripts/install.sh            # add --stage <name> to use another stage
```

`install.sh` installs picamera2 and a small virtualenv into
`/opt/dam-agent`, creates `/opt/dam-agent/.env.prod` from `.env.example`,
and installs the `dam-agent` systemd service. Then:

```bash
nano /opt/dam-agent/.env.prod   # set TIMEZONE and ENROLLMENT_TOKEN
sudo systemctl start dam-agent
journalctl -u dam-agent -f      # "enrolled as dam-…" then captures/uploads
```

Other ways to hand over the token:

- **Before first boot**: put `ENROLLMENT_TOKEN=<token>` into
  `dam-agent.env` on the SD card's boot partition (visible on your PC
  right after imaging).
- **Interactively**: `cd /opt/dam-agent && STAGE=prod .venv/bin/python -m agent.enroll <token>`.

The credential ends up in `/var/lib/dam-agent/credential.json`
(readable only by the agent's user). To update the agent later:
`git pull && ./scripts/install.sh`.

## Configuration

Everything lives in `/opt/dam-agent/.env.<stage>` — see `.env.example` for
the full list with explanations. The common ones:

| Key | Default | Meaning |
| --- | --- | --- |
| `TIMEZONE` | — (required) | IANA timezone of the site, e.g. `Europe/Berlin` |
| `ENROLLMENT_TOKEN` | — | one-time token from Register device (removed once used) |
| `DAM_ENDPOINT` | `https://device.chase-nova.com` | the service's device endpoint |
| `VIDEO_MINUTES` | `1` | minutes of video per day (sets the capture interval) |
| `CAPTURE_SIZE` | `1280,720` | JPEG size |
| `VIEWER_PORT` | `8080` | live MJPEG view on the LAN (`0` = off) |
| `CACHE_DIR` | `/var/cache/dam-agent` | offline spill cache |

The capture schedule, the Location, start/stop and Wi-Fi changes are set
by the operator in the web app and reach the device with its next upload
request — nothing to edit on the Pi.

## How it talks to the service

One small HTTPS API (`docs/protocol.md`): `POST /enroll` once, then
`POST /sign` per frame, which returns a presigned upload URL and carries
the device's status as its heartbeat. The device never sees cloud keys;
the operator can disable a device instantly.

## Development

```bash
pip install -e ".[dev]"
pytest
```

The tests use a fake camera and fake HTTP; no Pi or network needed.
`STAGE=test python -m agent.main` runs the agent with a fake camera.

## Security

See [SECURITY.md](SECURITY.md). Please report vulnerabilities privately.

## License

[Apache License 2.0](LICENSE).
