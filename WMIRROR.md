# wmirror — design contract

Mirror an output, or a **region** of one, onto another output on wlroots
compositors, by running the existing [`wl-mirror`](https://github.com/Ferdi265/wl-mirror)
and owning its lifetime. Pure stdlib (wl-mirror is a program we drive, never
a module we import). House rules per DESIGN.md.

It is the one tool in this box that clones nothing: there is no X11 original
called `wmirror`, and `xrandr` has no syntax for what it does.

## Why it exists (and why it is this small)

Everything below was measured on sway 1.11 / wlroots, three virtual heads,
two heads screendumped from outside the guest and compared with ImageMagick
(`compare -metric AE` counts differing pixels). The outputs were given
different wallpapers so "whose rendering is this?" is answerable from one
pixel.

**What the layout already does, with no helper at all:**

| two outputs at the same position | the second head shows | evidence |
|---|---|---|
| same logical size | **byte-identical**, the whole frame | `AE 0`, twice, wallpaper and status bar included |
| 1920x1080 vs 1280x1024 | **a crop** — the top-left 1280x1024; the rest is lost | `AE 0` against that crop; `RMSE 14%` against a squash, so not a scale |
| scale 2 (logical 960x540) | the top-left 960x540, magnified | `RMSE 1.0%` against that crop upscaled |
| transform 90 | the leftmost 1080 columns, on their side | un-rotate by -90 → `RMSE 0` |
| partial overlap at x=960 | the shared region byte-identical | `AE 0` over the whole shared region |

Two consequences of that first row decide this whole design:

* **a shared rectangle is a complete mirror**, not merely a shared scene:
  with per-output wallpapers live, both heads showed the *source's*
  wallpaper (`srgb(32,64,128)`), and separating them again put each head's
  own back;
* **a window on the second output's own workspace is drawn on both heads**
  (`AE 77`, the ticking clock only). Per-output workspaces do not partition
  the pixels.

So, of the three things geometry might not be able to express, sway has
one and a half:

1. **a region** of an output on another output — no layout expresses it.
   **Genuinely absent.**
2. **a whole output onto a differently-shaped one** — a shared position
   *crops*, exactly the case KWin needed `set_replication_source` for, and
   `zwlr_output_management_v1` has no replication request. **Genuinely
   absent.**
3. **two outputs that cannot share a position** — **empty**: every shared
   position and every overlap in the table above was accepted, silently.

wmirror exists for 1 and 2, and refuses everything else by name.

## Why a separate command, and not `wxrandr --same-as`

Three reasons, all measured rather than stylistic:

1. **`--same-as` cannot host it.** wl-mirror is an ordinary xdg-toplevel
   window made fullscreen on the target — not an output driver. Over a
   shared rectangle both heads draw the same composite *including that
   window*, so a mirror window on a target that shares the source's
   rectangle is drawn on the source too, and wl-mirror ends up capturing its
   own picture. wl-mirror's own guard (`fullscreen_output cannot be same as
   the output to be mirrored`) does not catch it: the two outputs are
   distinct objects that merely share a rectangle. A true whole-output copy
   here therefore needs the target to **keep its own place in the layout** —
   which is not what `--same-as` means, and not what a KWin replica does.
2. **A layout script has to keep running on a plain X11 box.** `warandr`
   saves `~/.screenlayout/*.sh` files that arandr loads and that real
   `xrandr` executes. There is no xrandr syntax for "mirror this rectangle
   there", so anything we invented would be a line arandr cannot parse and
   X11 cannot run. Nothing wmirror does can appear in a saved layout.
3. **wxrandr applies a layout and exits.** This starts a resident process
   that holds most of a core and stops the compositor ever idling (below).
   `--brightness` does leave a holder behind, but a gamma holder is asleep;
   this one is not, and a user who typed a geometry command would not go
   looking for it.

`wxrandr`, `warandr` and the rest are untouched by this feature. No other
package in the tree mentions wmirror — `tests/test_wmirror_cli.py` fails if
one starts to.

## The interface

```
wmirror SOURCE --to TARGET [--region WxH+X+Y] [--scaling fit|cover|exact]
               [--keep-layout] [--replace] [--dry-run]
wmirror --list                # what is running, verified, stale records reaped
wmirror --stop TARGET         # or --stop-all
wmirror --check               # can this session mirror at all, and what is missing
```

* **`--region WxH+X+Y`** is X11 geometry order — what `xrandr --fb`/`--pos`
  and `wwmctl -g` speak — in **layout coordinates**, the same numbers
  `wxrandr --query` prints beside each output and the same ones `slurp`
  produces. (wl-mirror's own region syntax is slurp's `x,y wxh`; we
  translate.)
* **`--scaling`** is passed straight through: `fit` letterboxes (default),
  `cover` fills and crops the sides, `exact` uses whole multiples only.
  Measured on a 1920x1080 → 1280x1024 pair: `fit` gives 1280x720+0+152,
  `exact` gives 960x540+0+242.
* **`--keep-layout`** is the way to ask for a mirror the layout could
  deliver: two same-sized outputs, mirrored while the target keeps its own
  rectangle (so the desktop keeps its area). Without it, that case is
  refused with the `wxrandr` command that does it for free.
* **exit codes**: 0 started (or the picture already exists), 1 refused or
  failed, 2 usage. `--stop TARGET` with nothing running is 1; `--stop-all`
  is always 0.

## The policy, in one sentence

**wl-mirror runs only for what the layout cannot express — a region, or a
whole output onto one of a different logical size — and never for two
outputs that share pixels, because a fullscreen window on the target is
drawn on the source too.**

What that means at the command line, all decided before the helper is
started:

| situation | wmirror |
|---|---|
| target already at the source's rectangle, same size, no region | **exit 0, starts nothing**: "already shows", because it does |
| same logical size, apart | **refused**, naming `wxrandr --output T --same-as S`; `--keep-layout` overrides |
| different logical size (or transform, or scale) | **runs** — the case a shared position crops |
| any `--region` | **runs** — no geometry expresses it |
| the two rectangles overlap at all | **refused**: the helper would capture its own window |
| region not wholly inside the source | **refused** with the source's rectangle (wl-mirror would silently clamp it — measured) |
| source == target, unknown output, disabled output | **refused**, naming it |
| target already mirroring | **refused** (wl-mirror would run two, the older invisible); `--replace` swaps it |
| source already mirroring the target | **refused**: they would capture each other |

## Lifetime

wl-mirror has to outlive the command that starts it, so wmirror follows the
repo's existing precedent for exactly that, `wxrandr/gamma.py`'s gamma
holder: **double-fork + `setsid`**, a `(pid, starttime)` pair in a state
file (`$XDG_RUNTIME_DIR/wmirror-state.json`, wmirror's own, keyed by the
compositor socket, using wxrandr's `State` for its locking, its three-way
merge and its refusal to trust a file that is not ours), a **euid check
before any signal**, and a bounded `SIGTERM` → `SIGKILL`.

One thing gamma does not have: **the process is not ours**. So what we
detach is a small supervisor of our own, and the record carries *two*
(pid, starttime) pairs — the supervisor's and wl-mirror's.

* The supervisor names itself up the status pipe **before** it can fail, so
  even a start that then hangs leaves a record a later `wmirror --stop` can
  act on. There is no unstoppable orphan.
* It watches the layout over `zwlr_output_management_v1` — the protocol
  wxrandr already speaks — and ends the mirror when the source or target
  **disappears or is switched off**, or when the two outputs **come to share
  pixels** (someone ran `wxrandr --same-as` underneath it). wl-mirror only
  handles the first of those itself.
* **If our supervisor is killed**, wl-mirror keeps painting — and the
  record's second pid pair means `--list` still shows it (`(supervisor
  gone)`) and `--stop` still ends it.
* **If wl-mirror dies** — its source unplugged or disabled, the compositor
  restarted — the supervisor exits with it, and the next `--list`/`--stop`
  reaps the record silently.
* A pid whose starttime no longer matches is a different process and is
  **never signalled**, whatever the state file says.
* The helper is **told which compositor to talk to** (`WAYLAND_DISPLAY` and
  `XDG_RUNTIME_DIR` set from the socket wmirror found by scanning
  `/run/user/*`), so a mirror starts from a hotkey, from `sudo`, and from
  `ssh root@box` with an empty environment, like everything else here.
* Every command that reads the records **verifies them first**, and a query
  that found nothing to correct does not rewrite the file — a session that
  has never mirrored has no state file at all.

Measured helper behaviour this is built on: source unplugged **or disabled**
→ wl-mirror exits (`output disappeared, closing`); source mode change or
move → survives and re-fits live; **target unplugged → survives**, and sway
relocated the window onto another output — in the measured run, onto its own
source; SIGTERM/SIGKILL → target's own desktop back on the next frame;
compositor restart → broken pipe, exits, no orphan; two mirrors on one
target → both run, the older invisible.

## What it costs

A mirror is not free, and the cost is architectural rather than an artefact
of the test rig. An idle sway desktop repaints only on damage; a running
mirror asks for a frame every frame, so the compositor composites at full
rate for as long as it lives. Measured in the guest (2 vCPU, software
rendering — a real GPU shrinks the absolute numbers, which is *inferred*,
not measured):

| state | wl-mirror | sway |
|---|---|---|
| no mirror, idle desktop | — | **0.0%** |
| mirroring a whole output | **88.3%** | **38.7%** |
| mirroring a 220x300 region | 48.0% | 41.6% |
| a second mirror, occluded | **0.0%** | — |

Latency was 28–184 ms behind the source, median ~63 ms, against a sampling
floor of ~65 ms — i.e. at or below what the rig could resolve, consistent
with one or two frames. Startup to a visible surface is ~150 ms.

`wxrandr --query` does not change while a mirror runs, and that is the
truth, not an omission: wl-mirror is a client window, so output management
sees nothing at all (all three heads reported unchanged geometry throughout
the measured run). `wmirror --list` is the only honest record, which is why
it verifies every pid rather than trusting the file.

## Where it does not exist

**GNOME and KDE, and it is not close.** wl-mirror's own README: it "does not
work on KDE and Gnome", needing wlroots or `ext-image-copy-capture-v1`.
Neither KWin nor Mutter implements the wlroots interfaces; on KWin
`ext_image_copy_capture_v1` is an open feature request. The only route there
is the desktop portal's ScreenCast, which prompts the user once per session
— useless from the hotkey a layout script exists for. This is the same split
`wxrandr/kwin.py` already records: KWin's `allowInterface` blacklists
`screencast` for unauthenticated clients while never blacklisting
`kde_output_*`, which is exactly why the output half needs no permission and
the capture half does. *(Upstream documentation, not re-measured on the rig.)*

**X11**: `xrandr --output B --same-as A` mirrors whole outputs, and a region
has no route in this toolbox. wmirror says so and exits 1; it never hands
over to anything, like `warandr`.

## Detection

Never assumed, always named:

* **no `wl-mirror` on PATH** → `wmirror: wl-mirror is not installed (no
  wl-mirror on PATH)` + `on Ubuntu/Debian: sudo apt install wl-mirror`. It
  is in Ubuntu **universe** — 24.04 has 0.16.1, 26.04 has 0.18.5, 46 kB,
  no exotic dependencies — so a user's step is one apt line. Both spell the
  three options we use the same way (`--fullscreen-output`, `--scaling`,
  `--region`), which is read from 0.16.1's own man page; only 0.18.5 was run
  on the rig. 0.16.1 has no extcopy backend at all, so on a compositor that
  offers `ext-image-copy-capture-v1` and nothing else it needs 0.17 or
  newer — the failure is then wl-mirror's own words, on our first line.
* **no capture protocol** (neither `zwlr_screencopy_manager_v1` nor
  `ext_image_copy_capture_manager_v1` in the registry) → say so, name both,
  and name the portal for GNOME and KDE. `zwlr_export_dmabuf_manager_v1`
  does not count: it is what wl-mirror's `auto` picks for a whole output,
  and a **region cannot use it** (measured: a region falls back to shm).
* **no `zwlr_output_manager_v1`** → we cannot read the layout, so we cannot
  check the policy; say that rather than guess.
* `wmirror --check` prints all of it, plus the outputs and what is running,
  and exits 1 if anything is missing.

The helper's stderr is **never** read as failure: `libEGL warning:` and even
`error:` lines appear there while it works perfectly. It goes to an unlinked
temp file rather than a pipe — a pipe fills if nobody drains it (stalling the
helper) and, after our own death, would kill it with SIGPIPE at an
unpredictable moment, which is exactly the mirror that is supposed to stay
alive and stoppable. The file is truncated when it grows past 64 KiB, and it
is read only when the process actually exits during the first second: that is
what turns wl-mirror's `error: options::find_output(): output X not found`
into our one-line refusal.

## Deliberately out of scope

* **A warandr GUI surface.** It would save a script X11 cannot run — see
  reason 2 above.
* **`--stream` mode.** wl-mirror can be re-aimed on stdin without a restart;
  worth having only once someone wants to move a region live.
* **`--no-show-cursor`.** The cursor is captured and mirrored by default
  (measured: pointer at layout 900,600 changed the target at exactly the
  fitted 600,552). The negative test was inconclusive on the rig, so the
  option is not exposed rather than documented on a guess.
* **Restarting a mirror the compositor ended.** When an output comes back,
  the user (or their layout script) starts it again; nothing here polls for
  hardware.

## What a live pass must still check

Everything above is measured except these, which are honest gaps:

1. **The self-capture claim** is derived from two measurements (a window on
   one output's workspace drawn on both heads at a shared rectangle, and the
   self-mirror that actually happened when the target was unplugged), not
   run as one experiment. Two outputs at 0,0, `wl-mirror --fullscreen-output
   B A`, screendump both heads.
2. **The supervisor's watch on a disabled output**: wl-mirror's exit on a
   disabled *source* is measured; that our watch also fires (sway destroying
   or updating the head) is proven against a fake in the tests, not on a
   live compositor.
3. **`--no-show-cursor`**, before it is ever exposed.
4. **The GPU numbers.** The CPU table is llvmpipe; only the shape of it
   ("the desktop never idles again") is claimed to generalise.

## Tests

* `tests/test_wmirror_cli.py` — the policy table above, region syntax, the
  argv built for each case, detection and its absence, the queries, and that
  no other package learned a new word.
* `tests/test_wmirror_lifetime.py` — every lifetime transition, driven
  against a stub standing in for wl-mirror (one that prints the libEGL
  chatter the real one prints, and that can fail at startup or die later):
  start, stop, replace, reap, a killed supervisor, a dying helper, a
  disappearing output, a compositor that goes away, `SIGTERM` then
  `SIGKILL`, and a recycled pid that must never be signalled.
