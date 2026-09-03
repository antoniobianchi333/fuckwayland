#!/usr/bin/env bash
# vmctl rig self-test.  Boots <flavor>-t with 3 heads and checks: GDM autologin
# into GNOME on Wayland (vmctl session), 3 monitors in mutter's DisplayConfig
# with Virtual-1 primary at (0,0), no stray dialog window, screenshots of every
# head that actually show a desktop (not a flat colour), hot-plug of a 4th head
# and its removal, and XDG_SESSION_ID/XDG_SESSION_TYPE in `vmctl user`.
# Leaves the VM running.   usage: vm/selftest.sh <flavor> [name]
set -eu
VM=$(cd -- "$(dirname -- "$0")" && pwd)/vmctl
flavor=$1; name=${2:-$flavor-t}
out=${OUT:-/tmp/vmctl-selftest-$name}; mkdir -p "$out"
t0=$(date +%s)
step() { echo "== [$(( $(date +%s) - t0 ))s] $*"; }

monitors() {
    "$VM" user "$name" -- gdbus call --session --dest org.gnome.Mutter.DisplayConfig \
        --object-path /org/gnome/Mutter/DisplayConfig \
        --method org.gnome.Mutter.DisplayConfig.GetCurrentState \
        | grep -o "'Virtual-[0-9]*'" | sort -u | tr -d "'" | tr '\n' ' '
}
layout() {   # primary logical monitor + open windows, from inside the session
    "$VM" user "$name" -- python3 - <<'EOF'
import gi, json, subprocess
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib
bus = Gio.bus_get_sync(Gio.BusType.SESSION)
st = bus.call_sync("org.gnome.Mutter.DisplayConfig", "/org/gnome/Mutter/DisplayConfig",
                   "org.gnome.Mutter.DisplayConfig", "GetCurrentState", None, None, 0, -1, None).unpack()
for x, y, scale, transform, primary, mons, props in st[2]:
    if primary:
        print("primary %s at %d,%d" % (mons[0][0], x, y))
# stray dialogs are Wayland-native GTK4 windows, invisible to wmctrl/xdotool (Xwayland),
# so look for the process instead: gnome-initial-setup (first-login or --upgrade-user
# "Welcome to Ubuntu" dialog) must not be running in a vmctl session
p = subprocess.run(["pgrep", "-a", "-f", "gnome-initial-setup"], capture_output=True, text=True)
print("initial-setup: %s" % (p.stdout.strip().replace("\n", "; ") or "none"))
EOF
}
check_shot() {   # a flat single-colour screendump = gnome-shell never finished its start-up
    local f=$1 sd
    if ! command -v identify >/dev/null; then echo "   $f: (imagemagick missing, content not checked)"; return 0; fi
    sd=$(identify -format '%[fx:standard_deviation]' "$f")
    if ! awk -v sd="$sd" 'BEGIN { exit !(sd > 0.01) }'; then
        echo "FAIL: $f is a flat image (stddev $sd): the desktop did not render"; exit 1
    fi
    echo "   $f: $(identify -format '%wx%h' "$f"), stddev $sd"
}
expect_monitors() {   # mutter needs a moment after a hotplug event
    local want=$1 got n
    for _ in $(seq 1 10); do
        got=$(monitors); n=$(echo $got | wc -w)
        [ "$n" = "$want" ] && { echo "   GetCurrentState: $n monitors: $got"; return 0; }
        sleep 2
    done
    echo "FAIL: expected $want monitors, GetCurrentState shows $n: $got"; exit 1
}

step "vmctl start $name --flavor $flavor --heads 3 --fresh"
port=$("$VM" start "$name" --flavor "$flavor" --heads 3 --fresh)
step "vmctl session $name"
"$VM" session "$name"
step "GetCurrentState: expect Virtual-1..3, Virtual-1 primary at 0,0, no gnome-initial-setup dialog"
expect_monitors 3
lay=$(layout); echo "   $lay" | tr '\n' ' '; echo
echo "$lay" | grep -q '^primary Virtual-1 at 0,0$' || { echo "FAIL: primary is not Virtual-1 at 0,0"; exit 1; }
echo "$lay" | grep -q '^initial-setup: none$' || { echo "FAIL: gnome-initial-setup dialog is running in the session"; exit 1; }
step "vmctl user: session id/type"
"$VM" user "$name" -- sh -c 'echo "   XDG_SESSION_ID=$XDG_SESSION_ID XDG_SESSION_TYPE=$XDG_SESSION_TYPE"; loginctl show-session "$XDG_SESSION_ID" -p Type -p State' | tr '\n' ' '; echo
"$VM" user "$name" -- sh -c 'loginctl show-session "$XDG_SESSION_ID" -p Type -p State' | grep -q '^Type=wayland' || { echo "FAIL: XDG_SESSION_ID is not a wayland session"; exit 1; }
step "vmctl heads $name"
"$VM" heads "$name"
step "vmctl shot $name --all $out/shot (each must show a desktop, not a flat colour)"
"$VM" shot "$name" --all "$out/shot"
for f in "$out"/shot-*.png; do check_shot "$f"; done
if [ -f "$out/shot-0.png" ] && [ -f "$out/shot-1.png" ] && command -v md5sum >/dev/null; then
    # the primary head carries the top bar and the dock: it must differ from a
    # secondary head. Identical images mean the screendumps were taken before
    # the shell painted (or the kernel console is still on all heads).
    if [ "$(md5sum < "$out/shot-0.png")" = "$(md5sum < "$out/shot-1.png")" ]; then
        echo "FAIL: shot-0.png and shot-1.png are identical: the desktop had not painted yet"; exit 1
    fi
    echo "   shot-0.png differs from shot-1.png (primary head has the top bar/dock)"
fi
step "vmctl head $name 3 1280x1024: expect a 4th monitor"
"$VM" head "$name" 3 1280x1024
expect_monitors 4
"$VM" heads "$name" | grep Virtual-4
step "vmctl head $name 3 off: expect 3 monitors again"
"$VM" head "$name" 3 off
expect_monitors 3
step "PASS: $name ($flavor) running, ssh port $port, screenshots in $out"
