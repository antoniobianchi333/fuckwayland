"""US-QWERTY character/keysym -> evdev keycode tables and keysequence parsing.

A resolved key is (evdev_keycode, shifted). "shifted" means the key needs
Shift held to produce the requested symbol on a US layout.
"""

from wdotool.keysyms import KEYSYM_TO_UNICODE, NAME_TO_KEYSYM

# evdev keycodes (input-event-codes.h)
KEY_ESC = 1
KEY_BACKSPACE = 14
KEY_TAB = 15
KEY_ENTER = 28
KEY_LEFTCTRL = 29
KEY_LEFTSHIFT = 42
KEY_RIGHTSHIFT = 54
KEY_KPASTERISK = 55
KEY_LEFTALT = 56
KEY_SPACE = 57
KEY_CAPSLOCK = 58
KEY_NUMLOCK = 69
KEY_SCROLLLOCK = 70
KEY_KPMINUS = 74
KEY_KPPLUS = 78
KEY_KPDOT = 83
KEY_KPENTER = 96
KEY_RIGHTCTRL = 97
KEY_KPSLASH = 98
KEY_SYSRQ = 99
KEY_RIGHTALT = 100
KEY_LINEFEED = 101
KEY_HOME = 102
KEY_UP = 103
KEY_PAGEUP = 104
KEY_LEFT = 105
KEY_RIGHT = 106
KEY_END = 107
KEY_DOWN = 108
KEY_PAGEDOWN = 109
KEY_INSERT = 110
KEY_DELETE = 111
KEY_KPEQUAL = 117
KEY_PAUSE = 119
KEY_KPCOMMA = 121
KEY_LEFTMETA = 125
KEY_RIGHTMETA = 126
KEY_COMPOSE = 127

_KP = {0: 82, 1: 79, 2: 80, 3: 81, 4: 75, 5: 76, 6: 77, 7: 71, 8: 72, 9: 73}


def _rows(unshifted: str, shifted: str, first_code: int, codes=None):
    for i, (u, s) in enumerate(zip(unshifted, shifted)):
        code = codes[i] if codes else first_code + i
        CHAR_TO_KEY[u] = (code, False)
        CHAR_TO_KEY[s] = (code, True)


# char -> (evdev keycode, shifted). All printable ASCII plus the control chars
# `xdotool type` produces (\n \r -> Return, \t -> Tab, \b, \x1b, \x7f).
CHAR_TO_KEY: dict[str, tuple[int, bool]] = {}
_rows("`1234567890-=", "~!@#$%^&*()_+", 41, codes=[41] + list(range(2, 14)))
_rows("qwertyuiop[]\\", "QWERTYUIOP{}|", 16, codes=list(range(16, 28)) + [43])
_rows("asdfghjkl;'", 'ASDFGHJKL:"', 30)
_rows("zxcvbnm,./", "ZXCVBNM<>?", 44)
CHAR_TO_KEY[" "] = (KEY_SPACE, False)
CHAR_TO_KEY["\n"] = (KEY_ENTER, False)
CHAR_TO_KEY["\r"] = (KEY_ENTER, False)
CHAR_TO_KEY["\t"] = (KEY_TAB, False)
CHAR_TO_KEY["\b"] = (KEY_BACKSPACE, False)
CHAR_TO_KEY["\x1b"] = (KEY_ESC, False)
CHAR_TO_KEY["\x7f"] = (KEY_DELETE, False)

# keysym name -> (keycode, shifted) for keys that are not (or not best) reached
# through the unicode fallback. Checked before the unicode path.
KEYSYM_KEYS: dict[str, tuple[int, bool]] = {
    "Return": (KEY_ENTER, False),
    "Linefeed": (KEY_LINEFEED, False),
    "BackSpace": (KEY_BACKSPACE, False),
    "Tab": (KEY_TAB, False),
    "ISO_Left_Tab": (KEY_TAB, True),
    "Escape": (KEY_ESC, False),
    "space": (KEY_SPACE, False),
    "Delete": (KEY_DELETE, False),
    "Insert": (KEY_INSERT, False),
    "Home": (KEY_HOME, False),
    "End": (KEY_END, False),
    "Prior": (KEY_PAGEUP, False),
    "Page_Up": (KEY_PAGEUP, False),
    "Next": (KEY_PAGEDOWN, False),
    "Page_Down": (KEY_PAGEDOWN, False),
    "Left": (KEY_LEFT, False),
    "Up": (KEY_UP, False),
    "Right": (KEY_RIGHT, False),
    "Down": (KEY_DOWN, False),
    "Print": (KEY_SYSRQ, False),
    "Sys_Req": (KEY_SYSRQ, False),
    "Pause": (KEY_PAUSE, False),
    "Break": (KEY_PAUSE, False),
    "Menu": (KEY_COMPOSE, False),
    "Caps_Lock": (KEY_CAPSLOCK, False),
    "Num_Lock": (KEY_NUMLOCK, False),
    "Scroll_Lock": (KEY_SCROLLLOCK, False),
    "Shift_L": (KEY_LEFTSHIFT, False),
    "Shift_R": (KEY_RIGHTSHIFT, False),
    "Control_L": (KEY_LEFTCTRL, False),
    "Control_R": (KEY_RIGHTCTRL, False),
    "Alt_L": (KEY_LEFTALT, False),
    "Alt_R": (KEY_RIGHTALT, False),
    # X's default layouts put Meta on the Alt keys (Mod1) and Super/Hyper on
    # the "windows" keys (Mod4); evdev KEY_LEFTMETA is the windows key.
    "Meta_L": (KEY_LEFTALT, False),
    "Meta_R": (KEY_RIGHTALT, False),
    "Super_L": (KEY_LEFTMETA, False),
    "Super_R": (KEY_RIGHTMETA, False),
    "Hyper_L": (KEY_LEFTMETA, False),
    "Hyper_R": (KEY_RIGHTMETA, False),
    "ISO_Level3_Shift": (KEY_RIGHTALT, False),
    "Mode_switch": (KEY_RIGHTALT, False),
    "KP_Enter": (KEY_KPENTER, False),
    "KP_Add": (KEY_KPPLUS, False),
    "KP_Subtract": (KEY_KPMINUS, False),
    "KP_Multiply": (KEY_KPASTERISK, False),
    "KP_Divide": (KEY_KPSLASH, False),
    "KP_Decimal": (KEY_KPDOT, False),
    "KP_Separator": (KEY_KPCOMMA, False),
    "KP_Equal": (KEY_KPEQUAL, False),
    "KP_Home": (_KP[7], False),
    "KP_Up": (_KP[8], False),
    "KP_Prior": (_KP[9], False),
    "KP_Page_Up": (_KP[9], False),
    "KP_Left": (_KP[4], False),
    "KP_Begin": (_KP[5], False),
    "KP_Right": (_KP[6], False),
    "KP_End": (_KP[1], False),
    "KP_Down": (_KP[2], False),
    "KP_Next": (_KP[3], False),
    "KP_Page_Down": (_KP[3], False),
    "KP_Insert": (_KP[0], False),
    "KP_Delete": (KEY_KPDOT, False),
}
for _n in range(10):
    KEYSYM_KEYS[f"KP_{_n}"] = (_KP[_n], False)
for _n in range(1, 11):
    KEYSYM_KEYS[f"F{_n}"] = (58 + _n, False)
KEYSYM_KEYS["F11"] = (87, False)
KEYSYM_KEYS["F12"] = (88, False)
for _n in range(13, 25):
    KEYSYM_KEYS[f"F{_n}"] = (170 + _n, False)  # KEY_F13=183 .. KEY_F24=194

# XF86 multimedia keysyms -> evdev KEY_* codes, hand-curated (they have no
# unicode, so the fallback can never reach them). Codes follow XF86keysym.h's
# "Use:" annotations and xkeyboard-config's evdev keycode->keysym mapping, so
# injecting the code makes the compositor's XKB map report the same XF86
# keysym that was requested. Newer names defined as _EVDEVK(code) resolve
# automatically in _keysym_value_to_key and need no entry here.
for _name, _code in {
    "XF86AudioMute": 113,           # KEY_MUTE
    "XF86AudioLowerVolume": 114,    # KEY_VOLUMEDOWN
    "XF86AudioRaiseVolume": 115,    # KEY_VOLUMEUP
    "XF86AudioMicMute": 248,        # KEY_MICMUTE
    "XF86AudioPlay": 164,           # KEY_PLAYPAUSE
    "XF86AudioPause": 201,          # KEY_PAUSECD
    "XF86AudioNext": 163,           # KEY_NEXTSONG
    "XF86AudioPrev": 165,           # KEY_PREVIOUSSONG
    "XF86AudioStop": 166,           # KEY_STOPCD
    "XF86AudioRecord": 167,         # KEY_RECORD
    "XF86AudioRewind": 168,         # KEY_REWIND
    "XF86AudioForward": 208,        # KEY_FASTFORWARD
    "XF86AudioMedia": 226,          # KEY_MEDIA
    "XF86MonBrightnessUp": 225,     # KEY_BRIGHTNESSUP
    "XF86MonBrightnessDown": 224,   # KEY_BRIGHTNESSDOWN
    "XF86MonBrightnessCycle": 243,  # KEY_BRIGHTNESS_CYCLE
    "XF86KbdBrightnessUp": 230,     # KEY_KBDILLUMUP
    "XF86KbdBrightnessDown": 229,   # KEY_KBDILLUMDOWN
    "XF86KbdLightOnOff": 228,       # KEY_KBDILLUMTOGGLE
    "XF86Back": 158,                # KEY_BACK
    "XF86Forward": 159,             # KEY_FORWARD
    "XF86Refresh": 173,             # KEY_REFRESH
    "XF86Reload": 173,              # KEY_REFRESH
    "XF86Stop": 128,                # KEY_STOP
    "XF86Search": 217,              # KEY_SEARCH
    "XF86HomePage": 172,            # KEY_HOMEPAGE
    "XF86WWW": 150,                 # KEY_WWW
    "XF86Mail": 155,                # KEY_MAIL
    "XF86Calculator": 140,          # KEY_CALC
    "XF86Explorer": 144,            # KEY_FILE
    "XF86Tools": 171,               # KEY_CONFIG
    "XF86Favorites": 156,           # KEY_BOOKMARKS
    "XF86MyComputer": 157,          # KEY_COMPUTER
    "XF86PowerOff": 116,            # KEY_POWER
    "XF86Sleep": 142,               # KEY_SLEEP
    "XF86Suspend": 205,             # KEY_SUSPEND
    "XF86WakeUp": 143,              # KEY_WAKEUP
    "XF86ScreenSaver": 152,         # KEY_COFFEE / KEY_SCREENLOCK
    "XF86Display": 227,             # KEY_SWITCHVIDEOMODE
    "XF86Eject": 162,               # KEY_EJECTCLOSECD
    "XF86Phone": 169,               # KEY_PHONE
    "XF86ScrollUp": 177,            # KEY_SCROLLUP
    "XF86ScrollDown": 178,          # KEY_SCROLLDOWN
    "XF86New": 181,                 # KEY_NEW
    "XF86Close": 206,               # KEY_CLOSE
    "XF86Save": 234,                # KEY_SAVE
    "XF86Documents": 235,           # KEY_DOCUMENTS
    "XF86Send": 231,                # KEY_SEND
    "XF86Reply": 232,               # KEY_REPLY
    "XF86MailForward": 233,         # KEY_FORWARDMAIL
    "XF86Messenger": 216,           # KEY_CHAT
    "XF86WebCam": 212,              # KEY_CAMERA
    "XF86Finance": 219,             # KEY_FINANCE
    "XF86Shop": 221,                # KEY_SHOP
    "XF86Battery": 236,             # KEY_BATTERY
    "XF86Bluetooth": 237,           # KEY_BLUETOOTH
    "XF86WLAN": 238,                # KEY_WLAN
    "XF86RFKill": 247,              # KEY_RFKILL
    "XF86Copy": 133,                # KEY_COPY
    "XF86Cut": 137,                 # KEY_CUT
    "XF86Paste": 135,               # KEY_PASTE
    "XF86Open": 134,                # KEY_OPEN
    "XF86Launch1": 148,             # KEY_PROG1
    "XF86Launch2": 149,             # KEY_PROG2
    "XF86Launch3": 202,             # KEY_PROG3
    "XF86Launch4": 203,             # KEY_PROG4
    "XF86LaunchB": 204,             # KEY_DASHBOARD
}.items():
    KEYSYM_KEYS[_name] = (_code, False)

# Case-insensitive aliases, from xdotool's symbol_map plus win/lock.
ALIASES = {
    "alt": "Alt_L",
    "ctrl": "Control_L",
    "control": "Control_L",
    "meta": "Meta_L",
    "super": "Super_L",
    "win": "Super_L",
    "shift": "Shift_L",
    "lock": "Caps_Lock",
    "enter": "Return",
    "return": "Return",
}

# The 8 modifier keys released by --clearmodifiers.
MODIFIER_KEYCODES = [
    KEY_LEFTSHIFT, KEY_RIGHTSHIFT, KEY_LEFTCTRL, KEY_RIGHTCTRL,
    KEY_LEFTALT, KEY_RIGHTALT, KEY_LEFTMETA, KEY_RIGHTMETA,
]

# Characters xdo rejects anywhere in a keysequence.
_BAD_SEQ_CHARS = set(" \t\n.-[]{}\\|")


def char_to_key(ch: str) -> tuple[int, bool] | None:
    return CHAR_TO_KEY.get(ch)


_EVDEVK_BASE = 0x10081000  # XF86keysym.h: _EVDEVK(v) == 0x10081000 + evdev code


def _keysym_value_to_key(ks: int) -> tuple[int, bool] | None:
    """Resolve a raw keysym number to (keycode, shifted) via unicode (or, for
    _EVDEVK-range XF86 keysyms, the evdev code embedded in the keysym)."""
    if 0 < ks - _EVDEVK_BASE < 0x1000:
        code = ks - _EVDEVK_BASE
        return (code, False) if code < 256 else None
    cp = KEYSYM_TO_UNICODE.get(ks)
    if cp is None:
        if 0x20 <= ks <= 0xFF:
            cp = ks  # latin-1 keysyms are their own codepoint
        elif ks & 0xFF000000 == 0x01000000:
            cp = ks & 0xFFFFFF
        else:
            return None
    return CHAR_TO_KEY.get(chr(cp))


def keysym_to_key(name: str) -> tuple[int, bool] | None:
    """Resolve one keysym name (no aliases) to (keycode, shifted)."""
    hit = KEYSYM_KEYS.get(name)
    if hit:
        return hit
    ks = NAME_TO_KEYSYM.get(name)
    if ks is None:
        return None
    return _keysym_value_to_key(ks)


def resolve_token(tok: str) -> tuple[int, bool] | None | str:
    """Resolve one '+'-separated keysequence token.

    Returns (keycode, shifted), or a warning string (token skipped), matching
    xdotool: unknown names warn and are ignored; digits are X keycodes.
    """
    name = ALIASES.get(tok.lower(), tok)
    if name in KEYSYM_KEYS or name in NAME_TO_KEYSYM:
        hit = keysym_to_key(name)
        if hit is None:
            return f"key '{tok}' is not reachable on the US layout. Ignoring it."
        return hit
    if len(tok) > 2 and tok[0] == "0" and tok[1] in "xX":
        # XStringToKeysym parity: "0x<hex>" is a raw keysym number.
        try:
            ks = int(tok, 16)
        except ValueError:
            ks = None
        if ks is not None:
            hit = _keysym_value_to_key(ks)
            if hit is None:
                return f"key '{tok}' is not reachable on the US layout. Ignoring it."
            return hit
    if tok[:1].isdigit():
        # Explicit numeric X keycode; evdev keycode = X keycode - 8.
        n = 0
        for c in tok:
            if not c.isdigit():
                break
            n = n * 10 + int(c)
        code = n - 8
        if 0 < code < 256:
            return (code, False)
        return f"key '{tok}' is not reachable on the US layout. Ignoring it."
    return f"(symbol) No such key name '{tok}'. Ignoring it."


def parse_keyseq(spec: str) -> tuple[list[tuple[int, bool]], list[str]]:
    """Parse 'ctrl+shift+t' into resolved keys + warnings.

    Raises ValueError for sequences xdo rejects outright (bad characters).
    """
    if _BAD_SEQ_CHARS & set(spec):
        raise ValueError(f"Error: Invalid key sequence '{spec}'")
    keys = []
    warnings = []
    for tok in spec.split("+"):
        if not tok:
            continue
        hit = resolve_token(tok)
        if isinstance(hit, str):
            warnings.append(hit)
        elif hit is not None:
            keys.append(hit)
    return keys, warnings
