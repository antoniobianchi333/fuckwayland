#!/bin/sh
# Build dist/<tool>: single-file executable zipapps (stdlib-only; warandr
# imports the system PyGObject/GTK 3 at run time).
set -eu
cd "$(dirname "$0")/.."

# Stamped into every zipapp so that a *copy* of one of these files installed
# under an original's name is recognised as ours by
# wdotool.passthrough.is_us() (the head sniff, which is the only "not us"
# guard that survives two copies under two names in two PATH directories).
# zipapp stores members uncompressed and __main__.py sorts first, so the
# marker lands in plain text in the first few hundred bytes -- which is what
# the guard reads. Keep it in sync with wdotool/passthrough.py:MARKER.
MARKER=fuckwayland

build() { # name entry_module packages...
  name=$1; entry=$2; shift 2
  rm -rf dist/.stage
  mkdir -p dist/.stage
  for p in "$@"; do cp -r "$p" "dist/.stage/$p"; done
  find dist/.stage -name __pycache__ -type d -exec rm -rf {} +
  printf '# %s-clone: %s (X11 passthrough marker, see wdotool/passthrough.py)\nimport sys\nfrom %s import main\n\nsys.exit(main())\n' \
    "$MARKER" "$name" "$entry" > dist/.stage/__main__.py
  printf '%s-clone: %s\n' "$MARKER" "$name" > "dist/.stage/_${MARKER}_marker"
  python3 -m zipapp dist/.stage -p "/usr/bin/env python3" -o "dist/$name"
  rm -rf dist/.stage
  chmod +x "dist/$name"
  # the guard reads the first 4 KiB: fail the build if the stamp is not there
  if ! head -c 4096 "dist/$name" | grep -aq "$MARKER"; then
    echo "build-pyz: $name: marker '$MARKER' missing from the first 4 KiB" >&2
    exit 1
  fi
  echo "built dist/$name ($(wc -c < "dist/$name") bytes)"
}

# wwmctl/wxprop ride with the packages they import (wdotool backends, x11_mini)
build wdotool wdotool.cli wdotool
build wwmctl  wwmctl.cli  wdotool wwmctl
build wxprop  wxprop.cli  wdotool wwmctl wxprop
build wxrandr wxrandr.cli wdotool wxrandr
# warandr bundles wxrandr: on Wayland it runs the same interpreter with
# -m wxrandr, PYTHONPATH pointing at the pyz itself (zipimport)
build warandr warandr.cli wdotool wxrandr warandr
