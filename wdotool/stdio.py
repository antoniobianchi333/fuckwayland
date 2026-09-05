"""What a tool does about a standard output that is not there any more.

Three things go wrong with stdout, and each one used to end in a traceback,
in a status no original ever produces, or in silence where output was lost:

* the reader of a pipe leaves early (``wdotool search . | head -1``).  The
  originals die of SIGPIPE without a word.  Python turns SIGPIPE into
  BrokenPipeError, and then -- because the interpreter flushes stdout once
  more on the way out -- prints "Exception ignored in: <_io.TextIOWrapper
  name='<stdout>' ...>" after the tool has already finished.
* the file behind stdout cannot take what was written (``>/dev/full``, a
  full disk, a quota).  Nothing reached the reader, so exiting 0 is a lie,
  and the interpreter's exit-time flush turns whatever was returned into
  exit 120 -- a status none of the six originals has.
* fd 1 (or fd 2) was closed before the interpreter started (``>&-``, a
  daemon that closed its descriptors).  ``sys.stdout`` is then None and the
  first ``print()`` is an AttributeError.

`repair_std()` answers the third at the top of ``main()``; `flush_stdout()`
answers the first two at the bottom of it.  It CLOSES the stream on failure,
which is the part that matters: the interpreter skips a *closed* stdout when
it flushes the standard files at exit, and flushes -- and fails on, and
turns into exit 120 -- an open one.  So nothing may print after it.
"""

import os
import sys


def repair_std() -> None:
    """Give `main()` a stdout and a stderr to write to when fd 1 or fd 2 was
    closed before the interpreter started.

    /dev/null, not an error: the tools we clone are C programs whose
    `printf` to a closed fd fails quietly, and a tool that cannot say
    anything still has work to do (`wdotool key ctrl+c >&-` must press the
    keys).  Idempotent, and never raises.
    """
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            try:
                setattr(sys, name, open(os.devnull, "w"))
            except OSError:
                pass


def flush_stdout(prog: str, quiet: bool = False) -> bool:
    """Push out what was printed, and say whether it got there.

    Call it last in `main()`: on failure the stream is closed, which is what
    stops the interpreter flushing it again on the way out (see the module
    docstring), and a closed stdout raises on the next `print()`.

    Silent on a broken pipe -- xrandr, xprop, wmctrl and xdotool all say
    nothing when their reader leaves -- and otherwise one ``prog: message``
    line on stderr, never a traceback.  `quiet` is for the caller that has
    already printed that line: a write big enough to overflow the buffer
    fails inside the command instead of here, and the same errno reported
    twice helps nobody.
    """
    out = getattr(sys, "stdout", None)
    if out is None:                       # `>&-` without repair_std()
        return True
    try:
        out.flush()
        return True
    except BrokenPipeError:
        pass
    except (OSError, ValueError, AttributeError) as e:
        err = None if quiet else getattr(sys, "stderr", None)
        if err is not None:
            try:
                err.write("%s: %s\n" % (prog, e))
            except (OSError, ValueError):
                pass                      # stderr is gone too; nothing to do
    try:
        out.close()                       # flushes again, and may fail again
    except (OSError, ValueError, AttributeError):
        pass                              # closed all the same: CPython marks
    return False                          # the file closed before re-raising


def exit_after_flush(prog: str, exc: SystemExit) -> None:
    """Flush on the way out through a SystemExit and re-raise it.

    argparse's ``--help``/``--version`` and its parse errors leave `main()`
    this way, which is how `--help >/dev/full` used to reach the exit-time
    flush with everything still buffered and exit 120.  The exception is
    re-raised rather than turned into a return value: callers that embed a
    tool as a library catch it, and `main()`'s own contract of *returning*
    the status is for the paths that get that far.  A lost stdout still
    turns a success into a failure.
    """
    code = exc.code
    if not isinstance(code, int):
        code = 0 if code is None else 1
    if flush_stdout(prog):
        raise exc
    raise SystemExit(code or 1) from None
