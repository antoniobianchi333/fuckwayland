"""Reverse keymap: which key produces a character under the ACTIVE layout.

We inject evdev keycodes through a virtual keyboard, and the compositor
interprets them through whatever XKB layout the session has active. The fixed
US-QWERTY table in `keymap.py` is therefore only right when the active layout
*is* US: under `de`, `fr` or Dvorak even plain ASCII comes out wrong. X11's
answer (rebind a spare keycode to the wanted keysym) has no Wayland
equivalent, and Mutter does not implement zwp_virtual_keyboard_v1, so the only
route left is to reverse the lookup: read the compositor's keymap, find the
key + modifiers that produce the character, and press *that*.

Every Wayland client is handed the full keymap as a file descriptor on
`wl_keyboard.keymap`, so `fetch()` binds the seat, takes the keyboard and
reads the fd — no protocol extension, no privileges, nothing to install.

    snap = fetch()                               # text + active group
    if not active_group_is_plain_us(snap.text, snap.group):
        rmap = build(snap.text, snap.group)      # else: keep the US table
        rmap.lookup_char("é")  -> [(13, 0), (18, 0)]   # dead_acute, then e

Layout of this module:
  * fetch()                      Wayland: keymap text + active group
  * parse()                      keymap text -> Keymap (keycodes/types/groups)
  * build()                      Keymap + group -> ReverseMap
  * ReverseMap.lookup_char()     char -> [(evdev keycode, modifier mask)]
  * active_group_is_plain_us()   the US bypass check -- deliberately its own
                                 scanner, sharing no code with the parser

An entry is (evdev keycode, modifier mask) where the mask is made of the
MOD_* bits below, NOT an X11 modifier mask: what the caller has to *press* is
a key, and which key carries level three is itself a property of the layout
(`ReverseMap.mod_keys`).

Env overrides (see README):
  WDOTOOL_LAYOUT=auto|us|xkb   force the fixed US table or the reverse map
  WDOTOOL_XKB_KEYMAP=<path>    read the keymap from a file, not the compositor
  WDOTOOL_XKB_GROUP=<n>        pin the active group (1-based)
"""

import os
import re
import struct
import unicodedata

from wdotool.keysyms import KEYSYM_TO_UNICODE, NAME_TO_KEYSYM

# Modifier *bits* of an entry's mask. MOD_SHIFT is 1 so that the fixed US
# table's `shifted` boolean and a reverse-map mask are the same value.
MOD_SHIFT = 1
MOD_LEVEL3 = 2  # AltGr / ISO_Level3_Shift
MOD_LEVEL5 = 4  # ISO_Level5_Shift

MOD_BITS = (MOD_SHIFT, MOD_LEVEL3, MOD_LEVEL5)

# Fallback keycodes for the modifier bits, used only when the keymap does not
# say which key carries them (it always does in practice).
_DEFAULT_MOD_KEYS = {MOD_SHIFT: 42, MOD_LEVEL3: 100, MOD_LEVEL5: None}


class XkbError(Exception):
    """The keymap could not be read, parsed or reversed. Always caught by the
    caller, which then falls back to the fixed US table."""


class Snapshot:
    """One reading of the compositor's keyboard state."""

    __slots__ = ("text", "group", "source", "group_known", "mods_seen")

    def __init__(self, text: str, group: int, source: str, group_known: bool, mods_seen: bool = False):
        self.text = text
        self.group = group          # 1-based, as in the keymap's name[N]
        self.source = source        # for diagnostics
        self.group_known = group_known  # False: assumed, not read back
        # Did a wl_keyboard.modifiers event actually arrive? A compositor
        # that does not send one to an unfocused client never will, so the
        # caller can stop paying `mods_wait` for it (B5).
        self.mods_seen = mods_seen


# ---------------------------------------------------------------------------
# which layout to type through


def layout_mode(forced: str | None = None) -> str:
    """"us", "xkb" or "auto" -- the layout the caller asked for, normalized.

    `forced` is a client's --layout, which outranks WDOTOOL_LAYOUT: a command
    line is the more specific statement of intent, and it is the only one that
    can reach a daemon spawned with a different environment. "fixed" is a
    spelling of "us", and anything unrecognized is "auto"."""
    mode = (forced or os.environ.get("WDOTOOL_LAYOUT") or "auto").strip().lower()
    if mode in ("us", "fixed"):
        return "us"
    return "xkb" if mode == "xkb" else "auto"


def decide(text: str, group: int, mode: str) -> bool:
    """THE BYPASS: is the fixed built-in US table the right answer here?

    "us" says so outright and nothing is read or parsed; "auto" says so when
    the active group is plain US, which is the common case and the fast one;
    "xkb" never does, even on a US keymap -- that is what asking for it means.
    """
    if mode == "us":
        return True
    return mode != "xkb" and active_group_is_plain_us(text, group)


# ---------------------------------------------------------------------------
# fetching


def fetch(timeout: float = 2.0, mods_wait: float = 0.08, keymap: str | None = None, group=None) -> Snapshot:
    """Read the active keymap + group. Raises XkbError, never hangs.

    `mods_wait` is how long to keep dispatching after the keymap arrives in
    the hope of a `wl_keyboard.modifiers` event carrying the active group.
    Mutter (and wlroots, and KWin) only send that event to the client that
    holds keyboard focus, which a headless injector never does -- so the wait
    usually expires and the group has to be inferred; see `choose_group`.

    `keymap` and `group` are what --keymap/--group pass; each falls back to
    WDOTOOL_XKB_KEYMAP / WDOTOOL_XKB_GROUP when the caller says nothing.
    """
    path = keymap if keymap is not None else os.environ.get("WDOTOOL_XKB_KEYMAP")
    forced = _pinned_group(group)
    if path:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError as e:
            raise XkbError(f"cannot read {path}: {e}") from None
        group, known = (forced, True) if forced else (None, False)
        if group is None:
            group, known = choose_group(text, None)
        return Snapshot(text, group, f"file:{path}", known)
    # A pinned group answers the only question the wait exists for, so skip
    # the wait entirely rather than paying it and throwing the answer away.
    text, group, mods_seen = _fetch_wayland(timeout, 0.0 if forced else mods_wait)
    if forced:
        return Snapshot(text, forced, "wayland (group pinned)", True, mods_seen)
    if group is None:
        group, known = choose_group(text, None)
        return Snapshot(text, group, "wayland", known, mods_seen)
    return Snapshot(text, group, "wayland", True, mods_seen)


def _pinned_group(forced=None):
    """The caller's --group, else WDOTOOL_XKB_GROUP. Anything that is not a
    plain 1..4 is not a pin, and the group gets inferred as usual."""
    raw = str(forced) if forced is not None else (os.environ.get("WDOTOOL_XKB_GROUP") or "")
    raw = raw.strip()
    if not raw.isdigit():
        return None
    n = int(raw)
    return n if 1 <= n <= 4 else None


def _fetch_wayland(timeout: float, mods_wait: float):
    """(keymap text, active group or None, modifiers event seen?)."""
    from wdotool import session
    from wdotool.wayland_mini import WlConn

    hit = session.find_wayland_socket()
    if hit is None:
        raise XkbError("no wayland socket found")
    try:
        conn = WlConn(hit[2])
    except OSError as e:
        raise XkbError(f"cannot connect to the compositor: {e}") from None
    # A wedged compositor must not hang the daemon while it holds the lock.
    conn.sock.settimeout(timeout)
    state = {"caps": 0, "text": None, "group": None, "mods": False}
    try:
        found = conn.find_global("wl_seat")
        if found is None:
            raise XkbError("the compositor advertises no wl_seat")
        seat = conn.bind(found[0], "wl_seat", min(found[1], 7))

        def seat_handler(op, cur, fds):
            if op == 0:  # capabilities(capabilities)
                state["caps"] = cur.u32()

        conn.on(seat, seat_handler)
        conn.roundtrip()
        if not state["caps"] & 2:  # WL_SEAT_CAPABILITY_KEYBOARD
            raise XkbError("the seat has no keyboard capability")
        kb = conn.alloc()
        conn.send(seat, 1, [("u", kb)])  # wl_seat.get_keyboard

        def kb_handler(op, cur, fds):
            if op == 0:  # keymap(format, fd, size)
                fmt, size = cur.u32(), cur.u32()
                if not fds:
                    return
                fd = fds.pop(0)
                try:
                    if fmt != 1:  # XKB_V1 is the only format there is
                        return
                    state["text"] = _read_keymap_fd(fd, size)
                finally:
                    os.close(fd)
            elif op == 4:  # modifiers(serial, depressed, latched, locked, group)
                cur.u32(), cur.u32(), cur.u32(), cur.u32()
                state["mods"] = True
                state["group"] = cur.u32() + 1  # wire is 0-based, keymap 1-based

        conn.on(kb, kb_handler)
        conn.roundtrip()
        if state["text"] is None:
            raise XkbError("the compositor sent no keymap")
        # The group only matters when there is more than one to choose from.
        if state["group"] is None and mods_wait > 0 and group_count(state["text"]) > 1:
            try:
                conn.dispatch(mods_wait)
            except (OSError, RuntimeError):
                pass
        return state["text"], state["group"], state["mods"]
    except (OSError, RuntimeError, ValueError, IndexError, struct.error) as e:
        raise XkbError(f"wayland keymap read failed: {e}") from None
    finally:
        conn.close()


def _read_keymap_fd(fd: int, size: int) -> str:
    import mmap

    if size <= 0 or size > (16 << 20):
        raise XkbError(f"implausible keymap size {size}")
    try:
        m = mmap.mmap(fd, size, mmap.MAP_PRIVATE, mmap.PROT_READ)
    except (OSError, ValueError):
        data = os.pread(fd, size, 0)  # a plain file (or a compositor that
    else:                             # sent a non-mmapable fd) still works
        try:
            data = m[:size]
        finally:
            m.close()
    return data.rstrip(b"\0").decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# parsing

_SECTION_RE = re.compile(r"\bxkb_(keycodes|types|compat\w*|symbols)\b[^{}]*\{")
_CODE_RE = re.compile(r"<([^<>\s]+)>\s*=\s*(\d+)\s*;")
_ALIAS_RE = re.compile(r"\balias\s+<([^<>\s]+)>\s*=\s*<([^<>\s]+)>")
_TYPE_RE = re.compile(r'\btype\s+"([^"]*)"\s*\{')
_MAP_RE = re.compile(r"\bmap\s*\[\s*([^\]]*?)\s*\]\s*=\s*(\d+)")
_GROUPNAME_RE = re.compile(r'\bname\s*\[\s*(?:Group)?(\d+)\s*\]\s*=\s*"([^"]*)"')
_KEY_RE = re.compile(r"\bkey\s+<([^<>\s]+)>\s*\{")
_FIELD_RE = re.compile(r"^(\w+)\s*(?:\[\s*(?:Group)?(\d+)\s*\])?\s*=\s*(.*)$", re.S)

# level -> mask, when the key's type says nothing else. This is the
# xkeyboard-config convention every layout follows: 3 is AltGr, 5 is level five.
# Public: keys_cmds.py builds the *forward* map (keycode + mask -> keysym)
# from the same convention, and a second copy of it would drift.
LEVEL_MASK = {
    1: 0,
    2: MOD_SHIFT,
    3: MOD_LEVEL3,
    4: MOD_SHIFT | MOD_LEVEL3,
    5: MOD_LEVEL5,
    6: MOD_SHIFT | MOD_LEVEL5,
    7: MOD_LEVEL3 | MOD_LEVEL5,
    8: MOD_SHIFT | MOD_LEVEL3 | MOD_LEVEL5,
}

# XKB modifier names we can actually press, real and virtual. Everything else
# (Lock, Control, Alt, NumLock, Mod1/2/4, Super, Meta, Hyper) makes a level
# unreachable for us: we will not hold Control down to type a character.
_MOD_NAME_BITS = {
    "shift": MOD_SHIFT,
    "levelthree": MOD_LEVEL3,
    "mod5": MOD_LEVEL3,
    "levelfive": MOD_LEVEL5,
    "mod3": MOD_LEVEL5,
}

# keysym -> the level-shift bit that key carries.
_LEVEL_KEYSYMS = {
    0xFFE1: MOD_SHIFT,   # Shift_L
    0xFFE2: MOD_SHIFT,   # Shift_R
    0xFE03: MOD_LEVEL3,  # ISO_Level3_Shift
    0xFF7E: MOD_LEVEL3,  # Mode_switch
    0xFE11: MOD_LEVEL5,  # ISO_Level5_Shift
}
# Preferred physical keys for each bit, best first: a real keyboard has AltGr
# on <RALT>, and <LVL3>/<LVL5> are the synthetic keycodes xkeyboard-config
# keeps for keyboards that have a dedicated key.
_MOD_KEY_ORDER = {
    MOD_SHIFT: ("LFSH", "RTSH"),
    MOD_LEVEL3: ("RALT", "ALGR", "LVL3", "MDSW"),
    MOD_LEVEL5: ("LVL5", "MDSW"),
}


class Group:
    """One layout inside the keymap (XKB calls them groups)."""

    def __init__(self, index: int):
        self.index = index          # 1-based
        self.name = ""
        self.syms: dict[str, list] = {}   # key name -> [keysym or None, ...]
        self.types: dict[str, str] = {}   # key name -> type name


class Keymap:
    def __init__(self):
        self.keycodes: dict[str, int] = {}     # key name -> evdev keycode
        self.types: dict[str, dict] = {}       # type name -> {level: mask|None}
        self.groups: list[Group] = []          # index 0 is group 1

    def group(self, n: int) -> Group:
        if not 1 <= n <= len(self.groups):
            raise XkbError(f"no group {n} in this keymap ({len(self.groups)})")
        return self.groups[n - 1]

    def resolved(self, n: int) -> dict:
        """{key name: [keysym, ...]} for group `n`, with XKB's group wrapping
        applied: a key that binds fewer groups than the keymap has repeats
        its own groups (the default groupsWrap). Most keys -- every key that
        is the same on every layout, <RTRN> and <SPCE> included -- bind only
        group 1, and would otherwise vanish from group 2."""
        self.group(n)
        out = {}
        for key, per_group in self._by_key().items():
            top = max(per_group)
            levels = per_group.get((n - 1) % top + 1) or per_group.get(1)
            if levels:
                out[key] = levels
        return out

    def _by_key(self) -> dict:
        by_key: dict = {}
        for g in self.groups:
            for key, levels in g.syms.items():
                by_key.setdefault(key, {})[g.index] = levels
        return by_key

    @property
    def group_names(self) -> list:
        return [g.name for g in self.groups]


# A string literal, or a comment. Matching the literal first is what keeps a
# `//` inside a group name from being taken for a comment.
_COMMENT_RE = re.compile(r'"(?:[^"\\\n]|\\.)*"|//[^\n]*|/\*.*?\*/', re.S)


def strip_comments(text: str) -> str:
    """XKB comments (`// ...`, `/* ... */`) removed, string literals kept.

    Pure text handling with no keymap knowledge, which is why the parser and
    the US bypass may share it: a `}` inside a comment would otherwise close
    a block early and silently halve the keymap (B4). No compositor emits
    comments, but `WDOTOOL_XKB_KEYMAP=<file>` is a documented input and
    hand-written keymaps are full of them.
    """
    if "//" not in text and "/*" not in text:
        return text  # the common case: not one byte copied
    return _COMMENT_RE.sub(lambda m: m.group(0) if m.group(0)[:1] == '"' else " ", text)


def group_name(text: str, n: int) -> str:
    """The name of group `n`, by regex: no parse, so the US bypass path can
    say which group it assumed without building anything (B1)."""
    for m in _GROUPNAME_RE.finditer(strip_comments(text)):
        if int(m.group(1)) == n:
            return m.group(2) or f"group {n}"
    return f"group {n}"


# libxkbcommon's XKB_MAX_GROUPS: no keymap can legitimately declare more.
# The group index is a number the compositor chose, not a length we measured,
# so `symbols[Group2000000000]` is eight bytes of keymap text that parse()
# would otherwise turn into two billion Group objects (a root daemon can be
# pointed at a planted Wayland socket, so the compositor is not always ours).
MAX_GROUPS = 4


def group_count(text: str) -> int:
    """How many groups the keymap declares, capped at MAX_GROUPS. Cheap: no
    full parse."""
    n = 0
    for m in _GROUPNAME_RE.finditer(text):
        n = max(n, int(m.group(1)))
    for m in re.finditer(r"\bsymbols\s*\[\s*(?:Group)?(\d+)\s*\]", text):
        n = max(n, int(m.group(1)))
    return min(n, MAX_GROUPS) or 1


def _brace_body(text: str, open_idx: int) -> str:
    """The text between the brace at open_idx and its match. String-aware."""
    depth = 0
    i = open_idx
    n = len(text)
    while i < n:
        c = text[i]
        if c == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[open_idx + 1:i]
        i += 1
    raise XkbError("unterminated block in the keymap")


def _sections(text: str) -> dict:
    out = {}
    for m in _SECTION_RE.finditer(text):
        kind = m.group(1)
        kind = "compat" if kind.startswith("compat") else kind
        if kind not in out:
            out[kind] = _brace_body(text, m.end() - 1)
    return out


def _split_fields(body: str) -> list:
    """Split a key block into its comma-separated fields, ignoring commas
    inside [ ... ] lists and strings."""
    fields = []
    depth = 0
    cur = []
    i = 0
    n = len(body)
    while i < n:
        c = body[i]
        if c == '"':
            cur.append(c)
            i += 1
            while i < n and body[i] != '"':
                cur.append(body[i])
                i += 1
            cur.append('"')
        elif c in "[({":
            depth += 1
            cur.append(c)
        elif c in "])}":
            depth -= 1
            cur.append(c)
        elif c == "," and depth == 0:
            fields.append("".join(cur).strip())
            cur = []
        else:
            cur.append(c)
        i += 1
    tail = "".join(cur).strip()
    if tail:
        fields.append(tail)
    return [f for f in fields if f]


def keysym_value(tok: str):
    """One symbol-list token -> keysym number, or None for NoSymbol."""
    tok = tok.strip()
    if not tok or tok in ("NoSymbol", "VoidSymbol", "any"):
        return None
    if tok[:2] in ("0x", "0X"):
        try:
            return int(tok, 16)
        except ValueError:
            return None
    ks = NAME_TO_KEYSYM.get(tok)
    if ks is not None:
        return ks
    m = re.fullmatch(r"U([0-9A-Fa-f]{4,6})", tok)
    if m:
        cp = int(m.group(1), 16)
        return cp if cp < 0x100 else 0x01000000 + cp
    return None


def keysym_char(ks: int):
    """The character a keysym types, or None (dead keys included: they type
    nothing on their own)."""
    if ks is None:
        return None
    cp = KEYSYM_TO_UNICODE.get(ks)
    if cp is None:
        if 0x20 <= ks <= 0xFF:
            cp = ks
        elif ks & 0xFF000000 == 0x01000000:
            cp = ks & 0xFFFFFF
        else:
            return None
    if cp < 0x20 or cp == 0x7F:  # Return/Tab/BackSpace: named keys, not text
        return None
    try:
        return chr(cp)
    except ValueError:
        return None


def _type_level_masks(body: str) -> dict:
    """A type's map[] entries -> {level: cheapest mask we can press}."""
    out = {1: 0}
    for m in _MAP_RE.finditer(body):
        names = [n.strip().lower() for n in m.group(1).split("+") if n.strip()]
        level = int(m.group(2))
        mask = 0
        ok = True
        for name in names:
            if name == "none":
                continue
            bit = _MOD_NAME_BITS.get(name)
            if bit is None:
                ok = False
                break
            mask |= bit
        if not ok:
            continue
        prev = out.get(level)
        if prev is None or bin(mask).count("1") < bin(prev).count("1"):
            out[level] = mask
    return out


def parse(text: str) -> Keymap:
    """Parse a keymap in XKB_KEYMAP_FORMAT_TEXT_V1 (what every compositor
    hands out). Raises XkbError on anything it cannot make sense of."""
    text = strip_comments(text)
    if "xkb_symbols" not in text:
        raise XkbError("not an xkb keymap (no xkb_symbols section)")
    sec = _sections(text)
    km = Keymap()

    # -- keycodes: <AD01> = 24;  (X keycodes; evdev is 8 lower)
    codes = sec.get("keycodes", "")
    for m in _CODE_RE.finditer(codes):
        x = int(m.group(2))
        if 8 < x < 264:
            km.keycodes[m.group(1)] = x - 8
    for m in _ALIAS_RE.finditer(codes):
        target = km.keycodes.get(m.group(2))
        if target is not None:
            km.keycodes.setdefault(m.group(1), target)
    if not km.keycodes:
        raise XkbError("the keymap declares no keycodes")

    # -- types: which modifiers reach which level
    types_body = sec.get("types", "")
    for m in _TYPE_RE.finditer(types_body):
        km.types[m.group(1)] = _type_level_masks(_brace_body(types_body, m.end() - 1))

    # -- symbols: the groups themselves
    syms = sec.get("symbols")
    if syms is None:
        raise XkbError("the keymap has no xkb_symbols section")
    ngroups = max(group_count(syms), 1)
    km.groups = [Group(i + 1) for i in range(ngroups)]
    for m in _GROUPNAME_RE.finditer(syms):
        i = int(m.group(1))
        if 1 <= i <= ngroups:
            km.groups[i - 1].name = m.group(2)
    for m in _KEY_RE.finditer(syms):
        name = m.group(1)
        if name not in km.keycodes:
            continue
        body = _brace_body(syms, m.end() - 1)
        bare = 0
        for field in _split_fields(body):
            if field.startswith("["):
                bare += 1
                _add_symbols(km, name, bare, field)
                continue
            fm = _FIELD_RE.match(field)
            if not fm:
                continue
            what, idx, value = fm.group(1).lower(), fm.group(2), fm.group(3).strip()
            if what == "symbols" and value.startswith("["):
                _add_symbols(km, name, int(idx) if idx else 1, value)
            elif what == "type" and value.startswith('"'):
                tname = value.strip('"; ')
                for g in ([int(idx)] if idx else range(1, ngroups + 1)):
                    if 1 <= g <= ngroups:
                        km.groups[g - 1].types[name] = tname
    if not any(g.syms for g in km.groups):
        raise XkbError("the keymap binds no symbols")
    return km


def _add_symbols(km: Keymap, key: str, group: int, listtext: str):
    if not 1 <= group <= len(km.groups):
        return
    inner = listtext[listtext.index("[") + 1:]
    inner = inner[:inner.rindex("]")] if "]" in inner else inner
    km.groups[group - 1].syms[key] = [keysym_value(t) for t in inner.split(",")]


# ---------------------------------------------------------------------------
# dead keys

# dead keysym -> (combining codepoint, the character the dead key + space
# produces). NFD-decomposing a character gives the combining mark, which is
# how "é" becomes dead_acute + e without shipping a Compose table.
#
# The second element is not the spacing accent that shares the dead key's
# name: it is whatever <dead_x> <space> yields in the Compose table every
# toolkit implements (/usr/share/X11/locale/*/Compose), which for acute,
# diaeresis and abovering is ASCII ' " and °. Claiming U+00B4 there made
# `type ´` send dead_acute + space and land an apostrophe instead (B3).
DEAD_KEYSYMS = {
    0xFE50: (0x0300, 0x0060),  # dead_grave
    0xFE51: (0x0301, 0x0027),  # dead_acute      -> apostrophe, not U+00B4
    0xFE52: (0x0302, 0x005E),  # dead_circumflex
    0xFE53: (0x0303, 0x007E),  # dead_tilde
    0xFE54: (0x0304, 0x00AF),  # dead_macron
    0xFE55: (0x0306, 0x02D8),  # dead_breve
    0xFE56: (0x0307, 0x02D9),  # dead_abovedot
    0xFE57: (0x0308, 0x0022),  # dead_diaeresis  -> quotedbl, not U+00A8
    0xFE58: (0x030A, 0x00B0),  # dead_abovering  -> degree, not U+02DA
    0xFE59: (0x030B, 0x02DD),  # dead_doubleacute
    0xFE5A: (0x030C, 0x02C7),  # dead_caron
    0xFE5B: (0x0327, 0x00B8),  # dead_cedilla
    0xFE5C: (0x0328, 0x02DB),  # dead_ogonek
    0xFE5D: (0x0345, None),    # dead_iota
    0xFE5E: (0x3099, None),    # dead_voiced_sound
    0xFE5F: (0x309A, None),    # dead_semivoiced_sound
    0xFE60: (0x0323, None),    # dead_belowdot
    0xFE61: (0x0309, None),    # dead_hook
    0xFE62: (0x031B, None),    # dead_horn
    0xFE63: (0x0338, None),    # dead_stroke
    0xFE64: (0x0313, None),    # dead_abovecomma
    0xFE65: (0x0314, None),    # dead_abovereversedcomma
    0xFE66: (0x030F, None),    # dead_doublegrave
    0xFE67: (0x0325, None),    # dead_belowring
    0xFE68: (0x0331, None),    # dead_belowmacron
    0xFE69: (0x032D, None),    # dead_belowcircumflex
    0xFE6A: (0x0330, None),    # dead_belowtilde
    0xFE6B: (0x032E, None),    # dead_belowbreve
    0xFE6C: (0x0324, None),    # dead_belowdiaeresis
    0xFE6D: (0x0311, None),    # dead_invertedbreve
    0xFE6E: (0x0326, None),    # dead_belowcomma
}

# <dead_x> <dead_x> types the spacing accent itself -- the only way to type
# one on a layout that has it *only* as a dead key, now that <dead_x> <space>
# is known to type something else. The six the Compose table lists, no more.
DEAD_DOUBLE = {
    0xFE50: 0x0060,  # dead_grave      -> `
    0xFE51: 0x00B4,  # dead_acute      -> ´
    0xFE52: 0x005E,  # dead_circumflex -> ^
    0xFE53: 0x007E,  # dead_tilde      -> ~
    0xFE57: 0x00A8,  # dead_diaeresis  -> ¨
    0xFE58: 0x00B0,  # dead_abovering  -> °
}

# Characters that are keys, not text. xdotool's `type` sends these through the
# named key, exactly as the fixed US table does.
_CONTROL_KEYSYMS = {"\n": 0xFF0D, "\r": 0xFF0D, "\t": 0xFF09, "\b": 0xFF08, "\x1b": 0xFF1B, "\x7f": 0xFFFF}


class ReverseMap:
    """char/keysym -> the keystrokes that produce it under one group."""

    def __init__(self, group_index: int, group_name: str):
        self.group = group_index
        self.name = group_name or f"group {group_index}"
        self.chars: dict[str, tuple] = {}      # char -> (keycode, mask)
        self.keysyms: dict[int, tuple] = {}    # keysym -> (keycode, mask)
        self.dead: dict[int, tuple] = {}       # combining cp -> (keycode, mask)
        self.dead_space: dict[str, tuple] = {}  # dead+space char -> entry
        self.dead_double: dict[str, tuple] = {}  # dead+dead char -> entry
        self.mod_keys: dict[int, int] = {}     # MOD_* bit -> evdev keycode
        self._best: dict = {}                  # (table, key) -> cost so far

    # -- lookups ----------------------------------------------------------

    def lookup_char(self, ch: str):
        """The keystrokes that type `ch`: [(keycode, mask), ...], or None.

        More than one keystroke means a dead-key sequence: press the dead key,
        then the base character, and the *application* composes them (that is
        how a physical keyboard types é on a French layout too)."""
        ks = _CONTROL_KEYSYMS.get(ch)
        if ks is not None:
            hit = self.keysyms.get(ks)
            return [hit] if hit else None
        hit = self.chars.get(ch)
        if hit:
            return [hit]
        seq = self._dead_sequence(ch)
        if seq:
            return seq
        return None

    def _dead_sequence(self, ch: str):
        dead = self.dead_space.get(ch)
        if dead:  # dead key, then space
            space = self.chars.get(" ")
            if space:
                return [dead, space]
        dead = self.dead_double.get(ch)
        if dead:  # a spacing accent: the dead key twice
            return [dead, dead]
        decomposed = unicodedata.normalize("NFD", ch)
        if len(decomposed) != 2:
            return None
        base, mark = decomposed
        dead = self.dead.get(ord(mark))
        base_hit = self.chars.get(base)
        if dead and base_hit:
            # Only if it really recomposes: NFC(base+mark) == ch. Anything
            # else would type two characters where one was asked for.
            if unicodedata.normalize("NFC", base + mark) == ch:
                return [dead, base_hit]
        return None

    def keysym_entry(self, name: str):
        """(keycode, mask) for a keysym *name*, or None. Used by `key`."""
        ks = NAME_TO_KEYSYM.get(name)
        return self.keysyms.get(ks) if ks is not None else None

    def modifier_keycodes(self, mask: int) -> list:
        return [self.mod_keys[b] for b in MOD_BITS if mask & b and self.mod_keys.get(b)]

    # -- building ---------------------------------------------------------

    def _better(self, tag: str, key, entry, rank: int = 0):
        """Keep the cheapest keystroke for `key` in the table named `tag`:
        fewest modifiers first, and a main-block key over a keypad one."""
        table = getattr(self, tag)
        cost = (rank,) + _cost(entry)
        old = self._best.get((tag, key))
        if old is None or cost < old:
            self._best[(tag, key)] = cost
            table[key] = entry


def _cost(entry) -> tuple:
    code, mask = entry
    return (bin(mask).count("1"), mask, code)


def _keypad_rank(code: int, ks) -> int:
    """0 for a key in the main block, 1 for one on the keypad (B6).

    Excluding the keypad by key *name* (`<KP...>`) leaks: Mutter's keymaps
    put KP_Decimal on <I129> and the keypad parentheses on <I187>/<I188>, so
    `.` on a French layout and `(` on every non-US one used to resolve to a
    keypad keycode. A keypad entry is still recorded -- it really does
    produce the character -- but it can never beat a main-block key."""
    if ks is not None and 0xFF80 <= ks <= 0xFFBD:  # KP_Space .. KP_Equal
        return 1
    return 1 if code >= 128 else 0


def build(text: str, group: int = 1) -> ReverseMap:
    """Parse `text` and reverse its group `group` (1-based)."""
    return reverse(parse(text), group)


def reverse(km: Keymap, group: int = 1) -> ReverseMap:
    g = km.group(group)
    syms = km.resolved(group)
    rmap = ReverseMap(group, g.name)

    # Which key carries Shift / level three / level five *in this group*: on a
    # German layout <RALT> is ISO_Level3_Shift, on a US one it is Alt_R.
    ranks: dict = {}
    for key, levels in sorted(syms.items()):
        code = km.keycodes.get(key)
        if code is None or not levels or not 0 < code < 256:
            continue
        bit = _LEVEL_KEYSYMS.get(levels[0])
        if bit is None:
            continue
        order = _MOD_KEY_ORDER.get(bit, ())
        rank = order.index(key) if key in order else len(order)
        if rank < ranks.get(bit, 99):
            ranks[bit] = rank
            rmap.mod_keys[bit] = code
    # What the *keymap* said, before the fallbacks are filled in: the guard
    # below has to test that, not the backfilled table, or it can never fire
    # for the case it names. Every fixture we have names all three keys, so
    # the fallbacks are belt and braces and this changes nothing for them.
    from_keymap = set(rmap.mod_keys)
    for bit, code in _DEFAULT_MOD_KEYS.items():
        if code and bit not in rmap.mod_keys:
            rmap.mod_keys[bit] = code

    for key, levels in sorted(syms.items()):
        code = km.keycodes.get(key)
        if code is None or not 0 < code < 256:
            continue
        masks = km.types.get(g.types.get(key, ""), None)
        for i, ks in enumerate(levels):
            if ks is None:
                continue
            level = i + 1
            mask = masks.get(level) if masks else LEVEL_MASK.get(level)
            if mask is None:
                continue
            # A level we cannot press (no AltGr key in this layout) is a level
            # that does not exist for us. Testing rmap.mod_keys here tested
            # the fallback keycodes too, so on a layout with four-level types
            # and no level-3 key -- lv3:none, a custom keymap -- 77 German
            # characters still resolved to "hold keycode 100", which without
            # ISO_Level3_Shift on it is a plain Alt_R: a menu accelerator on
            # both GNOME and KWin, i.e. a wrong action where the promise is a
            # warning and a skip.
            if any(mask & b and b not in from_keymap for b in MOD_BITS):
                continue
            entry = (code, mask)
            rank = _keypad_rank(code, ks)
            rmap._better("keysyms", ks, entry, rank)
            dead = DEAD_KEYSYMS.get(ks)
            if dead is not None:
                rmap._better("dead", dead[0], entry, rank)
                if dead[1] is not None:
                    rmap._better("dead_space", chr(dead[1]), entry, rank)
                twice = DEAD_DOUBLE.get(ks)
                if twice is not None:
                    rmap._better("dead_double", chr(twice), entry, rank)
                continue
            ch = keysym_char(ks)
            if ch is not None:
                rmap._better("chars", ch, entry, rank)
    if not rmap.chars:
        raise XkbError("no typable character in group %d" % group)
    return rmap


# ---------------------------------------------------------------------------
# group selection

_US_KEY_RE = re.compile(r"\bkey\s+<([^<>\s]+)>\s*\{([^{}]*)\}")
_US_SYMS_RE = re.compile(r"\bsymbols\s*\[\s*(?:Group)?(\d+)\s*\]\s*=\s*\[([^\]]*)\]")


def choose_group(text: str, from_modifiers=None) -> tuple:
    """(group, known) -- which group to reverse, and whether we *know*.

    The compositor only sends wl_keyboard.modifiers -- the one event that
    carries the active group -- to the client holding keyboard focus, which
    an injector never is. What is left:

      * one group, or several that bind the same symbols: the choice cannot
        matter (GNOME compiles a single `us` source as the two groups
        "us,us", so this is the ordinary GNOME case);
      * otherwise group 1, flagged as *assumed*. It is the first configured
        source, which is the active one whenever the user configured exactly
        one -- the case this whole module exists for. GNOME appends its own
        `us` fallback group after the user's sources, so "de,us" is a session
        with one German source, and group 1 is right.

    Deliberately regex-only: choosing a group must not need the parser, or
    the parser would run on a plain US layout, which the bypass promises it
    does not (see `active_group_is_plain_us`).
    """
    if from_modifiers:
        return from_modifiers, True
    if group_count(text) <= 1 or _groups_agree(text):
        return 1, True
    return 1, False


def _groups_agree(text: str) -> bool:
    """True when no key binds different symbols to different groups."""
    for m in _US_KEY_RE.finditer(text):
        lists = [sm.group(2).split() for sm in _US_SYMS_RE.finditer(m.group(2))]
        if len(lists) > 1 and any(sym != lists[0] for sym in lists[1:]):
            return False
    return True


# ---------------------------------------------------------------------------
# the US bypass
#
# Deliberately standalone: its own regexes, its own scan, no call into the
# parser or the reverse map above. When the active layout is plain US this
# function is the *only* new code that runs, so a bug in the machinery above
# cannot reach the most common setup.

_US_CODE_RE = re.compile(r"<([^<>\s]+)>\s*=\s*(\d+)\s*;")
_US_BARE_RE = re.compile(r"^\s*\[([^\]]*)\]\s*$")
_US_TYPE_RE = re.compile(r'\btype\s*(?:\[\s*(?:Group)?(\d+)\s*\])?\s*=\s*"([^"]*)"')

# The names xkeyboard-config gives the plain `us` layout, and nothing else.
# `USA` is what the same layout is called when the session picks a Macintosh
# keyboard model; the key-by-key check below is what actually decides.
US_GROUP_NAME = "English (US)"
US_GROUP_NAMES = (US_GROUP_NAME, "USA")

# Types whose level 2 is reached with Shift and level 1 with no modifier --
# the only shapes under which the fixed table's (keycode, shifted) pair is
# right. An unknown type on a key we care about fails the check.
_US_OK_TYPES = frozenset({
    "ONE_LEVEL", "TWO_LEVEL", "ALPHABETIC", "KEYPAD", "FOUR_LEVEL",
    "FOUR_LEVEL_ALPHABETIC", "FOUR_LEVEL_SEMIALPHABETIC",
    "FOUR_LEVEL_MIXED_KEYPAD", "FOUR_LEVEL_X", "FOUR_LEVEL_PLUS_LOCK",
    "FOUR_LEVEL_KEYPAD", "EIGHT_LEVEL", "EIGHT_LEVEL_ALPHABETIC",
    "EIGHT_LEVEL_SEMIALPHABETIC", "EIGHT_LEVEL_LEVEL_FIVE_LOCK",
    "EIGHT_LEVEL_ALPHABETIC_LEVEL_FIVE_LOCK",
    "EIGHT_LEVEL_SEMIALPHABETIC_LEVEL_FIVE_LOCK", "CTRL+ALT",
    "SEPARATE_CAPS_AND_SHIFT_ALPHABETIC", "PC_SYSRQ", "PC_BREAK",
})


def _expected_us() -> dict:
    """{evdev keycode: {level: keysym}} that the fixed US table assumes.

    Only the *printable* characters: Return, Tab, BackSpace, Escape and
    Delete are position keys, in the same place on every layout, and they go
    through `KEYSYM_KEYS` rather than through any layout table. Demanding
    them here made `us` + `caps:swapescape` -- a plain US session by any
    honest reading -- fail the check and drag the whole reverse map in (B2).
    """
    from wdotool import keymap as _keymap

    out: dict = {}
    for ch, (code, shifted) in _keymap.CHAR_TO_KEY.items():
        if len(ch) != 1 or ch < " " or ch == "\x7f":
            continue
        out.setdefault(code, {})[2 if shifted else 1] = ord(ch)
    return out


def active_group_is_plain_us(text: str, group: int = 1) -> bool:
    """Is the fixed US table exactly right for this keymap's active group?

    Not "is it called us": the name is only the fast reject. Every keycode
    the fixed table would ever emit is checked against the keysyms the keymap
    binds to it at level 1 and 2 in the active group, and every one of them
    has to be present and identical. A layout that passes types the same
    characters as the fixed table by construction, so the bypass cannot type
    the wrong thing; anything unexpected -- a missing key, an extra dead key
    on level 1, a type whose level 2 is not Shift, a keymap this scanner
    cannot read -- answers False and the reverse map takes over.
    """
    try:
        return _plain_us(text, group)
    except Exception:
        return False


def _plain_us(text: str, group: int) -> bool:
    text = strip_comments(text)
    if not re.search(r'\bname\s*\[\s*(?:Group)?%d\s*\]\s*=\s*"(?:%s)"'
                     % (group, "|".join(re.escape(n) for n in US_GROUP_NAMES)),
                     text):
        return False
    codes = {}
    for m in _US_CODE_RE.finditer(text):
        x = int(m.group(2))
        if 8 < x < 264:
            codes[m.group(1)] = x - 8
    want = _expected_us()
    seen = set()
    for m in _US_KEY_RE.finditer(text):
        code = codes.get(m.group(1))
        if code is None or code not in want:
            continue
        body = m.group(2)
        # A stated type only matters where the fixed table uses level 2: it
        # is the "level 2 is Shift" assumption that is being checked. On a
        # key the table only ever presses unshifted (<SPCE>, which
        # `grp:win_space_toggle` gives the type PC_SUPER_LEVEL2) the type is
        # none of our business (B2).
        if 2 in want[code]:
            for tm in _US_TYPE_RE.finditer(body):
                if tm.group(1) in (None, str(group)):
                    if tm.group(2) not in _US_OK_TYPES:
                        return False
        # XKB group wrapping: a key that binds fewer groups than the keymap
        # has repeats its own. <SPCE> binds group 1 only and is still the
        # space bar in group 2.
        per_group = {int(sm.group(1)): sm.group(2) for sm in _US_SYMS_RE.finditer(body)}
        if per_group:
            levels = per_group.get((group - 1) % max(per_group) + 1)
        else:
            bare = _US_BARE_RE.match(body.strip().rstrip(";").strip())
            levels = bare.group(1) if bare else None
        if levels is None:
            return False
        syms = [_us_keysym(t) for t in levels.split(",")]
        for level, ks in want[code].items():
            if len(syms) < level or syms[level - 1] != ks:
                return False
        seen.add(code)
    return seen == set(want)


def _us_keysym(tok: str):
    """Keysym number of one token, for the bypass check only (its own
    resolver: the check must not depend on the parser above)."""
    tok = tok.strip()
    if tok[:2] in ("0x", "0X"):
        try:
            return int(tok, 16)
        except ValueError:
            return None
    return NAME_TO_KEYSYM.get(tok)


# ---------------------------------------------------------------------------
# `wdotool __keymap`: the hidden diagnostic subcommand
#
# Not in the command registry and not in `help`: xdotool has no such command
# and wdotool's output is byte-compatible with its help. It exists because
# every question about this module ("what did the compositor actually send
# us", "which group is it using", "why is that character skipped") is one
# `wdotool __keymap` away, and because the test fixtures were captured with it.

_KEYMAP_USAGE = """Usage: wdotool __keymap [--info] [--chars STRING] [--group N]
Diagnostic: dump the keymap the compositor hands its clients.

--info          summarise instead of dumping: source, groups, active group,
                whether the US bypass would take it, the level-shift keys
--chars STRING  show the keystrokes each character of STRING would need
--group N       reverse group N (1-based) instead of the active one
--keymap PATH   read the keymap from a file instead of the compositor
"""


def diagnostic_main(argv) -> int:
    import sys

    want_info = want_chars = keymap = None
    group = None
    args = list(argv)
    while args:
        a = args.pop(0)
        if a in ("-h", "--help"):
            sys.stdout.write(_KEYMAP_USAGE)
            return 0
        elif a == "--info":
            want_info = True
        elif a == "--chars" and args:
            want_chars = args.pop(0)
        elif a == "--group" and args:
            group = args.pop(0)
            if not group.isdigit():
                sys.stderr.write("wdotool __keymap: --group wants a number\n")
                return 1
        elif a == "--keymap" and args:
            keymap = args.pop(0)
        else:
            sys.stderr.write(f"wdotool __keymap: unknown option '{a}'\n")
            sys.stderr.write(_KEYMAP_USAGE)
            return 1
    try:
        snap = fetch(keymap=keymap, group=group)
    except XkbError as e:
        sys.stderr.write(f"wdotool: {e}\n")
        return 2
    if not (want_info or want_chars):
        sys.stdout.write(snap.text)
        return 0
    try:
        km = parse(snap.text)
    except XkbError as e:
        sys.stderr.write(f"wdotool: {e}\n")
        return 1
    # The same decision the typing path makes, so the diagnostic cannot
    # describe a layout wdotool would not have used (B13).
    mode = layout_mode()
    bypass = decide(snap.text, snap.group, mode)
    rmap = None

    def reversed_map():
        """reverse() at most once, however many of --info and --chars ask."""
        nonlocal rmap
        if rmap is None:
            rmap = reverse(km, snap.group)
        return rmap

    if want_info:
        print(f"source:        {snap.source}")
        print(f"keymap:        {len(snap.text)} bytes, {len(km.keycodes)} keycodes, "
              f"{len(km.types)} types")
        for g in km.groups:
            mark = " <- active" + ("" if snap.group_known else " (assumed)")
            print(f"group {g.index}:      {g.name!r}" + (mark if g.index == snap.group else ""))
        if mode == "us":
            print("us bypass:     yes -- WDOTOOL_LAYOUT=us asks for it")
        elif bypass:
            print("us bypass:     yes -- the fixed US table is used")
        elif mode == "xkb" and active_group_is_plain_us(snap.text, snap.group):
            print("us bypass:     no -- WDOTOOL_LAYOUT=xkb overrides it")
        else:
            print("us bypass:     no")
        if not bypass:
            try:
                rmap = reversed_map()
            except XkbError as e:
                # --group 3 on a two-group keymap, a keymap with nothing
                # typable in it: the diagnostic reports what it found, like
                # every other failure here, and never tracebacks.
                sys.stderr.write("wdotool: %s\n" % e)
                return 1
            names = {MOD_SHIFT: "shift", MOD_LEVEL3: "level3", MOD_LEVEL5: "level5"}
            mods = ", ".join(f"{names[b]}=key {rmap.mod_keys[b]}" for b in MOD_BITS if rmap.mod_keys.get(b))
            print(f"level shifts:  {mods}")
            print(f"reachable:     {len(rmap.chars)} characters, "
                  f"{len(rmap.dead)} dead keys")
    if want_chars:
        if bypass:
            # Answer from the table wdotool would actually use, or the
            # diagnostic contradicts the "us bypass: yes" line above it.
            from wdotool import keymap as _keymap

            print("(the US bypass is in effect: these are the built-in US "
                  "table's keystrokes, which is what wdotool sends)")

            def lookup(ch):
                hit = _keymap.char_to_key(ch)
                return None if hit is None else [(hit[0], MOD_SHIFT if hit[1] else 0)]
        else:
            try:
                lookup = reversed_map().lookup_char
            except XkbError as e:
                sys.stderr.write("wdotool: %s\n" % e)
                return 1
        for ch in want_chars:
            seq = lookup(ch)
            if seq is None:
                print(f"{ch!r}: unreachable")
            else:
                print(f"{ch!r}: " + " then ".join(
                    "key %d%s" % (c, "+" + "+".join(
                        n for b, n in ((MOD_SHIFT, "shift"), (MOD_LEVEL3, "level3"),
                                       (MOD_LEVEL5, "level5")) if m & b) if m else "")
                    for c, m in seq))
    return 0
