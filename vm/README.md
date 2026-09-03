# vm/ — test VMs

Two rigs live here:

* **`vmctl`** (this document): full Ubuntu **GNOME on Wayland** desktops
  (`ubuntu-desktop`, GDM autologin of user `test`) in QEMU/KVM with a
  **multi-head virtio-vga** whose monitors are plugged, unplugged and resized
  from the host at runtime, plus host-side screenshots of every head. This is
  the rig for testing `wxrandr`/`wwmctl`/`wdotool`/`wxprop` against a real,
  default-configured GNOME session, and for the X-parity oracles (the golden
  image also carries the real `xdotool`, `wmctrl`, `x11-utils`, `x11-xserver-utils`).
* **`mkvm.sh` / `run.sh` / `compositor.sh` / `ssh.sh` / `scp.sh` / `stop.sh`**:
  the original headless-sway rig (single VM in this directory, root runs sway).
  Unchanged; the two rigs do not share state or ports (sway rig: 2222,
  vmctl: 2400-2499).

## Quickstart

```console
$ vm/vmctl build noble-gnome            # ~7 min, once; golden image -> ~/vm-data/golden/
$ vm/vmctl build resolute-gnome         # can run concurrently with the above
$ vm/vmctl start gnome1 --flavor noble-gnome --heads 3
vmctl: gnome1: QEMU pid 1234, flavor noble-gnome, 3 vCPU/4G, ssh port 2400, bus ...
vmctl: gnome1: heads: 0=1920x1080, 1=1920x1080, 2=1920x1080  (guest connectors Virtual-1, Virtual-2, Virtual-3; set before the guest boots)
vmctl: gnome1: ssh up after 23s
2400
$ vm/vmctl session gnome1               # blocks until test's Wayland session is active
SESSION_ID=1
XDG_RUNTIME_DIR=/run/user/1000
WAYLAND_DISPLAY=wayland-0
DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus
$ vm/vmctl user gnome1 -- gdbus call --session --dest org.gnome.Mutter.DisplayConfig \
      --object-path /org/gnome/Mutter/DisplayConfig \
      --method org.gnome.Mutter.DisplayConfig.GetCurrentState
$ vm/vmctl user gnome1 -- sh -c 'loginctl show-session $XDG_SESSION_ID -p Type'   # Type=wayland
$ vm/vmctl shot gnome1 --all /tmp/gnome1     # -> /tmp/gnome1-0.png, -1.png, -2.png
$ vm/vmctl head gnome1 3 1280x1024           # 4th monitor (Virtual-4) appears in GNOME
$ vm/vmctl head gnome1 3 off                 # ...and goes away again
$ vm/vmctl heads gnome1                      # guest connectors after a forced re-probe
$ vm/vmctl scp gnome1 dist/wxrandr.pyz gnome1:/home/test/
$ vm/vmctl user gnome1 -- python3 /home/test/wxrandr.pyz --query
$ vm/vmctl stop gnome1                       # or: destroy (also deletes the overlay)
$ vm/selftest.sh noble-gnome                 # the whole thing end to end, ~40 s (see below)
```

Host requirements: `qemu-system-x86_64` (8.2+, with the **dbus** display
backend and PNG screendump), `qemu-img`, `cloud-localds` (cloud-image-utils),
`dbus-daemon`, `gdbus` (libglib2.0-bin), `ssh`/`scp`/`ssh-keygen`, Python 3.10+,
KVM access, and the Ubuntu cloud images in `~/images/`
(`noble-server-cloudimg-amd64.img`, `ubuntu-26.04-server-cloudimg-amd64.img`;
override the directory with `VMIMAGES=`). `vmctl` is stdlib-only Python.
`selftest.sh` additionally wants ImageMagick's `identify` (to prove a screenshot is not a flat colour).

## Fresh host setup

Any Linux box (physical, or a VM whose hypervisor exposes KVM to it,
so that `/dev/kvm` exists inside it) works. On Ubuntu 24.04:

```console
$ sudo apt-get install -y qemu-system-x86 qemu-system-gui qemu-utils cloud-image-utils \
      dbus libglib2.0-bin socat imagemagick openssh-client
$ sudo usermod -aG kvm "$USER"          # re-login afterwards
$ mkdir -p ~/images && cd ~/images
$ curl -LO https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img
$ curl -LO https://cloud-images.ubuntu.com/releases/26.04/release/ubuntu-26.04-server-cloudimg-amd64.img
$ vm/vmctl build noble-gnome && vm/vmctl build resolute-gnome   # ~7 min each on 4 vCPU
$ vm/selftest.sh noble-gnome && vm/selftest.sh resolute-gnome
```

`qemu-system-gui` provides the dbus display module (`qemu-system-x86_64 -display help`
must list `dbus`). Golden images keep an absolute backing-file path to `~/images`, so
copying `~/vm-data/golden` and `~/images` to another host with the same home directory
layout works too. Sizing: a desktop VM needs 2 vCPU / 3 GB; a golden build 4 vCPU / 6 GB
(`--cpus 2` on small hosts). A 4 vCPU / 16 GB host runs one build or two desktop VMs at a
time comfortably; 8 vCPU / 32 GB runs four.

## Files

| path | what |
|---|---|
| `vm/vmctl` | the CLI (host side) |
| `vm/build-image.sh` | guest-side build script; embedded into the flavor's cloud-init user-data by `vmctl build`, runs once as root inside the build VM |
| `vm/flavors/<flavor>.yaml` | cloud-init user-data template per flavor (`@@ROOT_PUBKEY@@`, `@@BUILD_SCRIPT@@` placeholders; `# vmctl-base:` names the base cloud image) |
| `vm/reference/<flavor>-packages.txt` | `dpkg-query -W -f='${binary:Package}\n'` of the finished golden image: **what a default install of that flavor contains**. Multi-arch names carry their `:amd64` suffix (`libei1:amd64`), so grep for exact names with `^name(:amd64)?$`. |
| `vm/selftest.sh <flavor> [name]` | end-to-end check of a golden image: boot 3 heads, autologin, monitors/primary/no stray dialog, real screenshots, hotplug (details below) |

State (never in the repo) lives under `$VMDATA` (default `~/vm-data`):

```
golden/<flavor>.qcow2            golden image: qcow2 overlay whose backing file is the
                                 base cloud image by ABSOLUTE path (~/images/...). It does
                                 not depend on any build or instance directory.
golden/<flavor>-packages.txt     package list (copied into vm/reference/)
golden/<flavor>.build.log        serial log of the build VM
instances/<name>/disk.qcow2      overlay on golden/<flavor>.qcow2
instances/<name>/seed.img        cloud-init NoCloud seed (hostname, root key, user test)
instances/<name>/{qmp.sock,bus,dbus.pid,qemu.pid,serial.log,meta.json}
build/<flavor>/                  transient build VM (removed on success)
keys/id_ed25519[.pub]            guest root ssh key, generated once
```

## Commands

| command | what |
|---|---|
| `vmctl build <flavor> [--cpus 4] [--mem 6G] [--size 30G] [--force] [--keep]` | boot a fresh overlay of the base image with the flavor's cloud-init; wait for it to power itself off; keep it as the golden image. Refuses to overwrite an existing golden without `--force` (a rebuilt golden invalidates every instance overlay on it: restart those with `--fresh`). Progress lines from the guest are echoed; the full log is `golden/<flavor>.build.log`. |
| `vmctl start <name> --flavor <flavor> [--heads N] [--head-size WxH] [--mem 4G] [--cpus 3] [--fresh] [--no-wait]` | create (or reuse) the instance, start its private D-Bus, start QEMU daemonized, **size/plug heads `0..N-1` (default 1920x1080 each) before the guest boots**, wait for ssh, print the ssh port. Refuses a double start; stale pidfiles are detected. `--fresh` recreates the overlay disk and seed. Without `--flavor`/`--heads`/`--head-size` a reused instance keeps its previous values (including per-head sizes set with `vmctl head`). |
| `vmctl stop <name> [--timeout 60]` | `systemctl poweroff` over ssh (GNOME inhibits logind's power-key handling, so a bare ACPI button would only pop a dialog), falls back to QMP `system_powerdown`, then QMP `quit`/kill after the timeout; stops the dbus-daemon. |
| `vmctl destroy <name>` | stop + delete `instances/<name>`. |
| `vmctl list` / `vmctl status <name>` | instances with state/pid/port/heads; `status` also reads each QEMU console over D-Bus and says which heads are unplugged (QEMU keeps an unplugged console's last surface size, so the size alone would mislead). |
| `vmctl ssh <name> [-- cmd...]` | root ssh (`StrictHostKeyChecking=no`, known hosts `/dev/null`). |
| `vmctl scp <name> <src> <dst>` | scp; write the guest side as `<name>:<path>`. Extra scp flags need a `--` first: `vmctl scp n -- -r src n:/dst`. |
| `vmctl head <name> <idx> <WxH>\|off` | `SetUIInfo` on head `idx` (0-based; `0` = Virtual-1, which can only be resized). A running GNOME picks the change up in well under a second. |
| `vmctl heads <name>` | guest DRM connectors (status, enabled, preferred mode) after `echo detect > .../status` — the kernel's cached mode list is stale until something re-probes. |
| `vmctl shot <name> <idx> <out.png>` / `vmctl shot <name> --all <out>` | QMP `screendump` of one head (or every plugged head → `<out>-<idx>.png`). Written by QEMU straight to the host path. **The mouse cursor is not in it** (see Gotchas). |
| `vmctl session <name> [--timeout 180]` | wait until `loginctl` shows a session for user `test` with `Type=wayland`, `State=active` and `/run/user/1000/wayland-0` exists, **then until the desktop has painted its first frame** (GNOME: the `GNOME Shell started` journal line; other desktops: head 0's screendump changing; `--no-paint` skips this); print `SESSION_ID`, `XDG_RUNTIME_DIR`, `WAYLAND_DISPLAY`, `DBUS_SESSION_BUS_ADDRESS`. |
| `vmctl user <name> [-t] -- cmd...` | run `cmd` as `test` via `sudo -u test -H env XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-0 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus ...`, plus `DISPLAY`, `XAUTHORITY` (mutter's Xwayland auth file), `XDG_CURRENT_DESKTOP` etc. imported from the session's `systemctl --user show-environment` (fallback `DISPLAY=:0` when `/tmp/.X11-unix/X0` exists), plus `XDG_SESSION_ID` = logind's display session of `test` and `XDG_SESSION_TYPE` = that session's real `Type` (not a hardcoded `wayland`) — so `xrandr`/`wmctrl`/`xdotool`/`xprop` and `loginctl show-session $XDG_SESSION_ID` work in it. |

## Flavors

`noble-gnome` (Ubuntu 24.04 LTS, GNOME Shell 46 / mutter 46) and `resolute-gnome` (Ubuntu 26.04 LTS,
GNOME Shell 50 / mutter 50). Both:

* user `test` (uid 1000, password `test`, groups `adm,sudo`, bash, `NOPASSWD` sudo); root ssh by key
  (`~/vm-data/keys/id_ed25519`); hostname = flavor name (instances re-set it to the instance name).
* `ubuntu-desktop` installed non-interactively (`DEBIAN_FRONTEND=noninteractive`, `--force-confold`),
  so the image is as close to a default Ubuntu desktop install as a cloud image allows.
  Only four extra packages: `xdotool wmctrl x11-utils x11-xserver-utils` (parity oracles).
  Not in the image (not part of a default desktop, not needed by the tools): `python3-tk`;
  `python3-gi` + `gir1.2-gtk-3.0`/`gir1.2-gtk-4.0` *are* there for test windows.
* GDM: `/etc/gdm3/custom.conf` with `AutomaticLoginEnable=true`, `AutomaticLogin=test`,
  `WaylandEnable=true`; AccountsService pins the `ubuntu` (Wayland) session; `graphical.target` default.
* No screen lock / blank / idle sleep, no welcome tour: a gschema override
  (`/usr/share/glib-2.0/schemas/90_vmctl.gschema.override`, compiled in) sets
  `org.gnome.desktop.screensaver lock-enabled=false`, `org.gnome.desktop.session idle-delay=0`,
  `org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type='nothing'`,
  `org.gnome.shell welcome-dialog-last-shown-version='999'`.
* No `gnome-initial-setup` dialogs for `test`: `~/.config/gnome-initial-setup-done` (first login)
  **and** `~/.config/gnome-initial-setup/upgrade-<release>-done` (the "Welcome to Ubuntu 26.04 LTS!"
  dialog of `gnome-initial-setup-upgrade-login.service`, whose unit is gated on the first marker
  existing and the second one *not* existing — so the first marker alone switches that dialog on).
  The `update-notifier` / `ubuntu-report` autostarts are hidden for `test`.
* Automatic apt (`apt-daily*` timers, `unattended-upgrades`, `20auto-upgrades`) and snap
  auto-refresh are off.
* `systemd-networkd-wait-online` is disabled: NetworkManager (from the desktop) manages the NIC
  via netplan, and networkd's wait-online would otherwise hold `network-online.target` (and with it
  cloud-init and ssh) for its full 2-minute timeout on every boot. NM's wait-online covers the target.
* `/dev/uinput` is available for the injection tools (`uinput` is built into Ubuntu's kernel;
  `/etc/modules-load.d/uinput.conf` is there for kernels that have it as a module).
* The finished image's package list is dumped over the serial console and stored in
  `golden/<flavor>-packages.txt` (and, in the guest, `/var/lib/vmctl/packages.txt`).

Adding a flavor: copy a yaml, change `hostname`, `# vmctl-base:` and `/etc/vmctl-build.env`.
The build boots the overlay with `-display none`; cloud-init's `power_state` powers it off at
the end and `vmctl build` succeeds only if the guest printed `VMCTL-BUILD-OK`.

## Self-test

`vm/selftest.sh <flavor> [name]` (default name `<flavor>-t`, screenshots in
`$OUT`, default `/tmp/vmctl-selftest-<name>`) boots a fresh 3-head instance and asserts, in order:
`vmctl session` reports an active Wayland session; `GetCurrentState` lists exactly Virtual-1..3
with **Virtual-1 primary at (0,0)**; no `gnome-initial-setup` process in the session; `vmctl user`
exports an `XDG_SESSION_ID` whose logind `Type` is `wayland`; `vmctl heads` sees the connectors;
`vmctl shot --all` yields one PNG per head whose pixel standard deviation is > 0.01 (a session
that never finished starting paints every head a flat `#222222`); `vmctl head 3 1280x1024` makes
mutter report a 4th monitor and `head 3 off` removes it again. About 40 s per flavor; the VM is
left running.

## The QEMU / D-Bus facts this rig relies on

Proven on QEMU 8.2.2 with a 24.04 (kernel 6.8) and 26.04 (kernel 7.0) guest:

* GPU: `-device virtio-vga,id=gpu0,max_outputs=4,edid=on`
  `-display dbus,addr=unix:path=<BUS>,p2p=no` where `<BUS>` is a private session bus:
  `dbus-daemon --session --fork --nopidfile --print-pid=1 --address=unix:path=<BUS>`
  (dbus-daemon has no `--pidfile`; vmctl records the printed pid so it can stop it again).
  No host GUI is needed.
* On that bus QEMU owns the name **`org.qemu`** (not `org.qemu.Display1`). Each head of gpu0
  is an object `/org/qemu/Display1/Console_<N>` (N = head index, label `gpu0.N`) with interface
  `org.qemu.Display1.Console` and method
  `SetUIInfo(q width_mm, q height_mm, i xoff, i yoff, u width, u height)`:

  ```
  gdbus call --address unix:path=<BUS> --dest org.qemu \
        --object-path /org/qemu/Display1/Console_1 \
        --method org.qemu.Display1.Console.SetUIInfo 0 0 0 0 1920 1080
  ```

  hot-plugs head 1 as guest connector **`Virtual-2`** with an EDID whose preferred mode is
  1920x1080; width/height `0 0` unplugs it. Head 0 (`Virtual-1`) is always connected and is
  resized the same way. Head N ↔ `Virtual-(N+1)`. All four `Console_<N>` objects exist from the
  moment QEMU is up (the name is acquired asynchronously; `vmctl start` polls for it).
* **`SetUIInfo` works before the guest has booted**: QEMU keeps the requested size per scanout
  and reports it (plus the EDID) on the driver's first `GET_DISPLAY_INFO`, so heads set right
  after QEMU starts are simply *there* when GDM/mutter come up — that is what `vmctl start` does.
  Plugging them after ssh-up instead lands 2-6 s after gnome-shell starts and, on a loaded host,
  raced with gnome-shell's start-up animation: every head stayed a flat `#222222` (no top bar,
  dock or wallpaper — mutter had painted each view exactly once) until the next login.
* The guest kernel's `/sys/class/drm/*/modes` stays stale until something re-probes the
  connector (`echo detect > .../status`, or any `drmModeGetConnector` — compositors do that on
  the hotplug uevent). `vmctl heads` forces the re-probe; mutter's `GetCurrentState` is the
  authoritative view (a hotplug shows up there within ~0.5 s, an unplug within ~4 s).
* Screenshots: QMP `{"execute":"screendump","arguments":{"filename":"/abs/path.png",
  "device":"gpu0","head":N,"format":"png"}}` (after `qmp_capabilities`) over
  `-qmp unix:<path>,server,nowait`. The PNG is written by QEMU on the host.
* Guest access: cloud-init NoCloud seed (`cloud-localds`) with `disable_root: false` and the
  root key; user-mode networking with `hostfwd=tcp:127.0.0.1:<port>-:22`; `-enable-kvm -cpu host`.
* No virtual input devices are attached (`virtio-tablet/keyboard` are not needed: the tools
  inject through `uinput` inside the guest).

## What a hotkey-launched process sees

Measured on both flavors with a GNOME custom shortcut
(`org.gnome.settings-daemon.plugins.media-keys custom-keybindings`, `command='sh -c "env > /tmp/hotkey-env.txt"'`)
fired by `python3 -m wdotool key ...` from a root shell in the guest: the file appears within
1-4 s, written by a child of `gsd-media-keys` (`org.gnome.SettingsDaemon.MediaKeys.service` in the
user manager). It gets the **full graphical session environment** (46 variables on 24.04, 44 on
26.04), in particular:

```
WAYLAND_DISPLAY=wayland-0            DISPLAY=:0
XDG_RUNTIME_DIR=/run/user/1000       XAUTHORITY=/run/user/1000/.mutter-Xwaylandauth.XXXXXX
DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus
XDG_SESSION_TYPE=wayland             XDG_SESSION_CLASS=user
XDG_CURRENT_DESKTOP=ubuntu:GNOME     XDG_SESSION_DESKTOP=ubuntu   DESKTOP_SESSION=ubuntu
GDMSESSION=ubuntu                    GNOME_SHELL_SESSION_MODE=ubuntu (24.04)
GNOME_SETUP_DISPLAY=:1 (24.04) / unix:/tmp/.X11-unix/X1 (26.04)
SESSION_MANAGER=local/<host>:@/tmp/.ICE-unix/<pid>,unix/<host>:/tmp/.ICE-unix/<pid> (24.04)
SSH_AUTH_SOCK=/run/user/1000/keyring/ssh   GNOME_KEYRING_CONTROL=/run/user/1000/keyring
GIO_LAUNCHED_DESKTOP_FILE_PID, INVOCATION_ID, JOURNAL_STREAM, MANAGERPID, SYSTEMD_EXEC_PID,
MEMORY_PRESSURE_WATCH/WRITE (systemd user service plumbing)
QT_IM_MODULE=ibus  XMODIFIERS=@im=ibus  GTK_MODULES=gail:atk-bridge  QT_ACCESSIBILITY=1
XDG_DATA_DIRS, XDG_CONFIG_DIRS, XDG_MENU_PREFIX=gnome-, LANG=C.UTF-8
HOME=/home/test USER=test LOGNAME=test USERNAME=test SHELL=/bin/bash PWD=/home/test PATH=...:/snap/bin
```

26.04 additionally has `GNOME_DESKTOP_SESSION_ID`, `GPG_AGENT_INFO`, `IM_CONFIG_ENTRY`, `MANAGERPIDFDID`,
`QT_IM_MODULES`, `XDG_SESSION_EXTRA_DEVICE_ACCESS`. Neither has `XDG_SESSION_ID`. Notes:

* **Do not bind `<Ctrl><Alt>F1..F12`.** On GNOME Wayland mutter owns `<Primary><Alt>Fn` as
  `switch-to-session-n` (`org.gnome.mutter.wayland.keybindings`), `gsd-media-keys` logs
  `Failed to grab accelerator for keybinding custom:...` and the injected chord **switches the
  guest to ttyN**: the session goes `State=online`, the screen is a text console and nothing
  reacts until `vmctl ssh <name> -- chvt 2` (session active again, desktop back). This is GNOME's
  default, not something the rig changes. `<Super>F9` and `<Ctrl><Shift>F9` work. If a test
  needs the `Ctrl+Alt+Fn` chords, clear them for the session first:
  `vmctl user <name> -- gsettings set org.gnome.mutter.wayland.keybindings switch-to-session-9 "[]"`.
* On 26.04 `/tmp` is a tmpfs: anything a test writes there is gone after a reboot.

## What a cron @reboot process sees

`crontab -u test`: `@reboot sh -c 'sleep 20; env > /tmp/cron-env.txt'`. On both flavors cron
runs the job about 8 s after boot — **before the graphical session exists** (GDM autologin is
active ~20-28 s after boot, `systemd-analyze` 9-12 s). The process gets exactly six variables:

```
HOME=/home/test  LANG=C.UTF-8  LOGNAME=test  PWD=/home/test  SHELL=/bin/sh
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin
```

No `WAYLAND_DISPLAY`, `DBUS_SESSION_BUS_ADDRESS`, `XDG_RUNTIME_DIR`, `DISPLAY` or `XAUTHORITY`.
A cron-started tool must therefore wait for the session and set them itself:
`XDG_RUNTIME_DIR=/run/user/1000`, `WAYLAND_DISPLAY=wayland-0`,
`DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus`, and for the X side `DISPLAY=:0` plus the
`XAUTHORITY` from `systemctl --user show-environment` — exactly what `vmctl user` does.

## Gotchas

* **The session is "active" before anything is drawn.** logind reports the Wayland
  session active a few seconds before gnome-shell paints; a screendump taken then still
  shows the kernel console (mirrored on every head by fbdev), and on a slow host the
  window is several seconds wide. `vmctl session` therefore also waits for the first
  frame, and `selftest.sh` asserts that head 0 (top bar, dock) differs from head 1.

* `vmctl start --heads N` sizes heads `0..N-1` to 1920x1080 (`--head-size` changes it) before
  the guest boots, so Virtual-1 is the primary monitor (top bar, dock) at logical (0,0) and
  screen coordinates like `wdotool mousemove 300 200` land on it. Head 0 left at QEMU's default
  EDID (1280x800; only old instances or `--head-size` mixes get there) makes GNOME prefer the larger
  Virtual-2 as primary at (0,0), with Virtual-1 at x=1920 — confusing for coordinate tests.
  Sizes survive `stop`/`start` via `meta.json`.
* `vmctl head ... off` on head 0 is refused (virtio-vga's first scanout is always connected).
* Screendumps of a head the guest has never scanned out are black.
* **The mouse cursor is not in a screendump**: virtio-gpu puts it on a hardware cursor plane that
  QEMU's `screendump` does not compose. To verify pointer motion read the KMS cursor plane in the
  guest (`/sys/kernel/debug/dri/0/state`: the 64x64 `plane-N` shows `crtc-pos=64x64+296+196` for a
  pointer at (300,200) with the 4,4 hotspot), or, on the host, register an
  `org.qemu.Display1.Listener` on each `Console_<N>` and read the `MouseSet` calls
  (`x=296 y=196 on=1` on the head that holds the pointer, `on=0` on the others). With the default
  head layout `wdotool mousemove 300 200` lands on head 0 / Virtual-1, `mousemove 2220 200` on
  head 1 / Virtual-2 at its local (300,200).
* A screenshot that is a single flat `#222222` on every head means gnome-shell never finished
  starting (see the SetUIInfo fact above); `vmctl start` avoids the known cause, and
  `selftest.sh` checks for it. Logging out/in or a reboot clears it.
* `vmctl user` runs under `sudo -u test`; anything needing `/dev/uinput` still needs root
  (`vmctl ssh`) or `sudo` from inside (`test` has NOPASSWD sudo).
* A build that fails leaves `~/vm-data/build/<flavor>/` (serial.log, and root ssh on the
  printed port while the VM is alive) for inspection; `vmctl list` shows it as `(build)`.
* Instances re-run cloud-init once (new instance-id) — it only sets the hostname, re-applies
  the root key/user and regenerates ssh host keys; that is why `vmctl ssh` ignores known hosts.
* Rebuilding a golden (`build --force`) replaces the backing file of every instance overlay on
  it; restart those instances with `--fresh`.
