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

Everything but `noble_de.xkb` and `sway_de.xkb` comes from GNOME 50 / Mutter
on Ubuntu 26.04 (libxkbcommon 1.11, which writes every keysym as a hex number).
`noble_de.xkb` comes from GNOME 46 / Mutter on Ubuntu 24.04 (libxkbcommon
1.6, which writes keysym *names*) — the same layout in the other dialect, so
the parser is pinned against both. `sway_de.xkb` is the same layout again
from sway/wlroots, which compiles **only** the configured layout: one group,
so there is nothing to guess about which one is active.

Two facts these files record, both load-bearing for `wdotool/xkbmap.py`:

* GNOME always compiles **one more group than the user configured**, an
  `English (US)` fallback appended at the end. A session with a single `de`
  source therefore has two groups, and group 1 is the one the user picked.
* A key that binds fewer groups than the keymap has (`<SPCE>`, `<RTRN>`,
  every key that is the same on every layout) repeats its own groups — XKB's
  default `groupsWrap`. Group 2 of `us.xkb` is a full US layout even though
  most keys never mention group 2.

To capture more, in a session: `wdotool __keymap > new.xkb`.
