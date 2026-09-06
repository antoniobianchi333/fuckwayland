#!/usr/bin/env python3
"""Do the documents, the help text and the code agree?

Prose read against prose catches nothing: every contradiction this project has
shipped was between a document and a *fact* -- an option parser, an install
file, an import. So this reads the options out of the source, reads what each
tool prints when run, reads what the documents claim, and reports where the
three disagree.

It is a reporting tool, not a test: some findings are deliberate. Options that
exist for byte parity with the original are absent from `--help` on purpose,
and a document may reasonably mention an option belonging to another tool.
Judgement stays with the reader; the point is that nothing is missed.

  python3 scripts/check-docs.py            # everything
  python3 scripts/check-docs.py --tool wxrandr
  python3 scripts/check-docs.py --grep overlap    # one feature across everything
"""
import argparse
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = ["wdotool", "wwmctl", "wxprop", "wxrandr", "warandr", "wmirror"]
OPT = re.compile(r"""["'](--[a-z][a-z0-9-]{2,})["']""")


def options_in_code(tool):
    """Every long option spelled in the package's own source."""
    found = set()
    d = os.path.join(ROOT, tool)
    for name in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        if not name.endswith(".py"):
            continue
        with open(os.path.join(d, name), encoding="utf-8") as f:
            found |= set(OPT.findall(f.read()))
    return found


def options_in_help(tool):
    """Every long option the tool prints when asked for help."""
    env = dict(os.environ, PYTHONPATH=ROOT, FUCKWAYLAND_PASSTHROUGH="never")
    out = ""
    for args in (["--help"], ["-h"]):
        try:
            p = subprocess.run([sys.executable, "-m", tool] + args, env=env,
                               capture_output=True, text=True, timeout=30)
            out += p.stdout + p.stderr
        except (OSError, subprocess.SubprocessError):
            pass
    return set(re.findall(r"(--[a-z][a-z0-9-]{2,})", out))


def documents():
    """Every markdown file, as name -> text."""
    out = {}
    for base, _dirs, names in os.walk(ROOT):
        if any(p in base for p in (".git", "node_modules", "__pycache__")):
            continue
        for n in names:
            if n.endswith(".md"):
                path = os.path.join(base, n)
                with open(path, encoding="utf-8", errors="replace") as f:
                    out[os.path.relpath(path, ROOT)] = f.read()
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tool", action="append", choices=TOOLS,
                    help="only this tool (repeatable)")
    ap.add_argument("--grep", metavar="WORD",
                    help="only options and passages containing WORD, for "
                         "checking one feature across everything")
    args = ap.parse_args(argv)
    tools = args.tool or TOOLS
    docs = documents()
    problems = 0

    for tool in tools:
        code = options_in_code(tool)
        helped = options_in_help(tool)
        if args.grep:
            code = {o for o in code if args.grep in o}
            helped = {o for o in helped if args.grep in o}
        documented = {}
        for opt in sorted(code | helped):
            where = [name for name, text in docs.items() if opt in text]
            documented[opt] = where

        print("\n== %s: %d options in the source, %d in its help"
              % (tool, len(code), len(helped)))
        for opt in sorted(code | helped):
            where = documented[opt]
            flags = []
            if opt not in code:
                flags.append("in help, not in the source")
            if opt not in helped:
                flags.append("not in --help")
            if not where:
                flags.append("DOCUMENTED NOWHERE")
            if flags:
                problems += sum(1 for f in flags if "DOCUMENTED" in f)
                print("  %-28s %s%s" % (opt, "; ".join(flags),
                                        "" if where else ""))
            if where and len(where) > 3:
                print("  %-28s in %d documents: %s"
                      % (opt, len(where), ", ".join(sorted(where)[:4]) + " ..."))

    if args.grep:
        print("\n== every passage mentioning %r" % args.grep)
        for name in sorted(docs):
            hits = [i + 1 for i, ln in enumerate(docs[name].splitlines())
                    if args.grep in ln]
            if hits:
                print("  %-28s %2d lines: %s" % (name, len(hits),
                                                 ", ".join(map(str, hits[:12]))))
    print("\n%d option(s) documented nowhere" % problems)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
