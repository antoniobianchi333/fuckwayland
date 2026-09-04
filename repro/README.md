# Reproducers for the KDE + sway stress pass

Run these on the *host*: `sh deploy-to-vm.sh <repo> <vm>` builds the five
zipapps and installs them on a `vmctl` guest, then
`vmctl user <vm> -- sh /tmp/<script>.sh` runs one as the desktop user
(`vmctl ssh <vm> -- sh ...` runs it as root). The two `kde-outreg-*` scripts
are the exception: they need no guest at all. What they exercise — how Plasma
6.7 publishes outputs — does have an image now (`stonking-kde`), but that one
is a moving development release, and these two keep the same path checkable on
a machine with no Plasma 6.7 on it.

| script | finding | image |
|---|---|---|
| `kde-1-probe-kwin-id-reuse.sh` | KWin hands out a script id a live script still owns (gdbus only, no wdotool) | any KDE |
| `kde-1-script-id-race.sh` | the same, through concurrent wdotool commands | any KDE |
| `kde-5-pointer-no-uinput.sh` | `getmouselocation` needed /dev/uinput for a pure query | any KDE |
| `kde-tools-3-argv0-oracle.sh` | what the real wmctrl/xprop print as their program name | any |
| `kde-xid-twins.py` | one X11 client, N windows sharing a pid, a class, a title and a rectangle: the tie the Plasma 6 xid matcher used to settle by coin flip. `WM_WINDOW_ROLE` carries the truth (KWin exposes it, the matcher ignores it), so `wwmctl -l` can be graded against it | `resolute-kde` |
| `kdex11-1-sddm-x-cookie.sh` | Plasma on X11: SDDM keeps the X cookie in `/tmp/xauth_*`, so from a root shell the handover found no cookie at all (run as **root**) | `noble-kde-x11` |
| `kde5-verify.sh` | SHADED on a native window, the X-plane state fallback, WM_CLASS case, `-l -G` | `noble-kde` |
| `kde6-before.sh`, `kde6-verify-a.sh`, `kde6-verify-b.sh` | the Plasma 6.6 sweep | `resolute-kde` |
| `kde-outreg-conformance.py` | every wire constant in `wxrandr/kwin.py` against the upstream protocol XML — run it when a new Plasma lands | none (host, needs network) |
| `kde-outreg-specfake.py` | a Plasma 6.7 compositor generated from that XML: the registry discovery path end to end, and its three failure modes | none (host) |
| `matrix-1-warandr-empty-env.sh` | `env -i warandr --command` | any |
| `sway-1-layout-flag-fr.sh` | `--layout us` ignored by the daemon (needs a French keymap to show) | `resolute-sway` |
| `sway-3-root-desktops.sh` | synthesized `_NET_CURRENT_DESKTOP` past the count (as root) | `resolute-sway` |
| `sway-verify.sh`, `sway-verify-b.sh` | tiled resize/move exit codes, `-d` ordering, the rest | `resolute-sway` |

## Reproducers for the wxrandr + warandr stress pass

The wire-level ones need nothing but the repo: `tests/fixtures/fake_wlr.py` is
a `zwlr_output_manager_v1` server that can be told to go quiet at three
different moments, and `fake_kwin_server.py` runs the suite's own FakeKWin as
a standalone server, so the real CLI (a real `Session`, a separate process)
can be pointed at either.  The two `-vm` scripts run *inside* a guest against
the real compositor: unpack a tree at `/home/test/<name>` and
`vmctl user <vm> -- bash /home/test/repro.sh <name>`.

| script | finding | needs |
|---|---|---|
| `disp-hostile1-wlr-goes-quiet.sh` | a wlroots compositor that answers `succeeded` and then stops used to hang the CLI for ever | nothing |
| `disp-hostile2-scale-to-nothing.sh` | a `--scale` that truncates the logical size below the advertised 16x16 minimum | nothing |
| `disp-hostile4-non-finite.sh` | `nan` / `inf` / `1e400` to every option that takes a number | nothing |
| `disp-hostile9-broken-stdout.sh` | a closed or full stdout exited **120**, a code no xrandr produces | `/dev/full` |
| `disp-kwin-hostile.sh <tree>` | the same hostile set against KWin's protocol; takes a tree so before/after can be compared | nothing |
| `disp-layout2-wlr-scale-placement.sh` | `--right-of` off by 1-10 px at 149 of 201 fractional scales on the wlr backend | a headless sway (`$SWAYENV`) |
| `disp-sway-vm.sh before\|after` | the five sway/wlr findings in one pass, against the real compositor | `resolute-sway` |
| `disp-gnome-vm.sh before\|after` | `--same-as` relocating the primary, and the adjacency message | `noble-gnome` |
