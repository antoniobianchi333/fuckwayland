#!/usr/bin/env bash
# ssh into the test VM: vm/ssh.sh [ssh-args...] [command...]
. "$(dirname -- "$0")/common.sh"
require ssh
exec ssh -p "$SSH_PORT" "${SSH_OPTS[@]}" "$SSH_DEST" "$@"
