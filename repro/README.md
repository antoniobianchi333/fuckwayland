# Reproducers for the KDE + sway stress pass

Run these on the *host*: `sh deploy-to-vm.sh <repo> <vm>` builds the five
zipapps and installs them on a `vmctl` guest, then
`vmctl user <vm> -- sh /tmp/<script>.sh` runs one as the desktop user
(`vmctl ssh <vm> -- sh ...` runs it as root).

| script | finding | image |
|---|---|---|
| `kde-1-probe-kwin-id-reuse.sh` | KWin hands out a script id a live script still owns (gdbus only, no wdotool) | any KDE |
| `kde-1-script-id-race.sh` | the same, through concurrent wdotool commands | any KDE |
| `kde-5-pointer-no-uinput.sh` | `getmouselocation` needed /dev/uinput for a pure query | any KDE |
| `kde-tools-3-argv0-oracle.sh` | what the real wmctrl/xprop print as their program name | any |
| `kde5-verify.sh` | SHADED on a native window, the X-plane state fallback, WM_CLASS case, `-l -G` | `noble-kde` |
| `kde6-before.sh`, `kde6-verify-a.sh`, `kde6-verify-b.sh` | the Plasma 6.6 sweep | `resolute-kde` |
| `matrix-1-warandr-empty-env.sh` | `env -i warandr --command` | any |
| `sway-1-layout-flag-fr.sh` | `--layout us` ignored by the daemon (needs a French keymap to show) | `resolute-sway` |
| `sway-3-root-desktops.sh` | synthesized `_NET_CURRENT_DESKTOP` past the count (as root) | `resolute-sway` |
| `sway-verify.sh`, `sway-verify-b.sh` | tiled resize/move exit codes, `-d` ordering, the rest | `resolute-sway` |
