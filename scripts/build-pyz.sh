#!/bin/sh
# Build dist/<tool>: single-file executable zipapps (stdlib-only; warandr
# imports the system PyGObject/GTK 3 at run time).
set -eu
cd "$(dirname "$0")/.."

# Stamped into every zipapp so that a *copy* of one of these files installed
# under an original's name is recognised as ours by
# fwcommon.passthrough.is_us() (the head sniff, which is the only "not us"
# guard that survives two copies under two names in two PATH directories).
# zipapp stores members uncompressed and __main__.py sorts first, so the
# stamp lands in plain text in the first few hundred bytes -- which is what
# the guard reads. The guard matches the whole of "$STAMP", never the bare
# word: a third-party wrapper that merely mentions the project must not be
# mistaken for one of ours. Keep both in sync with
# fwcommon/passthrough.py:MARKER / STAMP.
MARKER=fuckwayland
STAMP="$MARKER-clone:"

build() { # name entry_module packages...
  name=$1; entry=$2; shift 2
  rm -rf dist/.stage
  mkdir -p dist/.stage
  for p in "$@"; do cp -r "$p" "dist/.stage/$p"; done
  find dist/.stage -name __pycache__ -type d -exec rm -rf {} +
  printf '# %s-clone: %s (X11 passthrough marker, see fwcommon/passthrough.py)\nimport sys\nfrom %s import main\n\nsys.exit(main())\n' \
    "$MARKER" "$name" "$entry" > dist/.stage/__main__.py
  printf '%s %s\n' "$STAMP" "$name" > "dist/.stage/_${MARKER}_marker"
  python3 -m zipapp dist/.stage -p "/usr/bin/env python3" -o "dist/$name"
  rm -rf dist/.stage
  chmod +x "dist/$name"
  # the guard reads the first 4 KiB: fail the build if the stamp is not there
  if ! head -c 4096 "dist/$name" | grep -aqF "$STAMP"; then
    echo "build-pyz: $name: stamp '$STAMP' missing from the first 4 KiB" >&2
    exit 1
  fi
  echo "built dist/$name ($(wc -c < "dist/$name") bytes)"
}

# fwcommon is in every bundle: every tool here finds its session, and every
# tool on an X11 session hands over to the original.
#
# wwmctl/wxprop ride with the package they import for backends and the X
# wire client, wdotool. backend_kwin uses x11_mini to read the X plane: the
# XWayland ids KWin 6 does not export, and the WM_CLASS pair 5.27
# lower-cases; since it lives in wdotool the shipped wdotool answers like a
# source checkout with nothing else in the bundle. wxrandr, warandr and
# wmirror carry wdotool for three files of it -- ctx.py for the exception
# class fwcommon.session raises, stdio.py and procs.py -- and zipapp copies
# whole package directories, so they still carry all of it.
build wdotool wdotool.cli fwcommon wdotool
build wwmctl  wwmctl.cli  fwcommon wdotool wwmctl
build wxprop  wxprop.cli  fwcommon wdotool wxprop
build wxrandr wxrandr.cli fwcommon wdotool wxrandr
# warandr bundles wxrandr: on Wayland it runs the same interpreter with
# -m wxrandr, PYTHONPATH pointing at the pyz itself (zipimport)
build warandr warandr.cli fwcommon wdotool wxrandr warandr
# wmirror drives an external binary but reads the layout through wxrandr's
# own wlr client, and the detached supervisor is this same zipapp re-entered
# by fork, so it needs nothing else in the bundle.
build wmirror wmirror.cli fwcommon wdotool wxrandr wmirror
