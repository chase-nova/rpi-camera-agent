#!/usr/bin/env bash
# Install or update dam-agent on this Raspberry Pi (idempotent).
#
# From a clone of the repository, ON the Pi, as the user the agent should
# run as (needs sudo):
#     ./scripts/install.sh [--stage prod]
# System setup only (packages, venv, sudo rules, Wi-Fi hardening), e.g.
# piped over SSH before copying the code some other way:
#     ssh -t <user>@<pi> 'bash -s -- --system-only' < scripts/install.sh
#
# Installs picamera2 (apt), creates /opt/dam-agent with a
# --system-site-packages venv (so apt's picamera2 is visible) and the
# agent's small runtime deps (no boto3 on devices — ADR-0003); then copies
# the agent, creates the stage env file and installs the systemd unit.
set -euo pipefail

DEST=/opt/dam-agent
STAGE=prod
SYSTEM_ONLY=0
while [ $# -gt 0 ]; do
    case "$1" in
        --stage) STAGE=${2:?--stage needs a value}; shift 2 ;;
        --system-only) SYSTEM_ONLY=1; shift ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

sudo apt-get update -qq
sudo apt-get install -y -qq python3-picamera2 python3-venv

sudo mkdir -p "$DEST"
sudo chown "$USER":"$USER" "$DEST"
# Offline spill cache + time anchor (design 12, ADR-0007): must survive
# reboots and be writable by the agent user. Matches CACHE_DIR's default.
sudo install -d -o "$USER" -g "$USER" /var/cache/dam-agent
# Device credential from enrollment (design 15 §6): owner-only directory,
# kept outside /opt/dam-agent so redeploys never touch it.
sudo install -d -m 700 -o "$USER" -g "$USER" /var/lib/dam-agent
# Remote Wi-Fi setup (design 13 §3): the agent runs nmcli from a systemd
# service, where Debian's default polkit rule (netdev members, interactive
# sessions only) does not apply. Grant the agent user NetworkManager
# actions explicitly; polkitd picks the file up without a restart.
sudo tee /etc/polkit-1/rules.d/50-dam-agent-networkmanager.rules >/dev/null <<EOF
// dam-agent (design 13): let the agent user manage Wi-Fi via NetworkManager
// from its service context (scan, add/activate/delete connections).
polkit.addRule(function(action, subject) {
    if (action.id.indexOf("org.freedesktop.NetworkManager.") === 0 &&
        subject.user === "$USER") {
        return polkit.Result.YES;
    }
});
EOF
sudo chmod 644 /etc/polkit-1/rules.d/50-dam-agent-networkmanager.rules
# Bookworm's NetworkManager (1.42) still answers "Insufficient privileges"
# for settings.modify.system under that rule (verified 2026-08-31), so the
# agent also has a narrow sudo path for nmcli — same pattern as poweroff.
echo "$USER ALL=(root) NOPASSWD: /usr/bin/nmcli" | sudo tee /etc/sudoers.d/dam-agent-nmcli >/dev/null
sudo chmod 440 /etc/sudoers.d/dam-agent-nmcli
sudo visudo -c >/dev/null

if [ ! -x "$DEST/.venv/bin/python" ]; then
    python3 -m venv --system-site-packages "$DEST/.venv"
fi
# typing-extensions: python-ulid needs it on Python 3.11 (Bookworm)
"$DEST/.venv/bin/pip" install --quiet --upgrade \
    python-dotenv python-ulid typing-extensions

# thermal last-resort shutdown (design 02 §5.2) — passwordless poweroff only
echo "$USER ALL=(root) NOPASSWD: /sbin/poweroff" \
    | sudo tee /etc/sudoers.d/dam-agent-poweroff > /dev/null
sudo chmod 440 /etc/sudoers.d/dam-agent-poweroff

# network self-healing last-resort reboot (design 12 §3 extension) — a wedged
# Wi-Fi radio is cured by a boot; the spill cache carries the frames across
echo "$USER ALL=(root) NOPASSWD: /usr/sbin/reboot" \
    | sudo tee /etc/sudoers.d/dam-agent-reboot > /dev/null
sudo chmod 440 /etc/sudoers.d/dam-agent-reboot

# Wi-Fi stability hardening (field lessons 2026-09-02): power-save OFF (the
# classic Pi connect-hold-drop cure), retry forever (NM's default 4 retries
# BLOCKS a profile after one bad patch), sync so abrupt power cuts cannot
# eat freshly-written profiles.
for p in $(nmcli -t -f NAME,TYPE connection show | grep wireless | cut -d: -f1); do
    sudo nmcli connection modify "$p" 802-11-wireless.powersave 2 \
        connection.autoconnect-retries 0 || true
done
sudo iw dev wlan0 set power_save off 2>/dev/null || true
sync

echo "provisioned: $DEST (python: $("$DEST/.venv/bin/python" --version))"
if [ "$SYSTEM_ONLY" = 1 ]; then
    exit 0
fi

# ── the agent itself ─────────────────────────────────────────────────────────
SRC=$(cd "$(dirname "$0")/.." && pwd)
rm -rf "$DEST/agent" "$DEST/dam_shared"
cp -r "$SRC/agent" "$SRC/dam_shared" "$DEST/"

ENV_FILE="$DEST/.env.$STAGE"
if [ ! -f "$ENV_FILE" ]; then
    install -m 600 "$SRC/.env.example" "$ENV_FILE"
    echo "created $ENV_FILE from .env.example — set TIMEZONE (and ENROLLMENT_TOKEN)"
fi

sed -e "s/@AGENT_USER@/$USER/" -e "s/@STAGE@/$STAGE/" \
    "$SRC/systemd/dam-agent.service" \
    | sudo tee /etc/systemd/system/dam-agent.service > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable dam-agent --quiet

# Start only once the agent can identify itself (design 15 §6) — otherwise
# it would exit "not enrolled" and systemd would restart it every 5 s.
token_in() { [ -f "$1" ] && grep -qE '^ENROLLMENT_TOKEN=.+' "$1"; }
if [ -f /var/lib/dam-agent/credential.json ] || token_in "$ENV_FILE" \
        || token_in /boot/firmware/dam-agent.env \
        || grep -qE '^DEVICE_TOKEN=.+' "$ENV_FILE"; then
    sudo systemctl restart dam-agent
    echo "dam-agent (re)started — follow it with: journalctl -u dam-agent -f"
else
    echo "installed but NOT started: register the device in the webapp, put"
    echo "  ENROLLMENT_TOKEN=<token> into $ENV_FILE, then"
    echo "  sudo systemctl start dam-agent"
fi
