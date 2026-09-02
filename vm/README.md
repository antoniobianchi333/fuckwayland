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
$ vm/vmctl build noble-gnome            # ~15 min, once; golden image -> ~/vm-data/golden/
$ vm/vmctl build resolute-gnome         # can run concurrently with the above
$ vm/vmctl start gnome1 --flavor noble-gnome --heads 3
vmctl: gnome1: QEMU pid 1234, flavor noble-gnome, 3 vCPU/4G, ssh port 2400, bus ...
vmctl: gnome1: ssh up after 21s
vmctl: gnome1: heads: 0=default, 1=1920x1080, 2=1920x1080  (guest connectors Virtual-1, Virtual-2, Virtual-3)
2400
$ vm/vmctl session gnome1               # blocks until test's Wayland session is active
SESSION_ID=2
XDG_RUNTIME_DIR=/run/user/1000
WAYLAND_DISPLAY=wayland-0
DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus
$ vm/vmctl user gnome1 -- gdbus call --session --dest org.gnome.Mutter.DisplayConfig \
      --object-path /org/gnome/Mutter/DisplayConfig \
      --method org.gnome.Mutter.DisplayConfig.GetCurrentState
$ vm/vmctl shot gnome1 --all /tmp/gnome1     # -> /tmp/gnome1-0.png, -1.png, -2.png
$ vm/vmctl head gnome1 3 1280x1024           # 4th monitor (Virtual-4) appears in GNOME
$ vm/vmctl head gnome1 3 off                 # ...and goes away again
$ vm/vmctl heads gnome1                      # guest connectors after a forced re-probe
$ vm/vmctl scp gnome1 dist/wxrandr.pyz gnome1:/home/test/
$ vm/vmctl user gnome1 -- python3 /home/test/wxrandr.pyz --query
$ vm/vmctl stop gnome1                       # or: destroy (also deletes the overlay)
```

Host requirements: `qemu-system-x86_64` (8.2+, with the **dbus** display
backend and PNG screendump), `qemu-img`, `cloud-localds` (cloud-image-utils),
`dbus-daemon`, `gdbus` (libglib2.0-bin), `ssh`/`scp`/`ssh-keygen`, Python 3.10+,
KVM access, and the Ubuntu cloud images in `~/images/`
(`noble-server-cloudimg-amd64.img`, `ubuntu-26.04-server-cloudimg-amd64.img`;
override the directory with `VMIMAGES=`). `vmctl` is stdlib-only Python.

## Files

| path | what |
|---|---|
| `vm/vmctl` | the CLI (host side) |
| `vm/build-image.sh` | guest-side build script; embedded into the flavor's cloud-init user-data by `vmctl build`, runs once as root inside the build VM |
| `vm/flavors/<flavor>.yaml` | cloud-init user-data template per flavor (`@@ROOT_PUBKEY@@`, `@@BUILD_SCRIPT@@` placeholders; `# vmctl-base:` names the base cloud image) |
| `vm/reference/<flavor>-packages.txt` | `dpkg-query -W -f='${binary:Package}\n'` of the finished golden image: **what a default install of that flavor contains** |

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
| `vmctl build <flavor> [--cpus 4] [--mem 6G] [--size 30G] [--force] [--keep]` | boot a fresh overlay of the base image with the flavor's cloud-init; wait for it to power itself off; keep it as the golden image. Refuses to overwrite an existing golden without `--force`. Progress lines from the guest are echoed; the full log is `golden/<flavor>.build.log`. |
| `vmctl start <name> --flavor <flavor> [--heads N] [--mem 4G] [--cpus 3] [--fresh] [--no-wait]` | create (or reuse) the instance, start its private D-Bus, start QEMU daemonized, wait for ssh, plug heads `1..N-1` at 1920x1080, print the ssh port. Refuses a double start; stale pidfiles are detected. `--fresh` recreates the overlay disk and seed. Without `--flavor`/`--heads` a reused instance keeps its previous values. |
| `vmctl stop <name> [--timeout 60]` | `systemctl poweroff` over ssh (GNOME inhibits logind's power-key handling, so a bare ACPI button would only pop a dialog), falls back to QMP `system_powerdown`, then QMP `quit`/kill after the timeout; stops the dbus-daemon. |
| `vmctl destroy <name>` | stop + delete `instances/<name>`. |
| `vmctl list` / `vmctl status <name>` | instances with state/pid/port/heads; `status` also reads each QEMU console's current size over D-Bus. |
| `vmctl ssh <name> [-- cmd...]` | root ssh (`StrictHostKeyChecking=no`, known hosts `/dev/null`). |
| `vmctl scp <name> <src> <dst>` | scp; write the guest side as `<name>:<path>` (extra scp flags such as `-r` pass through). |
| `vmctl head <name> <idx> <WxH>\|off` | `SetUIInfo` on head `idx` (0-based; `0` = Virtual-1, which can only be resized). |
| `vmctl heads <name>` | guest DRM connectors (status, enabled, preferred mode) after `echo detect > .../status` — the kernel's cached mode list is stale until something re-probes. |
| `vmctl shot <name> <idx> <out.png>` / `vmctl shot <name> --all <out>` | QMP `screendump` of one head (or every plugged head → `<out>-<idx>.png`). Written by QEMU straight to the host path. |
| `vmctl session <name> [--timeout 180]` | wait until `loginctl` shows a session for user `test` with `Type=wayland`, `State=active` and `/run/user/1000/wayland-0` exists; print `SESSION_ID`, `XDG_RUNTIME_DIR`, `WAYLAND_DISPLAY`, `DBUS_SESSION_BUS_ADDRESS`. |
| `vmctl user <name> [-t] -- cmd...` | run `cmd` as `test` via `sudo -u test -H env XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-0 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus ...`, plus `DISPLAY`, `XAUTHORITY` (mutter's Xwayland auth file), `XDG_CURRENT_DESKTOP` etc. imported from the session's `systemctl --user show-environment` (fallback `DISPLAY=:0` when `/tmp/.X11-unix/X0` exists) — so `xrandr`/`wmctrl`/`xdotool`/`xprop` work in it too. |

## Flavors

`noble-gnome` (Ubuntu 24.04 LTS) and `resolute-gnome` (Ubuntu 26.04 LTS). Both:

* user `test` (uid 1000, password `test`, groups `adm,sudo`, bash, `NOPASSWD` sudo); root ssh by key
  (`~/vm-data/keys/id_ed25519`); hostname = flavor name (instances re-set it to the instance name).
* `ubuntu-desktop` installed non-interactively (`DEBIAN_FRONTEND=noninteractive`, `--force-confold`),
  so the image is as close to a default Ubuntu desktop install as a cloud image allows.
  Only four extra packages: `xdotool wmctrl x11-utils x11-xserver-utils` (parity oracles).
* GDM: `/etc/gdm3/custom.conf` with `AutomaticLoginEnable=true`, `AutomaticLogin=test`,
  `WaylandEnable=true`; AccountsService pins the `ubuntu` (Wayland) session; `graphical.target` default.
* No screen lock / blank / idle sleep, no welcome tour: a gschema override
  (`/usr/share/glib-2.0/schemas/90_vmctl.gschema.override`, compiled in) sets
  `org.gnome.desktop.screensaver lock-enabled=false`, `org.gnome.desktop.session idle-delay=0`,
  `org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type='nothing'`,
  `org.gnome.shell welcome-dialog-last-shown-version='999'`. `gnome-initial-setup` is marked
  done for `test`; the `update-notifier` / `ubuntu-report` autostarts are hidden for `test`.
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

## The QEMU / D-Bus facts this rig relies on

Proven on QEMU 8.2.2 with a 24.04 (kernel 6.8) and 26.04 guest:

* GPU: `-device virtio-vga,id=gpu0,max_outputs=4,edid=on`
  `-display dbus,addr=unix:path=<BUS>,p2p=no` where `<BUS>` is a private session bus:
  `dbus-daemon --session --fork --address=unix:path=<BUS>` (vmctl uses `--pidfile` so it can
  stop it again). No host GUI is needed.
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
  resized the same way. Head N ↔ `Virtual-(N+1)`.
* The guest kernel's `/sys/class/drm/*/modes` stays stale until something re-probes the
  connector (`echo detect > .../status`, or any `drmModeGetConnector` — compositors do that on
  the hotplug uevent). `vmctl heads` forces the re-probe; mutter's `GetCurrentState` is the
  authoritative view.
* Screenshots: QMP `{"execute":"screendump","arguments":{"filename":"/abs/path.png",
  "device":"gpu0","head":N,"format":"png"}}` (after `qmp_capabilities`) over
  `-qmp unix:<path>,server,nowait`. The PNG is written by QEMU on the host.
* Guest access: cloud-init NoCloud seed (`cloud-localds`) with `disable_root: false` and the
  root key; user-mode networking with `hostfwd=tcp:127.0.0.1:<port>-:22`; `-enable-kvm -cpu host`.
* No virtual input devices are attached (`virtio-tablet/keyboard` are not needed: the tools
  inject through `uinput` inside the guest).

## Gotchas

* Head 0 keeps QEMU's default EDID (preferred mode 1280x800) unless you `vmctl head <name> 0 WxH`;
  `--heads N` sizes only heads `1..N-1` (1920x1080). Sizes survive `stop`/`start` via `meta.json`.
  With that mix GNOME makes the larger Virtual-2 the primary monitor (top bar, dock); resize head 0
  to 1920x1080 before the session starts if you want Virtual-1 primary.
* `vmctl head ... off` on head 0 is refused (virtio-vga's first scanout is always connected).
* Screendumps of a head the guest has never scanned out are black.
* `vmctl user` runs under `sudo -u test`; anything needing `/dev/uinput` still needs root
  (`vmctl ssh`) or `sudo` from inside (`test` has NOPASSWD sudo).
* A build that fails leaves `~/vm-data/build/<flavor>/` (serial.log, and root ssh on the
  printed port while the VM is alive) for inspection; `vmctl list` shows it as `(build)`.
* Instances re-run cloud-init once (new instance-id) — it only sets the hostname, re-applies
  the root key/user and regenerates ssh host keys; that is why `vmctl ssh` ignores known hosts.
