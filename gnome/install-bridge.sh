#!/bin/sh
# install-bridge.sh — install, enable, check or remove the fuckwayland GNOME
# Shell bridge extension (fuckwayland-bridge@fuckwayland), and optionally the
# udev rule that opens /dev/uinput to the logged-in user.
#
#   install-bridge.sh [--system] [--try-unsafe] [--no-enable]   install + enable
#   install-bridge.sh --check                                    report status
#   install-bridge.sh --uninstall [--system]                     disable + remove
#   install-bridge.sh --udev                                     udev rule (sudo)
#   install-bridge.sh --udev --uninstall                         remove the rule
#
# POSIX sh. Needs cp; the live parts use gnome-extensions / gsettings / gdbus,
# all shipped with a GNOME desktop (gdbus is in libglib2.0-bin). Run it as the
# desktop user or through sudo: the desktop user is taken from $SUDO_USER
# (or $PKEXEC_UID) and every shell/gsettings call is made as that user on
# that user's session bus.
#
# What "enable" can and cannot do without a logout (GNOME 46 and 50 alike):
# gnome-shell scans the extension directories only at login. Enabling an
# extension it already knows is instant (EnableExtension over D-Bus). A
# directory that appeared after login is invisible until the next login —
# unless the shell is put into unsafe mode and told to load it (--try-unsafe,
# see bootstrap_unsafe below; documented in notes/PLAN.md §2 (c)).
set -eu

UUID='fuckwayland-bridge@fuckwayland'
BUS_NAME='org.fuckwayland.Bridge'
OBJ_PATH='/org/fuckwayland/Bridge'
IFACE='org.fuckwayland.Bridge1'
SHELL_DEST='org.gnome.Shell'
SHELL_PATH='/org/gnome/Shell'
EXT_IFACE='org.gnome.Shell.Extensions'
SYSTEM_DIR='/usr/share/gnome-shell/extensions'
UDEV_RULE='60-fuckwayland-uinput.rules'
UDEV_DEST="/etc/udev/rules.d/$UDEV_RULE"
MODLOAD_SRC='modules-load-uinput.conf'
MODLOAD_DEST='/etc/modules-load.d/fuckwayland-uinput.conf'

HERE=$(cd "$(dirname "$0")" && pwd)
SRC="$HERE/$UUID"
ORIG_ARGS=$*

MODE=install
SYSTEM=0
TRY_UNSAFE=0
DO_ENABLE=1
UDEV=0

usage() {
    cat <<EOU
Usage: install-bridge.sh [--system] [--try-unsafe] [--no-enable]
       install-bridge.sh --check
       install-bridge.sh --uninstall [--system]
       install-bridge.sh --udev [--uninstall]

  --system      install into $SYSTEM_DIR (via sudo) instead of
                ~/.local/share/gnome-shell/extensions
  --no-enable   copy the files only
  --try-unsafe  if the running shell has not loaded the extension yet, try the
                Looking Glass bootstrap (needs wdotool with working input
                injection; see the comments in this script) instead of asking
                for a logout
  --check       print whether the files, the gsettings entry, the loaded
                extension, the $BUS_NAME name and the udev rule are present
  --uninstall   disable and delete the extension (with --udev: remove the rule)
  --udev        install $UDEV_RULE + $MODLOAD_DEST (needs sudo) so the
                logged-in user can open /dev/uinput without a relogin
EOU
}

while [ $# -gt 0 ]; do
    case $1 in
        --system) SYSTEM=1 ;;
        --check) MODE=check ;;
        --uninstall) MODE=uninstall ;;
        --try-unsafe) TRY_UNSAFE=1 ;;
        --no-enable) DO_ENABLE=0 ;;
        --udev) UDEV=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "install-bridge.sh: unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

have() { command -v "$1" >/dev/null 2>&1; }

# --- who is the desktop user? ---------------------------------------------
ME=$(id -u)
if [ "$ME" = 0 ] && [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ]; then
    TARGET_USER=$SUDO_USER
elif [ "$ME" = 0 ] && [ -n "${PKEXEC_UID:-}" ] && [ "$PKEXEC_UID" != 0 ]; then
    TARGET_USER=$(id -un "$PKEXEC_UID")
else
    TARGET_USER=$(id -un)
fi
TARGET_UID=$(id -u "$TARGET_USER")
TARGET_HOME=$(getent passwd "$TARGET_USER" 2>/dev/null | cut -d: -f6 || true)
[ -n "$TARGET_HOME" ] || TARGET_HOME=$(eval echo "~$TARGET_USER")

if [ "$ME" = 0 ] && [ "$TARGET_UID" != 0 ]; then
    RUNTIME_DIR=/run/user/$TARGET_UID
    BUS_ADDR=unix:path=$RUNTIME_DIR/bus
else
    RUNTIME_DIR=${XDG_RUNTIME_DIR:-/run/user/$TARGET_UID}
    BUS_ADDR=${DBUS_SESSION_BUS_ADDRESS:-unix:path=$RUNTIME_DIR/bus}
fi

if [ "$SYSTEM" = 1 ]; then
    DEST=$SYSTEM_DIR/$UUID
    EXT_TYPE=1   # ExtensionType.SYSTEM
else
    DEST=$TARGET_HOME/.local/share/gnome-shell/extensions/$UUID
    EXT_TYPE=2   # ExtensionType.PER_USER
fi

# Run a command as the desktop user on the desktop user's session bus.
as_user() {
    if [ "$ME" = 0 ] && [ "$TARGET_UID" != 0 ]; then
        if have runuser; then
            runuser -u "$TARGET_USER" -- env DBUS_SESSION_BUS_ADDRESS="$BUS_ADDR" \
                XDG_RUNTIME_DIR="$RUNTIME_DIR" "$@"
        else
            sudo -u "$TARGET_USER" env DBUS_SESSION_BUS_ADDRESS="$BUS_ADDR" \
                XDG_RUNTIME_DIR="$RUNTIME_DIR" "$@"
        fi
    else
        env DBUS_SESSION_BUS_ADDRESS="$BUS_ADDR" XDG_RUNTIME_DIR="$RUNTIME_DIR" "$@"
    fi
}

# gd DEST PATH METHOD [ARGS...] — gdbus call on the session bus
gd() {
    gd_dest=$1; gd_path=$2; gd_method=$3; shift 3
    as_user gdbus call --session --dest "$gd_dest" --object-path "$gd_path" \
        --method "$gd_method" "$@"
}

shell_running() {
    have gdbus || return 1
    gd org.freedesktop.DBus /org/freedesktop/DBus org.freedesktop.DBus.NameHasOwner \
        "$SHELL_DEST" 2>/dev/null | grep -q true
}

name_owned() {
    have gdbus || return 1
    gd org.freedesktop.DBus /org/freedesktop/DBus org.freedesktop.DBus.NameHasOwner \
        "$BUS_NAME" 2>/dev/null | grep -q true
}

# The running shell knows the uuid (loaded at login or bootstrapped).
ext_loaded() {
    have gdbus || return 1
    gd "$SHELL_DEST" "$SHELL_PATH" "$EXT_IFACE.ListExtensions" 2>/dev/null | grep -q "'$UUID'"
}

# Numeric ExtensionState: 1 ACTIVE 2 INACTIVE 3 ERROR 4 OUT_OF_DATE 6 INITIALIZED.
ext_state() {
    have gdbus || { echo '-'; return; }
    gd "$SHELL_DEST" "$SHELL_PATH" "$EXT_IFACE.GetExtensionInfo" "$UUID" 2>/dev/null \
        | sed -n "s/.*'state': <\([0-9]*\)\(\.[0-9]*\)\{0,1\}>.*/\1/p" | head -n 1
}

ext_error() {
    have gdbus || return 0
    gd "$SHELL_DEST" "$SHELL_PATH" "$EXT_IFACE.GetExtensionErrors" "$UUID" 2>/dev/null || true
}

bridge_version() {
    have gdbus || return 1
    gd "$BUS_NAME" "$OBJ_PATH" "$IFACE.GetVersion" 2>/dev/null \
        | sed -n 's/^(uint32 \([0-9]*\),)$/\1/p'
}

eval_ok() {
    gd "$SHELL_DEST" "$SHELL_PATH" org.gnome.Shell.Eval '1+1' 2>/dev/null | grep -q '^(true'
}

setting_has() {   # setting_has KEY -> uuid listed in org.gnome.shell KEY
    have gsettings || return 1
    as_user gsettings get org.gnome.shell "$1" 2>/dev/null | grep -q "'$UUID'"
}

# Add/remove the uuid in the enabled-/disabled-extensions lists.
enable_setting() {
    if have gnome-extensions; then
        # Edits the gsettings lists; the shell enables a loaded extension at
        # once and a not-yet-loaded one at next login. On a uuid the running
        # shell has never seen, gnome-extensions prints "does not exist" and
        # touches nothing -- hence the gsettings fallback below.
        as_user gnome-extensions enable "$UUID" >/dev/null 2>&1 && setting_has enabled-extensions && return 0
    fi
    if ! have gsettings; then
        echo "install-bridge.sh: neither gnome-extensions nor gsettings found; add $UUID to org.gnome.shell enabled-extensions yourself" >&2
        return 1
    fi
    cur=$(as_user gsettings get org.gnome.shell enabled-extensions 2>/dev/null || echo '[]')
    case $cur in
        *"'$UUID'"*) ;;
        '@as []'|'[]') as_user gsettings set org.gnome.shell enabled-extensions "['$UUID']" ;;
        *) as_user gsettings set org.gnome.shell enabled-extensions "${cur%]}, '$UUID']" ;;
    esac
    cur=$(as_user gsettings get org.gnome.shell disabled-extensions 2>/dev/null || echo '[]')
    case $cur in
        *"'$UUID'"*)
            new=$(printf '%s' "$cur" | sed -e "s/, *'$UUID'//" -e "s/'$UUID', *//" -e "s/'$UUID'//")
            as_user gsettings set org.gnome.shell disabled-extensions "$new" ;;
    esac
}

disable_setting() {
    if have gnome-extensions; then
        as_user gnome-extensions disable "$UUID" >/dev/null 2>&1 && return 0
    fi
    have gsettings || return 0
    cur=$(as_user gsettings get org.gnome.shell enabled-extensions 2>/dev/null || echo '[]')
    case $cur in
        *"'$UUID'"*)
            new=$(printf '%s' "$cur" | sed -e "s/, *'$UUID'//" -e "s/'$UUID', *//" -e "s/'$UUID'//")
            as_user gsettings set org.gnome.shell enabled-extensions "$new" ;;
    esac
}

# --- Looking Glass bootstrap (the only no-logout path for a NEW directory) ---
# notes/PLAN.md §2 (c): Alt+F2 -> "lg" -> `global.context.unsafe_mode = true`
# -> Escape; then org.gnome.Shell.Eval loads the extension object from its
# directory and EnableExtension turns it on; finally unsafe mode goes back off.
# Fragile by nature: needs wdotool with working input injection (root, or the
# uinput udev rule), an unlocked session, the Alt+F2 run dialog available, and
# nothing grabbing the keyboard. Keep hands off the keyboard for ~5 s.
bootstrap_unsafe() {
    WDO=${WDOTOOL:-wdotool}
    if ! have "$WDO"; then
        echo "install-bridge.sh: --try-unsafe needs $WDO in PATH" >&2
        return 1
    fi
    if ! eval_ok; then
        echo "install-bridge.sh: opening Looking Glass to switch unsafe mode on (do not type)..."
        "$WDO" key --clearmodifiers alt+F2 || return 1
        sleep 1
        "$WDO" type --delay 40 lg
        "$WDO" key Return
        sleep 2
        "$WDO" type --delay 20 'global.context.unsafe_mode = true'
        "$WDO" key Return
        sleep 1
        "$WDO" key Escape
        sleep 1
        if ! eval_ok; then
            echo "install-bridge.sh: unsafe mode did not switch on; log out and back in instead" >&2
            return 1
        fi
    fi
    js="Main.extensionManager.loadExtension(Main.extensionManager.createExtensionObject('$UUID', Gio.File.new_for_path('$DEST'), $EXT_TYPE))"
    r=$(gd "$SHELL_DEST" "$SHELL_PATH" org.gnome.Shell.Eval "$js" 2>&1) || true
    case $r in
        "(true"*) ;;
        *) echo "install-bridge.sh: Eval(loadExtension) returned: $r" >&2 ;;
    esac
    gd "$SHELL_DEST" "$SHELL_PATH" "$EXT_IFACE.EnableExtension" "$UUID" >/dev/null 2>&1 || true
    # Unsafe mode off again whatever happened above.
    gd "$SHELL_DEST" "$SHELL_PATH" org.gnome.Shell.Eval 'global.context.unsafe_mode = false' \
        >/dev/null 2>&1 || true
    ext_loaded
}

# Make the freshly copied extension live without a logout when possible.
activate_live() {
    if ! have gdbus; then
        echo "install-bridge.sh: gdbus not found (package libglib2.0-bin); cannot talk to the shell" >&2
        return 1
    fi
    if ! shell_running; then
        echo "install-bridge.sh: no GNOME Shell on $BUS_ADDR; the extension loads at next login" >&2
        return 1
    fi
    if ext_loaded; then
        r=$(gd "$SHELL_DEST" "$SHELL_PATH" "$EXT_IFACE.EnableExtension" "$UUID" 2>&1) || true
        case $r in
            *true*) ;;
            *) echo "install-bridge.sh: EnableExtension returned: $r" >&2 ;;
        esac
    elif [ "$TRY_UNSAFE" = 1 ]; then
        bootstrap_unsafe || return 1
    else
        cat >&2 <<EOM
install-bridge.sh: the running GNOME Shell has not loaded $UUID yet.
  gnome-shell only scans extension directories at login, so on Wayland you
  must log out and back in once (on an X11 session Alt+F2, "r", Return
  restarts the shell in place). The extension is already enabled in
  gsettings and will come up automatically after that.
  Alternative without logout: rerun with --try-unsafe (needs wdotool).
EOM
        return 1
    fi
    i=0
    while [ $i -lt 10 ]; do
        name_owned && break
        sleep 1
        i=$((i + 1))
    done
    if ! name_owned; then
        echo "install-bridge.sh: extension enabled but $BUS_NAME is not owned; state $(ext_state)" >&2
        ext_error >&2
        return 1
    fi
    return 0
}

# --- udev rule for /dev/uinput -----------------------------------------------
udev_status() {
    if [ -f "$UDEV_DEST" ]; then u=yes; else u=no; fi
    if [ -f "$MODLOAD_DEST" ]; then m=yes; else m=no; fi
    if [ -e /dev/uinput ]; then n=$(stat -c '%U:%G %a' /dev/uinput 2>/dev/null || echo '?'); else n=missing; fi
    if grep -qw '^uinput' /proc/modules 2>/dev/null; then l=loaded; else l='not loaded'; fi
    echo "udev rule:        $u ($UDEV_DEST)"
    echo "modules-load:     $m ($MODLOAD_DEST)"
    echo "/dev/uinput:      $n, module $l"
    if have getfacl && [ -e /dev/uinput ]; then
        acl=$(getfacl -p /dev/uinput 2>/dev/null | sed -n 's/^user:\([^:][^:]*\):\(...\).*/\1:\2/p' | tr '\n' ' ')
        echo "uinput ACL users: ${acl:-none}"
        case " $acl" in
            *" $TARGET_USER:rw"*) echo "uinput usable by $TARGET_USER: yes" ;;
            *) if [ -w /dev/uinput ] && [ "$ME" != 0 ]; then echo "uinput usable by $TARGET_USER: yes (group/mode)";
               else echo "uinput usable by $TARGET_USER: no (run: sudo sh $0 --udev)"; fi ;;
        esac
    fi
}

do_udev() {
    if [ "$ME" != 0 ]; then
        # shellcheck disable=SC2086
        exec sudo -- "$0" $ORIG_ARGS
    fi
    if [ "$MODE" = uninstall ]; then
        rm -f "$UDEV_DEST" "$MODLOAD_DEST"
        have udevadm && { udevadm control --reload 2>/dev/null || true; udevadm trigger --name-match=uinput 2>/dev/null || true; }
        echo "install-bridge.sh: removed $UDEV_DEST and $MODLOAD_DEST (ACL goes away at next trigger/login)"
        return 0
    fi
    if [ ! -f "$HERE/$UDEV_RULE" ] || [ ! -f "$HERE/$MODLOAD_SRC" ]; then
        echo "install-bridge.sh: $UDEV_RULE / $MODLOAD_SRC not found next to this script" >&2
        exit 1
    fi
    install -m 644 "$HERE/$UDEV_RULE" "$UDEV_DEST"
    install -m 644 "$HERE/$MODLOAD_SRC" "$MODLOAD_DEST"
    modprobe uinput 2>/dev/null || echo "install-bridge.sh: modprobe uinput failed (no module? it may be built in)" >&2
    if have udevadm; then
        udevadm control --reload
        # Re-run the rule on the existing node: udev's uaccess builtin hands
        # the active seat session's user an ACL right now, no relogin.
        udevadm trigger --name-match=uinput
        udevadm settle --timeout=5 2>/dev/null || true
    else
        echo "install-bridge.sh: udevadm not found; the rule applies at next boot" >&2
    fi
    echo "install-bridge.sh: installed $UDEV_DEST and $MODLOAD_DEST"
    udev_status
}

# --- modes -------------------------------------------------------------------
do_check() {
    rc=1
    for d in "$TARGET_HOME/.local/share/gnome-shell/extensions/$UUID" "$SYSTEM_DIR/$UUID"; do
        if [ -f "$d/extension.js" ]; then
            echo "files:            $d"
        fi
    done
    if setting_has enabled-extensions; then e=yes; else e=no; fi
    echo "gsettings enabled: $e"
    if have gsettings; then
        dis=$(as_user gsettings get org.gnome.shell disable-user-extensions 2>/dev/null || echo '?')
        echo "user extensions disabled: $dis"
    fi
    if shell_running; then
        if ext_loaded; then
            echo "loaded in shell:  yes (state $(ext_state); 1=active 2=inactive 3=error 4=out-of-date 6=initialized)"
            st=$(ext_state)
            [ "$st" = 3 ] && ext_error
        else
            echo "loaded in shell:  no (log out/in, or --try-unsafe)"
        fi
    else
        echo "loaded in shell:  no GNOME Shell on $BUS_ADDR"
    fi
    if name_owned; then
        echo "$BUS_NAME owned: yes"
        echo "bridge version:   $(bridge_version)"
        rc=0
    else
        echo "$BUS_NAME owned: no"
    fi
    udev_status
    return $rc
}

do_install() {
    if [ ! -f "$SRC/metadata.json" ] || [ ! -f "$SRC/extension.js" ]; then
        echo "install-bridge.sh: extension sources not found in $SRC" >&2
        exit 1
    fi
    if [ "$SYSTEM" = 1 ] && [ "$ME" != 0 ]; then
        # shellcheck disable=SC2086
        exec sudo -- "$0" $ORIG_ARGS
    fi
    # Remember which parent directories we create so we can hand them to the
    # user when running under sudo.
    created=''
    if [ "$SYSTEM" = 0 ]; then
        p="$TARGET_HOME/.local"
        for sub in share gnome-shell extensions; do
            p="$p/$sub"
            [ -d "$p" ] || created="$created $p"
        done
    fi
    mkdir -p "$DEST"
    cp "$SRC/metadata.json" "$SRC/extension.js" "$DEST/"
    [ -f "$SRC/org.fuckwayland.Bridge1.xml" ] && cp "$SRC/org.fuckwayland.Bridge1.xml" "$DEST/"
    chmod 755 "$DEST"
    chmod 644 "$DEST"/*
    if [ "$ME" = 0 ] && [ "$SYSTEM" = 0 ]; then
        # shellcheck disable=SC2086
        [ -z "$created" ] || chown "$TARGET_USER:" $created
        chown -R "$TARGET_USER:" "$DEST"
    fi
    echo "install-bridge.sh: installed to $DEST"
    [ "$DO_ENABLE" = 1 ] || exit 0
    enable_setting || true
    if activate_live; then
        echo "install-bridge.sh: $BUS_NAME is live (version $(bridge_version))"
        exit 0
    fi
    exit 1
}

do_uninstall() {
    if [ "$SYSTEM" = 1 ] && [ "$ME" != 0 ]; then
        # shellcheck disable=SC2086
        exec sudo -- "$0" $ORIG_ARGS
    fi
    if shell_running && ext_loaded; then
        gd "$SHELL_DEST" "$SHELL_PATH" "$EXT_IFACE.DisableExtension" "$UUID" >/dev/null 2>&1 || true
    fi
    disable_setting || true
    rm -rf "$DEST"
    echo "install-bridge.sh: removed $DEST (the shell drops the loaded copy at next login)"
}

if [ "$UDEV" = 1 ]; then
    case $MODE in
        check) udev_status ;;
        *) do_udev ;;
    esac
    exit $?
fi

case $MODE in
    check) do_check ;;
    install) do_install ;;
    uninstall) do_uninstall ;;
esac
