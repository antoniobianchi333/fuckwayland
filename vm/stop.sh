#!/usr/bin/env bash
# Stop the test VM: graceful poweroff over ssh, hard kill after a timeout.
. "$(dirname -- "$0")/common.sh"
require ssh

if ! pid=$(vm_pid); then
    echo "vm: not running"
    rm -f "$VM_DIR/qemu.pid"
    exit 0
fi

ssh -p "$SSH_PORT" "${SSH_OPTS[@]}" -o ConnectTimeout=3 "$SSH_DEST" poweroff 2>/dev/null || true
for _ in $(seq 1 30); do
    kill -0 "$pid" 2>/dev/null || { echo "vm: stopped"; rm -f "$VM_DIR/qemu.pid"; exit 0; }
    sleep 1
done
echo "vm: graceful poweroff timed out, killing pid $pid" >&2
kill "$pid" 2>/dev/null || true
sleep 3
kill -9 "$pid" 2>/dev/null || true
rm -f "$VM_DIR/qemu.pid"
echo "vm: killed"
