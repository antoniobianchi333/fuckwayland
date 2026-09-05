#!/bin/sh
# Build the zipapps from a repo checkout and install on a vmctl guest the five these
# reproducers use (wmirror has none here).
set -eu
REPO=$1; VM=$2
cd "$REPO"
sh scripts/build-pyz.sh >/dev/null
V=./vm/vmctl        # we are in the repo root
for t in wdotool wwmctl wxprop wxrandr warandr; do
  $V scp "$VM" "dist/$t" "$VM:/tmp/$t" >/dev/null
done
$V ssh "$VM" -- 'for t in wdotool wwmctl wxprop wxrandr warandr; do install -m 0755 /tmp/$t /usr/local/bin/$t; done; ls -l /usr/local/bin/'
