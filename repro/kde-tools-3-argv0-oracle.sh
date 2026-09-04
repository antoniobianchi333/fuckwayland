#!/bin/sh
echo "--- real wmctrl, absolute path"
/usr/bin/wmctrl -q 2>&1 | head -2
echo "--- real wmctrl via a relative path"
cd /usr/bin && ./wmctrl -q 2>&1 | head -2
echo "--- real xprop, absolute path"
/usr/bin/xprop -badflag 2>&1 | head -3
echo "--- real xprop via a symlink with another name"
ln -sf /usr/bin/xprop /tmp/zprop; /tmp/zprop -badflag 2>&1 | head -3
echo "--- real xprop, relative"
cd /usr/bin && ./xprop -badflag 2>&1 | head -3
