#!/bin/sh
# Build the five zipapps from a repo checkout and install them on a vmctl guest.
set -eu
REPO=$1; VM=$2
cd "$REPO"
sh scripts/build-pyz.sh >/dev/null
V=$HOME/work/main/vm/vmctl
for t in wdotool wwmctl wxprop wxrandr warandr; do
  $V scp "$VM" "dist/$t" "$VM:/tmp/$t" >/dev/null
done
$V ssh "$VM" -- 'for t in wdotool wwmctl wxprop wxrandr warandr; do install -m 0755 /tmp/$t /usr/local/bin/$t; done; ls -l /usr/local/bin/'
