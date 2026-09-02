#!/usr/bin/env bash
# scp to/from the test VM. Remote side is root@127.0.0.1:<path>, e.g.:
#   vm/scp.sh ./wdotool.pyz root@127.0.0.1:/root/
#   vm/scp.sh root@127.0.0.1:/root/shot.png .
. "$(dirname -- "$0")/common.sh"
require scp
exec scp -P "$SSH_PORT" "${SSH_OPTS[@]}" "$@"
