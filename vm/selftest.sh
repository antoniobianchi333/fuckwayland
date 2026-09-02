#!/usr/bin/env bash
# vmctl rig self-test.  Boots <flavor>-t with 3 heads and checks: GDM autologin
# into GNOME on Wayland (vmctl session), 3 monitors in mutter's DisplayConfig,
# screenshots of every head, hot-plug of a 4th head and its removal.
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
step "GetCurrentState: expect Virtual-1..3"
expect_monitors 3
step "vmctl heads $name"
"$VM" heads "$name"
step "vmctl shot $name --all $out/shot"
"$VM" shot "$name" --all "$out/shot"
step "vmctl head $name 3 1280x1024: expect a 4th monitor"
"$VM" head "$name" 3 1280x1024
expect_monitors 4
"$VM" heads "$name" | grep Virtual-4
step "vmctl head $name 3 off: expect 3 monitors again"
"$VM" head "$name" 3 off
expect_monitors 3
step "PASS: $name ($flavor) running, ssh port $port, screenshots in $out"
