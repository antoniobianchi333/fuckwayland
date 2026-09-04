#!/bin/bash
# vmctl guest-side stage 2 for an ISO flavor.  NOT run on the host and NOT run by
# cloud-init: vm/build-iso-golden.sh copies this script into the freshly installed
# system and runs it there once, as root, over ssh (the installer switches cloud-init
# off in the target, so there is no cloud-init to run it).
#
# The counterpart of vm/build-image.sh -- and deliberately about a twentieth of its
# size.  build-image.sh turns a cloud image INTO a desktop and then makes that desktop
# quiet: no lock screen, no blanking, no DPMS, no idle sleep, no first-run dialogs, no
# apt-daily timers, no unattended-upgrades, no snap refresh.  Here the desktop is
# already the real thing and NONE of that may happen: an image whose job is to answer
# "does this work on a default install?" has to keep every default it was installed
# with.  So this script only does what the rig cannot work without, and each of those
# four things is a numbered DEVIATION recorded in the flavor yaml and vm/README.md.
#
# Inputs (/etc/vmctl-build.env, written by the host script): FLAVOR, DESKTOP.
. /etc/vmctl-build.env
: "${DESKTOP:=gnome}"
LOG=/var/log/vmctl-iso-build.log
say() { echo "vmctl-build: $*" | tee -a "$LOG"; }
fail() { say "FAILED: $*"; echo VMCTL-BUILD-FAIL; exit 1; }
trap 'fail "line $LINENO: $BASH_COMMAND"' ERR
set -eE -o pipefail
exec 2>>"$LOG"
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l APT_LISTCHANGES_FRONTEND=none UCF_FORCE_CONFFOLD=1
APT="apt-get -y -q -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold"

say "flavor $FLAVOR ($DESKTOP): $(. /etc/os-release; echo "$PRETTY_NAME"), kernel $(uname -r)"
say "installed by: $(sed -n 's/^ *//p' /var/log/installer/media-info 2>/dev/null | head -1)"

# ---- DEVIATION 5: bring the install up to date -------------------------------------
# "tested on a fresh, UPDATED, as-standard-as-possible installation": the installer
# applies security updates while it runs (updates: security, its default), a real
# machine then takes whatever Software Updater offers on the first day.  This is that,
# non-interactively.  It upgrades packages; it installs nothing new by hand.
say "waiting for snapd seeding to finish"
snap wait system seed.loaded || say "warning: snap wait failed"
say "apt-get update && full-upgrade (packages before: $(dpkg-query -f '.\n' -W | wc -l))"
$APT update
$APT full-upgrade
# No autoremove: Software Updater does not remove packages either, it only says that some
# could be removed.  Nothing a default install has may disappear from this image.
say "full-upgrade done (packages after: $(dpkg-query -f '.\n' -W | wc -l))"
say "snap refresh"
snap refresh 2>&1 | tail -5 | while read -r l; do say "  snap: $l"; done || say "warning: snap refresh failed"

# ---- DEVIATION 6: GDM autologin ----------------------------------------------------
# The VM has no keyboard: nothing can type a password into the GDM greeter (vmctl drives
# the guest over QEMU's D-Bus display, which is output only).  Without this the rig would
# stop at the greeter forever.  This is the "session coming up without a password" the
# rig needs, and it is edited INTO the shipped /etc/gdm3/custom.conf -- two keys in the
# [daemon] section, everything else in the file left as the package wrote it.  Not
# touched: WaylandEnable (stock: absent, i.e. Wayland), InitialSetupEnable (stock:
# absent, i.e. GDM's own initial-setup behaviour), AccountsService's per-user session.
[ -f /etc/gdm3/custom.conf ] || fail "no /etc/gdm3/custom.conf (is this a GDM install?)"
cp -a /etc/gdm3/custom.conf /etc/gdm3/custom.conf.stock
python3 - <<'PY'
import re
p = "/etc/gdm3/custom.conf"
s = open(p).read()
add = ("# vmctl (vm/build-iso-image.sh): the test VM has no keyboard for the greeter.\n"
       "AutomaticLoginEnable=true\nAutomaticLogin=test\n")
for key in ("AutomaticLoginEnable", "AutomaticLogin"):
    s = re.sub(r"(?m)^\s*#?\s*%s\s*=.*\n" % key, "", s)
if re.search(r"(?m)^\[daemon\]\s*$", s):
    s = re.sub(r"(?m)^(\[daemon\]\s*\n)", r"\1" + add, s, count=1)
else:
    s += "\n[daemon]\n" + add
open(p, "w").write(s)
PY
grep -q '^AutomaticLogin=test$' /etc/gdm3/custom.conf || fail "GDM autologin not written"
say "GDM: AutomaticLogin=test added to the shipped custom.conf (stock copy: custom.conf.stock)"

# ---- DEVIATION 8: the first-run experience, completed rather than removed ----------
# On a default install the first login runs gnome-initial-setup in existing-user mode --
# the "Welcome to Ubuntu" pages (Ubuntu Pro, telemetry, apps).  A person clicks through
# them once and never sees them again; a VM with no keyboard cannot, so every login here
# would come up with that window over the desktop and every screenshot would have it in.
# So the markers a completed run leaves behind are written -- exactly the paths the units'
# own conditions test, read out of the units -- and nothing else: the package stays
# installed, both its units stay enabled, and update-notifier and ubuntu-report keep their
# autostart entries, all three of which the seven cloud-image flavors hide.  The markers
# name THIS release, so a future release upgrade would show its dialog again, as on a real
# machine.
install -d -o test -g test -m 0700 /home/test/.config
[ -e /home/test/.config/gnome-initial-setup-done ] || echo yes > /home/test/.config/gnome-initial-setup-done
# gnome-initial-setup 50 has a SECOND unit, gnome-initial-setup-upgrade-login.service (the
# "Welcome to Ubuntu 26.04 LTS!" release-notes dialog), whose conditions are: the marker
# above exists AND its own does not.  So writing only the first marker switches that dialog
# on at every login -- measured, not guessed: with just the first marker this image came up
# running `gnome-initial-setup --upgrade-user`.  Write whatever the installed unit names.
for u in /usr/lib/systemd/user/gnome-initial-setup-upgrade-login.service; do
    [ -f "$u" ] || continue
    for m in $(sed -n 's/^ConditionPathExists=!%E\/\([^ ]*\)$/\1/p' "$u"); do
        install -d "/home/test/.config/$(dirname "$m")"
        touch "/home/test/.config/$m"
    done
done
chown -R test:test /home/test/.config
say "first-run: markers written ($(cd /home/test/.config && ls -d gnome-initial-setup-done gnome-initial-setup/* 2>/dev/null | tr '\n' ' ')); the package, its autostart, update-notifier and ubuntu-report all stay enabled"

# ---- DEVIATION 7: cloud-init back on, networking left to NetworkManager -------------
# The installer switches cloud-init off in the target (/etc/cloud/cloud.cfg.d/99-installer.cfg
# with datasource_list: [None], plus /etc/cloud/cloud-init.disabled).  vmctl gives every
# instance a NoCloud seed that sets the hostname and installs the root ssh key, so the
# golden has to be able to read it.  Undone the way the installer itself undoes it for a
# golden image: /etc/cloud/clean.d/99-installer deletes exactly those two files, and
# `cloud-init clean` runs it (below, last).  The one thing added on top keeps cloud-init
# AWAY from the network: without it cloud-init would write a networkd renderer for the NIC
# and take it off NetworkManager, which is not how the installed system was left.
install -d /etc/cloud/cloud.cfg.d
cat > /etc/cloud/cloud.cfg.d/99-vmctl-no-network.cfg <<'EOF'
# vmctl (vm/build-iso-image.sh): cloud-init is re-enabled in this golden image only to
# read vmctl's per-instance seed (hostname, root ssh key).  Networking stays exactly as
# the desktop installer left it: NetworkManager, /etc/netplan/01-network-manager-all.yaml.
network: {config: disabled}
EOF

# ---- the rig's own bookkeeping (files, not behaviour) -------------------------------
install -d /var/lib/vmctl
echo "$FLAVOR $(date -u +%FT%TZ)" > /var/lib/vmctl/golden
printf 'FLAVOR=%s\nDESKTOP=%s\n' "$FLAVOR" "$DESKTOP" > /var/lib/vmctl/flavor.env
dpkg-query -W -f='${binary:Package}\n' | sort > /var/lib/vmctl/packages.txt
snap list | awk 'NR > 1 { print $1 }' | sort > /var/lib/vmctl/snaps.txt
say "$(wc -l < /var/lib/vmctl/packages.txt) packages, $(wc -l < /var/lib/vmctl/snaps.txt) snaps"

# What a reader of this image will want to know without booting it: which of the things
# build-image.sh switches off are still on here.  Reported, never changed.
say "left as installed (this is the point of the flavor):"
for u in apt-daily.timer apt-daily-upgrade.timer unattended-upgrades.service motd-news.timer; do
    # is-enabled exits non-zero for a disabled or unknown unit: that is an answer, not an error
    say "  $u: $({ systemctl is-enabled "$u" 2>&1 || true; } | head -1)"
done
say "  snap auto-refresh: $({ snap refresh --time 2>&1 || true; } | tr '\n' ' ')"
say "  screen lock: $(sudo -u test -H gsettings get org.gnome.desktop.screensaver lock-enabled 2>/dev/null || echo '?')"
say "  idle-delay:  $(sudo -u test -H gsettings get org.gnome.desktop.session idle-delay 2>/dev/null || echo '?')"
say "  gnome-initial-setup-done marker: $([ -e /home/test/.config/gnome-initial-setup-done ] && echo present || echo absent)"
say "  /dev/uinput: $([ -e /dev/uinput ] && echo present || echo 'absent (module not loaded)')"

apt-get clean
sync
say "stage 2 finished; cloud-init clean, then the host powers the VM off"
# Last, because it wipes cloud-init's state: instance data, the installer's two
# disabling files (via /etc/cloud/clean.d/99-installer) and the machine id.
cloud-init clean --machine-id
if [ -e /etc/cloud/cloud-init.disabled ]; then fail "cloud-init is still disabled after clean"; fi
if [ -e /etc/cloud/cloud.cfg.d/99-installer.cfg ]; then fail "99-installer.cfg survived the clean"; fi
echo VMCTL-BUILD-OK
