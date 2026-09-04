"""The reverse keymap (B13), over keymaps recorded from real compositors.

Every fixture in tests/fixtures/keymaps is a byte-for-byte copy of what a
compositor handed a client on `wl_keyboard.keymap` (see the README there).
The assertions are keycodes and modifier masks: what wdotool would actually
inject.

The last two classes are the safety requirement: on a plain US layout the
reverse map is not merely unused, it is never built -- proven by making
every entry point into the machinery raise.
"""

import contextlib
import io
import os
import re
import unittest

from wdotool import cli, daemon, xkbmap

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["FUCKWAYLAND_PASSTHROUGH"] = "never"

KEYMAPS = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "keymaps")
FIXTURES = ("us", "de", "fr", "es", "gb", "dvorak", "us_de", "de_fr",
            "noble_de", "sway_de", "us_swapescape", "us_grptoggle")


def text(name: str) -> str:
    with open(os.path.join(KEYMAPS, name + ".xkb"), encoding="utf-8") as f:
        return f.read()


def rmap(name: str, group: int = 1) -> xkbmap.ReverseMap:
    return xkbmap.build(text(name), group)


@contextlib.contextmanager
def env(**kw):
    """Set WDOTOOL_* variables for one block, restoring what was there."""
    old = {k: os.environ.get(k) for k in kw}
    try:
        for k, v in kw.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class RecorderDev:
    def __init__(self):
        self.events = []

    def emit(self, etype, code, value):
        self.events.append((etype, code, value))

    def syn(self):
        self.events.append(("SYN",))

    def key(self, code, down):
        self.events.append(("KEY", code, 1 if down else 0))

    def close(self):
        pass


def make_daemon():
    d = daemon._Daemon()
    d.kb, d.mouse, d.tablet = RecorderDev(), RecorderDev(), RecorderDev()
    d.dev_error = None
    d.geom = (0, 0, 1920, 1080)
    d._rel_abs = False
    return d


def taps(dev) -> list:
    """The (code, down) events, so a test reads like a keystroke list."""
    return [(e[1], e[2]) for e in dev.events if e[0] == "KEY"]


# ---------------------------------------------------------------------------


class TestParse(unittest.TestCase):
    def test_every_fixture_parses(self):
        for name in FIXTURES:
            km = xkbmap.parse(text(name))
            self.assertGreater(len(km.keycodes), 200, name)
            self.assertGreater(len(km.types), 5, name)
            self.assertGreaterEqual(len(km.groups), 1, name)

    def test_keycodes_are_evdev(self):
        km = xkbmap.parse(text("us"))
        # X keycode 24 (<AD01>) is evdev 16, the `q` key of a US board
        self.assertEqual(km.keycodes["AD01"], 16)
        self.assertEqual(km.keycodes["ESC"], 1)
        self.assertEqual(km.keycodes["SPCE"], 57)
        self.assertEqual(km.keycodes["RTRN"], 28)
        self.assertEqual(km.keycodes["RALT"], 100)

    def test_aliases_resolve(self):
        km = xkbmap.parse(text("de"))
        self.assertEqual(km.keycodes["ALGR"], km.keycodes["RALT"])

    def test_the_two_keysym_dialects_agree(self):
        """GNOME 50 writes every keysym as a hex number, GNOME 46 writes
        names ("symbols[Group1]= [ q, Q, at, ... ]"), and sway hands out a
        keymap with a single group. Same layout, same answers."""
        de, noble, sway = rmap("de"), rmap("noble_de"), rmap("sway_de")
        for ch in "zyäöüß@€é|":
            self.assertEqual(de.lookup_char(ch), noble.lookup_char(ch), ch)
            self.assertEqual(de.lookup_char(ch), sway.lookup_char(ch), ch)

    def test_a_single_group_keymap(self):
        """sway compiles exactly the configured layout: one group, and
        nothing to guess about which one is active."""
        self.assertEqual(xkbmap.group_count(text("sway_de")), 1)
        self.assertEqual(xkbmap.choose_group(text("sway_de")), (1, True))
        self.assertEqual(rmap("sway_de").name, "German")

    def test_group_names(self):
        self.assertEqual(xkbmap.parse(text("de")).group_names,
                         ["German", "English (US)"])
        self.assertEqual(xkbmap.parse(text("us_de")).group_names,
                         ["English (US)", "German", "English (US)"])
        self.assertEqual(xkbmap.parse(text("de_fr")).group_names,
                         ["German", "French", "English (US)"])

    def test_group_count_is_cheap_and_agrees(self):
        for name in FIXTURES:
            self.assertEqual(xkbmap.group_count(text(name)),
                             len(xkbmap.parse(text(name)).groups), name)

    def test_types_carry_level_masks(self):
        km = xkbmap.parse(text("de"))
        four = km.types["FOUR_LEVEL"]
        self.assertEqual(four[1], 0)
        self.assertEqual(four[2], xkbmap.MOD_SHIFT)
        self.assertEqual(four[3], xkbmap.MOD_LEVEL3)
        self.assertEqual(four[4], xkbmap.MOD_SHIFT | xkbmap.MOD_LEVEL3)
        # ALPHABETIC reaches level 2 with Shift or with Lock; Shift is the one
        # we can press, and it is the cheaper of the two anyway.
        self.assertEqual(km.types["ALPHABETIC"][2], xkbmap.MOD_SHIFT)

    def test_type_with_unpressable_modifier_is_dropped(self):
        km = xkbmap.parse(text("de"))
        # PC_CONTROL_LEVEL2's level 2 needs Control held: not a typing route.
        self.assertNotIn(2, km.types["PC_CONTROL_LEVEL2"])

    def test_group_wrapping(self):
        """A key that binds only group 1 still exists in group 2 (groupsWrap):
        <SPCE> and <RTRN> are never repeated per layout."""
        km = xkbmap.parse(text("us_de"))
        self.assertNotIn("SPCE", km.groups[1].syms)      # not written for group 2
        self.assertIn("SPCE", km.resolved(2))            # but present all the same
        self.assertEqual(km.resolved(2)["SPCE"], km.resolved(1)["SPCE"])
        # ... while a key the German group does rebind differs
        self.assertNotEqual(km.resolved(2)["AD06"], km.resolved(1)["AD06"])

    def test_rejects_rubbish(self):
        for bad in ("", "hello", "xkb_keymap { }", "xkb_symbols {"):
            with self.assertRaises(xkbmap.XkbError):
                xkbmap.parse(bad)


class TestKeysymTokens(unittest.TestCase):
    def test_values(self):
        self.assertEqual(xkbmap.keysym_value("0x61"), 0x61)
        self.assertEqual(xkbmap.keysym_value(" a "), 0x61)
        self.assertEqual(xkbmap.keysym_value("dead_acute"), 0xFE51)
        self.assertEqual(xkbmap.keysym_value("U0161"), 0x1000161)
        self.assertEqual(xkbmap.keysym_value("U00E4"), 0xE4)
        self.assertIsNone(xkbmap.keysym_value("NoSymbol"))
        self.assertIsNone(xkbmap.keysym_value(""))
        self.assertIsNone(xkbmap.keysym_value("not_a_keysym"))

    def test_chars(self):
        self.assertEqual(xkbmap.keysym_char(0x61), "a")
        self.assertEqual(xkbmap.keysym_char(0xE4), "ä")
        self.assertEqual(xkbmap.keysym_char(0x20AC), "€")   # XK_EuroSign
        self.assertEqual(xkbmap.keysym_char(0x1002032), "′")  # unicode keysym
        self.assertIsNone(xkbmap.keysym_char(0xFE51))       # dead_acute types nothing
        self.assertIsNone(xkbmap.keysym_char(0xFF0D))       # Return is a key
        self.assertIsNone(xkbmap.keysym_char(None))


class TestReverseMap(unittest.TestCase):
    """Recorded layout in, keycode + modifier mask out."""

    def check(self, name, group, expected):
        r = rmap(name, group)
        for ch, want in expected.items():
            self.assertEqual(r.lookup_char(ch), want,
                             f"{name} group {group}: {ch!r}")

    def test_us(self):
        S = xkbmap.MOD_SHIFT
        self.check("us", 1, {
            "a": [(30, 0)], "A": [(30, S)], "z": [(44, 0)], "1": [(2, 0)],
            "!": [(2, S)], "@": [(3, S)], " ": [(57, 0)], "\n": [(28, 0)],
            "\t": [(15, 0)], "\x1b": [(1, 0)], "\b": [(14, 0)],
        })

    def test_german_is_qwertz(self):
        S, L3 = xkbmap.MOD_SHIFT, xkbmap.MOD_LEVEL3
        self.check("de", 1, {
            "z": [(21, 0)], "y": [(44, 0)], "Z": [(21, S)],
            "ä": [(40, 0)], "Ä": [(40, S)], "ö": [(39, 0)], "ü": [(26, 0)],
            "ß": [(12, 0)], "@": [(16, L3)], "€": [(18, L3)],
            "µ": [(50, L3)], "|": [(86, L3)],
        })

    def test_french_is_azerty(self):
        L3 = xkbmap.MOD_LEVEL3
        self.check("fr", 1, {
            "a": [(16, 0)], "q": [(30, 0)], "z": [(17, 0)], "w": [(44, 0)],
            "m": [(39, 0)], "é": [(3, 0)], "è": [(8, 0)], "ç": [(10, 0)],
            "à": [(11, 0)], "ù": [(40, 0)], "@": [(11, L3)], "€": [(18, L3)],
        })

    def test_spanish(self):
        L3 = xkbmap.MOD_LEVEL3
        self.check("es", 1, {"ñ": [(39, 0)], "ç": [(43, 0)], "¡": [(13, 0)],
                             "@": [(3, L3)], "€": [(18, L3)]})

    def test_uk(self):
        S, L3 = xkbmap.MOD_SHIFT, xkbmap.MOD_LEVEL3
        self.check("gb", 1, {"#": [(43, 0)], "£": [(4, S)], "@": [(40, S)],
                             "\\": [(86, 0)], "|": [(86, S)], "€": [(5, L3)]})

    def test_dvorak(self):
        self.check("dvorak", 1, {
            "a": [(30, 0)], "q": [(45, 0)], "z": [(53, 0)], "x": [(48, 0)],
            "p": [(19, 0)], "e": [(32, 0)], ",": [(17, 0)], ".": [(18, 0)],
            "'": [(16, 0)],
        })

    def test_level_shift_keys_come_from_the_layout(self):
        # AltGr is <RALT> where the layout puts ISO_Level3_Shift on it ...
        self.assertEqual(rmap("de").mod_keys[xkbmap.MOD_LEVEL3], 100)
        self.assertEqual(rmap("fr").mod_keys[xkbmap.MOD_LEVEL3], 100)
        # ... and the synthetic <LVL3> keycode where it does not (plain US
        # and Dvorak leave <RALT> as Alt_R).
        self.assertEqual(rmap("dvorak").mod_keys[xkbmap.MOD_LEVEL3], 84)
        for name in FIXTURES:
            self.assertEqual(rmap(name).mod_keys[xkbmap.MOD_SHIFT], 42, name)

    def test_keypad_never_wins_a_character(self):
        # KP_1 types "1" (with NumLock); the number row must win anyway.
        for name in ("us", "de", "fr"):
            self.assertEqual(rmap(name).lookup_char("1")[0][0],
                             2 if name != "fr" else 2)

    def test_unreachable_characters(self):
        for name in ("us", "de", "fr", "dvorak"):
            r = rmap(name)
            self.assertIsNone(r.lookup_char("漢"), name)
            self.assertIsNone(r.lookup_char("́"), name)  # a bare combiner
        self.assertIsNone(rmap("us").lookup_char("€"))
        self.assertIsNone(rmap("dvorak").lookup_char("ß"))

    def test_keysym_entry_for_key_sequences(self):
        de = rmap("de")
        self.assertEqual(de.keysym_entry("z"), (21, 0))
        self.assertEqual(de.keysym_entry("at"), (16, xkbmap.MOD_LEVEL3))
        self.assertEqual(rmap("gb").keysym_entry("sterling"), (4, xkbmap.MOD_SHIFT))
        self.assertIsNone(de.keysym_entry("nosuchkeysym"))

    def test_modifier_keycodes(self):
        de = rmap("de")
        self.assertEqual(de.modifier_keycodes(0), [])
        self.assertEqual(de.modifier_keycodes(xkbmap.MOD_SHIFT), [42])
        self.assertEqual(de.modifier_keycodes(xkbmap.MOD_LEVEL3), [100])
        self.assertEqual(
            de.modifier_keycodes(xkbmap.MOD_SHIFT | xkbmap.MOD_LEVEL3), [42, 100])


class TestDeadKeys(unittest.TestCase):
    def test_german_acute(self):
        # <AE12> is dead_acute on a German board: é is two keystrokes.
        self.assertEqual(rmap("de").lookup_char("é"), [(13, 0), (18, 0)])
        self.assertEqual(rmap("de").lookup_char("è"), [(13, xkbmap.MOD_SHIFT),
                                                       (18, 0)])

    def test_french_circumflex(self):
        fr = rmap("fr")
        self.assertEqual(fr.lookup_char("ô"), [(26, 0), (24, 0)])
        self.assertEqual(fr.lookup_char("î"), [(26, 0), (23, 0)])
        # é and è are single keys on French, not dead-key sequences
        self.assertEqual(len(fr.lookup_char("é")), 1)

    def test_spanish_and_uk(self):
        es = rmap("es")
        self.assertEqual(es.lookup_char("á"), [(40, 0), (30, 0)])
        self.assertEqual(es.lookup_char("ü"), [(40, xkbmap.MOD_SHIFT), (22, 0)])
        self.assertEqual(rmap("gb").lookup_char("é"),
                         [(39, xkbmap.MOD_LEVEL3), (18, 0)])

    def test_bare_accent_is_dead_key_then_space(self):
        # <dead_grave> <space> is "`" in the Compose table, so this one is
        # still a space; the accents Compose spells differently are in
        # TestDeadKeyPlusSpace below (B3).
        de = rmap("de")
        self.assertEqual(de.lookup_char("`"), [(13, xkbmap.MOD_SHIFT), (57, 0)])
        self.assertEqual(de.lookup_char("^"), [(41, 0), (57, 0)])

    def test_dead_key_is_not_a_character(self):
        # dead_acute must never be handed out as a way to type something
        for name in ("de", "fr", "es", "gb"):
            r = rmap(name)
            for entry in r.chars.values():
                self.assertNotIn(entry, list(r.dead.values()) or [None],
                                 f"{name}: a dead key leaked into chars")

    def test_only_sequences_that_recompose(self):
        de = rmap("de")
        # ẃ decomposes to w + acute, and NFC puts it back: allowed.
        self.assertEqual(de.lookup_char("ẃ"), [(13, 0), (17, 0)])
        # A three-codepoint decomposition (ế) is not a two-keystroke sequence.
        self.assertIsNone(de.lookup_char("ế"))
        # The case the name is actually about, which neither line above
        # reaches: two codepoints, both typable, that do NOT recompose. Both
        # assertions above are satisfied by the `len(decomposed) != 2` check
        # one line earlier, so deleting the NFC test left the suite green.
        # U+0958 is a composition exclusion: NFD is U+0915 U+093C and NFC
        # leaves the pair as it is, so typing the two would produce a
        # different string from the one asked for.
        de.chars["\u0915"] = (99, 0)
        de.dead[0x93C] = (98, 0)
        self.assertIsNone(de.lookup_char("\u0958"))
        # the control: both entries really are reachable, so the None above
        # comes from the NFC test and from nothing else
        self.assertEqual(de.lookup_char("\u0915\u093c"), [(98, 0), (99, 0)])


class TestALevelWeCannotPress(unittest.TestCase):
    """A layout with four-level types whose level-3 key has been removed
    (lv3:none, a custom keymap): the levels behind AltGr do not exist for us
    and must not be offered. The guard used to test the backfilled fallback
    table rather than what the keymap said, so it could never fire for the
    case its own comment names."""

    @staticmethod
    def _no_level3():
        src = text("de")
        src = re.sub(r"0xfe03|0xff7e", "0xffea", src)      # -> Alt_R
        return src.replace("ISO_Level3_Shift", "Alt_R").replace(
            "Mode_switch", "Alt_R")

    def test_level_3_characters_are_dropped_not_mistyped(self):
        r = xkbmap.reverse(xkbmap.parse(self._no_level3()))
        offered = [c for c, e in r.chars.items() if e[1] & xkbmap.MOD_LEVEL3]
        self.assertEqual(offered, [], "AltGr levels on a layout with no AltGr")
        # '@' is AltGr+q on a German layout: with no level-3 key it is simply
        # not typable, which the caller reports rather than pressing Alt_R.
        self.assertIsNone(r.lookup_char("@"))

    def test_the_real_layout_is_untouched(self):
        r = rmap("de")
        self.assertEqual(r.lookup_char("@"), [(16, xkbmap.MOD_LEVEL3)])
        self.assertEqual(r.modifier_keycodes(xkbmap.MOD_LEVEL3), [100])


class TestGroups(unittest.TestCase):
    def test_second_group_is_a_different_layout(self):
        us_de = text("us_de")
        self.assertEqual(xkbmap.build(us_de, 1).lookup_char("z"), [(44, 0)])
        self.assertEqual(xkbmap.build(us_de, 2).lookup_char("z"), [(21, 0)])
        self.assertEqual(xkbmap.build(us_de, 2).name, "German")
        self.assertEqual(xkbmap.build(us_de, 3).name, "English (US)")

    def test_three_groups(self):
        de_fr = text("de_fr")
        self.assertEqual(xkbmap.build(de_fr, 1).lookup_char("a"), [(30, 0)])
        self.assertEqual(xkbmap.build(de_fr, 2).lookup_char("a"), [(16, 0)])

    def test_wrapped_keys_are_in_every_group(self):
        for g in (1, 2, 3):
            r = xkbmap.build(text("us_de"), g)
            self.assertEqual(r.lookup_char(" "), [(57, 0)], g)
            self.assertEqual(r.lookup_char("\n"), [(28, 0)], g)

    def test_no_such_group(self):
        with self.assertRaises(xkbmap.XkbError):
            xkbmap.build(text("de"), 5)

    def test_choose_group_when_all_groups_are_the_same(self):
        # GNOME compiles a single `us` source as the two groups "us,us":
        # which one is active cannot matter, so the answer is known.
        self.assertEqual(xkbmap.choose_group(text("us")), (1, True))

    def test_choosing_a_group_never_needs_the_parser(self):
        """It runs on a US layout, where the parser must not."""
        real = xkbmap.parse
        self.addCleanup(setattr, xkbmap, "parse", real)
        xkbmap.parse = lambda *a, **k: self.fail("parse() was reached")
        for name in FIXTURES:
            xkbmap.choose_group(text(name))

    def test_choose_group_when_they_differ(self):
        # ... but "de,us" (a single German source plus GNOME's appended
        # fallback) is a guess: group 1, flagged as assumed.
        for name in ("de", "fr", "es", "gb", "dvorak", "us_de", "de_fr"):
            self.assertEqual(xkbmap.choose_group(text(name)), (1, False), name)

    def test_modifiers_event_wins(self):
        self.assertEqual(xkbmap.choose_group(text("us_de"), 2), (2, True))


class TestUsBypassDetection(unittest.TestCase):
    """`active_group_is_plain_us` decides whether any of this runs at all."""

    def test_true_only_for_a_us_group(self):
        cases = {
            ("us", 1): True, ("us", 2): True,
            ("de", 1): False, ("de", 2): True,       # 2 is GNOME's fallback
            ("fr", 1): False, ("fr", 2): True,
            ("es", 1): False, ("gb", 1): False,
            ("dvorak", 1): False, ("dvorak", 2): True,
            ("us_de", 1): True, ("us_de", 2): False, ("us_de", 3): True,
            ("de_fr", 1): False, ("de_fr", 2): False, ("de_fr", 3): True,
            ("noble_de", 1): False, ("noble_de", 2): True,
            ("sway_de", 1): False,
        }
        for (name, group), want in cases.items():
            self.assertEqual(xkbmap.active_group_is_plain_us(text(name), group),
                             want, f"{name} group {group}")

    def test_one_changed_key_is_enough_to_refuse(self):
        """The name is not what is trusted: the keysyms are. A keymap still
        called "English (US)" whose Q key is not q must not be bypassed."""
        us = text("us")
        self.assertTrue(xkbmap.active_group_is_plain_us(us, 1))
        tampered = us.replace("""	key <AD01> {
		symbols[1]= [ 0x71, 0x51 ],""", """	key <AD01> {
		symbols[1]= [ 0x27, 0x22 ],""", 1)
        self.assertNotEqual(tampered, us)
        self.assertFalse(xkbmap.active_group_is_plain_us(tampered, 1))

    def test_a_missing_key_is_enough_to_refuse(self):
        us = text("us")
        gone = us.replace("	key <SPCE> {	[ 0x20 ] };\n", "", 1)
        self.assertNotEqual(gone, us)
        self.assertFalse(xkbmap.active_group_is_plain_us(gone, 1))

    def test_a_dead_key_on_level_one_is_enough_to_refuse(self):
        # us-intl's apostrophe key: same layout name, dead_acute on level 1.
        us = text("us")
        intl = us.replace("symbols[1]= [ 0x27, 0x22 ]",
                          "symbols[1]= [ 0xfe51, 0xfe57 ]", 1)
        self.assertNotEqual(intl, us)
        self.assertFalse(xkbmap.active_group_is_plain_us(intl, 1))

    def test_a_type_whose_level_two_is_not_shift_is_enough_to_refuse(self):
        us = text("us")
        odd = us.replace("""	key <AD01> {
		symbols[1]= [ 0x71, 0x51 ],""", """	key <AD01> {
		type= "PC_ALT_LEVEL2",
		symbols[1]= [ 0x71, 0x51 ],""", 1)
        self.assertNotEqual(odd, us)
        self.assertFalse(xkbmap.active_group_is_plain_us(odd, 1))

    def test_a_renamed_group_is_enough_to_refuse(self):
        us = text("us").replace('name[1]="English (US)"',
                                'name[1]="English (US, intl.)"', 1)
        self.assertFalse(xkbmap.active_group_is_plain_us(us, 1))

    def test_never_raises(self):
        for junk in ("", "{{{{", "xkb_keymap {", "\x00\xff" * 100,
                     text("us")[:5000]):
            self.assertFalse(xkbmap.active_group_is_plain_us(junk, 1))
        self.assertFalse(xkbmap.active_group_is_plain_us(text("us"), 99))


class TestFetchAndSnapshot(unittest.TestCase):
    def test_file_override(self):
        path = os.path.join(KEYMAPS, "de.xkb")
        with env(WDOTOOL_XKB_KEYMAP=path, WDOTOOL_XKB_GROUP=None):
            snap = xkbmap.fetch()
        self.assertEqual(snap.group, 1)
        self.assertFalse(snap.group_known)   # de,us: group 1 is an assumption
        self.assertIn("de.xkb", snap.source)
        self.assertTrue(snap.text.startswith("xkb_keymap"))

    def test_group_override(self):
        path = os.path.join(KEYMAPS, "us_de.xkb")
        with env(WDOTOOL_XKB_KEYMAP=path, WDOTOOL_XKB_GROUP="2"):
            snap = xkbmap.fetch()
        self.assertEqual((snap.group, snap.group_known), (2, True))
        self.assertEqual(xkbmap.build(snap.text, snap.group).name, "German")

    def test_bad_group_override_is_ignored(self):
        path = os.path.join(KEYMAPS, "us_de.xkb")
        for bad in ("0", "9", "two", ""):
            with env(WDOTOOL_XKB_KEYMAP=path, WDOTOOL_XKB_GROUP=bad):
                self.assertEqual(xkbmap.fetch().group, 1)

    def test_missing_file(self):
        with env(WDOTOOL_XKB_KEYMAP="/nonexistent/keymap.xkb"):
            with self.assertRaises(xkbmap.XkbError):
                xkbmap.fetch()

    def test_no_compositor(self):
        with env(WDOTOOL_XKB_KEYMAP=None, WAYLAND_DISPLAY=None,
                 XDG_RUNTIME_DIR="/nonexistent"):
            with self.assertRaises(xkbmap.XkbError):
                xkbmap.fetch()


# ---------------------------------------------------------------------------
# the daemon's typing path


class TestTypingThroughTheLayout(unittest.TestCase):
    def daemon_for(self, name, group=None, mode=None):
        d = make_daemon()
        self._env = env(WDOTOOL_XKB_KEYMAP=os.path.join(KEYMAPS, name + ".xkb"),
                        WDOTOOL_XKB_GROUP=group, WDOTOOL_LAYOUT=mode)
        self._env.__enter__()
        self.addCleanup(self._env.__exit__, None, None, None)
        return d

    def test_german_ascii_is_remapped(self):
        d = self.daemon_for("de")
        warns = d.op_type("yz", 0, False)
        self.assertEqual(taps(d.kb), [(44, 1), (44, 0), (21, 1), (21, 0)])
        # the one-off "which group?" notice, and nothing else
        self.assertEqual(len(warns), 1, warns)
        self.assertIn("German", warns[0])

    def test_german_umlaut_and_altgr(self):
        d = self.daemon_for("de")
        d.op_type("äÄ€", 0, False)
        self.assertEqual(taps(d.kb), [
            (40, 1), (40, 0),                             # ä
            (42, 1), (40, 1), (40, 0), (42, 0),           # shift+ä
            (100, 1), (18, 1), (18, 0), (100, 0),         # AltGr+e
        ])

    def test_dead_key_sequence_is_two_keystrokes(self):
        d = self.daemon_for("fr")
        d.op_type("ô", 0, False)
        self.assertEqual(taps(d.kb),
                         [(26, 1), (26, 0), (24, 1), (24, 0)])

    def test_unreachable_character_warns_and_the_rest_still_types(self):
        d = self.daemon_for("de")
        warns = d.op_type("a漢b", 0, False)
        self.assertEqual(taps(d.kb), [(30, 1), (30, 0), (48, 1), (48, 0)])
        self.assertEqual([w for w in warns if "漢" in w],
                         ["Can't type character '漢' (not on the German layout)."
                          " Skipping."])

    def test_key_sequence_uses_the_layout(self):
        d = self.daemon_for("de")
        d.op_key("ctrl+z", "press", 0, False)
        # xdo_send_keysequence_window releases in press order, not reversed
        self.assertEqual(taps(d.kb),
                         [(29, 1), (21, 1), (29, 0), (21, 0)])

    def test_key_sequence_keeps_position_keys_fixed(self):
        d = self.daemon_for("fr")
        d.op_key("ctrl+Return", "press", 0, False)
        self.assertEqual(taps(d.kb), [(29, 1), (28, 1), (29, 0), (28, 0)])

    def test_key_sequence_needing_altgr(self):
        d = self.daemon_for("de")
        d.op_key("at", "press", 0, False)
        self.assertEqual(taps(d.kb), [(100, 1), (16, 1), (100, 0), (16, 0)])

    def test_unreachable_key_names_the_layout(self):
        d = self.daemon_for("dvorak")
        warns = d.op_key("ssharp", "down", 0, False)
        self.assertEqual(taps(d.kb), [])
        self.assertIn("not reachable on the English (Dvorak) layout", warns[-1])

    def test_the_layout_notice_is_not_doubled_by_a_key_press(self):
        """`key` prints xdo's own diagnostics twice (B12); ours is not one
        of them."""
        d = self.daemon_for("de")
        warns = d.op_key("bogus", "press", 0, False)
        self.assertEqual(len([w for w in warns if "assuming" in w]), 1)
        self.assertEqual(len([w for w in warns if "No such key name" in w]), 2)

    def test_group_two_of_a_two_layout_session(self):
        d = self.daemon_for("us_de", group="2")
        warns = d.op_type("z", 0, False)
        self.assertEqual(taps(d.kb), [(21, 1), (21, 0)])
        self.assertEqual(warns, [])  # the group was pinned: nothing to warn about

    def test_group_one_of_the_same_session_is_bypassed(self):
        d = self.daemon_for("us_de", group="1")
        self.assertEqual(taps(d.kb), [])
        d.op_type("z", 0, False)
        self.assertEqual(taps(d.kb), [(44, 1), (44, 0)])  # US: z is <AB01>
        self.assertIsNone(d._layout_cache[1])             # the bypass took it


class TestCacheAndInvalidation(unittest.TestCase):
    def test_same_keymap_is_reused(self):
        with env(WDOTOOL_XKB_KEYMAP=os.path.join(KEYMAPS, "de.xkb"),
                 WDOTOOL_LAYOUT=None, WDOTOOL_XKB_GROUP=None):
            d = make_daemon()
            first = d._layout()
            self.assertIs(d._layout(), first)
            self.assertIsNotNone(first)

    def test_a_new_group_rebuilds(self):
        """The user switched layout without changing the keymap: same digest,
        different group, so the cached map must not be handed back."""
        path = os.path.join(KEYMAPS, "de_fr.xkb")
        d = make_daemon()
        with env(WDOTOOL_XKB_KEYMAP=path, WDOTOOL_XKB_GROUP="1",
                 WDOTOOL_LAYOUT=None):
            self.assertEqual(d._layout().name, "German")
        with env(WDOTOOL_XKB_KEYMAP=path, WDOTOOL_XKB_GROUP="2",
                 WDOTOOL_LAYOUT=None):
            self.assertEqual(d._layout().name, "French")
        # ... and the third group is plain US, so it bypasses instead
        with env(WDOTOOL_XKB_KEYMAP=path, WDOTOOL_XKB_GROUP="3",
                 WDOTOOL_LAYOUT=None):
            self.assertIsNone(d._layout())

    def test_a_new_keymap_rebuilds(self):
        d = make_daemon()
        with env(WDOTOOL_XKB_KEYMAP=os.path.join(KEYMAPS, "de.xkb"),
                 WDOTOOL_LAYOUT=None, WDOTOOL_XKB_GROUP=None):
            self.assertEqual(d._layout().name, "German")
        with env(WDOTOOL_XKB_KEYMAP=os.path.join(KEYMAPS, "fr.xkb"),
                 WDOTOOL_LAYOUT=None, WDOTOOL_XKB_GROUP=None):
            self.assertEqual(d._layout().name, "French")


class TestFallbacks(unittest.TestCase):
    """Every failure lands on the fixed US table, with a warning, never a
    traceback."""

    def type_hello(self, **kw):
        d = make_daemon()
        with env(**kw):
            warns = d.op_type("aA", 0, False)
        return d, warns

    def test_no_compositor(self):
        d, warns = self.type_hello(WDOTOOL_XKB_KEYMAP=None, WAYLAND_DISPLAY=None,
                                   XDG_RUNTIME_DIR="/nonexistent",
                                   WDOTOOL_LAYOUT=None)
        self.assertEqual(taps(d.kb),
                         [(30, 1), (30, 0), (42, 1), (30, 1), (30, 0), (42, 0)])
        self.assertIn("cannot read the compositor's keymap", warns[0])
        self.assertIn("read", d._xkb_said)   # the daemon log says it once

    def test_unparsable_keymap(self):
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".xkb", delete=False) as f:
            f.write("this is not a keymap\n")
        self.addCleanup(os.unlink, f.name)
        d, warns = self.type_hello(WDOTOOL_XKB_KEYMAP=f.name, WDOTOOL_LAYOUT=None)
        self.assertEqual(taps(d.kb),
                         [(30, 1), (30, 0), (42, 1), (30, 1), (30, 0), (42, 0)])
        self.assertIn("build", d._xkb_said)

    def test_a_crash_in_the_new_code_is_survivable(self):
        boom = lambda *a, **k: 1 / 0  # noqa: E731
        self.addCleanup(setattr, xkbmap, "build", xkbmap.build)
        xkbmap.build = boom
        d, warns = self.type_hello(
            WDOTOOL_XKB_KEYMAP=os.path.join(KEYMAPS, "de.xkb"), WDOTOOL_LAYOUT=None)
        self.assertEqual(taps(d.kb),
                         [(30, 1), (30, 0), (42, 1), (30, 1), (30, 0), (42, 0)])

    def test_failure_is_not_retried_on_every_keystroke(self):
        d = make_daemon()
        calls = []
        real = xkbmap.fetch
        self.addCleanup(setattr, xkbmap, "fetch", real)
        xkbmap.fetch = lambda *a, **k: (calls.append(1), real(*a, **k))[1]
        with env(WDOTOOL_XKB_KEYMAP="/nonexistent/x.xkb", WDOTOOL_LAYOUT=None):
            for _ in range(5):
                d.op_type("a", 0, False)
        self.assertEqual(len(calls), 1)


class TestOverride(unittest.TestCase):
    def test_layout_us_never_reads_a_keymap(self):
        d = make_daemon()
        self.addCleanup(setattr, xkbmap, "fetch", xkbmap.fetch)
        xkbmap.fetch = lambda *a, **k: self.fail("fetch() must not be called")
        with env(WDOTOOL_LAYOUT="us",
                 WDOTOOL_XKB_KEYMAP=os.path.join(KEYMAPS, "de.xkb")):
            d.op_type("yz", 0, False)
        self.assertEqual(taps(d.kb), [(21, 1), (21, 0), (44, 1), (44, 0)])

    def test_layout_xkb_forces_the_reverse_map_on_a_us_keymap(self):
        d = make_daemon()
        with env(WDOTOOL_LAYOUT="xkb",
                 WDOTOOL_XKB_KEYMAP=os.path.join(KEYMAPS, "us.xkb")):
            layout = d._layout()
        self.assertIsNotNone(layout)          # built, though the bypass applied
        self.assertEqual(layout.name, "English (US)")
        self.assertEqual(layout.lookup_char("a"), [(30, 0)])

    def test_unknown_mode_behaves_like_auto(self):
        d = make_daemon()
        with env(WDOTOOL_LAYOUT="banana",
                 WDOTOOL_XKB_KEYMAP=os.path.join(KEYMAPS, "de.xkb")):
            self.assertIsNotNone(d._layout())


# ---------------------------------------------------------------------------
# the safety requirement


class TestUsLayoutNeverReachesTheNewCode(unittest.TestCase):
    """On a plain US layout the reverse map is not built, not consulted, and
    not even importable-in-anger: every entry point is replaced with a mine.

    The bypass check itself must still run -- something has to look at the
    keymap to say "this is US" -- so it is left alone and counted.
    """

    def setUp(self):
        self.calls = []
        self.mined = {}
        for name in ("parse", "reverse", "build"):
            self.mined[name] = getattr(xkbmap, name)

            def mine(*a, _n=name, **k):
                raise AssertionError(f"xkbmap.{_n}() was reached on a US layout")

            setattr(xkbmap, name, mine)
        real_check = xkbmap.active_group_is_plain_us
        self.mined["active_group_is_plain_us"] = real_check

        def counted(text, group=1):
            self.calls.append(group)
            return real_check(text, group)

        xkbmap.active_group_is_plain_us = counted

    def tearDown(self):
        for name, fn in self.mined.items():
            setattr(xkbmap, name, fn)

    def us_daemon(self, fixture="us"):
        d = make_daemon()
        self._e = env(WDOTOOL_XKB_KEYMAP=os.path.join(KEYMAPS, fixture + ".xkb"),
                      WDOTOOL_LAYOUT=None, WDOTOOL_XKB_GROUP=None)
        self._e.__enter__()
        self.addCleanup(self._e.__exit__, None, None, None)
        return d

    def test_type_is_byte_identical_to_the_fixed_table(self):
        d = self.us_daemon()
        warns = d.op_type("aA!\n", 0, False)
        self.assertEqual(warns, [])
        self.assertEqual(taps(d.kb), [
            (30, 1), (30, 0),
            (42, 1), (30, 1), (30, 0), (42, 0),
            (42, 1), (2, 1), (2, 0), (42, 0),
            (28, 1), (28, 0),
        ])
        self.assertEqual(self.calls, [1])   # checked once, then cached

    def test_the_unreachable_warning_is_the_old_one(self):
        d = self.us_daemon()
        warns = d.op_type("é", 0, False)
        self.assertEqual(
            warns, ["Can't type character 'é' (not on the US layout). Skipping."])

    def test_key_sequences_use_the_fixed_table(self):
        d = self.us_daemon()
        warns = d.op_key("ctrl+shift+t", "press", 0, False)
        self.assertEqual(warns, [])
        self.assertEqual(taps(d.kb), [(29, 1), (42, 1), (20, 1),
                                      (29, 0), (42, 0), (20, 0)])

    def test_a_two_layout_session_on_its_us_group_is_also_bypassed(self):
        d = self.us_daemon("us_de")
        warns = d.op_type("zZ", 0, False)
        self.assertEqual(taps(d.kb),
                         [(44, 1), (44, 0), (42, 1), (44, 1), (44, 0), (42, 0)])
        # ... and says which group that was (B1). Naming it is a regex scan
        # of the keymap text -- still not the parser, which is mined.
        self.assertEqual(len(warns), 1, warns)
        self.assertIn("assuming 'English (US)'", warns[0])

    def test_the_mines_are_live(self):
        """This class only proves something if the mines would go off."""
        with self.assertRaises(AssertionError):
            xkbmap.build("anything")


class TestDiagnosticSubcommand(unittest.TestCase):
    def run_it(self, *args, mode=None):
        """`__keymap` is a CLI: it sets WDOTOOL_XKB_* for its own process.
        Keep that inside the test."""
        out, err = io.StringIO(), io.StringIO()
        with env(WDOTOOL_XKB_KEYMAP=None, WDOTOOL_XKB_GROUP=None,
                 WDOTOOL_LAYOUT=mode):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = cli.main(["wdotool", "__keymap", *args])
        return rc, out.getvalue(), err.getvalue()

    def test_dump(self):
        rc, out, _ = self.run_it("--keymap", os.path.join(KEYMAPS, "de.xkb"))
        self.assertEqual(rc, 0)
        self.assertEqual(out, text("de"))

    def test_info(self):
        rc, out, _ = self.run_it("--keymap", os.path.join(KEYMAPS, "de.xkb"),
                                 "--info")
        self.assertEqual(rc, 0)
        self.assertIn("group 1:      'German' <- active (assumed)", out)
        self.assertIn("us bypass:     no", out)
        self.assertIn("level3=key 100", out)

    def test_info_says_when_the_bypass_applies(self):
        rc, out, _ = self.run_it("--keymap", os.path.join(KEYMAPS, "us.xkb"),
                                 "--info")
        self.assertEqual(rc, 0)
        self.assertIn("us bypass:     yes", out)

    def test_chars(self):
        rc, out, _ = self.run_it("--keymap", os.path.join(KEYMAPS, "de.xkb"),
                                 "--chars", "zé€漢")
        self.assertEqual(rc, 0)
        self.assertIn("'z': key 21", out)
        self.assertIn("'é': key 13 then key 18", out)
        self.assertIn("'€': key 18+level3", out)
        self.assertIn("'漢': unreachable", out)

    def test_chars_on_a_bypassed_session_answers_from_the_fixed_table(self):
        """The diagnostic must not contradict the line above it: on a
        bypassed session wdotool sends the fixed table's keystrokes, so that
        is what --chars has to show (it used to print the reverse map's, and
        `(` came out as the keypad key)."""
        rc, out, _ = self.run_it("--keymap", os.path.join(KEYMAPS, "us.xkb"),
                                 "--info", "--chars", "(a")
        self.assertEqual(rc, 0)
        self.assertIn("us bypass:     yes", out)
        self.assertIn("US bypass is in effect", out)
        self.assertIn("'(': key 10+shift", out)
        self.assertIn("'a': key 30", out)

    def test_forcing_the_reverse_map_is_shown_and_used(self):
        rc, out, _ = self.run_it("--keymap", os.path.join(KEYMAPS, "us.xkb"),
                                 "--info", "--chars", "(", mode="xkb")
        self.assertEqual(rc, 0)
        self.assertIn("us bypass:     no -- WDOTOOL_LAYOUT=xkb overrides it", out)
        self.assertNotIn("US bypass is in effect", out)
        self.assertIn("'(': key 10+shift", out)   # the same key, the long way

    def test_group_option(self):
        rc, out, _ = self.run_it("--keymap", os.path.join(KEYMAPS, "us_de.xkb"),
                                 "--group", "2", "--chars", "z")
        self.assertEqual(rc, 0)
        self.assertIn("'z': key 21", out)

    def test_help_and_bad_option(self):
        rc, out, _ = self.run_it("--help")
        self.assertEqual(rc, 0)
        self.assertIn("Usage: wdotool __keymap", out)
        rc, _, err = self.run_it("--bogus")
        self.assertEqual(rc, 1)
        self.assertIn("unknown option", err)

    def test_no_compositor_exits_two(self):
        with env(WDOTOOL_XKB_KEYMAP=None, WAYLAND_DISPLAY=None,
                 XDG_RUNTIME_DIR="/nonexistent"):
            rc, _, err = self.run_it()
        self.assertEqual(rc, 2)
        self.assertIn("wdotool:", err)

    def test_it_is_not_a_command(self):
        """__keymap is hidden: not in the registry, not in help."""
        from wdotool import commands

        self.assertFalse(commands.is_command("__keymap"))
        self.assertIsNone(commands.lookup("__keymap"))


# ---------------------------------------------------------------------------
# the review's findings, one regression test each


class TestTheAssumedGroupIsAnnounced(unittest.TestCase):
    """B1: the "which layout is active?" notice lived on the reverse-map
    path only, so a `us,de` session -- the one case where the bypass is taken
    *and* the group is a guess -- typed US characters and said nothing."""

    def type_on(self, name, group=None, s="z"):
        d = make_daemon()
        with env(WDOTOOL_XKB_KEYMAP=os.path.join(KEYMAPS, name + ".xkb"),
                 WDOTOOL_XKB_GROUP=group, WDOTOOL_LAYOUT=None):
            return d, d.op_type(s, 0, False)

    def test_the_bypass_path_says_which_group_it_assumed(self):
        d, warns = self.type_on("us_de")
        self.assertIsNone(d._layout_cache[1])              # bypassed, as before
        self.assertEqual(taps(d.kb), [(44, 1), (44, 0)])   # US z, as before
        self.assertEqual(len(warns), 1, warns)
        self.assertIn("assuming 'English (US)'", warns[0])
        self.assertIn("WDOTOOL_XKB_GROUP", warns[0])

    def test_a_pinned_group_says_nothing(self):
        for group in ("1", "2"):
            _, warns = self.type_on("us_de", group=group)
            self.assertEqual(warns, [], group)

    def test_a_session_with_nothing_to_guess_says_nothing(self):
        for name in ("us", "sway_de", "us_swapescape"):
            _, warns = self.type_on(name, s="a")
            self.assertEqual([w for w in warns if "assuming" in w], [], name)

    def test_it_is_said_again_when_the_layout_changes(self):
        d = make_daemon()
        said = []
        for name in ("de", "fr", "de"):
            with env(WDOTOOL_XKB_KEYMAP=os.path.join(KEYMAPS, name + ".xkb"),
                     WDOTOOL_LAYOUT=None, WDOTOOL_XKB_GROUP=None):
                w = []
                d._layout(w)
                d._layout(w)      # same state twice: still one notice
                said.append([x for x in w if "assuming" in x])
        self.assertEqual([len(s) for s in said], [1, 1, 1])
        self.assertIn("French", said[1][0])

    def test_group_name_needs_no_parse(self):
        """The notice runs on the bypass path, so it must not drag the
        parser in with it."""
        self.addCleanup(setattr, xkbmap, "parse", xkbmap.parse)
        xkbmap.parse = lambda *a, **k: self.fail("parse() was reached")
        self.assertEqual(xkbmap.group_name(text("us_de"), 2), "German")
        self.assertEqual(xkbmap.group_name(text("de"), 1), "German")
        self.assertEqual(xkbmap.group_name(text("us_de"), 9), "group 9")


class TestUsLayoutsWithOptions(unittest.TestCase):
    """B2: a plain `us` session with keyboard *options* set is still a plain
    US session. Refusing to bypass `caps:swapescape` ran the whole reverse
    map on exactly the setup the safety requirement is about."""

    def test_they_are_bypassed(self):
        for name in ("us_swapescape", "us_grptoggle"):
            self.assertTrue(xkbmap.active_group_is_plain_us(text(name), 1), name)

    def test_they_type_through_the_fixed_table(self):
        for name in ("us_swapescape", "us_grptoggle"):
            d = make_daemon()
            with env(WDOTOOL_XKB_KEYMAP=os.path.join(KEYMAPS, name + ".xkb"),
                     WDOTOOL_LAYOUT=None, WDOTOOL_XKB_GROUP=None):
                warns = d.op_type("aA!", 0, False)
            self.assertIsNone(d._layout_cache[1], name)
            self.assertEqual(warns, [], name)
            self.assertEqual(taps(d.kb), [
                (30, 1), (30, 0),
                (42, 1), (30, 1), (30, 0), (42, 0),
                (42, 1), (2, 1), (2, 0), (42, 0)], name)

    def test_the_position_keys_are_not_part_of_the_check(self):
        """<CAPS> carrying Escape is what used to fail it. Escape, Return,
        Tab, BackSpace and Delete are in the same place on every layout and
        go through KEYSYM_KEYS, not through any layout table."""
        want = xkbmap._expected_us()
        for code in (1, 14, 15, 28, 111):   # Esc BackSpace Tab Return Delete
            self.assertNotIn(code, want)
        self.assertEqual(want[30], {1: ord("a"), 2: ord("A")})

    def test_a_type_is_still_checked_where_the_table_shifts_the_key(self):
        us = text("us")
        odd = us.replace('''\tkey <AD01> {
\t\tsymbols[1]= [ 0x71, 0x51 ],''', '''\tkey <AD01> {
\t\ttype= "PC_ALT_LEVEL2",
\t\tsymbols[1]= [ 0x71, 0x51 ],''', 1)
        self.assertNotEqual(odd, us)
        self.assertFalse(xkbmap.active_group_is_plain_us(odd, 1))

    def test_but_not_on_a_key_it_only_ever_presses_unshifted(self):
        """`grp:win_space_toggle` gives <SPCE> the type PC_SUPER_LEVEL2.
        The fixed table presses that key at level 1 and never shifts it, so
        its type is none of our business."""
        us = text("us")
        spce = us.replace("\tkey <SPCE> {\t[ 0x20 ] };",
                          '\tkey <SPCE> {\n\t\ttype= "PC_SUPER_LEVEL2",'
                          "\n\t\tsymbols[1]= [ 0x20 ]\n\t};", 1)
        self.assertNotEqual(spce, us)
        self.assertTrue(xkbmap.active_group_is_plain_us(spce, 1))

    def test_the_macintosh_group_name_is_accepted_but_still_verified(self):
        """A Macintosh keyboard model calls the same layout "USA"."""
        us = text("us").replace('name[1]="English (US)"', 'name[1]="USA"', 1)
        self.assertTrue(xkbmap.active_group_is_plain_us(us, 1))
        de = text("de").replace('name[1]="German"', 'name[1]="USA"', 1)
        self.assertNotEqual(de, text("de"))
        self.assertFalse(xkbmap.active_group_is_plain_us(de, 1))


class TestDeadKeyPlusSpace(unittest.TestCase):
    """B3: <dead_x> <space> types what the Compose table says it types, not
    the spacing accent that shares the dead key's name. `type ´` on a German
    layout used to land an apostrophe, silently."""

    # /usr/share/X11/locale/en_US.UTF-8/Compose, the table every toolkit
    # implements. These are the two rules it gives for a dead key that is
    # not followed by a letter.
    COMPOSE_SPACE = {
        0xFE50: "`", 0xFE51: "\u0027", 0xFE52: "^", 0xFE53: "~", 0xFE54: "\u00af",
        0xFE55: "\u02d8", 0xFE56: "\u02d9", 0xFE57: '"', 0xFE58: "\u00b0",
        0xFE59: "\u02dd", 0xFE5A: "\u02c7", 0xFE5B: "\u00b8", 0xFE5C: "\u02db",
    }
    COMPOSE_DOUBLE = {0xFE50: "`", 0xFE51: "\u00b4", 0xFE52: "^", 0xFE53: "~",
                      0xFE57: "\u00a8", 0xFE58: "\u00b0"}

    def test_the_tables_agree_with_compose(self):
        for ks, ch in self.COMPOSE_SPACE.items():
            self.assertEqual(chr(xkbmap.DEAD_KEYSYMS[ks][1]), ch, hex(ks))
        self.assertEqual({k: chr(v) for k, v in xkbmap.DEAD_DOUBLE.items()},
                         self.COMPOSE_DOUBLE)

    def test_a_spacing_accent_is_the_dead_key_twice(self):
        de = rmap("de")
        self.assertEqual(de.lookup_char("\u00b4"), [(13, 0), (13, 0)])
        self.assertEqual(de.lookup_char("\u00a8"),
                         [(26, xkbmap.MOD_LEVEL3)] * 2)

    def test_and_a_dead_key_plus_space_where_compose_says_space(self):
        self.assertEqual(rmap("de").lookup_char("^"), [(41, 0), (57, 0)])

    def test_a_character_that_is_on_a_key_is_still_one_keystroke(self):
        de = rmap("de")
        self.assertEqual(de.lookup_char("\u00b0"), [(41, xkbmap.MOD_SHIFT)])
        self.assertEqual(de.lookup_char("~"), [(27, xkbmap.MOD_LEVEL3)])
        self.assertEqual(de.lookup_char("'"), [(43, xkbmap.MOD_SHIFT)])


class TestCommentsInAKeymap(unittest.TestCase):
    """B4: comments are keymap syntax. A `}` inside one closed a block early
    and `build()` then *succeeded*, with a quarter of the characters."""

    def with_stray_brace(self, name, comment):
        t = text(name)
        marked = t.replace("\tkey <AD01> {", comment + "\n\tkey <AD01> {", 1)
        self.assertNotEqual(marked, t)
        return marked

    def test_a_brace_in_a_line_comment_no_longer_halves_the_keymap(self):
        clean = xkbmap.build(text("de"), 1)
        marked = xkbmap.build(self.with_stray_brace("de", "\t// stray } brace"), 1)
        self.assertEqual(len(marked.chars), len(clean.chars))
        self.assertEqual(marked.lookup_char("\u00e4"), [(40, 0)])
        self.assertEqual(marked.lookup_char("@"), [(16, xkbmap.MOD_LEVEL3)])

    def test_a_block_comment_too(self):
        marked = self.with_stray_brace("de", "\t/* } and\n\t   } again */")
        self.assertEqual(xkbmap.build(marked, 1).lookup_char("\u00e4"), [(40, 0)])

    def test_the_bypass_reads_them_as_comments_too(self):
        self.assertTrue(xkbmap.active_group_is_plain_us(
            self.with_stray_brace("us", "\t// } "), 1))

    def test_a_string_is_not_a_comment(self):
        kept = xkbmap.strip_comments('name[1]="a//b"; // dropped')
        self.assertIn('"a//b"', kept)
        self.assertNotIn("dropped", kept)
        untouched = "no comment characters here"
        self.assertIs(xkbmap.strip_comments(untouched), untouched)


class TestTheModifiersWait(unittest.TestCase):
    """B5: the 80 ms wait for a wl_keyboard.modifiers event Mutter never
    sends an unfocused client was paid on every command of a plain US GNOME
    session, because it was switched off by the wrong condition -- GNOME
    compiles a lone `us` source as two identical groups, which makes the
    group *known* and left the wait switched on for ever."""

    def waits(self, mods_seen, name="us"):
        d = make_daemon()
        seen = []
        body = text(name)

        def fake_fetch(timeout=2.0, mods_wait=0.08):
            seen.append(mods_wait)
            return xkbmap.Snapshot(body, 1, "test", True, mods_seen)

        self.addCleanup(setattr, xkbmap, "fetch", xkbmap.fetch)
        xkbmap.fetch = fake_fetch
        with env(WDOTOOL_LAYOUT=None, WDOTOOL_XKB_KEYMAP=None,
                 WDOTOOL_XKB_GROUP=None):
            d._layout()
            d._layout()
        return seen

    def test_a_compositor_that_sends_no_modifiers_event_is_waited_for_once(self):
        self.assertEqual(self.waits(False), [0.08, 0.0])

    def test_the_regression_itself(self):
        # us.xkb is two identical groups, so the group is "known" ...
        self.assertEqual(xkbmap.choose_group(text("us")), (1, True))
        # ... and the wait must be switched off all the same.
        self.assertEqual(self.waits(False, "us")[1], 0.0)

    def test_the_wait_stays_while_the_event_does_arrive(self):
        self.assertEqual(self.waits(True), [0.08, 0.08])

    def test_a_pinned_group_never_waits(self):
        seen = []

        def fake_wayland(timeout, mods_wait):
            seen.append(mods_wait)
            return text("us_de"), None, False

        self.addCleanup(setattr, xkbmap, "_fetch_wayland", xkbmap._fetch_wayland)
        xkbmap._fetch_wayland = fake_wayland
        with env(WDOTOOL_XKB_KEYMAP=None, WDOTOOL_XKB_GROUP="2"):
            snap = xkbmap.fetch()
        self.assertEqual((snap.group, snap.group_known, seen), (2, True, [0.0]))
        with env(WDOTOOL_XKB_KEYMAP=None, WDOTOOL_XKB_GROUP=None):
            xkbmap.fetch()
        self.assertEqual(seen[-1], 0.08)   # not pinned: the wait is paid


class TestTheKeypadIsNeverTheAnswer(unittest.TestCase):
    """B6: excluding the keypad by key *name* leaked. Mutter puts KP_Decimal
    on <I129> and the keypad parentheses on <I187>/<I188>, so `(` resolved to
    evdev 179 on every non-US layout and French `.` to 121 -- keys the layout
    does not intend for those characters."""

    KEYPAD_CODES = frozenset(
        {55, 71, 72, 73, 74, 75, 76, 77, 78, 79, 80, 81, 82, 83, 96, 98,
         117, 121, 179, 180})

    def test_no_character_of_any_fixture_resolves_to_a_keypad_key(self):
        for name in FIXTURES:
            body = text(name)
            for group in range(1, xkbmap.group_count(body) + 1):
                for ch, (code, _) in xkbmap.build(body, group).chars.items():
                    self.assertNotIn(code, self.KEYPAD_CODES,
                                     f"{name} group {group}: {ch!r}")
                    self.assertLess(code, 128, f"{name} group {group}: {ch!r}")

    def test_the_characters_the_leak_produced(self):
        S = xkbmap.MOD_SHIFT
        self.assertEqual(rmap("de").lookup_char("("), [(9, S)])
        self.assertEqual(rmap("de").lookup_char(")"), [(10, S)])
        self.assertEqual(rmap("es").lookup_char("("), [(9, S)])
        self.assertEqual(rmap("fr").lookup_char("("), [(6, 0)])
        self.assertEqual(rmap("fr").lookup_char("."), [(51, S)])
        self.assertEqual(rmap("de").lookup_char("."), [(52, 0)])

    def test_the_keypad_is_demoted_not_deleted(self):
        """`key KP_Add` must still find the keypad key."""
        self.assertEqual(rmap("de").keysym_entry("KP_Add"), (78, 0))
        self.assertEqual(rmap("fr").keysym_entry("KP_Decimal"), (121, 0))

    def test_the_rank_covers_both_shapes(self):
        self.assertEqual(xkbmap._keypad_rank(78, 0xFFAB), 1)   # KP_Add
        self.assertEqual(xkbmap._keypad_rank(121, 0xFFAE), 1)  # KP_Decimal
        self.assertEqual(xkbmap._keypad_rank(179, 0x28), 1)    # <I187>, parenleft
        self.assertEqual(xkbmap._keypad_rank(9, 0x28), 0)      # the 8 key


class TestADegradedSessionKeepsSayingSo(unittest.TestCase):
    """Falling back to the fixed US table under a non-US layout types the
    wrong characters. Telling only whichever client happened to ask first
    tells nobody: every command that types through the fallback says so."""

    def test_every_client_is_warned_while_the_keymap_cannot_be_read(self):
        d = make_daemon()
        with env(WDOTOOL_XKB_KEYMAP="/nonexistent/x.xkb", WDOTOOL_LAYOUT=None):
            first = d.op_type("a", 0, False)
            second = d.op_type("a", 0, False)   # inside the 5 s backoff
        self.assertEqual(len(first), 1, first)
        self.assertIn("cannot read the compositor's keymap", first[0])
        self.assertEqual(first, second)

    def test_an_unusable_keymap_warns_every_client_too(self):
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".xkb", delete=False) as f:
            f.write("this is not a keymap\n")
        self.addCleanup(os.unlink, f.name)
        d = make_daemon()
        with env(WDOTOOL_XKB_KEYMAP=f.name, WDOTOOL_LAYOUT=None):
            first = d.op_type("a", 0, False)
            second = d.op_type("a", 0, False)   # served from the cache
        self.assertIn("cannot use the compositor's keymap", first[0])
        self.assertEqual(first, second)

    def test_and_stops_when_the_keymap_can_be_read_again(self):
        d = make_daemon()
        with env(WDOTOOL_XKB_KEYMAP="/nonexistent/x.xkb", WDOTOOL_LAYOUT=None):
            self.assertTrue(d.op_type("a", 0, False))
        d._xkb_backoff = 0.0                     # the compositor came back
        with env(WDOTOOL_XKB_KEYMAP=os.path.join(KEYMAPS, "us.xkb"),
                 WDOTOOL_LAYOUT=None, WDOTOOL_XKB_GROUP=None):
            self.assertEqual(d.op_type("a", 0, False), [])



class GroupCountIsBounded(unittest.TestCase):
    """The group index is a number in the keymap, not a length we measured:
    `symbols[Group2000000000]` is eight bytes of text that used to become two
    billion Group objects (3.2 GB at five million). The compositor is not
    always ours -- a root daemon can be pointed at a planted Wayland socket."""

    def test_a_huge_group_index_is_clamped(self):
        base = "xkb_symbols { key <AE01> { [ a ] }; };"
        for n in (5, 1000, 5_000_000, 2_000_000_000):
            text = base.replace("[ a ]", "symbols[Group%d] = [ a ]" % n)
            self.assertEqual(xkbmap.group_count(text), xkbmap.MAX_GROUPS)
        self.assertEqual(xkbmap.MAX_GROUPS, 4)      # libxkbcommon's maximum

    def test_a_real_keymap_is_unchanged(self):
        self.assertEqual(xkbmap.group_count(text("sway_de")), 1)
        self.assertEqual(xkbmap.group_count(text("de")), 2)
        self.assertEqual(len(xkbmap.parse(text("us_de")).groups), 3)

if __name__ == "__main__":
    unittest.main()
