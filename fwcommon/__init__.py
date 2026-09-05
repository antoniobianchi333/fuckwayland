"""What every command here shares: which session this is, how to hand a
command over to the X11 original, and the two wire clients.

A package of its own rather than a corner of `wdotool`, because these four
modules are what the *display* tools use of it: they find a session, talk
D-Bus and talk Wayland, and they never type a key, never open a window
backend and never start the input daemon.

- `session`      -- which Wayland/X11 session this is, and where its sockets,
                    cookies and runtime directory are, from any uid.
- `passthrough`  -- on an X11 session, execve the real xdotool/wmctrl/xprop/
                    xrandr with argv untouched, and never ourselves.
- `dbus_mini`    -- a D-Bus client: SASL, marshalling, calls, signals.
- `wayland_mini` -- a Wayland client: the registry, roundtrips, fd passing.

One edge is still attached: `session` raises `wdotool.ctx.CmdError`, which is
the exception class every command already catches, so `wdotool/ctx.py` is the
one module outside this package that any of these four names. It is not moved
here because `ctx.py` reaches the other way -- into `backend_detect` and the
input daemon -- and would drag all of `wdotool` back in with it.
"""

#: The release. This is the constant the packages build their own VERSION
#: from; pyproject.toml, debian/changelog and flake.nix state the same number
#: for their own build systems, and scripts/build-deb.sh refuses to build
#: when the first two disagree.
VERSION = "0.3.0"
