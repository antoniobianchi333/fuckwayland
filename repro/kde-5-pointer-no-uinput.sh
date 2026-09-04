#!/bin/sh
export PATH=/usr/local/bin:$PATH
pkill -x wdotool 2>/dev/null
rm -f /run/user/1000/wdotool*.sock* 2>/dev/null
sleep 1
echo "sockets left: $(ls /run/user/1000/ 2>/dev/null | grep -c wdotool)"
echo "uinput: $(ls -l /dev/uinput 2>&1 | head -1)"
echo -n "getmouselocation: "; wdotool getmouselocation 2>&1; echo "rc=$?"
