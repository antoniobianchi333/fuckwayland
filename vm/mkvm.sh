#!/usr/bin/env bash
# Build the wdotool test VM: base image + ssh key + cloud-init seed + overlay disk.
# Idempotent: every step skips or regenerates cheaply if already done.
. "$(dirname -- "$0")/common.sh"
require curl ssh-keygen cloud-localds qemu-img

cd "$VM_DIR"
BASE=ubuntu-26.04-server-cloudimg-amd64.img

# 1. Base image (release channel, daily as fallback). Gitignored.
if [ ! -s "$BASE" ]; then
    for url in \
        https://cloud-images.ubuntu.com/releases/26.04/release/ubuntu-26.04-server-cloudimg-amd64.img \
        https://cloud-images.ubuntu.com/resolute/current/resolute-server-cloudimg-amd64.img
    do
        echo "vm: downloading $url"
        if curl -fL --retry 3 --connect-timeout 30 -o "$BASE.part" "$url"; then
            mv "$BASE.part" "$BASE"
            break
        fi
        rm -f "$BASE.part"
    done
    [ -s "$BASE" ] || { echo "vm: could not download the Ubuntu 26.04 cloud image" >&2; exit 1; }
fi

# 2. SSH keypair (no passphrase). Gitignored.
if [ ! -f keys/id_ed25519 ]; then
    mkdir -p keys
    ssh-keygen -q -t ed25519 -N '' -C wdotool-vm -f keys/id_ed25519
fi
PUBKEY=$(cat keys/id_ed25519.pub)

# 3. Cloud-init NoCloud seed. Regenerated every run (same instance-id, so
#    cloud-init will not re-run on an existing disk).
cat > user-data <<EOF
#cloud-config
hostname: wdotool-vm
manage_etc_hosts: true
disable_root: false
ssh_pwauth: false
ssh_authorized_keys:
  - $PUBKEY
write_files:
  - path: /etc/ssh/sshd_config.d/60-wdotool.conf
    content: "PermitRootLogin prohibit-password\n"
  - path: /etc/modules-load.d/uinput.conf
    content: "uinput\n"
package_update: true
packages:
  - sway
  - foot
  - grim
  - ffmpeg
  - jq
  - fonts-dejavu-core
runcmd:
  - modprobe uinput
  - systemctl reload ssh || true
EOF
cat > meta-data <<EOF
instance-id: wdotool-vm-1
local-hostname: wdotool-vm
EOF
cloud-localds seed.img user-data meta-data

# 4. Overlay disk backed by the base image. Gitignored.
if [ ! -f disk.qcow2 ]; then
    qemu-img create -f qcow2 -F qcow2 -b "$BASE" disk.qcow2 20G
fi

echo "vm: ready — boot with vm/run.sh"
