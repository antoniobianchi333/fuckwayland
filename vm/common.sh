# Shared prologue for vm/ scripts. Source it, don't run it.
set -eu
VM_DIR=$(cd -- "$(dirname -- "$0")" && pwd)
REPO=$(dirname "$VM_DIR")
SELF=$VM_DIR/$(basename -- "$0")
_ARGS=("$@")

# Re-exec through the repo's nix dev shell if any needed tool is missing.
require() {
    local t
    for t in "$@"; do
        command -v "$t" >/dev/null 2>&1 && continue
        if [ "${WDOTOOL_NIX:-}" = 1 ]; then
            echo "vm: tool '$t' missing even inside the dev shell" >&2
            exit 127
        fi
        WDOTOOL_NIX=1 exec nix develop "$REPO" --command "$SELF" ${_ARGS[@]+"${_ARGS[@]}"}
    done
}

SSH_PORT=2222
SSH_DEST=root@127.0.0.1
SSH_OPTS=(
    -F /dev/null
    -i "$VM_DIR/keys/id_ed25519"
    -o StrictHostKeyChecking=no
    -o UserKnownHostsFile=/dev/null
    -o ConnectTimeout=5
    -o LogLevel=ERROR
)

vm_pid() {
    [ -f "$VM_DIR/qemu.pid" ] || return 1
    local pid
    pid=$(cat "$VM_DIR/qemu.pid" 2>/dev/null) || return 1
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null || return 1
    echo "$pid"
}
