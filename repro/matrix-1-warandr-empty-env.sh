#!/bin/sh
# matrix-1: warandr from a root shell with an empty environment.
echo "--- warandr --print-backend"; env -i /usr/local/bin/warandr --print-backend; echo "rc=$?"
echo "--- warandr --print-backend --verbose"; env -i /usr/local/bin/warandr --print-backend --verbose 2>&1 | head -4
echo "--- warandr --command"; env -i /usr/local/bin/warandr --command; echo "rc=$?"
echo "--- warandr --save"; env -i /usr/local/bin/warandr --save /tmp/l.sh; echo "rc=$?"; head -2 /tmp/l.sh 2>/dev/null
echo "--- wxrandr (control)"; env -i /usr/local/bin/wxrandr --print-backend; echo "rc=$?"
