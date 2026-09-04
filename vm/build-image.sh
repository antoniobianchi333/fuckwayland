#!/bin/bash
# vmctl guest-side golden-image build.  NOT run on the host: vmctl embeds this
# file into the flavor's cloud-init user-data (write_files -> /usr/local/sbin/
# vmctl-build) and cloud-init runs it once, as root, inside the build VM.
# Progress lines ("vmctl-build: ...") and the final markers go to the serial
# console so `vmctl build` can follow them and decide success.
#
# Inputs (written by the flavor yaml to /etc/vmctl-build.env):
#   FLAVOR       flavor name (also the hostname)
#   DESKTOP      gnome | kde | kde-x11 | xfce | sway  (default gnome): which display
#                manager / session set-up below applies
#   DESKTOP_PKG  the desktop metapackage(s), e.g. ubuntu-desktop, or
#                "kubuntu-desktop plasma-workspace-wayland"
#   EXTRA_PKGS   test-support packages (real X tools for parity oracles)

. /etc/vmctl-build.env
: "${DESKTOP:=gnome}"
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
# hide an /etc/xdg/autostart entry for user test (a Hidden=true override in ~/.config/autostart)
hide_autostart() {
    local d
    install -d -o test -g test -m 0700 /home/test/.config /home/test/.config/autostart
    for d in "$@"; do
        printf '[Desktop Entry]\nType=Application\nName=%s (disabled by vmctl)\nHidden=true\n' "$d" > "/home/test/.config/autostart/$d.desktop"
    done
}
say "flavor $FLAVOR ($DESKTOP): $(. /etc/os-release; echo "$PRETTY_NAME"), kernel $(uname -r)"

say "waiting for network"
wait_net || fail "no network"

say "apt-get update"
$APT update
# the small packages first: after the desktop install the network may be gone,
# and nothing that follows the desktop needs it
say "installing test-support packages: $EXTRA_PKGS"
apt_install $EXTRA_PKGS
if [ "$DESKTOP" = xfce ]; then
  # xubuntu-desktop also drags in gdm3 (on 26.04, through gnome-shell), and the
  # first display manager to configure itself would become THE display manager:
  # answer debconf's question before any of them asks it.
  echo "lightdm shared/default-x-display-manager select lightdm" | debconf-set-selections
fi
say "installing $DESKTOP_PKG (expect 5-20 minutes)"
apt_install $DESKTOP_PKG

case "$DESKTOP" in
gnome)
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
hide_autostart gnome-initial-setup-first-login update-notifier ubuntu-report-on-upgrade
;;

kde|kde-x11)
# Plasma 5.27 (24.04): the Wayland session is plasmawayland.desktop from
# plasma-workspace-wayland (NOT a dependency of kubuntu-desktop there, so the
# flavor lists it); Plasma 6 (26.04): plasma.desktop from plasma-session-wayland.
# The X11 session (DESKTOP=kde-x11) is plasma.desktop from plasma-workspace on
# 5.27 and plasmax11.desktop from plasma-workspace-x11 on 6.
sess=""
if [ "$DESKTOP" = kde-x11 ]; then
  for f in /usr/share/xsessions/plasmax11.desktop /usr/share/xsessions/plasma.desktop; do
    [ -f "$f" ] && { sess=${f##*/}; break; }
  done
  [ -n "$sess" ] || fail "no Plasma X11 session file in /usr/share/xsessions: $(ls /usr/share/xsessions 2>/dev/null | tr '\n' ' ')"
  # SDDM's autologin resolves the session NAME against the Wayland directory
  # first (Display::attemptAutologin), so a name present in both directories
  # would quietly start the Wayland session -- the one thing this flavor must
  # never do.  Refuse to build an image whose session type is ambiguous.
  [ -f "/usr/share/wayland-sessions/$sess" ] && \
    fail "$sess exists in /usr/share/xsessions AND /usr/share/wayland-sessions; SDDM would autologin into the Wayland one"
  kind="X11 (Xorg + kwin_x11)"
else
  for f in /usr/share/wayland-sessions/plasma.desktop /usr/share/wayland-sessions/plasmawayland.desktop; do
    [ -f "$f" ] && { sess=${f##*/}; break; }
  done
  [ -n "$sess" ] || fail "no Plasma Wayland session file in /usr/share/wayland-sessions: $(ls /usr/share/wayland-sessions 2>/dev/null | tr '\n' ' ')"
  kind="Wayland"
fi
say "SDDM: autologin of user test into the Plasma $kind session ($sess), graphical.target"
install -d /etc/sddm.conf.d
cat > /etc/sddm.conf.d/autologin.conf <<EOF
# Written by vmctl (vm/build-image.sh): autologin the test user into Plasma on $kind.
[Autologin]
User=test
Session=$sess
Relogin=false
EOF
# SDDM 0.20 still defaults DisplayServer=x11 for its greeter; the session itself
# is started from the directory the name was found in (see above).
install -d -m 0755 /var/lib/AccountsService/users
cat > /var/lib/AccountsService/users/test <<EOF
[User]
Session=${sess%.desktop}
SystemAccount=false
EOF
kw=$(command -v kwriteconfig6 || command -v kwriteconfig5 || true)
[ -n "$kw" ] || fail "kwriteconfig5/6 not found (libkf5config-bin / libkf6config-bin)"
say "KDE: no screen lock / display power management for user test ($kw: kscreenlockerrc, powerdevilrc)"
install -d -o test -g test -m 0700 /home/test/.config
cfg=/home/test/.config
$kw --file $cfg/kscreenlockerrc --group Daemon --key Autolock false
$kw --file $cfg/kscreenlockerrc --group Daemon --key LockOnResume false
# the same as system-wide defaults (cascaded by KConfig; harmless duplicates)
$kw --file /etc/xdg/kscreenlockerrc --group Daemon --key Autolock false
$kw --file /etc/xdg/kscreenlockerrc --group Daemon --key LockOnResume false
if [ "${kw##*/}" = kwriteconfig6 ]; then
  # powerdevil 6: per-profile groups in powerdevilrc
  for prof in AC Battery LowBattery; do
    $kw --file $cfg/powerdevilrc --group $prof --group Display --key DimDisplayWhenIdle false
    $kw --file $cfg/powerdevilrc --group $prof --group Display --key TurnOffDisplayWhenIdle false
    $kw --file $cfg/powerdevilrc --group $prof --group SuspendAndShutdown --key AutoSuspendAction 0
  done
else
  # powerdevil 5: profiles live in powermanagementprofilesrc (SimpleConfig, no
  # /etc/xdg cascade; generated with idle actions only when the file is empty).
  # An action is disabled by its group being absent: keep just the button handling.
  cat > $cfg/powermanagementprofilesrc <<'EOF'
[AC]
icon=battery-charging

[AC][HandleButtonEvents]
lidAction=1
powerButtonAction=16
powerDownAction=16
triggerLidActionWhenExternalMonitorPresent=false

[Battery]
icon=battery-060

[Battery][HandleButtonEvents]
lidAction=1
powerButtonAction=16
powerDownAction=16
triggerLidActionWhenExternalMonitorPresent=false

[LowBattery]
icon=battery-low

[LowBattery][HandleButtonEvents]
lidAction=1
powerButtonAction=16
powerDownAction=16
triggerLidActionWhenExternalMonitorPresent=false
EOF
fi
say "user test: hide the Plasma welcome centre / Discover update notifier autostarts"
hide_autostart org.kde.plasma-welcome org.kde.discover.notifier
# Plasma 6: the welcome centre is no autostart entry any more but launched by a
# kded module (kded_plasma_welcome) whenever plasma-welcomerc's LastSeenVersion
# is missing or older than plasma-welcome itself.  Mark this version as seen.
if v=$(dpkg-query -W -f='${Version}' plasma-welcome 2>/dev/null); then
  v=${v#*:}; v=${v%%-*}
  $kw --file $cfg/plasma-welcomerc --group General --key LastSeenVersion "$v"
  say "plasma-welcome $v marked as seen (plasma-welcomerc)"
fi
;;

xfce)
[ -f /usr/share/xsessions/xubuntu.desktop ] || fail "no /usr/share/xsessions/xubuntu.desktop (xubuntu-default-settings)"
# `ls` of a missing path fails; keep the pipeline (set -o pipefail, ERR trap) happy
dms=$({ ls /usr/sbin/gdm3 /usr/bin/sddm 2>/dev/null || true; } | tr '\n' ' ')
say "LightDM is the display manager (installed alongside: ${dms:-none})"
echo /usr/sbin/lightdm > /etc/X11/default-display-manager
systemctl disable gdm gdm3 sddm 2>/dev/null || true       # drops their display-manager.service alias
systemctl enable lightdm
[ "$(readlink -f /etc/systemd/system/display-manager.service)" = /usr/lib/systemd/system/lightdm.service ] \
  || fail "display-manager.service is not lightdm: $(readlink -f /etc/systemd/system/display-manager.service)"
say "LightDM: autologin of user test into the xubuntu (X11) session, graphical.target"
install -d /etc/lightdm/lightdm.conf.d
cat > /etc/lightdm/lightdm.conf.d/50-autologin.conf <<'EOF'
# Written by vmctl (vm/build-image.sh): autologin the test user into Xfce (Xorg).
[Seat:*]
autologin-user=test
autologin-session=xubuntu
autologin-user-timeout=0
EOF
# Ubuntu's lightdm-autologin PAM stack needs no group; add test to the ones
# other distributions gate autologin on, if a package created them.
for g in autologin nopasswdlogin; do
  getent group "$g" >/dev/null && usermod -aG "$g" test && say "user test added to group $g"
done
install -d -m 0755 /var/lib/AccountsService/users
cat > /var/lib/AccountsService/users/test <<'EOF'
[User]
XSession=xubuntu
SystemAccount=false
EOF
say "Xfce: xfce4-screensaver/light-locker off, no blanking/DPMS/idle sleep (xfconf), autostarts hidden"
xc=/home/test/.config/xfce4/xfconf/xfce-perchannel-xml
install -d -o test -g test -m 0700 /home/test/.config /home/test/.config/xfce4 /home/test/.config/xfce4/xfconf "$xc"
cat > "$xc/xfce4-screensaver.xml" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!-- vmctl test rig: never lock or blank -->
<channel name="xfce4-screensaver" version="1.0">
  <property name="saver" type="empty">
    <property name="enabled" type="bool" value="false"/>
    <property name="idle-activation" type="empty">
      <property name="enabled" type="bool" value="false"/>
    </property>
  </property>
  <property name="lock" type="empty">
    <property name="enabled" type="bool" value="false"/>
    <property name="saver-activation" type="empty">
      <property name="enabled" type="bool" value="false"/>
    </property>
    <property name="sleep-activation" type="empty">
      <property name="enabled" type="bool" value="false"/>
    </property>
  </property>
</channel>
EOF
cat > "$xc/xfce4-power-manager.xml" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!-- vmctl test rig: no DPMS, no blanking, no idle sleep, no lock on suspend -->
<channel name="xfce4-power-manager" version="1.0">
  <property name="xfce4-power-manager" type="empty">
    <property name="dpms-enabled" type="bool" value="false"/>
    <property name="blank-on-ac" type="int" value="0"/>
    <property name="blank-on-battery" type="int" value="0"/>
    <property name="dpms-on-ac-sleep" type="uint" value="0"/>
    <property name="dpms-on-ac-off" type="uint" value="0"/>
    <property name="dpms-on-battery-sleep" type="uint" value="0"/>
    <property name="dpms-on-battery-off" type="uint" value="0"/>
    <property name="inactivity-on-ac" type="uint" value="14"/>
    <property name="inactivity-on-battery" type="uint" value="14"/>
    <property name="lock-screen-suspend-hibernate" type="bool" value="false"/>
    <property name="presentation-mode" type="bool" value="true"/>
  </property>
</channel>
EOF
cat > "$xc/xfce4-session.xml" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<channel name="xfce4-session" version="1.0">
  <property name="shutdown" type="empty">
    <property name="LockScreen" type="bool" value="false"/>
  </property>
</channel>
EOF
cat > "$xc/displays.xml" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!-- vmctl test rig: what xfsettingsd does with a hot-plugged output (/Notify:
     0 nothing, 1 the "new display" dialog = the default, 2 mirror, 3 extend):
     extend it, so a hot-plug shows up in xrandr - -listmonitors with no dialog -->
<channel name="displays" version="1.0">
  <property name="Notify" type="int" value="3"/>
</channel>
EOF
chown -R test:test /home/test/.config/xfce4
hide_autostart xfce4-screensaver light-locker update-notifier
;;

sway)
say "greetd: autologin of user test into sway on vt 1 (logind seat session), graphical.target"
[ -x /usr/sbin/greetd ] || fail "greetd is not installed"
cat > /etc/greetd/config.toml <<'EOF'
# Written by vmctl (vm/build-image.sh).  [initial_session] is greetd's autologin:
# on the first run after boot greetd starts the test user's sway straight away,
# as a Class=user logind session on seat0 / vt 1 (pam_systemd; greetd exports
# XDG_SESSION_TYPE=tty, and sway's libseat then switches the logind Type to
# wayland).  Only when that session ends does [default_session] run -- here
# sway again (as a greeter-class session), so a logout brings the desktop back
# on a VM that has no keyboard for a text greeter.  No seatd: sway takes the
# DRM device through libseat's logind backend.
[terminal]
vt = 1

[default_session]
command = "sway"
user = "test"

[initial_session]
command = "sway"
user = "test"
EOF
install -d /etc/systemd/system/greetd.service.d
cat > /etc/systemd/system/greetd.service.d/vmctl-vt1.conf <<'EOF'
# vmctl: the packaged unit only conflicts with getty@tty7 (its default vt);
# we run on vt 1, so keep the tty1 getty away from it.
[Unit]
Conflicts=getty@tty1.service
After=getty@tty1.service
EOF
systemctl disable getty@tty1.service 2>/dev/null || true
systemctl enable greetd.service
say "sway: config for user test = the packaged default + xwayland, Virtual-N left to right, session env export"
cat > /usr/local/bin/vmctl-sway-layout <<'EOF'
#!/usr/bin/env python3
"""vmctl: put sway's outputs Virtual-1, Virtual-2, ... side by side at y=0 in
connector order, once, when the session starts.  Stock sway/wlroots adds the
initial outputs to the layout in reverse enumeration order, which on a 3-head
virtio-vga puts Virtual-3 at (0,0) and Virtual-1 at x=3840; the rig's contract
is head 0 = Virtual-1 at (0,0).  Nothing is watched afterwards: a test that
moves outputs, or a hot-plugged head (wlroots appends it on the right), is
left alone."""
import json, subprocess

def sway(*args):
    return subprocess.run(["swaymsg", *args], capture_output=True, text=True)

def key(o):
    n = o["name"].rsplit("-", 1)[-1]
    return int(n) if n.isdigit() else 1 << 30

outs = sorted((o for o in json.loads(sway("-t", "get_outputs").stdout) if o.get("active")), key=key)
x = 0
for o in outs:
    sway("output", o["name"], "pos", str(x), "0")
    x += o["rect"]["width"]
if outs:
    sway("focus", "output", outs[0]["name"])
EOF
chmod 0755 /usr/local/bin/vmctl-sway-layout
install -d -o test -g test -m 0700 /home/test/.config /home/test/.config/sway
{
  cat /etc/sway/config
  cat <<'EOF'

### vmctl additions (vm/build-image.sh) -- everything above is /etc/sway/config verbatim
# Xwayland for the X11-parity oracles (xdotool/wmctrl/xprop/xrandr through Xwayland);
# "enable" = lazy start on the first X client (sway's default when Xwayland is installed).
xwayland enable
# workspace N on Virtual-N (stock sway hands workspace 1 to whichever output it
# enabled first, which is Virtual-3 on a 3-head virtio-vga)
workspace 1 output Virtual-1
workspace 2 output Virtual-2
workspace 3 output Virtual-3
workspace 4 output Virtual-4
# Publish the session to the systemd user manager + D-Bus activation environment so
# user services and `vmctl user` can find it (greetd starts sway with a bare env).
exec dbus-update-activation-environment --systemd WAYLAND_DISPLAY SWAYSOCK DISPLAY XDG_CURRENT_DESKTOP=sway XDG_SESSION_TYPE=wayland
# Virtual-1 at (0,0), Virtual-2 to its right, ... (once; see the script)
exec /usr/local/bin/vmctl-sway-layout
EOF
} > /home/test/.config/sway/config
chown test:test /home/test/.config/sway/config
sway -C -c /home/test/.config/sway/config >/dev/null 2>&1 || say "warning: sway -C could not validate the config here (no display); check in the guest"
;;

*) fail "unknown DESKTOP=$DESKTOP (gnome|kde|kde-x11|xfce|sway)" ;;
esac

systemctl set-default graphical.target
# NetworkManager (from the desktop) now manages the NIC via netplan; networkd's
# wait-online would otherwise block network-online.target for its full 2 min
# timeout on every boot.  NetworkManager-wait-online covers the target.  (The
# sway flavor keeps networkd: nothing installs NetworkManager there.)
if [ -x /usr/sbin/NetworkManager ]; then
  systemctl disable systemd-networkd-wait-online.service 2>/dev/null || true
fi
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
printf 'FLAVOR=%s\nDESKTOP=%s\n' "$FLAVOR" "$DESKTOP" > /var/lib/vmctl/flavor.env
dpkg-query -W -f='${binary:Package}\n' | sort > /var/lib/vmctl/packages.txt
say "$(wc -l < /var/lib/vmctl/packages.txt) packages installed; dumping the list to the serial console"
sync
dmesg -n 1
{ echo VMCTL-PACKAGES-BEGIN; cat /var/lib/vmctl/packages.txt; echo VMCTL-PACKAGES-END; } > /dev/ttyS0
say "golden image build finished"
con VMCTL-BUILD-OK
