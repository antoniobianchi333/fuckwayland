#!/bin/sh
# Plasma on X11 (SDDM), as ROOT with no session environment: where is the X cookie?
# Run with `vmctl ssh <vm> -- sh /tmp/kdex11-1-sddm-x-cookie.sh` on a kde-x11 guest,
# with the five tools installed over the originals in /usr/local/bin and a checkout
# of the branch at /home/test/src.
#
# Before the fix: find_xauthority() came back empty, repair_x_env() injected DISPLAY
# alone, and every handover died with "Authorization required, but no authorization
# protocol specified" -- on the one platform whose display manager keeps the cookie
# in /tmp.  After it: session.py reads the cookie out of the session's own leader.
echo "--- the cookie this session actually uses"
ls -l /tmp/xauth_* /home/test/.Xauthority /run/user/1000/xauth_* 2>&1 | head -3
echo "--- ...and the session processes that name it (uid 1000, root can read /proc)"
for p in $(pgrep -u 1000 -x startplasma-x11; pgrep -u 1000 -x kwin_x11; pgrep -u 1000 -x plasmashell); do
    printf '%-16s %s\n' "$(cat /proc/$p/comm)" "$(tr '\0' '\n' < /proc/$p/environ | sed -n 's/^XAUTHORITY=//p' | head -1)"
done
echo "--- what the branch finds from here"
PYTHONPATH=/home/test/src python3 -c '
from wdotool import passthrough
e = {}
passthrough.repair_x_env(e)
print("repair_x_env({}) ->", e)
'
echo "--- and therefore, with an empty environment:"
for c in "/usr/local/bin/xdotool getdisplaygeometry" "/usr/local/bin/wmctrl -m" "/usr/local/bin/xrandr --listmonitors"; do
    printf '%-44s ' "$c"
    out=$(env -i $c 2>&1 | head -1); echo "-> $out"
done
