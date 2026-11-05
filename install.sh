#!/usr/bin/env bash
# RB-SDR-Radio installer for Debian 12+ / Raspberry Pi OS 64-bit.
#
#     sudo ./install.sh                  install and enable the service
#     sudo ./install.sh --user NAME      run the service as NAME instead of pi
#     sudo ./install.sh --no-service     files only, no systemd
#     sudo ./install.sh --uninstall      remove everything except config and recordings
#
# Installs to /opt/rb-sdr-radio, config to /etc/rb-sdr-radio. Never needs root at
# runtime. The service is enabled but not started: pin the dongle first.
set -euo pipefail

PREFIX=/opt/rb-sdr-radio
CONFDIR=/etc/rb-sdr-radio
UNIT=rb-sdr-radio.service
PLAYBACK=/opt/stratux/www/playback
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DO_SERVICE=1
RUN_USER=pi
UNINSTALL=0

while [ $# -gt 0 ]; do
  case "$1" in
    --uninstall) UNINSTALL=1 ;;
    --no-service) DO_SERVICE=0 ;;
    --user) shift; RUN_USER="${1:?--user needs a name}" ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m    warning: %s\033[0m\n' "$*"; }
die()  { printf '\033[31m    error: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run with sudo"

if [ "$UNINSTALL" = 1 ]; then
  say "Uninstalling"
  systemctl disable --now "$UNIT" 2>/dev/null || true
  rm -f "/etc/systemd/system/$UNIT" /etc/udev/rules.d/99-rb-sdr-radio.rules \
        /etc/modprobe.d/rb-sdr-radio-blacklist.conf
  rm -rf "$PREFIX" "/etc/systemd/system/$UNIT.d"
  systemctl daemon-reload
  udevadm control --reload-rules || true
  echo "    kept: $CONFDIR and the recordings in $PLAYBACK"
  exit 0
fi

# --------------------------------------------------------------------------- #
say "Checking the platform"
. /etc/os-release 2>/dev/null || true
echo "    ${PRETTY_NAME:-unknown}, $(uname -m), python $(python3 -V 2>&1 | cut -d' ' -f2)"
python3 - <<'PY' || die "Python 3.11 or newer is required"
import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)
PY
getent passwd "$RUN_USER" >/dev/null || die "no user '$RUN_USER'; choose one with --user NAME"

# --------------------------------------------------------------------------- #
say "Installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
# rtl-sdr pulls in whichever librtlsdr this release packages, whatever its name.
apt-get install -y -qq --no-install-recommends \
  python3 python3-numpy python3-scipy python3-jsonschema \
  python3-aiohttp python3-websockets rtl-sdr alsa-utils usbutils \
  || die "package installation failed"

# --------------------------------------------------------------------------- #
# The V4's R828D tuner needs the rtl-sdr-blog fork of librtlsdr. On the stock driver
# it appears to tune and produces nothing usable, with no error (rb-vor O7).
say "Checking the SDR driver"
if command -v rtl_test >/dev/null 2>&1; then
  OUTPUT="$(timeout 5 rtl_test -t 2>&1 || true)"
  echo "$OUTPUT" | sed 's/^/    /'
  if echo "$OUTPUT" | grep -qi 'R828D\|Blog V4' \
     && ! strings /usr/lib/*/librtlsdr.so* /usr/local/lib/librtlsdr.so* 2>/dev/null \
          | grep -qi 'Blog V4'; then
    warn "RTL-SDR Blog V4 with the stock librtlsdr: install the vendor's fork,"
    warn "or it will hear nothing in the airband."
  fi
fi

say "Blacklisting the DVB-T kernel driver"
install -m 0644 "$SRC/systemd/rb-sdr-radio-blacklist.conf" /etc/modprobe.d/
for m in dvb_usb_rtl28xxu rtl2832 rtl2830; do
  modprobe -r "$m" 2>/dev/null || true
done

say "Installing udev rules and group membership"
getent group plugdev >/dev/null || groupadd plugdev
usermod -aG plugdev,audio "$RUN_USER"
install -m 0644 "$SRC/systemd/99-rb-sdr-radio.rules" /etc/udev/rules.d/
udevadm control --reload-rules && udevadm trigger || warn "udev reload failed"

# --------------------------------------------------------------------------- #
say "Installing files to $PREFIX"
install -d -m 0755 "$PREFIX" "$CONFDIR"
cp -r "$SRC/rbsdrradio" "$SRC/schema" "$SRC/config" "$SRC/tools" "$PREFIX/"
find "$PREFIX" -name __pycache__ -prune -exec rm -rf {} +
chmod -R a+rX "$PREFIX"

if [ -f "$CONFDIR/rb-sdr-radio.json" ]; then
  echo "    keeping existing $CONFDIR/rb-sdr-radio.json"
  cp "$SRC/config/rb-sdr-radio.json" "$CONFDIR/rb-sdr-radio.json.new"
  echo "    new default written to rb-sdr-radio.json.new for comparison"
else
  install -m 0644 "$SRC/config/rb-sdr-radio.json" "$CONFDIR/rb-sdr-radio.json"
fi

say "Validating the configuration"
( cd "$PREFIX" && python3 -c "
from rbsdrradio.config import load, ConfigError
import sys
try:
    print('    ok:', load('$CONFDIR/rb-sdr-radio.json').source)
except ConfigError as e:
    print('    ' + str(e)); sys.exit(1)
" ) || die "configuration is invalid; refusing to install a broken service"

say "Preparing the recording folder"
# On an RB image the folder is a symlink to USB storage, made by the build script.
# This never creates that link and never replaces what is already there.
if [ -d "$PLAYBACK" ]; then
  echo "    $PLAYBACK -> $(readlink -f "$PLAYBACK")"
  runuser -u "$RUN_USER" -- test -w "$PLAYBACK" \
    || warn "$RUN_USER cannot write to $PLAYBACK: recording will show as a fault."
elif [ -L "$PLAYBACK" ]; then
  warn "$PLAYBACK links to $(readlink "$PLAYBACK"), which is not there."
  warn "Is the USB storage mounted? Recording will show as a fault until it is."
elif [ -d "$(dirname "$PLAYBACK")" ]; then
  install -d "$PLAYBACK"
  chown "$RUN_USER:" "$PLAYBACK"
else
  warn "$(dirname "$PLAYBACK") does not exist: not an RB image."
  warn "Set record.dir in $CONFDIR/rb-sdr-radio.json, and ReadWritePaths in the unit."
fi

# --------------------------------------------------------------------------- #
if [ "$DO_SERVICE" = 1 ]; then
  say "Installing the systemd unit"
  install -m 0644 "$SRC/systemd/$UNIT" /etc/systemd/system/
  if [ "$RUN_USER" != pi ]; then
    install -d "/etc/systemd/system/$UNIT.d"
    printf '[Service]\nUser=%s\nEnvironment=XDG_RUNTIME_DIR=/run/user/%s\n' \
      "$RUN_USER" "$(id -u "$RUN_USER")" > "/etc/systemd/system/$UNIT.d/user.conf"
  fi
  systemctl daemon-reload
  systemctl enable "$UNIT"
  echo "    enabled (not started)"
fi

# --------------------------------------------------------------------------- #
say "Done. Next:"
cat <<MSG

    cd $PREFIX
    python3 -m rbsdrradio self-test          # the software, no hardware
    python3 -m rbsdrradio devices            # find the dongle's serial
    # put it in "device_serial" in $CONFDIR/rb-sdr-radio.json
    python3 -m rbsdrradio test-fm 101.5      # a loud local station
    python3 -m rbsdrradio test-am 118.7      # your nearest ATIS
    sudo systemctl start rb-sdr-radio

MSG
