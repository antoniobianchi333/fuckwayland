"""C number parsing, once: atoi(), atof() and strtol(s, NULL, 0).

The tools promise byte parity with C programs that hand their arguments to
these three functions and never check for failure, so the answer for junk is
0 / 0.0 rather than an error. Every character class here is deliberately
ASCII: C's isspace() and isdigit() are ASCII in the "C" locale, while
Python's `\\d` and str.isdigit() also accept Arabic-Indic and other Unicode
digits, and str.strip() also eats U+00A0. `wwmctl -s <U+0664><U+0662>` selects
desktop 0, as wmctrl does, not desktop 42.

Callers may pass an option default that is already an int (input_cmds keeps
xdotool's numeric defaults), so every entry point coerces with str().
"""

import math
import re

_SPACE = r"[ \t\n\r\f\v]*"  # C isspace() in the "C" locale

_ATOI_RE = re.compile(_SPACE + r"([+-]?[0-9]+)")
# strtol/strtoul with base 0: 0x-hex, then C octal, then decimal. The decimal
# arm cannot start with 0 -- the octal arm has already taken that -- so it is
# spelled [1-9] and no Unicode digit can reach int().
_STRTOL_RE = re.compile(_SPACE + r"([+-]?)(0[xX][0-9a-fA-F]+|0[0-7]*|[1-9][0-9]*)")
_ATOF_RE = re.compile(
    _SPACE + r"[+-]?(?:0[xX][0-9a-fA-F]*(?:\.[0-9a-fA-F]*)?(?:[pP][+-]?[0-9]+)?"
    r"|(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?"
    r"|[iI][nN][fF](?:[iI][nN][iI][tT][yY])?|[nN][aA][nN])"
)


def _text(s) -> str:
    """The string C would have seen. None is the empty string, not "None"."""
    return "" if s is None else str(s)


def atoi(s) -> int:
    """C atoi(): leading whitespace, optional sign, digits, else 0."""
    m = _ATOI_RE.match(_text(s))
    return int(m.group(1)) if m else 0


def strtol(s) -> int:
    """C strtol(s, NULL, 0): a leading integer in hex, octal or decimal, 0 if
    none. Python's int(s, 0) is a different function and gets two cases wrong:
    it rejects C's octal `0755` and accepts `0b101` as binary where strtol
    stops at the 'b' and returns 0."""
    m = _STRTOL_RE.match(_text(s))
    if not m:
        return 0
    sign, digits = m.group(1), m.group(2)
    base = 16 if digits[:2].lower() == "0x" else 8 if digits.startswith("0") else 10
    return int(sign + digits, base)


def atof(s) -> float:
    """C atof(): parse a leading double (decimal, exponent, or hex float),
    0.0 when nothing parses."""
    m = _ATOF_RE.match(_text(s))
    if not m:
        return 0.0
    t = m.group().strip()
    try:
        return float.fromhex(t) if re.match(r"[+-]?0[xX]", t) else float(t)
    except ValueError:
        return 0.0
    except OverflowError:
        # C strtod() saturates to +-HUGE_VAL and sets ERANGE; only the hex
        # spelling gets here, because float("1e400") already answers inf.
        # `sleep 0x1p1024` used to end in "hexadecimal value too large to
        # represent as a float" where the oracle returns 0 at once.
        return -math.inf if t.startswith("-") else math.inf
