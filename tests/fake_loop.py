"""A stand-in for the tracked loop: an ``evolve --session`` that obeys the session file.

The real loop is an evolution run - a database, a CSV, minutes per generation - which is
exactly what makes it the wrong thing to start inside a test.  This one does the two
things the controls depend on: it marks itself running with its own pid, and it calls
``Session.checkpoint`` at every generation boundary, which is where ``pause``, ``resume``
and ``stop`` take effect (the same call the real ``Evolver`` makes).
"""

from __future__ import annotations

import sys
from pathlib import Path

#: The repository root, so the child can import ``selftrade`` whatever its cwd is
#: (``-c`` puts the *current* directory on ``sys.path``, and tests may run from anywhere).
ROOT = str(Path(__file__).resolve().parents[1])

SOURCE = """
import os
import sys
import time

sys.path.insert(0, sys.argv[4])
from selftrade.session import Session

session_file, total, step = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
s = Session(session_file)
s.mark_running(total)
print(f"fake loop: pid {os.getpid()} | {total} generations", flush=True)
stopped = False
for gen in range(total):
    if s.checkpoint(gen, total) == "stop":
        stopped = True
        print(f"fake loop: stop requested at generation {gen}", flush=True)
        break
    time.sleep(step)
s.mark_finished("stopped" if stopped else "done")
print("fake loop: finished", flush=True)
"""


def fake_argv(session_file, generations: int, step_s: float = 0.05) -> list[str]:
    """The argv a ``LoopRunner`` should spawn to run this fake instead of the real loop."""
    return [
        sys.executable,
        "-u",
        "-c",
        SOURCE,
        str(session_file),
        str(int(generations)),
        str(float(step_s)),
        ROOT,
    ]
