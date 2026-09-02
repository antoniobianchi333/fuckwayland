#!/usr/bin/env bash
# (Re)start headless sway as root inside the VM and print its env.
# libinput MUST stay in WLR_BACKENDS: uinput devices arrive through it.
. "$(dirname -- "$0")/common.sh"
require ssh

ssh -p "$SSH_PORT" "${SSH_OPTS[@]}" "$SSH_DEST" bash -s <<'REMOTE'
set -eu
pkill -x sway >/dev/null 2>&1 && sleep 1 || true

export XDG_RUNTIME_DIR=/run/wdotool-wl
mkdir -p "$XDG_RUNTIME_DIR"
chmod 700 "$XDG_RUNTIME_DIR"
rm -f "$XDG_RUNTIME_DIR"/wayland-* "$XDG_RUNTIME_DIR"/sway-ipc.*

cat > /root/sway-headless.conf <<'CFG'
output HEADLESS-1 mode 1280x720
output * background #1b2028 solid_color
xwayland enable
default_border none
# unaccelerated 1:1 pointer so injected REL_X/Y move by exact pixels
input type:pointer accel_profile flat
input type:pointer pointer_accel 0
CFG

env WLR_BACKENDS=headless,libinput WLR_LIBINPUT_NO_DEVICES=1 \
    WLR_RENDERER=pixman LIBSEAT_BACKEND=builtin \
    nohup sway -c /root/sway-headless.conf \
    </dev/null >"$XDG_RUNTIME_DIR/sway.log" 2>&1 &

WD= SOCK=
for _ in $(seq 1 50); do
    WD=$(cd "$XDG_RUNTIME_DIR" && ls wayland-* 2>/dev/null | grep -m1 -v '\.lock$' || true)
    SOCK=$(ls "$XDG_RUNTIME_DIR"/sway-ipc.* 2>/dev/null | head -1 || true)
    [ -n "$WD" ] && [ -n "$SOCK" ] && break
    sleep 0.2
done
if [ -z "$WD" ] || [ -z "$SOCK" ]; then
    echo "vm: sway failed to start; log tail:" >&2
    tail -30 "$XDG_RUNTIME_DIR/sway.log" >&2
    exit 1
fi
echo "XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR"
echo "WAYLAND_DISPLAY=$WD"
echo "SWAYSOCK=$SOCK"
REMOTE
