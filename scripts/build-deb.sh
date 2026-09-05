#!/bin/sh
# Build the fuckwayland .deb.  One command, from a clean clone, on Ubuntu
# 24.04 and 26.04 alike:
#
#     sh scripts/build-deb.sh
#
# -> dist/fuckwayland_<version>_all.deb  (plus the .changes/.buildinfo)
#
#   --no-deps     do not apt-get anything; fail if a build tool is missing
#   --lintian     run lintian on the result (needs the `lintian` package)
#   --keep        keep debian/fuckwayland/ and the other build droppings
#
# Everything it installs is in the Ubuntu archive of both releases and is
# named in debian/control's Build-Depends; nothing here wants a PPA, a
# network build isolation step, or a tool a default desktop cannot apt-get.
# The package itself is Architecture: all and pure stdlib, so the .deb built
# on either release installs on the other.
set -eu

cd "$(dirname "$0")/.."

DEPS=0; LINTIAN=0; CLEAN=1
for a in "$@"; do
    case $a in
        --no-deps) DEPS=1 ;;
        --lintian) LINTIAN=1 ;;
        --keep) CLEAN=0 ;;
        -h|--help) sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "build-deb.sh: unknown option: $a" >&2; exit 2 ;;
    esac
done

# --- one version, from pyproject.toml ---------------------------------------
# debian/changelog is the Debian-side record and dpkg reads the version from
# there, so the two have to agree; pyproject.toml is the one that decides.
pver=$(sed -n 's/^version *= *"\([^"]*\)".*/\1/p' pyproject.toml | head -n 1)
dver=$(sed -n '1s/^[^ ]* *(\([^)]*\)).*/\1/p' debian/changelog)
if [ -z "$pver" ] || [ -z "$dver" ]; then
    echo "build-deb.sh: cannot read the version from pyproject.toml / debian/changelog" >&2
    exit 1
fi
if [ "$pver" != "$dver" ]; then
    cat >&2 <<EOM
build-deb.sh: version mismatch
  pyproject.toml:   $pver
  debian/changelog: $dver
Add a debian/changelog entry for $pver (dch -v $pver, or edit the top stanza).
EOM
    exit 1
fi
echo "build-deb.sh: building fuckwayland $pver"

# --- build tools -------------------------------------------------------------
# Kept in step with debian/control by hand; dpkg-checkbuilddeps below is what
# catches it if they ever drift.
APT_BUILD_DEPS='dpkg-dev debhelper dh-python pybuild-plugin-pyproject python3-all python3-setuptools'

need_apt=''
for p in $APT_BUILD_DEPS; do
    dpkg-query -W -f='${db:Status-Status}' "$p" 2>/dev/null | grep -q '^installed$' || need_apt="$need_apt $p"
done
if [ -n "$need_apt" ]; then
    if [ "$DEPS" = 1 ]; then
        echo "build-deb.sh: missing build tools:$need_apt" >&2
        echo "build-deb.sh: sudo apt-get install -y$need_apt" >&2
        exit 1
    fi
    echo "build-deb.sh: installing build tools:$need_apt"
    # shellcheck disable=SC2086
    sudo apt-get install -y $need_apt
fi

if ! dpkg-checkbuilddeps; then
    echo "build-deb.sh: debian/control asks for more than the list above installs" >&2
    exit 1
fi

# --- build -------------------------------------------------------------------
# -b: binary only.  No source package is made, so an untracked file or a dirty
# tree in the clone cannot fail the build, and nothing needs an orig tarball.
# Rules-Requires-Root: no means no fakeroot either.
dpkg-buildpackage -b -us -uc

mkdir -p dist
for f in ../fuckwayland_"$pver"_all.deb ../fuckwayland_"$pver"_*.changes \
         ../fuckwayland_"$pver"_*.buildinfo; do
    [ -e "$f" ] && mv -f "$f" dist/
done

if [ "$CLEAN" = 1 ]; then
    # dh_clean, plus the .egg-info and .pybuild pybuild leaves in the tree
    debian/rules clean >/dev/null 2>&1 || true
fi

deb=dist/fuckwayland_${pver}_all.deb
[ -f "$deb" ] || { echo "build-deb.sh: $deb was not produced" >&2; exit 1; }
echo "build-deb.sh: built $deb ($(wc -c < "$deb") bytes)"

if [ "$LINTIAN" = 1 ]; then
    if command -v lintian >/dev/null 2>&1; then
        lintian -i --profile ubuntu "$deb" || true
    else
        echo "build-deb.sh: lintian is not installed (sudo apt-get install lintian)" >&2
    fi
fi

echo
echo "Install it with:  sudo apt-get install ./$deb"
