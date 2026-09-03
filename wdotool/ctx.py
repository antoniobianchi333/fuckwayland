"""Shared command context. FROZEN — edit only if broken.

Command function contract (all *_cmds.py modules):

    def cmd_foo(ctx: Context, args: list[str]) -> int

The return value is the number of argv tokens the command consumed (NOT counting
the command name itself). Raise CmdError(msg) on failure: the driver prints the
message to stderr, aborts the rest of the chain, and exits 1.
"""


class CmdError(Exception):
    """A command failed. The driver prints str(self) and exits with
    `exit_code` (1 unless a subclass says otherwise)."""

    exit_code = 1


class NoSessionError(CmdError):
    """No Wayland session / window-management backend could be found at all
    (B5). Distinct from "the session is fine but nothing matched", which
    stays rc 1, so a script can tell "not logged in yet / no bridge" from "no
    such window" -- see SESSION READINESS in README.md."""

    exit_code = 2


# Highest valid X/Mutter window id: both are 64-bit unsigned in our wire
# formats, and anything outside the range cannot name a window.
_MAX_WINDOW_ID = 2 ** 64 - 1


class Context:
    def __init__(self):
        self.stack: list[int] = []
        self._backend = None
        self._daemon = None
        # Non-fatal failure marker (e.g. `search` with no results): the chain
        # continues/completes but the process exits with this code.
        self.exit_code = 0

    def backend(self):
        if self._backend is None:
            from wdotool import backend_detect

            self._backend = backend_detect.detect()
        return self._backend

    def daemon(self):
        if self._daemon is None:
            from wdotool.daemon import DaemonClient

            self._daemon = DaemonClient.connect_or_spawn()
        return self._daemon

    def _resolve_one(self, arg: str) -> int:
        if arg.startswith("%"):
            ref = arg[1:]
            if not self.stack:
                raise CmdError("There are no windows on the stack")
            if ref == "@":
                return self.stack[0]
            try:
                n = int(ref)
            except ValueError:
                raise CmdError(f"Invalid window stack reference '{arg}'") from None
            # Negative refs count from the end, like xdotool's window_list():
            # index = len(stack) + n, valid when it lands in [1, len(stack)].
            idx = len(self.stack) + n if n < 0 else n
            if idx <= 0 or idx > len(self.stack):
                raise CmdError(
                    f"Invalid window stack reference '{arg}' (stack has {len(self.stack)} windows)"
                )
            return self.stack[idx - 1]
        try:
            return int(arg, 0)
        except ValueError:
            raise CmdError(f"Invalid window id '{arg}'") from None

    def resolve_window(self, arg: str | None = None) -> int:
        """Resolve an optional window argument like xdotool: explicit arg (decimal,
        0x-hex, or %N/%@ stack ref), else %1. Like xdotool, an omitted window
        argument with an empty window stack is an error (the real tool validates
        the implicit "%1" against the stack and refuses; being lenient here made
        `wdotool windowclose` close the focused window where xdotool errors)."""
        if arg is not None:
            return self._resolve_one(arg)
        if self.stack:
            return self.stack[0]
        raise CmdError("There are no windows on the stack")

    def resolve_windows(self, arg: str | None = None) -> list[int]:
        """Like resolve_window, but %@ expands to the entire stack."""
        if arg == "%@":
            if not self.stack:
                raise CmdError("There are no windows on the stack")
            return list(self.stack)
        return [self.resolve_window(arg)]
