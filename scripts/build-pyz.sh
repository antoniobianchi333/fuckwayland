#!/bin/sh
# Build dist/<tool>: single-file, stdlib-only executable zipapps.
set -eu
cd "$(dirname "$0")/.."

build() { # name entry_module packages...
  name=$1; entry=$2; shift 2
  rm -rf dist/.stage
  mkdir -p dist/.stage
  for p in "$@"; do cp -r "$p" "dist/.stage/$p"; done
  find dist/.stage -name __pycache__ -type d -exec rm -rf {} +
  printf 'import sys\nfrom %s import main\n\nsys.exit(main())\n' "$entry" > dist/.stage/__main__.py
  python3 -m zipapp dist/.stage -p "/usr/bin/env python3" -o "dist/$name"
  rm -rf dist/.stage
  chmod +x "dist/$name"
  echo "built dist/$name ($(wc -c < "dist/$name") bytes)"
}

# wwmctl/wxprop ride with the packages they import (wdotool backends, x11_mini)
build wdotool wdotool.cli wdotool
build wwmctl  wwmctl.cli  wdotool wwmctl
build wxprop  wxprop.cli  wdotool wwmctl wxprop
build wxrandr wxrandr.cli wdotool wxrandr
