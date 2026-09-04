#!/bin/sh
SC() { gdbus call --session --dest org.kde.KWin --object-path /Scripting --method org.kde.kwin.Scripting."$@"; }
nodes() { gdbus introspect --session --dest org.kde.KWin --object-path /Scripting --xml | grep -o 'node name="[^"]*"'; }
rootnodes() { gdbus introspect --session --dest org.kde.KWin --object-path / --xml | grep -o 'node name="[^"]*"'; }
echo 'callDBus("org.freedesktop.DBus","/org/freedesktop/DBus","org.freedesktop.DBus","GetId");' > /tmp/p.js
echo "start nodes: $(nodes | tr '\n' ' ')"
echo "root  nodes: $(rootnodes | tr '\n' ' ')"
echo "A=$(SC loadScript /tmp/p.js pA)"; echo "B=$(SC loadScript /tmp/p.js pB)"
echo "nodes: $(nodes | tr '\n' ' ')"
SC unloadScript pA >/dev/null; sleep 1
echo "after unload pA nodes: $(nodes | tr '\n' ' ')"
echo "C=$(SC loadScript /tmp/p.js pC)   <- expect collision with B"
echo "nodes: $(nodes | tr '\n' ' ')"
echo "D=$(SC loadScript /tmp/p.js pD)   <- expect a free index"
echo "nodes: $(nodes | tr '\n' ' ')"
for n in pB pC pD; do SC unloadScript $n >/dev/null; done
sleep 1; echo "final nodes: $(nodes | tr '\n' ' ')"
