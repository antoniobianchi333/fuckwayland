# vm/ — test VMs

Two rigs live here:

* **`vmctl`** (this document): full, default-configured Ubuntu desktops in QEMU/KVM —
  **seven flavors** over four desktops (GNOME, KDE Plasma, Xfce, sway) and two releases,
  each with autologin of user `test` — on a **multi-head virtio-vga** whose monitors are
  plugged, unplugged and resized from the host at runtime, plus host-side screenshots of
  every head. This is the rig for testing `wxrandr`/`wwmctl`/`wdotool`/`wxprop` against
  real Wayland *and* X11 sessions, and for the X-parity oracles (every golden image also
  carries the real `xdotool`, `wmctrl`, `x11-utils`, `x11-xserver-utils`). What the five
  tools currently manage on each desktop — including where they have no backend at all —
  is written down per flavor under *What the five tools do on each flavor*, which is the
  measurement behind the *Desktop support* matrix in the repo README.
* **`mkvm.sh` / `run.sh` / `compositor.sh` / `ssh.sh` / `scp.sh` / `stop.sh`**:
  the original headless-sway rig (single VM in this directory, root runs sway).
  Unchanged; the two rigs do not share state or ports (sway rig: 2222,
  vmctl: 2400-2499).

## Quickstart

```console
$ vm/vmctl build noble-gnome            # ~7 min, once; golden image -> ~/vm-data/golden/
$ vm/vmctl build resolute-kde           # any of the seven flavors (see Flavors below)
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
$ vm/selftest.sh resolute-kde                # same check, Plasma's own tools (see below)
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
| `vm/flavors/<flavor>.yaml` | cloud-init user-data template per flavor (`@@ROOT_PUBKEY@@`, `@@BUILD_SCRIPT@@` placeholders; `# vmctl-base:` names the base cloud image, `# vmctl-desktop:` the desktop — `gnome`, `kde`, `xfce` or `sway`) |
| `vm/reference/<flavor>-packages.txt` | `dpkg-query -W -f='${binary:Package}\n'` of the finished golden image: **what a default install of that flavor contains**. Multi-arch names carry their `:amd64` suffix (`libei1:amd64`), so grep for exact names with `^name(:amd64)?$`. |
| `vm/selftest.sh <flavor> [name]` | end-to-end check of any flavor's golden image: boot 3 heads, autologin, monitors/primary in that desktop's own display tool, no stray first-run window, real screenshots, hotplug (details below) |

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
| `vmctl ssh <name> [-- cmd...]` | root ssh (`StrictHostKeyChecking=no`, known hosts `/dev/null`). A command of several words is quoted before it goes to ssh, so `vmctl ssh n -- sh -c 'a; b'` runs what it says (ssh joins the words with spaces and the guest's shell re-splits them; unquoted, that ran `sh -c a`). A single word is passed through untouched — `vmctl ssh n -- 'a \| b'` still reaches the remote shell as a command line. |
| `vmctl scp <name> <src> <dst>` | scp; write the guest side as `<name>:<path>`. Extra scp flags need a `--` first: `vmctl scp n -- -r src n:/dst`. |
| `vmctl head <name> <idx> <WxH>\|off` | `SetUIInfo` on head `idx` (0-based; `0` = Virtual-1, which can only be resized). A running GNOME picks the change up in well under a second. On an **X11 flavor** an unplug also releases the output in the X server: RandR keeps a removed output's CRTC (mode and position) until the desktop's display daemon disables it, so `off` waits ~5 s for the desktop to do it and otherwise runs `xrandr --output Virtual-N --off` in the session itself, logging `Virtual-N was still enabled in the X server after the unplug; disabled it`. |
| `vmctl heads <name>` | guest DRM connectors (status, enabled, preferred mode) after `echo detect > .../status` — the kernel's cached mode list is stale until something re-probes. |
| `vmctl shot <name> <idx> <out.png>` / `vmctl shot <name> --all <out>` | QMP `screendump` of one head (or every plugged head → `<out>-<idx>.png`). Written by QEMU straight to the host path. **The mouse cursor is not in it** (see Gotchas). |
| `vmctl session <name> [--timeout 180] [--no-paint]` | wait until `loginctl` shows an **active session of user `test` on a seat** whose `Type` is the one the flavor's desktop registers (`wayland`; `x11` for Xfce; sway's greetd session starts as `tty` and libseat turns it into `wayland`) and whose sockets are there (`/run/user/1000/wayland-N`, `/tmp/.X11-unix/X0`, sway's `sway-ipc.*.sock`), **then until the desktop has painted its first frame**: GNOME the `GNOME Shell started` journal line, the others the compositor (`kwin_wayland`, `Xorg`, `sway`) owning head 0's scanout framebuffer in `/sys/kernel/debug/dri/0/state` *plus* the shell processes (`plasmashell`; `xfce4-panel`+`xfdesktop`; `swaybar`) up and any splash (`ksplashqml`) gone — and, when the guest offers none of that, head 0's screendump changing. Whatever the guest says is then confirmed **against the pixels of every head**: a screendump of each active head (QMP `screendump` in PPM, read without any image library) must not be one flat colour (sampled standard deviation >= 0.02). Head 0 can be painted seconds before the others are — on Xfce the panel is up while heads 1 and 2 are still black, which is what `waiting for the first frame: WAIT flat head 1,2` in the log means. `--no-paint` skips the paint wait. Prints `SESSION_ID`, `XDG_RUNTIME_DIR`, `WAYLAND_DISPLAY`/`DISPLAY`/`SWAYSOCK`, `DBUS_SESSION_BUS_ADDRESS`, `XDG_SESSION_TYPE`. |
| `vmctl user <name> [-t] -- cmd...` | run `cmd` as `test` via `sudo -u test -H env XDG_RUNTIME_DIR=/run/user/1000 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus ...` with the session's own environment reconstructed, first match wins: what the session published to the systemd user manager (`gnome-session`, `startplasma`, the X session's, sway's `dbus-update-activation-environment`), else the compositor's `/proc/<pid>/environ` (`gnome-shell`, `kwin_wayland`, `xfce4-session`, `sway`), else the sockets that are actually there. Result: `WAYLAND_DISPLAY` and `SWAYSOCK` on the Wayland flavors, `DISPLAY`+`XAUTHORITY` everywhere (Xwayland's on Wayland, Xorg's on Xfce), `XDG_CURRENT_DESKTOP`, `XDG_SESSION_ID` = logind's display/seat session of `test`, and `XDG_SESSION_TYPE` = `wayland` or `x11` as the session itself reports it (sway publishes `wayland` although greetd registered a `tty` session) — so `xrandr`/`wmctrl`/`xdotool`/`xprop`, `kscreen-doctor`, `swaymsg` and `loginctl show-session $XDG_SESSION_ID` all work in it. `-t` may follow `<name>`. |

## Flavors

Seven golden images: four desktops over two Ubuntu releases. A flavor is one
`vm/flavors/<flavor>.yaml` (cloud-init user-data). Its `# vmctl-base:` header names the base
cloud image and its `# vmctl-desktop:` header (`gnome`, `kde`, `xfce`, `sway`) is what `vmctl`
and `selftest.sh` key off: which display manager owns the session, what `loginctl` calls its
`Type`, which sockets `vmctl user` must export, which process paints the first frame, and which
native tool reports monitors.

| flavor | release | desktop (as built) | display manager | session | native display tool |
|---|---|---|---|---|---|
| `noble-gnome` | 24.04 LTS | GNOME Shell 46 / mutter 46 (`ubuntu-desktop`) | GDM | Wayland | mutter's `org.gnome.Mutter.DisplayConfig.GetCurrentState` |
| `resolute-gnome` | 26.04 LTS | GNOME Shell 50 / mutter 50 (`ubuntu-desktop`) | GDM | Wayland | the same |
| `noble-kde` | 24.04 LTS | Plasma 5.27 / KWin 5.27 (`kubuntu-desktop`, `plasma-workspace-wayland`) | SDDM | Wayland | `kscreen-doctor -o` |
| `resolute-kde` | 26.04 LTS | Plasma 6.6 / KWin 6.6 (`kubuntu-desktop`) | SDDM | Wayland | `kscreen-doctor -o` |
| `noble-xfce` | 24.04 LTS | Xfce 4.18 (`xubuntu-desktop`) | LightDM | **X11** | `xrandr` |
| `resolute-xfce` | 26.04 LTS | Xfce 4.20 (`xubuntu-desktop`) | LightDM | **X11** | `xrandr` |
| `resolute-sway` | 26.04 LTS | sway 1.11 / wlroots, Xwayland, `foot`, `grim` | greetd | Wayland | `swaymsg -t get_outputs` |

Every flavor:

* user `test` (uid 1000, password `test`, groups `adm,sudo`, bash, `NOPASSWD` sudo); root ssh by key
  (`~/vm-data/keys/id_ed25519`); hostname = flavor name (instances re-set it to the instance name).
* the desktop metapackage installed non-interactively (`DEBIAN_FRONTEND=noninteractive`,
  `--force-confold`), so the image is as close to a default install of that Ubuntu flavor as a
  cloud image allows. Only four extra packages: `xdotool wmctrl x11-utils x11-xserver-utils`
  (the X-parity oracles; on the Wayland flavors they talk to Xwayland). Not in the image
  (not part of a default desktop, not needed by the tools): `python3-tk`; `python3-gi` +
  `gir1.2-gtk-3.0`/`gir1.2-gtk-4.0` *are* there for test windows.
* autologin of `test` into that desktop's own session, `graphical.target` as the default target,
  and no screen lock, blanking, DPMS or idle sleep.
* no first-run/welcome window in the session — the thing `selftest.sh` asserts is absent.
* automatic apt (`apt-daily*` timers, `unattended-upgrades`, `20auto-upgrades`) and snap
  auto-refresh off; `/dev/uinput` available for the injection tools.
* `systemd-networkd-wait-online` disabled where the desktop brings NetworkManager (GNOME, Plasma,
  Xfce): NM manages the NIC via netplan and networkd's wait-online would otherwise hold
  `network-online.target` — and with it cloud-init and ssh — for its full 2-minute timeout on
  every boot. `resolute-sway` has no NetworkManager; there networkd stays in charge.
* the finished image's package list is dumped over the serial console and stored in
  `golden/<flavor>-packages.txt` and `vm/reference/<flavor>-packages.txt`.

**GNOME** (`noble-gnome`, `resolute-gnome`)

* GDM: `/etc/gdm3/custom.conf` with `AutomaticLoginEnable=true`, `AutomaticLogin=test`,
  `WaylandEnable=true`; AccountsService pins the `ubuntu` (Wayland) session.
* a gschema override (`/usr/share/glib-2.0/schemas/90_vmctl.gschema.override`, compiled in) sets
  `org.gnome.desktop.screensaver lock-enabled=false`, `org.gnome.desktop.session idle-delay=0`,
  `org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type='nothing'`,
  `org.gnome.shell welcome-dialog-last-shown-version='999'`.
* no `gnome-initial-setup` dialogs for `test`: `~/.config/gnome-initial-setup-done` (first login)
  **and** `~/.config/gnome-initial-setup/upgrade-<release>-done` (the "Welcome to Ubuntu 26.04 LTS!"
  dialog of `gnome-initial-setup-upgrade-login.service`, whose unit is gated on the first marker
  existing and the second one *not* existing — so the first marker alone switches that dialog on).
  The `update-notifier` / `ubuntu-report` autostarts are hidden for `test`.

**KDE Plasma** (`noble-kde`, `resolute-kde`)

* SDDM: `/etc/sddm.conf.d/` autologin of `test` into the **Plasma Wayland** session —
  `plasmawayland.desktop` on 5.27, `plasma.desktop` on 6 (the build picks whichever
  `/usr/share/wayland-sessions/` file exists and fails if neither does).
* `kwriteconfig5`/`kwriteconfig6` (whichever the release ships) turn off the screen locker
  (`kscreenlockerrc`) and display power management (`powerdevilrc`) for `test`.
* the welcome centre and the Discover update notifier are hidden as autostart entries — enough on
  Plasma 5.27. On **Plasma 6** the welcome centre is no autostart entry at all any more but a kded
  module (`kded_plasma_welcome.so`) that opens it whenever `plasma-welcomerc`'s `LastSeenVersion`
  is missing or older than the installed `plasma-welcome`, so the build also writes that key.
* Plasma reaches its first frame in several steps, so `vmctl session` waits for all of them:
  `kwin_wayland` owning head 0's scanout, `plasmashell` up, and `ksplashqml` gone.

**Xfce** (`noble-xfce`, `resolute-xfce`) — the X11 flavors

* LightDM autologin of `test` into the `xubuntu` (Xorg) session; `test` is added to the
  `autologin`/`nopasswdlogin` groups if the packages created them.
* **Display-manager collision.** On 26.04 `xubuntu-desktop` drags GDM in (through `gnome-shell`),
  and the first display manager to configure itself becomes *the* display manager — a golden built
  without care boots the GNOME greeter instead of Xfce. The build therefore answers debconf's
  question up front (`lightdm shared/default-x-display-manager select lightdm`), writes
  `/etc/X11/default-display-manager`, disables `gdm`/`gdm3`/`sddm`, enables `lightdm` and **fails
  the build** unless `display-manager.service` resolves to `lightdm.service`.
* xfconf channels written for `test` before the first login: no screensaver/blanking/DPMS
  (`xfce4-screensaver`, `xfce4-power-manager`, `xfce4-session`), and `displays.xml`
  `Notify=3` so `xfsettingsd` **extends** a hot-plugged output instead of popping the
  "new display detected" dialog (`Notify=1`, the default) — without it `vmctl head` would put a
  modal window on the screen and leave the new output disabled.
* `xfce4-screensaver`, `light-locker` and `update-notifier` autostarts hidden.

**sway** (`resolute-sway`)

* greetd `[initial_session]` starts `test`'s sway directly on vt 1 on the first boot (no greeter:
  the VM has no keyboard to type into one); `[default_session]` starts sway again, so a logout
  brings the desktop back. `getty@tty1` is disabled and the unit is ordered after it.
* the logind session greetd registers is `Type=tty` (greetd exports `XDG_SESSION_TYPE=tty`);
  sway's libseat then switches it to `wayland`. Both `vmctl session` and `vmctl user` accept
  either and always export `XDG_SESSION_TYPE=wayland`, which is what the session itself publishes.
  sway takes the DRM device through libseat's logind backend — no `seatd`.
* the config is the packaged default plus: `xwayland enable` (lazy Xwayland for the X-parity
  oracles), `workspace N output Virtual-N`, a `dbus-update-activation-environment` line that
  publishes `WAYLAND_DISPLAY`/`SWAYSOCK`/`DISPLAY`/`XDG_CURRENT_DESKTOP`/`XDG_SESSION_TYPE` to the
  systemd user manager (greetd starts sway with a bare environment, so without it nothing outside
  the session could find it), and `exec /usr/local/bin/vmctl-sway-layout`.
* **`vmctl-sway-layout`** runs once at session start and puts the active outputs side by side at
  `y=0` in connector order (`Virtual-1` at (0,0), `Virtual-2` to its right, ...). Stock
  sway/wlroots adds the initial outputs to the layout in reverse enumeration order, which on a
  3-head virtio-vga would put `Virtual-3` at (0,0) and `Virtual-1` at x=3840 — the rig's contract
  is head 0 = `Virtual-1` = (0,0). Nothing is watched afterwards: a test that moves outputs, or a
  head hot-plugged later (wlroots appends it on the right), is left alone.

Adding a flavor: copy a yaml, change `hostname`, `# vmctl-base:`, `# vmctl-desktop:` and
`/etc/vmctl-build.env` (`DESKTOP`, `DESKTOP_PKG`, `EXTRA_PKGS`). A new *desktop* additionally
needs a branch in `build-image.sh`, an entry in `vmctl`'s `DESKTOPS` table (session kind, logind
types, display-manager unit, compositor/shell/splash process names) and a case in `selftest.sh`'s
`monitors`/`layout`. The build boots the overlay with `-display none`; cloud-init's `power_state`
powers it off at the end and `vmctl build` succeeds only if the guest printed `VMCTL-BUILD-OK`.

## Self-test

`vm/selftest.sh <flavor> [name]` (default name `<flavor>-t`, screenshots in `$OUT`, default
`/tmp/vmctl-selftest-<name>`) boots a fresh 3-head instance of **any** flavor and asserts, in
order, using that desktop's own tools:

1. `vmctl session` reaches an active session of the expected kind and the desktop has painted.
2. the native display tool lists exactly `Virtual-1`, `-2`, `-3`, with **`Virtual-1` at (0,0)** —
   and *primary* where the desktop has the notion: GNOME's logical-monitor `primary` flag and
   Plasma's `priority 1` are required; sway has no primary, so its focused output stands in, and
   Xorg only marks one if something asked it to.
3. no first-run window in the session: `gnome-initial-setup`, `plasma-welcome`, or a window whose
   title looks like Xfce's "new display" dialog.
4. `vmctl user` gives a working session environment: `XDG_SESSION_ID` whose logind `Type` is the
   flavor's (`wayland`, `x11`, or `wayland`/`tty` for sway), `XDG_SESSION_TYPE` exactly `wayland`
   or `x11`, the display sockets (`$XDG_RUNTIME_DIR/$WAYLAND_DISPLAY`, `$SWAYSOCK`) and an X
   display `xdpyinfo` can reach — Xwayland on the Wayland flavors, Xorg on Xfce.
5. `vmctl heads` sees the DRM connectors, and `vmctl shot --all` yields one PNG per head whose
   pixel standard deviation is > 0.01 (a session that never finished starting paints every head a
   flat `#222222`). A head that is still flat is re-shot every 4 s, up to six times, before it
   counts as a failure — `vmctl session` already waits for a picture on every head, so this is
   only the safety net under it.
6. **head 0 differs from head 1** — the proof that the screendumps caught the desktop and not the
   kernel console, which fbdev mirrors onto every head. This one is flavor-aware: GNOME (top bar
   and dock), Plasma (panel on the primary output) and Xfce (`xfce4-panel` on the first monitor)
   must differ, so identical heads fail the test; sway draws the same `swaybar` on every output
   and legitimately may not differ, so there it only warns — and the per-output workspace pinning
   (`Virtual-N` shows workspace `N`) is asserted through `swaymsg` instead.
7. `vmctl head 3 1280x1024` makes the native tool report a 4th monitor and `Virtual-4` appear in
   `vmctl heads`; `vmctl head 3 off` takes it away again (up to 60 s — on X11 the removal is only
   complete once the X server has let go of the output, which `vmctl head` sees to; a failure
   prints the DRM connectors and, on Xfce, `xrandr`'s view of `Virtual-*`).

Roughly 40 s for GNOME; a desktop that starts more slowly takes correspondingly longer. The VM
is left running so a failure can be inspected — `vmctl stop <flavor>-t` when done.

## What the five tools do on each flavor

The point of the extra flavors is to see where `wxrandr`, `wwmctl`, `wdotool`, `wxprop`
and `warandr` stand outside GNOME. This is the measured state, honest gaps included: the
branch's own checkout copied into each guest and run as `python3 -m <tool>` **inside the
session** (`vmctl user`), plus a second round as root over plain `vmctl ssh` with an empty
environment (`env -i`). Every message below is verbatim. None of it is a rig defect —
`selftest.sh` passes on all seven flavors. On the **X11 flavors** what is measured is the
passthrough: on a plain X11 session the tools hand over to the real `xdotool`/`wmctrl`/`xprop`/
`xrandr` (repo README, *X11*), all four of which the goldens carry, so there they behave as the
originals do.

**This table and the repo README's *Desktop support* matrix are one measurement.** That one
is grouped by desktop and is what a user reads; this one is grouped by flavor and names the
image. Neither may claim what the other denies: change them together.

| flavor | `wxrandr` | `wwmctl -m` | `wwmctl -l` | `wdotool` | `wxprop -root` | `warandr` |
|---|---|---|---|---|---|---|
| `noble-gnome`, `resolute-gnome` | works | works | needs the bridge extension | `getdisplaygeometry` works; window/input commands need the bridge extension | works | works |
| `noble-kde`, `resolute-kde` | works | works | works | works | works | works |
| `noble-xfce`, `resolute-xfce` | works (hands over to `xrandr`) | works (`wmctrl`) | works (`wmctrl`) | works (`xdotool` — but see the version note below) | works (`xprop`) | works (runs the real `xrandr`) |
| `resolute-sway` | works | works | works | works, bar `windowstate MAXIMIZED_*` and moving a *tiled* window | works in the session; **synthesized** from a root shell | works, once the image has the GTK 3 bindings |

Install the bridge extension on the GNOME flavors (`gnome/install-bridge.sh`, then log the
session out and in) and both of them pass every cell of this table, on 46 and on 50 —
`wwmctl -l/-d/-m`, every `wdotool` window command and `wxprop -id` on native windows, as
`test` and as root. `sudo gnome/install-bridge.sh --udev` is what lets the desktop user
open `/dev/uinput`; without it every injection command has to run as root, on all seven.

**GNOME** — `wxrandr` prints the real listing through mutter's DisplayConfig
(`Screen 0: minimum 16 x 16, current 5760 x 1080, maximum 32767 x 32767`,
`Virtual-1 connected primary 1920x1080+0+0 (normal left inverted right x axis y axis) 480mm x 270mm`),
`wwmctl -m` prints `Name: GNOME Shell` … `Window manager's "showing the desktop" mode: OFF`,
`wdotool getdisplaygeometry` prints `5760 1080`, and `wxprop -root _NET_SUPPORTING_WM_CHECK`
prints `_NET_SUPPORTING_WM_CHECK(WINDOW): window id # 0x400001` (Xwayland's root; the id varies).
Everything that needs the window list — `wwmctl -l`, `wdotool getactivewindow` and the rest —
exits non-zero with

> `gnome backend: the fuckwayland bridge extension is not running in GNOME Shell; run gnome/install-bridge.sh and restart the session (log out and back in)`

The goldens are stock desktops and deliberately do not carry the extension: install it in the
guest (`gnome/install-bridge.sh`) and log the session out and in to exercise those paths.
Same results as root over `vmctl ssh` — the GNOME backends need no session environment.

**KDE Plasma** — all four work, in the session and as root. `wxrandr` prints the real listing
through KWin's `kde_output_management_v2` (`Screen 0: minimum 16 x 16, current 5760 x 1080,
maximum 32767 x 32767`, `Virtual-1 connected primary 1920x1080+0+0 (normal left inverted right
x axis y axis) 480mm x 270mm`), and the window side is KWin scripting over the session bus with
**nothing installed** in the guest (`loadScript` is unprivileged on 5.27 and 6 alike).
`wwmctl -m` prints `Name: KWin` (`Class: N/A`, `PID: N/A`); `wwmctl -l` lists KWin's windows —
XWayland rows with their real X ids and native rows with the backend id KWin's uuid is minted
into (`0x4…`) — and `-lpxG` agrees with real `wmctrl` on id, class, pid and size for the X rows.
`wdotool getactivewindow` returns such an id, `getdisplaygeometry` returns `5760 1080`, and
`wxprop -id` on an XWayland window is a byte-identical dump of real `xprop`'s.

**As root over plain `vmctl ssh` the KWin backend works too** (the session bus is found by
scanning `/run/user/*`), so `vmctl ssh` and `vmctl user` both drive Plasma. That was not true
while the backend used `dbus-monitor`; it is the `dbus_mini` client that made it work.

**Xfce** — the X11 flavors, and every tool hands over there, so the answers are the real
tools' own. `wxrandr` prints the X server's listing
(`Screen 0: minimum 320 x 200, current 5760 x 1080, maximum 8192 x 8192`,
`Virtual-1 connected 1920x1080+0+0 (normal left inverted right x axis y axis) 487mm x 274mm` —
X's numbers, not the Wayland backends'). `wwmctl -m` prints `Name: Xfwm4`, `Class: xfwm4` and
xfwm4's real `PID:`; `wwmctl -l` lists the X windows —
`0x01600003 -1 <hostname> xfce4-panel`, `0x01800017 -1 <hostname> Desktop` per monitor, and a GTK
test window as `0x01400003  0 <hostname> vmctl-probe-window`. `wxprop -root
_NET_SUPPORTING_WM_CHECK` prints `_NET_SUPPORTING_WM_CHECK(WINDOW): window id # 0xe00032`, and
`wdotool getdisplaygeometry` prints `1920 1080` — xdotool answers per screen, where the Wayland
backends report the 5760x1080 span. `wdotool getactivewindow` prints the focused window's X id
(`25165855`); with nothing focused it fails the way the original does, exit 1 with

> `XGetWindowProperty[_NET_ACTIVE_WINDOW] failed (code=1)`
> `xdo_get_active_window reported an error`

(seen on `noble-xfce` right after login — on `resolute-xfce` xfdesktop holds the focus, and once a
window is open both answer with its id). **All of it works as root over plain `vmctl ssh` too**:
the passthrough supplies the session owner's `DISPLAY` and cookie, so root gets the same output
that `test` gets — the one flavor where `vmctl ssh` is as good as `vmctl user`. The flavors keep
their other job: they are the X-parity oracles (`xrandr`, `wmctrl`, `xdotool`, `xprop` are
installed), so the same command can be run through us and through the original and compared.

**Version note.** Both releases carry **xdotool 3.20160805.1**, not the 4.20260303.1 the repo
claims parity against, so a 4.x-only verb is simply absent on an X11 session: `windowstate
--add MAXIMIZED_VERT <id>` there answers `xdotool: Unknown command: windowstate` / `Run
'xdotool help' if you want a command list`, rc 1. That is the handover working, not a defect —
but nothing may claim `windowstate` works on X11. The listings above come from one 3-head run
each: how many `Desktop` rows xfdesktop publishes, and whether `xrandr` marks `Virtual-1`
`primary`, is the desktop's own state and differs between 4.18 and 4.20.

**sway** — all five work, in the session and as root, with the two exceptions at the end of
this paragraph. `wxrandr` prints the full listing
(`Screen 0: minimum 16 x 16, current 5760 x 1080, maximum 32767 x 32767`,
`Virtual-3 connected 1920x1080+3840+0 (normal left inverted right x axis y axis) 480mm x 270mm`),
`wwmctl -m` prints `Name: wlroots wm` in the session (`Name: sway` from a root shell), and with a
window open (`swaymsg exec foot`) `wwmctl -l` prints `0x00000009  0 <hostname> foot`,
`wdotool getactivewindow` prints `9` and `getwindowname 9` prints `foot`. `wxprop -root
_NET_SUPPORTING_WM_CHECK` prints `_NET_SUPPORTING_WM_CHECK(WINDOW): window id # 0x200004`.
On an **empty** session `getactivewindow` exits 1 with

> `xdo_get_active_window reported an error`

which is xdotool's own wording for "nothing is focused", not a backend failure —
`getdisplaygeometry` (`5760 1080`) and the window commands answer normally on the same session.

Two things are **not** the same from a root shell here, and only here. wlroots starts Xwayland
with no `-auth` file (`Xwayland :0 -rootless -core -terminate 10 -listenfd … -wm …`), so only
the session user's own processes may open that display: real `xprop` over `vmctl ssh` answers
`Authorization required, but no authorization protocol specified` / `xprop: unable to open
display ':0'`, and there is no cookie anywhere for us to find and hand it. `wxprop -root` then
falls back to sway's IPC and prints a *synthesized* set — `_NET_SUPPORTING_WM_CHECK(WINDOW):
window id # 0x0`, a `_NET_CLIENT_LIST` of compositor ids, no `_XKB_RULES_NAMES` — where inside
the session it prints Xwayland's real root. GNOME, KDE and Xfce all give root the real X root,
because their Xwayland/Xorg does have a cookie file. Second: `resolute-sway` is the only golden
without `python3-gi`/`gir1.2-gtk-3.0`, so `warandr`'s GUI there says which package to install
and exits 1 until they are installed (`--print-backend`, `--command` and `--save` need none of
it and work as shipped). `wdotool windowstate MAXIMIZED_VERT|MAXIMIZED_HORZ` is unsupported by
the sway backend and says so, rc 1; `wwmctl -r … -b add,maximized_vert` is the same non-answer,
rc 0.

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
  `-qmp unix:<path>,server,nowait`. The PNG is written by QEMU on the host. `"format":"ppm"`
  (QEMU >= 7.1) writes a raw P6 instead — that is how `vmctl session` checks whether a head has
  painted without depending on an image library in the host's python.
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

* **The session is "active" before anything is drawn.** logind reports the session active
  a few seconds before the desktop paints; a screendump taken then still shows the kernel
  console (mirrored on every head by fbdev), and on a slow host the window is several
  seconds wide. `vmctl session` therefore also waits for the first frame (per desktop: see
  the command table), and `selftest.sh` asserts that head 0 differs from head 1 — except on
  sway, whose bar is identical on every output. **The heads do not paint at once**: on Xfce the
  panel is on head 0 while heads 1 and 2 are still black (`xfdesktop` draws the wallpaper per
  monitor, seconds later), so what the guest reports about head 0 is not enough — the wait ends
  only when no head is still a flat colour.

* `vmctl start --heads N` sizes heads `0..N-1` to 1920x1080 (`--head-size` changes it) before
  the guest boots, so Virtual-1 is the primary monitor (top bar, dock) at logical (0,0) and
  screen coordinates like `wdotool mousemove 300 200` land on it. Head 0 left at QEMU's default
  EDID (1280x800; only old instances or `--head-size` mixes get there) makes GNOME prefer the larger
  Virtual-2 as primary at (0,0), with Virtual-1 at x=1920 — confusing for coordinate tests.
  Sizes survive `stop`/`start` via `meta.json`.
* `vmctl head ... off` on head 0 is refused (virtio-vga's first scanout is always connected).
* **X11: a removed output stays in `xrandr` until someone disables it.** The unplug takes the DRM
  connector away (`vmctl heads` → `Virtual-4 disconnected`), but the X server keeps scanning the
  output out: `xrandr --query` shows `Virtual-4 disconnected 1280x1024+5760+0`, `--listmonitors`
  still counts it, and `vmctl heads` says `disconnected enabled=enabled`. Disabling it is the
  desktop's job; Xfce 4.18 does it within a second, Xfce 4.20 is unreliable — one boot released
  every removed output in 1-3 s, another held them for over a minute. `vmctl head <idx> off`
  therefore finishes the job itself on the X11 flavors (5 s of grace, then `xrandr --output
  Virtual-N --off`), which turns a minute-long stall into a 6 s removal.
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
* **Xfce is the X11 flavor**: `vmctl user` exports no `WAYLAND_DISPLAY` there and the compositor
  is `Xorg`, so Wayland-only tools have nothing to talk to — that is the point of the flavor
  (it is the X-side oracle). Our four tools do not fail there: on an X11 session they hand over
  to the real `xdotool`/`wmctrl`/`xprop`/`xrandr` (see *What the four tools do on each flavor*). On 26.04 `xubuntu-desktop` also installs `gnome-shell` and GDM;
  the build forces LightDM and fails if it did not win (see Flavors).
* **Plasma 6 opens its welcome centre from a kded module**, not from an autostart entry, so
  hiding the `.desktop` file is not enough: `plasma-welcomerc`'s `LastSeenVersion` must name the
  installed version.
* **sway enumerates its initial outputs in reverse**: without the `vmctl-sway-layout` line in the
  session config, `Virtual-3` would be at (0,0) and `Virtual-1` off to the right, and workspace 1
  would land on the last output. A golden built without it breaks every coordinate test.
