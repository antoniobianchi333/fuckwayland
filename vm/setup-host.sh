#!/bin/sh
# vm/setup-host.sh -- prepare a Linux machine for the vm/ test rig (vm/SETUP.md).
#
# Checks for hardware virtualization and says what it found, installs the
# packages (Ubuntu 24.04 / 26.04: one apt-get, announced before it runs, the
# only thing here that needs sudo), verifies every QEMU feature vmctl relies
# on -- the dbus display backend, virtio-vga with four heads, SetUIInfo over
# D-Bus, QMP screendump to PNG -- by starting a paused, diskless machine for a
# fraction of a second, creates the directories, and prints what to run next.
# Idempotent: run it again after fixing something and it re-checks everything.
#
# usage: vm/setup-host.sh [--no-apt]
#   --no-apt   do not install anything; only check, create directories, report
# Exit status 0 when the rig can run here now, 1 when a step is left for you
# (each one is printed with the command that does it).
set -eu

HERE=$(cd -- "$(dirname -- "$0")" && pwd)
VMDATA=${VMDATA:-$HOME/vm-data}
VMIMAGES=${VMIMAGES:-$HOME/images}
NO_APT=
for a in "$@"; do
    case $a in
        --no-apt) NO_APT=1 ;;
        -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
        *) echo "setup-host: unknown option $a (see --help)" >&2; exit 2 ;;
    esac
done

# One line per fact.  "ok" is a check that passed, "--" a detail under it,
# "TODO" something this script cannot do for you, with the command that can.
todo_n=0
ok()   { printf 'ok    %s\n' "$*"; }
note() { printf '      %s\n' "$*"; }
todo() { printf 'TODO  %s\n' "$*"; todo_n=$((todo_n + 1)); }
head1() { printf '\n== %s\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }

# ---------------------------------------------------------------- hardware virtualization
head1 "hardware virtualization"
kvm_ok=1
flags=$(grep -m1 '^flags' /proc/cpuinfo 2>/dev/null || true)
case " $flags " in
    *" vmx "*) ok "CPU has hardware virtualization (vmx: Intel VT-x)" ;;
    *" svm "*) ok "CPU has hardware virtualization (svm: AMD-V)" ;;
    *)  kvm_ok=
        todo "the CPU shows neither 'vmx' nor 'svm' in /proc/cpuinfo: no hardware virtualization"
        note "On a physical machine that is a firmware setting (look for VT-x, AMD-V, SVM)."
        note "On a virtual machine the hypervisor underneath has to expose it to its guests" ;;
esac
virt=$(systemd-detect-virt 2>/dev/null || true)
case ${virt:-none} in
    none) note "this is a physical machine (systemd-detect-virt: none)" ;;
    *)    note "this machine is itself a virtual machine (systemd-detect-virt: $virt)"
          if [ -n "$kvm_ok" ]; then
              note "and its hypervisor exposes virtualization to it: nested virtualization works here"
          else
              note "(nested virtualization): it is not enabled for this machine, or not offered on this"
              note "kind of machine; the machine's own settings or its provider's documentation say which."
          fi ;;
esac
if [ -c /dev/kvm ]; then
    ok "/dev/kvm exists ($(stat -c '%A %U:%G' /dev/kvm))"
    if [ -r /dev/kvm ] && [ -w /dev/kvm ]; then
        ok "$(id -un) can open /dev/kvm"
    else
        kvm_ok=
        todo "$(id -un) cannot open /dev/kvm. Ubuntu gives access to the kvm group (and, through"
        note "an ACL, to a user logged in at the machine's own screen, which an ssh login is not):"
        note "    sudo usermod -aG kvm $(id -un)     # then log out and back in, and run this again"
    fi
elif [ -n "$kvm_ok" ]; then
    kvm_ok=
    todo "the CPU can do it, but there is no /dev/kvm: the kvm module is not loaded."
    note "    sudo modprobe kvm_intel   (or kvm_amd)   -- if that fails, the firmware has it off"
else
    note "no /dev/kvm either, as expected without the CPU feature"
fi
if [ -z "$kvm_ok" ]; then
    note "Without KVM nothing in vm/ boots: vmctl, build-iso-golden.sh and run.sh all pass"
    note "-enable-kvm, and QEMU then stops at 'failed to initialize kvm' instead of falling back to"
    note "software emulation. Everything below is still checked so that the rest is ready."
fi

# ---------------------------------------------------------------- packages
head1 "packages"
os_id=; os_ver=
if [ -r /etc/os-release ]; then
    os_id=$(. /etc/os-release; echo "${ID:-}"); os_ver=$(. /etc/os-release; echo "${VERSION_ID:-}")
    note "$( . /etc/os-release; echo "${PRETTY_NAME:-$os_id $os_ver}")"
fi
# What the rig calls, and the Ubuntu package that ships it:
#   qemu-system-x86_64                 qemu-system-x86
#   ui-dbus.so (-display dbus)         qemu-system-modules-opengl  (24.04 and 26.04; qemu-system-gui depends on it)
#   qemu-img                           qemu-utils
#   cloud-localds                      cloud-image-utils   (needs genisoimage)
#   isoinfo                            genisoimage         (build-iso-golden.sh)
#   dbus-daemon                        dbus
#   gdbus                              libglib2.0-bin
#   ssh scp ssh-keygen                 openssh-client
#   python3 >= 3.10                    python3             (vmctl is stdlib-only)
#   identify                           imagemagick         (selftest.sh, optional)
PKGS="qemu-system-x86 qemu-system-modules-opengl qemu-utils cloud-image-utils genisoimage dbus libglib2.0-bin openssh-client python3 imagemagick"
if have dpkg-query && have apt-get; then
    missing=
    for p in $PKGS; do
        case $(dpkg-query -W -f='${db:Status-Status}' "$p" 2>/dev/null || echo none) in
            installed) ;;
            *) missing="$missing $p" ;;
        esac
    done
    case "$os_id/$os_ver" in
        ubuntu/24.04|ubuntu/26.04) ;;
        *) note "not Ubuntu 24.04 or 26.04: the Ubuntu package names are tried anyway (apt found)" ;;
    esac
    if [ -z "$missing" ]; then
        ok "all packages installed:$(printf ' %s' $PKGS)"
    elif [ -n "$NO_APT" ]; then
        todo "packages missing (--no-apt given):$missing"
        note "    sudo apt-get install -y$missing"
    else
        SUDO=
        if [ "$(id -u)" != 0 ]; then
            if have sudo; then SUDO="sudo"; else
                todo "packages missing and no sudo here:$missing"
                note "    as root: apt-get install -y$missing"
            fi
        fi
        if [ "$(id -u)" = 0 ] || [ -n "$SUDO" ]; then
            note "missing:$missing"
            note "about to run (the one step that needs root):"
            note "    $SUDO apt-get update && $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y$missing"
            if $SUDO apt-get update && $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y $missing; then
                ok "installed:$missing"
            else
                todo "apt-get failed; fix that and run this again"
            fi
        fi
    fi
else
    note "no apt here: install the equivalents of$(printf ' %s' $PKGS) with your package manager;"
    note "the tools are checked one by one below"
fi

# ---------------------------------------------------------------- tools
head1 "tools"
tools_ok=1
for t in qemu-system-x86_64 qemu-img cloud-localds dbus-daemon gdbus ssh scp ssh-keygen python3; do
    if have "$t"; then ok "$t: $(command -v "$t")"; else tools_ok=; todo "$t not found"; fi
done
if have isoinfo; then ok "isoinfo: $(command -v isoinfo) (build-iso-golden.sh)"; else
    todo "isoinfo not found (genisoimage): vm/build-iso-golden.sh needs it, vmctl does not"; fi
if have identify; then ok "identify: $(command -v identify) (selftest.sh checks screenshots with it)"; else
    note "identify (imagemagick) not found: selftest.sh runs, but does not check screenshot content"; fi
if have python3; then
    if python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
        ok "$(python3 --version) (vmctl wants 3.10 or newer)"
    else
        tools_ok=; todo "$(python3 --version) is older than 3.10, which vmctl needs"
    fi
fi
if have qemu-system-x86_64; then
    qv=$(qemu-system-x86_64 --version | sed -n 's/^QEMU emulator version \([0-9][0-9.]*\).*/\1/p')
    qmaj=${qv%%.*}; qrest=${qv#*.}; qmin=${qrest%%.*}
    if [ "${qmaj:-0}" -gt 8 ] || { [ "${qmaj:-0}" -eq 8 ] && [ "${qmin:-0}" -ge 2 ]; }; then
        ok "QEMU $qv (the rig is proven on 8.2; screendump's format argument needs 7.1)"
    else
        note "QEMU $qv is older than 8.2, where the rig was proven; the probe below decides"
    fi
fi
if have cloud-localds; then
    t=$(mktemp -d)
    printf '#cloud-config\nhostname: probe\n' > "$t/user-data"
    printf 'instance-id: probe\n' > "$t/meta-data"
    if cloud-localds "$t/seed.img" "$t/user-data" "$t/meta-data" >/dev/null 2>"$t/err" && [ -s "$t/seed.img" ]; then
        ok "cloud-localds makes a NoCloud seed ($(stat -c %s "$t/seed.img") bytes)"
    else
        tools_ok=; todo "cloud-localds failed: $(head -c 300 "$t/err")"
    fi
    rm -rf "$t"
fi

# ---------------------------------------------------------------- QEMU features vmctl relies on
head1 "QEMU features"
qemu_ok=
if have qemu-system-x86_64; then
    if qemu-system-x86_64 -display help 2>/dev/null | grep -qx dbus; then
        ok "-display dbus is available"
        qemu_ok=1
    else
        todo "QEMU has no dbus display backend (-display help does not list it)."
        note "On Ubuntu it is ui-dbus.so in qemu-system-modules-opengl; elsewhere QEMU must be"
        note "built with -Ddbus_display=enabled (it needs gio and, for screendump, libpng)."
    fi
    if qemu-system-x86_64 -device help 2>/dev/null | grep -q '"virtio-vga"'; then
        ok "virtio-vga device present"
    else
        qemu_ok=; todo "QEMU has no virtio-vga device"
    fi
fi
if [ -n "$qemu_ok" ] && have dbus-daemon && have gdbus && have python3; then
    # The real thing, small: a private session bus, a paused machine with no disk
    # and no KVM, QEMU's four Console_N objects on that bus, one SetUIInfo (what
    # `vmctl head` does), one QMP screendump to PNG (what `vmctl shot` does).
    t=$(mktemp -d)
    probe_cleanup() {
        if [ -f "$t/qemu.pid" ]; then kill "$(cat "$t/qemu.pid")" 2>/dev/null || true; fi
        if [ -f "$t/dbus.pid" ]; then kill "$(cat "$t/dbus.pid")" 2>/dev/null || true; fi
        rm -rf "$t"
    }
    trap probe_cleanup EXIT
    dbus-daemon --session --fork --nopidfile --print-pid=1 --address="unix:path=$t/bus" > "$t/dbus.pid"
    if qemu-system-x86_64 -S -nodefaults -m 128 -display "dbus,addr=unix:path=$t/bus,p2p=no" \
            -device virtio-vga,id=gpu0,max_outputs=4,edid=on \
            -qmp "unix:$t/qmp,server,nowait" -daemonize -pidfile "$t/qemu.pid" 2>"$t/qemu.err"; then
        ok "a paused machine with -display dbus and virtio-vga,max_outputs=4 starts"
        i=0; consoles=
        while [ $i -lt 50 ]; do
            consoles=$(gdbus introspect --address "unix:path=$t/bus" --dest org.qemu \
                       --object-path /org/qemu/Display1 2>/dev/null | grep -o 'node Console_[0-9]*' | sort | tr '\n' ' ')
            case $consoles in *Console_3*) break ;; esac
            i=$((i + 1)); sleep 0.1
        done
        case $consoles in
            *Console_0*Console_1*Console_2*Console_3*) ok "QEMU owns org.qemu on the bus and exports $consoles" ;;
            *) qemu_ok=; todo "QEMU did not export /org/qemu/Display1/Console_0..3 on its bus (got: ${consoles:-nothing})" ;;
        esac
        if gdbus call --address "unix:path=$t/bus" --dest org.qemu --object-path /org/qemu/Display1/Console_1 \
                --method org.qemu.Display1.Console.SetUIInfo 0 0 0 0 1920 1080 >/dev/null 2>"$t/err"; then
            ok "SetUIInfo on Console_1 accepted (hot-plug of a head, what 'vmctl head' does)"
        else
            qemu_ok=; todo "SetUIInfo failed: $(head -c 300 "$t/err")"
        fi
        if python3 - "$t/qmp" "$t/shot.png" <<'PY' 2>"$t/err"
import json, socket, sys
s = socket.socket(socket.AF_UNIX); s.settimeout(10); s.connect(sys.argv[1]); f = s.makefile("rwb")
f.readline()                                                       # greeting
for cmd in ({"execute": "qmp_capabilities"},
            {"execute": "screendump", "arguments": {"filename": sys.argv[2], "device": "gpu0",
                                                    "head": 0, "format": "png"}}):
    f.write((json.dumps(cmd) + "\n").encode()); f.flush()
    r = json.loads(f.readline())
    if "error" in r:
        sys.exit("QMP %s: %s" % (cmd["execute"], r["error"].get("desc", r["error"])))
PY
        then
            if [ -s "$t/shot.png" ] && [ "$(head -c 4 "$t/shot.png" | od -An -c | tr -d ' ')" = "211PNG" ]; then
                ok "QMP screendump of head 0 in PNG works ($(stat -c %s "$t/shot.png") bytes)"
            else
                qemu_ok=; todo "screendump wrote no PNG (QEMU built without libpng?)"
            fi
        else
            qemu_ok=; todo "QMP screendump with format=png failed: $(head -c 300 "$t/err")"
            note "the PNG format needs QEMU 7.1 or newer built with libpng"
        fi
    else
        qemu_ok=; todo "QEMU refused to start with -display dbus: $(head -c 300 "$t/qemu.err")"
    fi
    probe_cleanup; trap - EXIT
elif [ -n "$qemu_ok" ]; then
    note "dbus-daemon, gdbus or python3 missing: the live probe is skipped"
fi

# ---------------------------------------------------------------- directories
head1 "directories"
for d in "$VMDATA" "$VMDATA/golden" "$VMDATA/instances" "$VMDATA/build" "$VMDATA/keys" "$VMIMAGES"; do
    mkdir -p "$d"
done
chmod 700 "$VMDATA/keys"
ok "\$VMDATA   $VMDATA  (golden/ instances/ build/ keys/)"
ok "\$VMIMAGES $VMIMAGES"
avail=$(df -k --output=avail "$VMDATA" | tail -1)
note "free space there: $((avail / 1048576)) GB (a golden image is 1-9 GB, see vm/SETUP.md)"
if [ "$(df --output=target "$VMDATA" | tail -1)" != "$(df --output=target "$VMIMAGES" | tail -1)" ]; then
    note "(different filesystems: fine; a golden refers to its base image by absolute path)"
fi

# ---------------------------------------------------------------- images
head1 "images the flavors want in \$VMIMAGES"
imgs_missing=
for b in $(sed -n 's/^#[[:space:]]*vmctl-base:[[:space:]]*//p' "$HERE"/flavors/*.yaml | sort -u); do
    if [ -s "$VMIMAGES/$b" ]; then ok "$b ($(du -h "$VMIMAGES/$b" | cut -f1))"; else
        note "missing: $b"; imgs_missing="$imgs_missing $b"; fi
done
iso=$(sed -n 's/^#[[:space:]]*vmctl-iso:[[:space:]]*//p' "$HERE"/flavors/*.yaml | sort -u | head -1)
iso_url=$(sed -n 's/^#[[:space:]]*vmctl-iso-url:[[:space:]]*//p' "$HERE"/flavors/*.yaml | sort -u | head -1)
if [ -n "$iso" ]; then
    if [ -s "$VMIMAGES/$iso" ]; then ok "$iso ($(du -h "$VMIMAGES/$iso" | cut -f1); ISO flavor)"; else
        note "missing: $iso (ISO flavor only; ${iso_url:-see releases.ubuntu.com})"; fi
fi
if [ -n "$imgs_missing" ]; then
    note "cloud images come from https://cloud-images.ubuntu.com/ -- e.g."
    note "    cd $VMIMAGES && curl -LO https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img"
    note "(one download per release you want to build; vm/SETUP.md lists them)"
fi

# ---------------------------------------------------------------- verdict
head1 "next"
if [ -n "$kvm_ok" ] && [ -n "$tools_ok" ] && [ -n "$qemu_ok" ] && [ "$todo_n" = 0 ]; then
    ok "this machine can run the rig"
    note "    $HERE/vmctl build noble-gnome         # first golden image, about 7 minutes (needs the noble image above)"
    note "    $HERE/selftest.sh noble-gnome         # boots it with 3 heads and checks the rig end to end"
    note "    $HERE/vmctl stop noble-gnome-t        # selftest leaves its VM running"
    exit 0
fi
note "$todo_n step(s) marked TODO above are left for you; run this again afterwards."
exit 1
