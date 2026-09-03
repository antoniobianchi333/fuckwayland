#!/bin/bash
# vmctl guest-side golden-image build.  NOT run on the host: vmctl embeds this
# file into the flavor's cloud-init user-data (write_files -> /usr/local/sbin/
# vmctl-build) and cloud-init runs it once, as root, inside the build VM.
# Progress lines ("vmctl-build: ...") and the final markers go to the serial
# console so `vmctl build` can follow them and decide success.
#
# Inputs (written by the flavor yaml to /etc/vmctl-build.env):
#   FLAVOR       flavor name (also the hostname)
#   DESKTOP_PKG  the desktop metapackage, e.g. ubuntu-desktop
#   EXTRA_PKGS   test-support packages (real X tools for parity oracles)

. /etc/vmctl-build.env
LOG=/var/log/vmctl-build.log
exec >>"$LOG" 2>&1
# The serial console is the channel to `vmctl build` on the host.  A getty runs
# on ttyS0 and hangs the line up when it (re)starts, which kills every
# long-lived fd on it (a tee to /dev/ttyS0 died with EIO), so: stop the getty
# and open /dev/ttyS0 afresh for every write.  Everything else goes to $LOG.
systemctl stop serial-getty@ttyS0.service 2>/dev/null || true
con() { printf '%s\n' "$*" > /dev/ttyS0 2>/dev/null || true; }
say() { echo "vmctl-build: $*"; con "vmctl-build: $*"; }
fail() {
    say "FAILED: $*"
    { echo "vmctl-build: last lines of $LOG:"; tail -n 30 "$LOG"; echo VMCTL-BUILD-FAIL; } > /dev/ttyS0 2>/dev/null || true
    exit 1
}
trap 'fail "line $LINENO: $BASH_COMMAND"' ERR
set -eE -o pipefail

# NEEDRESTART_MODE=l: needrestart must not restart services mid-build -- restarting
# systemd-networkd after the desktop pulled in NetworkManager left the guest with no
# IP at all (24.04).  The image is powered off right after the build anyway.
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l APT_LISTCHANGES_FRONTEND=none UCF_FORCE_CONFFOLD=1
APT="apt-get -y -q -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold"
wait_net() {   # the network can vanish mid-build (networkd <-> NetworkManager hand-over)
    local i
    for i in $(seq 1 90); do
        getent hosts archive.ubuntu.com >/dev/null 2>&1 && return 0
        if [ "$i" = 15 ] || [ "$i" = 45 ]; then
            say "network is down; netplan apply"
            netplan apply >/dev/null 2>&1 || true
        fi
        sleep 2
    done
    return 1
}
apt_install() {
    local n
    for n in 1 2 3 4 5; do
        wait_net || say "warning: archive.ubuntu.com does not resolve"
        $APT install "$@" && return 0
        say "apt-get install $* failed (attempt $n/5); retrying in 15 s"
        sleep 15
        $APT update || true
    done
    return 1
}
say "flavor $FLAVOR: $(. /etc/os-release; echo "$PRETTY_NAME"), kernel $(uname -r)"

say "waiting for network"
wait_net || fail "no network"

say "apt-get update"
$APT update
# the small packages first: after the desktop install the network may be gone,
# and nothing that follows the desktop needs it
say "installing test-support packages: $EXTRA_PKGS"
apt_install $EXTRA_PKGS
say "installing $DESKTOP_PKG (expect 5-20 minutes)"
apt_install $DESKTOP_PKG

say "GDM: autologin of user test on Wayland, graphical.target"
install -d /etc/gdm3
cat > /etc/gdm3/custom.conf <<'EOF'
# Written by vmctl (vm/build-image.sh): autologin the test user into the
# Wayland session.  WaylandEnable is left true so GDM never falls back to Xorg.
[daemon]
AutomaticLoginEnable=true
AutomaticLogin=test
WaylandEnable=true
InitialSetupEnable=false

[security]

[xdmcp]

[chooser]

[debug]
EOF
install -d -m 0755 /var/lib/AccountsService/users
cat > /var/lib/AccountsService/users/test <<'EOF'
[User]
Session=ubuntu
XSession=ubuntu
SystemAccount=false
EOF
systemctl set-default graphical.target
# NetworkManager (from the desktop) now manages the NIC via netplan; networkd's
# wait-online would otherwise block network-online.target for its full 2 min
# timeout on every boot.  NetworkManager-wait-online covers the target.
systemctl disable systemd-networkd-wait-online.service 2>/dev/null || true

say "GNOME defaults: no screen lock/blank/idle sleep, no welcome tour (gschema override)"
cat > /usr/share/glib-2.0/schemas/90_vmctl.gschema.override <<'EOF'
# vmctl test rig: keep the desktop awake, unlocked and quiet.
[org.gnome.desktop.screensaver]
lock-enabled=false
idle-activation-enabled=false

[org.gnome.desktop.session]
idle-delay=uint32 0

[org.gnome.settings-daemon.plugins.power]
sleep-inactive-ac-type='nothing'
sleep-inactive-battery-type='nothing'
idle-dim=false

[org.gnome.shell]
welcome-dialog-last-shown-version='999'
EOF
glib-compile-schemas /usr/share/glib-2.0/schemas
# verify the override took (glib-compile-schemas ignores a whole override file on an unknown key)
[ "$(gsettings get org.gnome.desktop.session idle-delay 2>/dev/null)" = "uint32 0" ] || fail "gschema override not applied"
[ "$(gsettings get org.gnome.shell welcome-dialog-last-shown-version 2>/dev/null)" = "'999'" ] || fail "gschema override (shell) not applied"

say "user test: skip gnome-initial-setup (first login AND post-upgrade), hide update-notifier autostarts"
install -d -o test -g test -m 0700 /home/test/.config /home/test/.config/autostart /home/test/.config/gnome-initial-setup
echo yes > /home/test/.config/gnome-initial-setup-done
# gnome-initial-setup >= 50 (26.04) also has gnome-initial-setup-upgrade-login.service
# ("Welcome to Ubuntu 26.04 LTS!" / release notes dialog): it runs when the
# -done marker above EXISTS and gnome-initial-setup/upgrade-<release>-done does
# NOT, so the first-login marker alone turns that dialog on at every login.
# Create the marker for this release plus whatever the installed unit names.
rel=$(. /etc/os-release; echo "$VERSION_ID")
touch "/home/test/.config/gnome-initial-setup/upgrade-${rel}-done"
for u in /usr/lib/systemd/user/gnome-initial-setup-upgrade-login.service; do
  [ -f "$u" ] || continue
  for m in $(sed -n 's/^ConditionPathExists=!%E\/\([^ ]*\)$/\1/p' "$u"); do
    install -d "/home/test/.config/$(dirname "$m")"
    touch "/home/test/.config/$m"
  done
  say "post-upgrade dialog markers: $(ls /home/test/.config/gnome-initial-setup | tr '\n' ' ')"
done
for d in gnome-initial-setup-first-login update-notifier ubuntu-report-on-upgrade; do
  printf '[Desktop Entry]\nType=Application\nName=%s (disabled by vmctl)\nHidden=true\n' "$d" > "/home/test/.config/autostart/$d.desktop"
done
chown -R test:test /home/test/.config

say "disabling automatic apt / unattended-upgrades / snap refresh"
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "0";
APT::Periodic::Download-Upgradeable-Packages "0";
APT::Periodic::AutocleanInterval "0";
APT::Periodic::Unattended-Upgrade "0";
EOF
systemctl disable --now apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true
systemctl mask apt-daily.service apt-daily-upgrade.service >/dev/null 2>&1 || true
systemctl disable --now unattended-upgrades.service 2>/dev/null || true
systemctl disable --now motd-news.timer 2>/dev/null || true
snap refresh --hold >/dev/null 2>&1 || true

say "cleanup"
$APT autoremove
apt-get clean
install -d /var/lib/vmctl
echo "$FLAVOR $(date -u +%FT%TZ)" > /var/lib/vmctl/golden
dpkg-query -W -f='${binary:Package}\n' | sort > /var/lib/vmctl/packages.txt
say "$(wc -l < /var/lib/vmctl/packages.txt) packages installed; dumping the list to the serial console"
sync
dmesg -n 1
{ echo VMCTL-PACKAGES-BEGIN; cat /var/lib/vmctl/packages.txt; echo VMCTL-PACKAGES-END; } > /dev/ttyS0
say "golden image build finished"
con VMCTL-BUILD-OK
