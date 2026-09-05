# Recorded XKB keymaps

Real keymaps, captured **as the compositor hands them to its clients** —
`wl_keyboard.keymap`, read with `wdotool __keymap > <name>.xkb` inside a
running session. Nothing in them is hand-written or trimmed; that is the
point of them.

| file | session layout(s) | groups in the keymap |
|---|---|---|
| `us.xkb` | `us` | `English (US)`, `English (US)` |
| `de.xkb` | `de` | `German`, `English (US)` |
| `fr.xkb` | `fr` | `French`, `English (US)` |
| `es.xkb` | `es` | `Spanish`, `English (US)` |
| `gb.xkb` | `gb` | `English (UK)`, `English (US)` |
| `dvorak.xkb` | `us+dvorak` | `English (Dvorak)`, `English (US)` |
| `us_de.xkb` | `us`, `de` | `English (US)`, `German`, `English (US)` |
| `de_fr.xkb` | `de`, `fr` | `German`, `French`, `English (US)` |
| `noble_de.xkb` | `de` | `German`, `English (US)` |
| `sway_de.xkb` | `de` | `German` |
| `us_swapescape.xkb` | `us` + `caps:swapescape` | `English (US)`, `English (US)` |
| `us_grptoggle.xkb` | `us` + `grp:win_space_toggle` | `English (US)`, `English (US)` |
| `neo.xkb` | `de(neo)` | `German (Neo 2)` |
| `kde_us.xkb` | `us` | `English (US)` |
| `kde_de.xkb` | `de` | `German` |
| `kde_gr.xkb` | `gr` | `Greek` |
| `kde_us_de.xkb` | `us`, `de` | `English (US)`, `German` |
| `kde5_de.xkb` | `de` | `German` |

`us_swapescape.xkb` and `us_grptoggle.xkb` are plain `us` sessions with a
keyboard *option* set, which is
the shape that made the US bypass refuse: `caps:swapescape` moves Escape onto
`<CAPS>` and `grp:win_space_toggle` gives `<SPCE>` the type `PC_SUPER_LEVEL2`.
Neither changes a single character the built-in US table types, so both have
to bypass.

`neo.xkb` is the one file here that was not captured from a live session:
it is `xkbcli compile-keymap --layout de --variant neo` (libxkbcommon 1.6 —
the same compiler the compositors hand their clients the output of), because
Neo 2 is the layout that settles a question no recorded session here can.
It puts `ISO_Level3_Shift` on `<CAPS>` and `<BKSL>` and `ISO_Level5_Shift` on
`<RALT>`, so "the third-level key is right Alt" — true of every other fixture
— is false in it, and `wdotool keys watch` has to report the key that was
really pressed rather than the one the layout nominates (`<LVL3>`, 84).

Everything but the `kde*` five, `noble_de.xkb`, `sway_de.xkb` and `neo.xkb`
comes from GNOME 50 / Mutter on Ubuntu 26.04 (libxkbcommon 1.11, which writes
every keysym as a hex number). `noble_de.xkb` comes from GNOME 46 / Mutter on
Ubuntu 24.04 (libxkbcommon 1.6, which writes keysym *names*) — the same layout
in the other dialect, so the parser is pinned against both. `sway_de.xkb` is
the same layout again from sway/wlroots, which compiles **only** the
configured layout: one group, so there is nothing to guess about which one is
active.

The four `kde_*.xkb` are KWin's, from Plasma 6.6 on Ubuntu 26.04, and
`kde5_de.xkb` is KWin's from Plasma 5.27 on 24.04, each captured with the
layout set the way a KDE user sets it (System Settings writes `kxkbrc`; on
5.27 the session has to restart before KWin re-reads it). KWin compiles
exactly the layouts the user configured, the way sway does — one `de` source
is one group — and `kde_de.xkb` is `sway_de.xkb` **byte for byte** (that file
kept a trailing NUL, this one did not). So the compositor contributes nothing
of its own to a keymap: the only thing that changes the bytes is the
libxkbcommon behind it. What the KDE files add is the *shape* KWin asks for,
which no GNOME capture has: a keymap with a single non-US group, and a
two-layout keymap with no appended fallback. `kde_gr.xkb` is the only
non-Latin layout here at all — Greek reaches every level (`€` on AltGr, tonos
and dialytika as dead keys) and no Latin letter on any of them.

Two facts these files record, both load-bearing for `wdotool/xkbmap.py`:

* GNOME always compiles **one more group than the user configured**, an
  `English (US)` fallback appended at the end. A session with a single `de`
  source therefore has two groups, and group 1 is the one the user picked.
  KWin and sway do not: one source is one group. `kde_us_de.xkb` is what a
  KDE user who adds a second layout gets — two groups, no fallback — and
  which of the two is live is still a guess, because no compositor sends
  `wl_keyboard.modifiers` to a client that is not focused.
* A key that binds fewer groups than the keymap has (`<SPCE>`, `<RTRN>`,
  every key that is the same on every layout) repeats its own groups — XKB's
  default `groupsWrap`. Group 2 of `us.xkb` is a full US layout even though
  most keys never mention group 2.

To capture more, in a session: `wdotool __keymap > new.xkb`. Mutter
re-reads `xkb-options` only when the *sources* setting changes, so set the
options first and then poke `org.gnome.desktop.input-sources sources`
through another value and back, or the keymap you capture will be the old
one, byte for byte.

On KDE the same trap has a different shape. `kwriteconfig6 --file kxkbrc
--group Layout --key LayoutList de` alone changes nothing: KWin watches the
file through KConfig's change notification, so the write needs `--notify`
*and* has to change the value — rewriting the value it already has notifies
nobody. Plasma 5.27's `kwriteconfig5` has no `--notify` at all and neither
`kded5`'s keyboard module nor `org.kde.KWin.reconfigure` makes KWin re-read
it, so there the session has to be restarted after the write.
