#!/usr/bin/env bash
# Boot the wdotool test VM (daemonized QEMU/KVM). Refuses to double-start.
. "$(dirname -- "$0")/common.sh"
require qemu-system-x86_64

if pid=$(vm_pid); then
    echo "vm: already running (pid $pid) — use vm/stop.sh first" >&2
    exit 1
fi
rm -f "$VM_DIR/qemu.pid"
[ -f "$VM_DIR/disk.qcow2" ] && [ -f "$VM_DIR/seed.img" ] \
    || { echo "vm: no disk/seed — run vm/mkvm.sh first" >&2; exit 1; }

cd "$VM_DIR"
qemu-system-x86_64 \
    -enable-kvm -cpu host -m 6G -smp 4 \
    -display none -daemonize -pidfile qemu.pid \
    -serial file:serial.log \
    -drive file=disk.qcow2,if=virtio,format=qcow2 \
    -drive file=seed.img,if=virtio,format=raw,readonly=on \
    -device virtio-rng-pci \
    -netdev user,id=net0,hostfwd=tcp:127.0.0.1:$SSH_PORT-:22 \
    -device virtio-net-pci,netdev=net0

echo "vm: booted (pid $(cat qemu.pid)); ssh on 127.0.0.1:$SSH_PORT, serial in vm/serial.log"
