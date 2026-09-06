#!/bin/sh
# install-overlap.sh — install, enable, check or remove fuckwayland-overlap,
# the GNOME Shell extension behind `wxrandr --unsafe-gnome-overlap`.
#
#   install-overlap.sh [--system] [--no-enable]   install + enable
#   install-overlap.sh --check                    report status, and probe
#   install-overlap.sh --uninstall [--system]     disable + remove
#
# This is deliberately NOT part of install-bridge.sh and it is not installed by
# the .deb.  The bridge is feature-detected JavaScript over public API and is
# meant to be installed and forgotten; this one ships a compiled type
# description pinned to the private layout of one libmutter generation and
# writes into gnome-shell's own memory.  Two different kinds of thing, two
# installers, two enable steps, and nobody gets this one by accident.
#
# It is safe to leave installed and enabled: the extension does nothing at
# login and nothing at all until wxrandr calls it.  --check proves that by
# asking it to run every guard and report, which writes nothing.
#
# POSIX sh, same conventions as install-bridge.sh: run it as the desktop user
# or through sudo (the user comes from $SUDO_USER / $PKEXEC_UID and every
# gsettings/gdbus call is made as that user on that user's session bus).
set -eu

UUID='fuckwayland-overlap@fuckwayland'
BUS_NAME='org.fuckwayland.Overlap'
OBJ_PATH='/org/fuckwayland/Overlap'
IFACE='org.fuckwayland.Overlap1'
SHELL_DEST='org.gnome.Shell'
SHELL_PATH='/org/gnome/Shell'
EXT_IFACE='org.gnome.Shell.Extensions'
SYSTEM_DIR='/usr/share/gnome-shell/extensions'

HERE=$(cd "$(dirname "$0")" && pwd)
SRC="$HERE/$UUID"

MODE=install
SYSTEM=0
DO_ENABLE=1

usage() {
    cat <<EOU
Usage: install-overlap.sh [--system] [--no-enable]
       install-overlap.sh --check
       install-overlap.sh --uninstall [--system]

  --system     install into $SYSTEM_DIR (via sudo)
  --no-enable  copy the files only
  --check      files, gsettings entry, extension state, $BUS_NAME, and a
               Probe: every guard run against the running libmutter, no write
  --uninstall  disable and delete the extension

What this extension is for, and what it risks, is in gnome/README.md and in
docs/WXRANDR.md under "--unsafe-gnome-overlap".  Nothing else in fuckwayland
needs it: wdotool, wwmctl and wxprop need the *bridge*, which is a different
extension with a different installer.
EOU
}

while [ $# -gt 0 ]; do
    case $1 in
        --system) SYSTEM=1 ;;
        --check) MODE=check ;;
        --uninstall) MODE=uninstall ;;
        --no-enable) DO_ENABLE=0 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "install-overlap.sh: unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

have() { command -v "$1" >/dev/null 2>&1; }

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
else
    DEST=$TARGET_HOME/.local/share/gnome-shell/extensions/$UUID
fi

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

gd() {
    gd_dest=$1; gd_path=$2; gd_method=$3; shift 3
    as_user gdbus call --session --dest "$gd_dest" --object-path "$gd_path" \
        --method "$gd_method" "$@"
}

shell_version() {
    have gdbus || return 1
    as_user gdbus call --session --dest "$SHELL_DEST" --object-path "$SHELL_PATH" \
        --method org.freedesktop.DBus.Properties.Get "$SHELL_DEST" ShellVersion \
        2>/dev/null | sed -n "s/^(<'\([^']*\)'>,)$/\1/p"
}

name_owned() {
    have gdbus || return 1
    gd org.freedesktop.DBus /org/freedesktop/DBus org.freedesktop.DBus.NameHasOwner \
        "$BUS_NAME" 2>/dev/null | grep -q true
}

ext_loaded() {
    have gdbus || return 1
    gd "$SHELL_DEST" "$SHELL_PATH" "$EXT_IFACE.ListExtensions" 2>/dev/null | grep -q "'$UUID'"
}

ext_state() {
    have gdbus || { echo '-'; return; }
    gd "$SHELL_DEST" "$SHELL_PATH" "$EXT_IFACE.GetExtensionInfo" "$UUID" 2>/dev/null \
        | sed -n "s/.*'state': <\([0-9]*\)\(\.[0-9]*\)\{0,1\}>.*/\1/p" | head -n 1
}

setting_has() {
    have gsettings || return 1
    as_user gsettings get org.gnome.shell "$1" 2>/dev/null | grep -q "'$UUID'"
}

enable_setting() {
    if have gnome-extensions; then
        as_user gnome-extensions enable "$UUID" >/dev/null 2>&1 && setting_has enabled-extensions && return 0
    fi
    have gsettings || {
        echo "install-overlap.sh: add $UUID to org.gnome.shell enabled-extensions yourself" >&2
        return 1
    }
    cur=$(as_user gsettings get org.gnome.shell enabled-extensions 2>/dev/null || echo '[]')
    case $cur in
        *"'$UUID'"*) ;;
        '@as []'|'[]') as_user gsettings set org.gnome.shell enabled-extensions "['$UUID']" ;;
        *) as_user gsettings set org.gnome.shell enabled-extensions "${cur%]}, '$UUID']" ;;
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

copy_files() {
    if [ "$SYSTEM" = 1 ] && [ "$ME" != 0 ]; then
        if [ "$DO_ENABLE" = 0 ]; then
            exec sudo -- "$0" --system --no-enable
        fi
        exec sudo -- "$0" --system
    fi
    mkdir -p "$DEST/typelib"
    cp -f "$SRC/metadata.json" "$SRC/extension.js" "$SRC/rules.js" \
          "$SRC/generations.json" "$SRC/org.fuckwayland.Overlap1.xml" "$DEST/"
    cp -f "$SRC"/typelib/*.typelib "$DEST/typelib/"
    if [ "$SYSTEM" = 0 ] && [ "$ME" = 0 ] && [ "$TARGET_UID" != 0 ]; then
        chown -R "$TARGET_USER" "$TARGET_HOME/.local/share/gnome-shell" 2>/dev/null || true
    fi
    echo "install-overlap.sh: installed into $DEST"
}

# Everything version-specific comes out of the table, so this script has no
# list of its own to fall out of step with it.  Two greps over one file, which
# this project generates and keeps one record per line: the namespaces (one
# typelib each must be present) and the GNOME majors (which shells this has been
# measured on).
TABLE="$SRC/generations.json"

table_field() {
    sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\"\{0,1\}\([^\",]*\).*/\1/p" "$TABLE"
}

case $MODE in
install)
    [ -d "$SRC" ] || { echo "install-overlap.sh: $SRC is missing" >&2; exit 1; }
    [ -f "$TABLE" ] || { echo "install-overlap.sh: $TABLE is missing" >&2; exit 1; }
    for ns in $(table_field namespace); do
        t="$SRC/typelib/$ns-1.0.typelib"
        [ -f "$t" ] || {
            echo "install-overlap.sh: $t is missing; run" \
                 "python3 gnome/overlap-typelib/gen-gir.py" >&2
            exit 1
        }
    done
    MAJORS=$(table_field shell_major | tr '\n' ' ')
    v=$(shell_version || true)
    if [ -z "${v:-}" ]; then
        echo "install-overlap.sh: no running GNOME Shell to ask; installing anyway" >&2
    else
        known=no
        for m in $MAJORS; do
            case ${v:-} in "$m"|"$m".*) known=yes ;; esac
        done
        [ "$known" = yes ] || \
            echo "install-overlap.sh: GNOME Shell $v: this extension has been" \
                 "measured on ${MAJORS% } only and refuses to write on anything" \
                 "else.  Installing it is harmless -- it does nothing until it is" \
                 "called -- but wxrandr --unsafe-gnome-overlap will say this" \
                 "compositor is not measured, and will print what a maintainer" \
                 "needs to add it." >&2
    fi
    copy_files
    if [ "$DO_ENABLE" = 1 ]; then
        enable_setting || true
        if ext_loaded; then
            gd "$SHELL_DEST" "$SHELL_PATH" "$EXT_IFACE.EnableExtension" "$UUID" >/dev/null 2>&1 || true
        fi
    fi
    if name_owned; then
        echo "install-overlap.sh: $BUS_NAME is up"
        exit 0
    fi
    cat >&2 <<'EOM'
install-overlap.sh: log out and back in once.  gnome-shell scans extension
directories only at login, and on Wayland it cannot be restarted in place.
The extension is already enabled, so it comes up by itself after the re-login
-- and comes up idle: it does nothing until wxrandr calls it.
EOM
    exit 1
    ;;
check)
    echo "uuid:         $UUID"
    echo "files:        $([ -f "$DEST/extension.js" ] && echo "$DEST" || echo 'not installed')"
    echo "table:        $([ -f "$DEST/generations.json" ] && echo installed || echo MISSING)"
    echo "typelibs:     $(ls "$DEST/typelib" 2>/dev/null | tr '\n' ' ' || echo none)"
    echo "shell:        $(shell_version || echo unknown)"
    echo "enabled:      $(setting_has enabled-extensions && echo yes || echo no)"
    echo "loaded:       $(ext_loaded && echo yes || echo 'no (log out and back in)')"
    echo "state:        $(ext_state)   (1 ACTIVE 2 INACTIVE 3 ERROR 4 OUT_OF_DATE 6 INITIALIZED)"
    echo "$BUS_NAME: $(name_owned && echo owned || echo 'not owned')"
    if name_owned; then
        echo "probe:"
        gd "$BUS_NAME" "$OBJ_PATH" "$IFACE.Probe" '{}' 2>&1 | sed 's/^/  /'
    fi
    ;;
uninstall)
    disable_setting || true
    if ext_loaded; then
        gd "$SHELL_DEST" "$SHELL_PATH" "$EXT_IFACE.DisableExtension" "$UUID" >/dev/null 2>&1 || true
    fi
    if [ "$SYSTEM" = 1 ] && [ "$ME" != 0 ]; then
        exec sudo -- "$0" --system --uninstall
    fi
    rm -rf "$DEST"
    echo "install-overlap.sh: removed $DEST"
    ;;
esac
