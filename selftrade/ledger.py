"""The machine-written half of the verification ledger.

``docs/VERIFICATION.md`` is a record of commands that were actually run. Its weak point is
arithmetic: the size of the suite is quoted in the README, in ``CONTRIBUTING.md``, in the ledger
itself, on the social card and in the launch kit - and by the time this module was written
**every one of those copies had drifted**, four of them to an older ``809`` and the launch kit to
an even older ``785``, against a suite that collects ``815``. A hand-typed number that five
documents agree on is a number nobody owns.

The suite count is also the one number in those documents that is a *pure function of the working
tree*: collecting it takes about two seconds, needs no key, no network, no data file and no
randomness, and it cannot be argued with. So it is measured instead of typed, and the place it is
written is marked in the document::

    <!-- generated: suite -->**815 pass, exit 0** ...<!-- /generated: suite -->

``check`` is the guard, and it is cheap enough to run in CI: it collects the suite, then requires
that **every** whole-suite count anywhere in the documentation equals what it measured - marked or
not, and whether it was written as prose or into a ``-count`` chip. Both halves matter, and the chip
half did not use to: only :func:`write` ever touches a fragment, so a checker that compared the prose
and merely required a chip to *carry* a number left the number in the chip - the one that goes on a
badge, or into the HTML a screenshot is drawn from - free to drift. A count that drifted fails the
check wherever it lives, marked or bare, prose or chip - which is the only version of this that
works, because the row a person forgets is never the row they remember.

There is one exception, and it is not a loophole: a count inside a fragment that says *which
interpreter* produced it (the ledger keeps one run recorded on 3.13 beside the one on 3.14) is a
dated record rather than a claim about today. This run reports it and leaves it alone; the 3.13 job
in CI is what enforces it. Without that, a developer with a single interpreter installed could
never make the check pass, and a check that cannot be satisfied gets deleted rather than fixed.

What this module deliberately does **not** generate is the research numbers: the holdout Sharpe,
the drawdown, the cost drag. Those are outputs of ``evolve``, ``invest`` and ``stress``, they cost
minutes to recompute, and each one already has to name the command that produced it. A generator
for them would be a second and weaker source of truth. This covers what can be *proved* cheaply
and exactly, and the ledger says so in the block it writes.

A document whose numbers are a historical snapshot (``docs/LOOP-AI-PLAN.md`` records an inspection
of an earlier tree) declares that in-band with ``<!-- ledger: historical -->``. The declaration is
honoured **and reported**: an exemption nobody can see is just a stale number with a comment in
front of it.
"""

from __future__ import annotations

import platform
import re
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from pathlib import Path

#: The slot the running interpreter owns. A slot may be bound to another interpreter series with a
#: ``-pyXYZ`` suffix (``suite-py313``), which only a run *under that interpreter* may rewrite. A
#: ``-count`` suffix asks for the bare number instead of the full record, for prose and chips where
#: a sentence has to keep reading like a sentence.
DEFAULT_SLOT = "suite"
_SLOT = re.compile(r"^suite(?:-py(?P<series>\d+(?:\.\d+)?))?(?P<count>-count)?$")
#: A fragment that says which interpreter produced it: "…, on Python 3.14.5".
_NAMED_PYTHON = re.compile(r"on Python (\d+[\d.]*)")

#: A marked fragment. The body may span lines; the close marker must name the same slot.
_BLOCK = re.compile(
    r"<!--\s*generated:\s*(?P<slot>[\w.-]+)\s*-->"
    r"(?P<body>.*?)"
    r"<!--\s*/\s*generated:\s*(?P=slot)\s*-->",
    re.S,
)
#: Any marker at all, used to notice one the block regex could not pair up.
_ANY_MARKER = re.compile(r"<!--\s*/?\s*generated:")
#: ``<!-- ledger: historical -->``: the whole document is a snapshot, not a live claim.
_HISTORICAL = re.compile(r"<!--\s*ledger:\s*historical\b[^>]*-->", re.I)
#: A count that claims to be the whole suite. The floor leaves module-level counts alone: the
#: ledger's own coverage rows say things like "31 tests" or "16 tests" about one module, and those
#: are not claims about the suite.
_CLAIM = re.compile(r"(?<![\d.,])(?P<n>\d{3,})(?:\s*)(?P<unit>tests?|passed|pass)\b", re.I)
SUITE_FLOOR = 100
#: A ``-count`` fragment is nothing but the number.
_BARE_INT = re.compile(r"(\d+)")

#: pytest's ``--collect-only -q`` prints one ``path/to/test_x.py: 12`` line per module. The node-id
#: branch in :func:`parse_collection` is the fallback, if a pytest release prints ids instead.
_PER_FILE = re.compile(r"^(?P<file>\S.*?\.py):\s*(?P<n>\d+)\s*$", re.M)


class LedgerError(RuntimeError):
    """The measurement could not be made, so nothing about the ledger can be said."""


def _series_of(version: str) -> str:
    """``3.14.5`` -> ``314``: major and minor, dots removed, the way a ``-pyXYZ`` slot is spelled."""
    parts = version.split(".")
    return f"{parts[0]}{int(parts[1])}" if len(parts) > 1 else parts[0]


def _series_label(series: str) -> str:
    digits = series.replace(".", "")
    return f"Python {digits[0]}.{digits[1:]}" if len(digits) > 1 else f"Python {digits}"


# --------------------------------------------------------------------------
# measuring
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SuiteFacts:
    """What a pytest invocation said, and on which interpreter.

    ``mode`` is ``"collected"`` (the suite was enumerated) or ``"run"`` (it was executed). The
    distinction is kept because the ledger records **runs**: a collection proves a count, but it
    cannot support the word "pass".
    """

    total: int
    modules: int
    python: str
    system: str
    mode: str
    seconds: float
    exit_code: int
    failed: int | None = None
    errors: int | None = None
    skipped: int | None = None

    def claims_run(self) -> bool:
        return self.mode == "run"

    def fragment(self, shape: str = "full") -> str:
        """The marked text: what belongs between a pair of ``suite`` markers.

        ``shape`` is ``"full"`` (the record: what ran, on what, how long) or ``"count"`` (the bare
        integer, for a sentence or a badge that has to go on parsing).
        """
        if shape == "count":
            return str(self.total)
        where = f"{self.modules} test modules, on Python {self.python}"
        if self.mode == "collected":
            return f"**{self.total} tests collected**, {where}"
        if self.failed:
            head = f"**{self.failed} of {self.total} failed, exit {self.exit_code}**"
        else:
            head = f"**{self.total} pass, exit {self.exit_code}**"
        return (
            f"{head} - {self.failed} failed / {self.errors} errors / {self.skipped} skipped, "
            f"{self.modules} test modules, {self.seconds:.0f} s, on Python {self.python}"
        )

    def as_dict(self) -> dict:
        return asdict(self)


def parse_collection(out: str) -> tuple[int, int]:
    """Read pytest's collection output into ``(tests, modules)``.

    Two shapes are understood, because the printed form is a pytest implementation detail and this
    function is the difference between a working check and one that silently counts zero tests.
    """
    per_file = [(m.group("file"), int(m.group("n"))) for m in _PER_FILE.finditer(out)]
    if per_file:
        return sum(n for _, n in per_file), len({f for f, _ in per_file})
    nodes = [line.strip() for line in out.splitlines() if "::" in line]
    if nodes:
        return len(nodes), len({node.split("::", 1)[0] for node in nodes})
    raise LedgerError(
        "pytest collected nothing this parser recognises: the collection output changed shape"
    )


def parse_junit(text: str) -> dict[str, int]:
    """``tests``/``failures``/``errors``/``skipped``/``modules`` out of a JUnit report."""
    root = ET.fromstring(text)
    suite = root if root.tag == "testsuite" else root.find("testsuite")
    if suite is None:  # pragma: no cover - pytest always writes one
        raise LedgerError("the JUnit report has no <testsuite> element")
    modules = set()
    for case in suite.iter("testcase"):
        file_attr = case.get("file")
        classname = case.get("classname") or ""
        if file_attr:
            modules.add(file_attr)
        elif "." in classname:
            modules.add(classname.split(".")[1])
        else:
            modules.add(classname)
    return {
        "tests": int(suite.get("tests", 0)),
        "failures": int(suite.get("failures", 0)),
        "errors": int(suite.get("errors", 0)),
        "skipped": int(suite.get("skipped", 0)),
        "modules": len(modules),
    }


def collect(root: Path, *, timeout: int = 300) -> SuiteFacts:
    """Enumerate the suite without running it: the cheap, exact measurement.

    Collection imports every test module and resolves every parametrization, so it catches a syntax
    error, a broken import and a duplicate test id. It just does not execute anything.
    """
    argv = [
        sys.executable,
        "-m",
        "pytest",
        "--collect-only",
        "-q",
        "--no-header",
        "-p",
        "no:cacheprovider",
    ]
    started = time.monotonic()
    try:
        proc = subprocess.run(
            argv, cwd=root, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as exc:  # pragma: no cover - a hung pytest is the host
        raise LedgerError(f"pytest --collect-only did not finish within {timeout}s") from exc
    seconds = time.monotonic() - started
    if proc.returncode != 0:
        tail = (proc.stdout + proc.stderr).strip().splitlines()[-8:]
        raise LedgerError(
            f"pytest --collect-only exited {proc.returncode}, so the suite cannot be counted:\n  "
            + "\n  ".join(tail)
        )
    total, modules = parse_collection(proc.stdout)
    return SuiteFacts(
        total=total,
        modules=modules,
        python=platform.python_version(),
        system=platform.system(),
        mode="collected",
        seconds=seconds,
        exit_code=proc.returncode,
    )


def run_suite(root: Path, *, timeout: int = 1800) -> SuiteFacts:
    """Execute the suite and read the result back from the JUnit XML pytest writes.

    Reading the runner's own report rather than scraping stdout is what keeps this honest: a suite
    that fails still produces the numbers, so a failure gets recorded instead of crashing the tool.
    """
    with tempfile.TemporaryDirectory(prefix="selftrade-ledger-") as tmp:
        report = Path(tmp) / "junit.xml"
        argv = [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--no-header",
            "-p",
            "no:cacheprovider",
            f"--junit-xml={report}",
        ]
        started = time.monotonic()
        try:
            proc = subprocess.run(
                argv, cwd=root, capture_output=True, text=True, timeout=timeout, check=False
            )
        except subprocess.TimeoutExpired as exc:  # pragma: no cover
            raise LedgerError(f"the suite did not finish within {timeout}s") from exc
        seconds = time.monotonic() - started
        if not report.exists():
            tail = (proc.stdout + proc.stderr).strip().splitlines()[-8:]
            raise LedgerError(
                f"pytest exited {proc.returncode} without writing a report:\n  " + "\n  ".join(tail)
            )
        counts = parse_junit(report.read_text(encoding="utf-8"))
    return SuiteFacts(
        total=counts["tests"],
        modules=counts["modules"],
        python=platform.python_version(),
        system=platform.system(),
        mode="run",
        seconds=seconds,
        exit_code=proc.returncode,
        failed=counts["failures"],
        errors=counts["errors"],
        skipped=counts["skipped"],
    )


def measure(root: Path, *, run: bool = False) -> SuiteFacts:
    return run_suite(root) if run else collect(root)


# --------------------------------------------------------------------------
# reading the documents
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Slot:
    """A marked fragment: the one place a document lets a machine write."""

    file: str
    slot: str
    line: int
    content: str
    count: int | None

    def owned_by_us(self) -> bool:
        return bool(_SLOT.match(self.slot))

    def series(self) -> str | None:
        match = _SLOT.match(self.slot)
        return match.group("series") if match else None

    def shape(self) -> str:
        match = _SLOT.match(self.slot)
        return "count" if match and match.group("count") else "full"

    def recorded_elsewhere(self, python: str) -> bool:
        """Whether this fragment records a run on another interpreter, the way :class:`Claim` does.

        A ``suite-py313`` fragment - or one whose body says "on Python 3.13.14" - is evidence of what
        ran that day, and only that interpreter's own run may move it. Holding a 3.14 checkout to it
        is what would make the check unsatisfiable for anyone with one interpreter installed.
        """
        series = self.series()
        if series and series.replace(".", "") != _series_of(python):
            return True
        named = _NAMED_PYTHON.search(self.content)
        return bool(named) and named.group(1) != python

    def as_dict(self) -> dict:
        data = asdict(self)
        data["owned"] = self.owned_by_us()
        return data


@dataclass(frozen=True)
class Claim:
    """A whole-suite count in prose, inside a marker or not."""

    file: str
    line: int
    text: str
    value: int
    marked: bool
    #: The fragment that owns this line, and the interpreter that fragment names, if any.
    slot: str | None = None
    interpreter: str | None = None

    def recorded_elsewhere(self, python: str) -> bool:
        """Whether this claim is a *dated record* of a run on another interpreter.

        A fragment saying "on Python 3.13.14" is evidence of what was run that day, not a claim
        about today's suite, and only that interpreter (or its own CI job) can refresh it. Holding
        a 3.14 checkout to it would make the check impossible to satisfy for anyone with one
        interpreter installed, which is how a check gets switched off instead of fixed.
        """
        return bool(self.interpreter) and self.interpreter != python

    def as_dict(self) -> dict:
        return asdict(self)


def _rel(root: Path, path: Path) -> str:
    """A document's path as the documentation writes it, so a CI failure on Ubuntu and one on
    Windows report the same string (and so the message reads like the file a reader would open)."""
    return path.relative_to(root).as_posix()


def _read(path: Path) -> str:
    """Read without newline translation.

    ``Path.read_text`` would normalise CRLF to LF on the way in and re-emit the platform's line
    ending on the way out, so writing a document back would reflow every line in it. The marked
    files are CRLF on Windows; a rewrite must leave every byte outside the markers alone.
    """
    with open(path, "r", encoding="utf-8", newline="") as handle:
        return handle.read()


def _write(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def watched(root: Path) -> list[Path]:
    """Every document allowed to quote a count: the root pages, ``docs/``, and the card.

    Discovered rather than listed, so a new document is covered the day it is added. Scratch
    copies this project leaves around (``.tmp-*``) are skipped: something whose name starts with a
    dot is not documentation.
    """
    candidates: list[Path] = []
    candidates += sorted(root.glob("*.md"))
    candidates += sorted(root.glob("docs/**/*.md"))
    candidates += sorted(root.glob("docs/**/*.html"))
    return [
        path
        for path in candidates
        if not path.is_dir()
        and not any(part.startswith(".") for part in path.relative_to(root).parts)
    ]


def _unpaired_markers(text: str) -> str | None:
    """The first marker the block regex could not pair up, i.e. a malformed fragment."""
    stripped = _BLOCK.sub("", text)
    match = _ANY_MARKER.search(stripped)
    return match.group(0) if match else None


def slots(root: Path) -> list[Slot]:
    """Every marked fragment in every watched document, with the count it carries."""
    found: list[Slot] = []
    for path in watched(root):
        rel = _rel(root, path)
        text = _read(path)
        if _unpaired_markers(text) is not None:
            raise LedgerError(
                f"{rel}: an unpaired generated marker: a fragment must be "
                "'<!-- generated: slot -->…<!-- /generated: slot -->'"
            )
        for match in _BLOCK.finditer(text):
            body = match.group("body")
            found.append(
                Slot(
                    file=rel,
                    slot=match.group("slot"),
                    line=text.count("\n", 0, match.start()) + 1,
                    content=body,
                    count=count_in(body),
                )
            )
    return found


def count_in(text: str) -> int | None:
    """The count a fragment carries.

    A ``suite`` fragment states it in prose (``**815 pass, exit 0**``); a ``suite-count`` fragment
    *is* the number, because it is going to sit inside a sentence or a chip that has to keep
    parsing. Both shapes are read here, and a fragment carrying neither is reported rather than
    silently treated as agreement.
    """
    match = _CLAIM.search(text)
    if match:
        return int(match.group("n"))
    bare = _BARE_INT.fullmatch(text.strip())
    return int(bare.group(1)) if bare else None


def _named_python(body: str) -> str | None:
    """The interpreter a fragment's own text names, if it names one."""
    named = _NAMED_PYTHON.search(body)
    return named.group(1) if named else None


def _fragment_lines(text: str) -> dict[int, tuple[str, str | None]]:
    """Line number -> (slot, interpreter named) for every line a marked fragment covers."""
    spans: dict[int, tuple[str, str | None]] = {}
    for match in _BLOCK.finditer(text):
        named = _NAMED_PYTHON.search(match.group("body"))
        info = (match.group("slot"), named.group(1) if named else None)
        first = text.count("\n", 0, match.start()) + 1
        last = text.count("\n", 0, match.end()) + 1
        for line in range(first, last + 1):
            spans[line] = info
    return spans


def exemptions(root: Path) -> list[dict]:
    """Documents that declare themselves a historical snapshot rather than a live claim.

    Matched over the whole document, not line by line: the declaration is an HTML comment, and a
    comment is allowed to wrap - reading it per line would make an exemption silently stop working
    the moment someone reflowed it.
    """
    out = []
    for path in watched(root):
        text = _read(path)
        for match in _HISTORICAL.finditer(text):
            out.append(
                {
                    "file": _rel(root, path),
                    "line": text.count("\n", 0, match.start()) + 1,
                    "text": " ".join(match.group(0).split()),
                }
            )
    return out


def claims(root: Path) -> list[Claim]:
    """Every whole-suite count in the documentation, and whether a marker owns a machine-writable
    copy of it on the same line.

    ``marked`` is informational: the check treats a hand-typed count and a marked one the same way,
    because what matters is whether the number is *true*, not who wrote it.
    """
    exempted = {e["file"] for e in exemptions(root)}
    found: list[Claim] = []
    for path in watched(root):
        rel = _rel(root, path)
        if rel in exempted:
            continue
        text = _read(path)
        fragments = _fragment_lines(text)
        for index, line in enumerate(text.splitlines(), 1):
            for match in _CLAIM.finditer(line):
                value = int(match.group("n"))
                if value < SUITE_FLOOR:
                    continue
                slot, interpreter = fragments.get(index, (None, None))
                found.append(
                    Claim(
                        file=rel,
                        line=index,
                        text=match.group(0).strip(),
                        value=value,
                        marked=slot is not None,
                        slot=slot,
                        interpreter=interpreter,
                    )
                )
    return found


# --------------------------------------------------------------------------
# the two operations
# --------------------------------------------------------------------------


def check(root: Path, facts: SuiteFacts | None = None, *, run: bool = False) -> dict:
    """Compare the documentation against a fresh measurement of the suite.

    ``ok`` is true only when every whole-suite count that speaks for *this* interpreter equals what
    the suite actually collects, and at least one marked slot exists so a future change has
    somewhere to be written rather than somewhere to be forgotten.

    A count inside a fragment that names a different interpreter is returned under ``recorded``
    instead: it is evidence of a run that happened, its own interpreter's CI job is what enforces
    it, and this run can neither verify nor repair it. It is never silently dropped - ``describe``
    prints it on every call.
    """
    facts = facts or measure(root, run=run)
    found_slots = slots(root)
    found_claims = claims(root)
    problems: list[str] = []
    recorded: list[Claim] = []
    if not [s for s in found_slots if s.owned_by_us()]:
        problems.append(
            "no generated slot found: the fastest number in this repository is not owned by a "
            "machine, so it is a number somebody has to remember"
        )
    for claim in found_claims:
        if claim.value == facts.total:
            continue
        if claim.recorded_elsewhere(facts.python):
            recorded.append(claim)
            continue
        problems.append(
            f"{claim.file}:{claim.line}: {claim.text!r} but the suite has {facts.total} tests"
        )
    held_lines = {(claim.file, claim.line) for claim in recorded}
    for slot in found_slots:
        if not slot.owned_by_us():
            continue
        if slot.count is None:
            problems.append(
                f"{slot.file}:{slot.line}: the {slot.slot!r} fragment carries no count, so the "
                "check cannot compare anything against it"
            )
        elif slot.count == facts.total:
            continue
        elif slot.recorded_elsewhere(facts.python):
            # Reported when nothing on that line already is: a fragment whose value is a record of
            # another interpreter's run must never be silently held, and must never be silent.
            if (slot.file, slot.line) not in held_lines:
                recorded.append(
                    Claim(
                        file=slot.file,
                        line=slot.line,
                        text=" ".join(slot.content.split()),
                        value=slot.count,
                        marked=True,
                        slot=slot.slot,
                        interpreter=slot.series() or _named_python(slot.content),
                    )
                )
        else:
            problems.append(
                f"{slot.file}:{slot.line}: the {slot.slot!r} fragment says {slot.count}, but the "
                f"suite has {facts.total}"
            )
    return {
        "ok": not problems,
        "facts": facts.as_dict(),
        "slots": [s.as_dict() for s in found_slots],
        "claims": [c.as_dict() for c in found_claims],
        "exemptions": exemptions(root),
        "recorded": [c.as_dict() for c in recorded],
        "problems": problems,
    }


def write(root: Path, facts: SuiteFacts, *, force: bool = False) -> dict:
    """Rewrite every marked fragment from ``facts``, touching nothing outside the markers.

    A fragment that names another interpreter - through a ``-pyXYZ`` slot or through the run it
    records - is left alone unless the facts come from that interpreter, and the skip is reported.
    Rewriting it here would turn a record of a real run on 3.13 into a claim about a run that never
    happened; ``force`` exists for the case where that is deliberate.

    A collection is written as a collection (``847 tests collected``), never as a pass: the
    wording follows the evidence. :func:`record` is the two-phase entry point that upgrades it.
    """
    # The comparison is against the interpreter that produced ``facts``, not against whatever is
    # running: what matters is which run is being written down, and that is what the caller says.
    running = facts.python
    series = _series_of(running)
    written: list[dict] = []
    skipped: list[dict] = []
    for path in watched(root):
        rel = _rel(root, path)
        text = _read(path)
        if _unpaired_markers(text) is not None:
            raise LedgerError(f"{rel}: an unpaired generated marker, refusing to write")
        names: list[str] = []
        held: list[dict] = []

        # The callback rebuilds the fragment from the marker text around the body, so the markers
        # themselves are never retyped by this module - only the text between them.
        def replacement(match: re.Match[str]) -> str:
            slot = match.group("slot")
            spec = _SLOT.match(slot)
            if not spec:
                return match.group(0)
            bound = spec.group("series")
            named = _NAMED_PYTHON.search(match.group("body"))
            if not force:
                if bound and bound.replace(".", "") != series:
                    held.append(
                        {
                            "file": rel,
                            "slot": slot,
                            "reason": f"bound to {_series_label(bound)}: rewrite it from a run "
                            "under that interpreter",
                        }
                    )
                    return match.group(0)
                if named and named.group(1) != running:
                    held.append(
                        {
                            "file": rel,
                            "slot": slot,
                            "reason": f"records a run on Python {named.group(1)} and this is "
                            f"Python {running}: rewriting it would turn a real record into a "
                            "claim about a run that never happened",
                        }
                    )
                    return match.group(0)
            names.append(slot)
            shape = "count" if spec.group("count") else "full"
            whole = match.group(0)
            body_start = match.start("body") - match.start()
            body_end = match.end("body") - match.start()
            return whole[:body_start] + facts.fragment(shape) + whole[body_end:]

        updated = _BLOCK.sub(replacement, text)
        skipped.extend(held)
        if updated != text:
            _write(path, updated)
            written.append({"file": rel, "slots": sorted(set(names))})
    return {"written": written, "skipped": skipped}


def record(root: Path, *, force: bool = False, timeout: int = 1800) -> dict:
    """Bring every document's numbers up to date, then record the run that proves them.

    Two phases, for a reason that is worth stating: **the suite contains the check that enforces
    this file.** A run started while the documents are stale goes red on that check, so a tool that
    refused to touch the ledger until the suite was green could never repair the very state it
    exists to repair.

    So the counts are made current first, from a collection - about two seconds, and the fragment
    it writes (``847 tests collected``) is true, just weaker. The run that follows is then green,
    and its record replaces the weaker wording. If the run is red for any other reason the second
    phase is skipped rather than written: the documents keep the count (which is correct), and a
    failing run does not get recorded as normal until someone asks for it with ``force``.
    """
    counted = collect(root)
    first = write(root, counted, force=force)
    ran = run_suite(root, timeout=timeout)
    second = {"written": [], "skipped": []}
    if ran.exit_code == 0 or force:
        second = write(root, ran, force=force)
    return {
        "counted": counted.as_dict(),
        "ran": ran.as_dict(),
        "written": first["written"] + second["written"],
        "skipped": second["skipped"],
        "report": check(root, ran),
    }


def describe(report: dict) -> list[str]:
    """The report as lines for a human, in the house style of the other commands."""
    facts = report["facts"]
    lines = [
        "selftrade verification ledger (the suite count is measured, never typed)",
        f"  measured  {facts['total']} tests in {facts['modules']} modules on Python "
        f"{facts['python']} ({facts['mode']}, {facts['seconds']:.1f} s, exit {facts['exit_code']})",
        f"  slots     {len(report['slots'])} marked fragment(s) in "
        f"{len({s['file'] for s in report['slots']})} file(s)",
        f"  claims    {len(report['claims'])} whole-suite count(s) in the documentation",
    ]
    for exemption in report["exemptions"]:
        lines.append(
            f"  exempt    {exemption['file']}:{exemption['line']} declares itself a historical "
            "snapshot, so its numbers are not policed"
        )
    for claim in report["recorded"]:
        lines.append(
            f"  recorded  {claim['file']}:{claim['line']} is a run on Python {claim['interpreter']} "
            f"({claim['value']} tests then, {facts['total']} now): that interpreter's own run "
            "refreshes it, this one cannot"
        )
    if report["ok"]:
        lines.append("  ok        every recorded count matches the measurement")
    else:
        lines.append("  STALE     the documentation disagrees with the suite:")
        for problem in report["problems"]:
            lines.append(f"      {problem}")
        lines.append(
            "  fix       python -m selftrade verify --ledger --write   (runs the suite, ~4 min)"
        )
    return lines
