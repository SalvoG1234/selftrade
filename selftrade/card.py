"""The social preview card, and the record of the tree it was drawn from.

``docs/img/social-preview.png`` is the one copy of the suite count that a reader cannot check. It is
a 1280x640 raster of ``docs/img/social-preview.html``, and the number lives in a chip drawn into the
pixels. ``ledger`` holds every *textual* copy of that count to the suite - the README, the
contributing rules, the verification ledger, the launch kit, the card's own source - but it cannot
see through a PNG. So a ``--write`` can repair the HTML the card is rendered from and leave the
committed picture showing last week's number, and nothing in the repository notices: the drift that
this whole series exists to make impossible survives in the one file that is uploaded to GitHub.

The fix is the one this repository already uses for the suite count itself: the artifact carries a
record of what it was made from, and a machine holds it to that. The renderer writes a ``tEXt``
chunk into the PNG::

    count=852
    html=<sha256 of docs/img/social-preview.html>
    renderer=Chromium 153.0.8010.12
    date=2026-10-14T09:20Z

``check`` then compares that record with this tree - the count against the *measured* suite, the
digest against the source as it is now - and fails when either has moved. It needs no browser, no
network and about two seconds, so it runs anywhere the matrix runs.

What it deliberately does not do is compare pixels. Font rasterisation differs between machines and
between Chrome releases, so a freshly drawn card is never byte-identical to one drawn elsewhere: a
requirement of byte equality would go red for reasons that have nothing to do with the number, and a
check that cannot be satisfied gets deleted rather than fixed. ``--render`` therefore draws the
source again and requires what *is* portable - 1280x640, not blank, and carrying the measured count
in the rendered text - and *reports* the byte difference instead of failing on it.

That last part is a measurement, not a precaution. While this module was written, the same browser
drew the same source on the same machine into files that differed in 173 060 bytes in one run and
matched byte for byte in another: a re-drawing is not reliably reproducible, even locally, so an
equality gate would go red for reasons that are not drift. There was a ``--strict`` flag that
enforced it, and it is gone - what the same automation *can* promise is the count and the digest,
and those are what is enforced.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from datetime import datetime, timezone
from pathlib import Path

from . import ledger

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
#: The ``tEXt`` keyword the record is written under. PNG text chunks are Latin-1, so the payload is
#: ``key=value`` lines: readable with ``strings``, and ignored by every viewer and by GitHub.
RECORD_KEY = "selftrade-card"
#: Where the card and its source live, relative to the checkout the command is run in.
CARD = "docs/img/social-preview.png"
SOURCE = "docs/img/social-preview.html"
#: GitHub's social preview, and the only size this card is allowed to be.
SIZE = (1280, 640)
#: A blank 1280x640 PNG compresses to a few hundred bytes; a real drawing of this card does not.
#: The same kind of floor ``tests/test_docs.py`` puts under the dashboard screenshots.
FLOOR = 20_000
#: The fields a record has to carry before it can be held to anything.
REQUIRED = ("count", "html", "renderer", "date")

#: Browsers, in the order they are preferred. ``google-chrome`` first because that is what a
#: GitHub-hosted Ubuntu runner has; the rest are what a person is likely to have installed.
BROWSERS = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "chrome",
    "msedge",
    "chrome.exe",
    "msedge.exe",
)
#: Locations that are not on ``PATH``. Kept short and explicit: a browser this module cannot find is
#: a message telling the caller to name one, not a search of the whole filesystem.
WELL_KNOWN = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)
#: Set this instead of passing ``--chrome``: a path, or the name of a binary on ``PATH``.
CHROME_ENV = "SELFTRADE_CHROME"


class CardError(RuntimeError):
    """The card could not be read, drawn or recorded, so nothing about it can be said."""


# --------------------------------------------------------------------------
# the record inside the file
# --------------------------------------------------------------------------


def chunks(data: bytes) -> list[tuple[int, bytes, bytes]]:
    """``(offset, type, payload)`` for every chunk, with each CRC verified.

    The CRC is checked rather than trusted because the whole point of this file is that it is
    evidence: a PNG whose bytes were edited by hand, or truncated by a bad copy, must fail here
    instead of being read as a record.
    """
    if not data.startswith(PNG_MAGIC):
        raise CardError("not a PNG: the 8-byte signature is missing")
    found: list[tuple[int, bytes, bytes]] = []
    pos = len(PNG_MAGIC)
    while pos < len(data):
        if pos + 8 > len(data):
            raise CardError("truncated PNG: a chunk header ends past the end of the file")
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        kind = data[pos + 4 : pos + 8]
        end = pos + 12 + length
        if end > len(data):
            raise CardError(f"truncated PNG: the {kind!r} chunk runs past the end of the file")
        payload = data[pos + 8 : pos + 8 + length]
        (crc,) = struct.unpack(">I", data[pos + 8 + length : end])
        if crc != zlib.crc32(kind + payload) & 0xFFFFFFFF:
            raise CardError(f"{kind!r}: the chunk CRC does not match its contents")
        found.append((pos, kind, payload))
        if kind == b"IEND":
            break
        pos = end
    if not found or found[-1][1] != b"IEND":
        raise CardError("no IEND chunk: the PNG is incomplete")
    return found


def png_size(data: bytes) -> tuple[int, int]:
    """The declared size, from the ``IHDR`` the format puts first."""
    found = chunks(data)
    if found[0][1] != b"IHDR":
        raise CardError("the first chunk is not IHDR: this is not a PNG image")
    if len(found[0][2]) < 8:
        raise CardError("the IHDR chunk is too short to declare a size: this is not a PNG image")
    width, height = struct.unpack(">II", found[0][2][:8])
    return width, height


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def record_text(count: int, source_digest: str, renderer: str, when: str) -> str:
    return f"count={count}\nhtml={source_digest}\nrenderer={renderer}\ndate={when}"


def read_record(data: bytes) -> dict[str, str] | None:
    """The record this card carries, or ``None`` when it carries none.

    Nothing is validated here: an incomplete or nonsensical record is returned as it is, and
    :func:`check` is what reports it. A reader that silently returned ``{}`` for a corrupt record
    would be the silent failure this module exists to prevent.
    """
    for _, kind, payload in chunks(data):
        if kind != b"tEXt" or b"\x00" not in payload:
            continue
        keyword, text = payload.split(b"\x00", 1)
        if keyword.decode("latin-1") != RECORD_KEY:
            continue
        out: dict[str, str] = {}
        for line in text.decode("latin-1").splitlines():
            if "=" in line:
                name, value = line.split("=", 1)
                out[name.strip()] = value.strip()
        return out
    return None


def _strip(data: bytes) -> tuple[bytes, bool]:
    """The drawing without the record: the bytes as the renderer wrote them.

    This is what makes a re-drawing comparable at all - the committed card carries a record and a
    fresh render does not, so the two are only comparable once both have had it removed. Comparing
    them is reported, never enforced: see the module docstring for why.
    """
    out = bytearray(PNG_MAGIC)
    dropped = False
    for _, kind, payload in chunks(data):
        if kind == b"tEXt" and payload.split(b"\x00", 1)[0].decode("latin-1") == RECORD_KEY:
            dropped = True
            continue
        out += _chunk(kind, payload)
    return bytes(out), dropped


def strip_record(data: bytes) -> bytes:
    return _strip(data)[0]


def stamp(data: bytes, record: str) -> bytes:
    """Write the record into the card, immediately before ``IEND``.

    Every other chunk is re-emitted byte for byte, CRCs included: the pixels are exactly what the
    renderer drew, and only a ``tEXt`` chunk is added. An older record is replaced rather than
    joined by a second one, so the file never carries two answers to the same question.
    """
    payload = RECORD_KEY.encode("latin-1") + b"\x00" + record.encode("latin-1")
    if len(RECORD_KEY) > 79:
        raise CardError("the tEXt keyword is longer than the format allows")
    out = bytearray(PNG_MAGIC)
    written = False
    for _, kind, body in chunks(data):
        if kind == b"tEXt" and body.split(b"\x00", 1)[0].decode("latin-1") == RECORD_KEY:
            if not written:
                out += _chunk(b"tEXt", payload)
                written = True
            continue
        if kind == b"IEND" and not written:
            out += _chunk(b"tEXt", payload)
            written = True
        out += _chunk(kind, body)
    return bytes(out)


def digest_of(path: Path) -> str:
    """The SHA-256 of the source, with line endings normalised.

    A checkout is not the file. With ``core.autocrlf=true`` - the default on Windows - the working
    tree holds CRLF while the repository stores LF, and hashing the bytes on disk made the record
    mean "drawn from this source" on the machine that drew the card and "the source changed after
    the card was drawn" on every runner that did not. It was measured rather than reasoned about:
    the first push of a card drawn here went red on all three ubuntu jobs while the Windows ones
    stayed green, because the check was answering a question about the checkout.

    Both sites - the record the renderer writes and the check that reads it - come through here, so
    they cannot disagree about what the source was. The file on disk is never rewritten: only what
    is hashed is normalised.
    """
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


# --------------------------------------------------------------------------
# drawing it
# --------------------------------------------------------------------------


def find_chromium(explicit: str | None = None) -> str:
    """The browser to draw with: what the caller named, the environment, or a well-known one.

    Naming one is the escape hatch that keeps this command usable on a machine with an unusual
    install, and in CI it is how the runner's own ``google-chrome`` is found.
    """
    for candidate in (explicit, os.environ.get(CHROME_ENV)):
        if not candidate:
            continue
        found = shutil.which(candidate) or (candidate if Path(candidate).is_file() else None)
        if found:
            return found
        raise CardError(f"{candidate!r} is not a browser this can run: check the path")
    for name in BROWSERS:
        found = shutil.which(name)
        if found:
            return found
    for place in WELL_KNOWN:
        if Path(place).is_file():
            return place
    raise CardError(
        "no Chromium anywhere this can find: install one, or name it with --chrome or "
        f"${CHROME_ENV}"
    )


def _argv(exe: str, *extra: str, url: str) -> list[str]:
    argv = [exe, "--headless", "--disable-gpu", "--hide-scrollbars"]
    if sys.platform.startswith("linux") and hasattr(os, "geteuid") and os.geteuid() == 0:
        # A container runs as root with no usable sandbox; the same flag the Docker-based
        # screenshot recipes use. Everywhere else the sandbox stays on.
        argv.append("--no-sandbox")
    return argv + list(extra) + [url]


def renderer_name(exe: str) -> str:
    """``Chromium 153.0.8010.12``: which browser drew it, for the record and for a byte diff that
    someone will one day have to explain.

    Probed under ``--headless`` and with a short budget, because ``--version`` is not a promise: a
    bundled Chromium that does not understand it launches a browser window instead of printing a
    version and never exits. A name read off a five-second probe is worth having; a stray window and
    a hung command are not, and the executable's own name is a perfectly good record either way.
    """
    try:
        proc = subprocess.run(
            [exe, "--headless", "--disable-gpu", "--version"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return Path(exe).name
    for line in proc.stdout.splitlines():
        if re.search(r"\d+\.\d+\.\d+\.\d+", line) and "ERROR" not in line:
            return line.strip()[:80]
    return Path(exe).name


def draw(source: Path, out: Path, *, chrome: str | None = None, timeout: int = 240) -> dict:
    """Draw ``source`` at :data:`SIZE` into ``out``, and refuse anything that is not that.

    Called ``draw`` and not ``render`` because :func:`check` takes a ``render`` flag that means "draw
    it again and compare" - and the flag shadowed this function when it was called ``render``, so
    the one code path that re-draws anything raised *'bool' object is not callable*. A checker that
    breaks when it is asked to check is exactly what the tests around this module are for.

    An absolute output path and a ``file://`` URL, because a relative ``--screenshot`` is resolved
    against whatever directory the browser decides it is in, which is how a render silently writes
    nothing.
    """
    exe = find_chromium(chrome)
    out.parent.mkdir(parents=True, exist_ok=True)
    argv = _argv(
        exe,
        f"--window-size={SIZE[0]},{SIZE[1]}",
        "--virtual-time-budget=3000",
        f"--screenshot={out.resolve()}",
        url=source.resolve().as_uri(),
    )
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise CardError(f"the browser did not finish drawing the card within {timeout}s") from exc
    if not out.exists():
        tail = (proc.stdout + proc.stderr).strip().splitlines()[-6:]
        raise CardError(
            f"the browser exited {proc.returncode} without writing {out.name}:\n  "
            + "\n  ".join(tail)
        )
    data = out.read_bytes()
    size = png_size(data)
    if size != SIZE:
        raise CardError(f"the render came out {size[0]}x{size[1]}, not {SIZE[0]}x{SIZE[1]}")
    if len(data) < FLOOR:
        raise CardError(
            f"the render is only {len(data)} bytes: that is a blank page, not the card"
        )
    return {"bytes": len(data), "size": list(size), "renderer": renderer_name(exe), "exe": exe}


def text_of(source: Path, *, chrome: str | None = None, timeout: int = 120) -> str:
    """The rendered text of the source.

    ``--dump-dom`` rather than a pixel comparison: the serialized document is the same string on
    every host, and it is enough to know that the browser loaded the file and that the chip is in
    what it drew.
    """
    exe = find_chromium(chrome)
    argv = _argv(exe, "--dump-dom", "--virtual-time-budget=3000", url=source.resolve().as_uri())
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:  # pragma: no cover - a hung browser is the host
        raise CardError(f"the browser did not return the document within {timeout}s") from exc
    if proc.returncode != 0 and not proc.stdout.strip():
        raise CardError(f"the browser exited {proc.returncode} without returning the document")
    return proc.stdout


# --------------------------------------------------------------------------
# the two operations
# --------------------------------------------------------------------------


def source_count(root: Path) -> int | None:
    """The number the card's source prints into its own chip, read with the ledger's own parser.

    ``None`` when the source carries no marked fragment, which is what a document that is not this
    repository's card looks like - and is not a reason to refuse to draw it.
    """
    for slot in ledger.slots(root):
        if slot.file == SOURCE and slot.slot.startswith("suite"):
            return slot.count
    return None


def write(root: Path, *, chrome: str | None = None, facts: ledger.SuiteFacts | None = None) -> dict:
    """Draw the card from the source in ``root`` and record what it was drawn from.

    The count recorded is the *measured* suite, not the number in the HTML, because that is the
    claim the card makes to a reader: it is not "the source says this", it is "this repository has
    this many tests". When the two disagree, ``check`` reports it and the ledger has already failed.
    """
    facts = facts or ledger.collect(root)
    source = root / SOURCE
    if not source.exists():
        raise CardError(f"{SOURCE} is missing: there is nothing to draw the card from")
    printed = source_count(root)
    if printed is not None and printed != facts.total:
        raise CardError(
            f"{SOURCE} still says {printed} while the suite collects {facts.total}: repair the "
            "documents first (`python -m selftrade verify --ledger --write`), or the card would "
            "carry a record of a number its own picture does not show"
        )
    with tempfile.TemporaryDirectory(prefix="selftrade-card-") as tmp:
        drawn = draw(source, Path(tmp) / "card.png", chrome=chrome)
        data = (Path(tmp) / "card.png").read_bytes()
    record = record_text(
        count=facts.total,
        source_digest=digest_of(source),
        renderer=drawn["renderer"],
        when=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
    )
    stamped = stamp(data, record)
    target = root / CARD
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(stamped)
    return {
        "png": CARD,
        "bytes": len(stamped),
        "size": list(drawn["size"]),
        "count": facts.total,
        "renderer": drawn["renderer"],
        "html": digest_of(source),
    }


def check(
    root: Path,
    *,
    facts: ledger.SuiteFacts | None = None,
    chrome: str | None = None,
    render: bool = False,
) -> dict:
    """Hold the committed card to this tree.

    ``ok`` is true only when the picture is a 1280x640 PNG above the byte floor, carries a complete
    record, and that record's count is what the suite collects and its digest is the source as it is
    now. With ``render``, the source is drawn again too, and the difference between the two drawings
    is reported in the answer - never turned into a problem, because a re-drawing is not
    reproducible even on the machine and browser that drew the committed one.
    """
    facts = facts or ledger.collect(root)
    source = root / SOURCE
    target = root / CARD
    problems: list[str] = []
    card: dict = {"png": CARD, "html": SOURCE, "record": None, "bytes": None, "size": None}
    drawn: dict = {}

    if not source.exists():
        problems.append(f"{SOURCE} is missing: the card has no source in this tree")
    if not target.exists():
        problems.append(
            f"{CARD} is missing: the count in the documentation is quoted by a card that is not here"
        )
        return {
            "ok": False,
            "card": card,
            "facts": facts.as_dict(),
            "expected": {"count": facts.total, "html": digest_of(source) if source.exists() else None},
            "render": drawn,
            "problems": problems,
        }

    data = target.read_bytes()
    card["bytes"] = len(data)
    try:
        size = png_size(data)
        card["size"] = list(size)
        if size != SIZE:
            problems.append(
                f"{CARD} is {size[0]}x{size[1]}: GitHub's social preview is "
                f"{SIZE[0]}x{SIZE[1]}"
            )
        if len(data) < FLOOR:
            problems.append(
                f"{CARD} is {len(data)} bytes, under the {FLOOR}-byte floor: that is a blank "
                "picture, not a drawing of the card"
            )
        record = read_record(data)
    except CardError as exc:
        problems.append(f"{CARD}: {exc}")
        record = None
    card["record"] = record

    if record is None:
        problems.append(
            f"{CARD} carries no record: it was not drawn by `python -m selftrade verify --card "
            "--write`, so nothing ties it to this tree"
        )
    else:
        missing = [name for name in REQUIRED if name not in record]
        if missing:
            problems.append(f"{CARD}: the record has no {' or '.join(missing)}")
        if record.get("count") is not None:
            try:
                recorded = int(record["count"])
            except ValueError:
                problems.append(f"{CARD}: the record's count {record['count']!r} is not a number")
            else:
                if recorded != facts.total:
                    problems.append(
                        f"{CARD}: the record says {recorded} tests, the suite collects "
                        f"{facts.total}"
                    )
        if source.exists() and record.get("html") and record["html"] != digest_of(source):
            problems.append(
                f"{CARD}: drawn from a different {SOURCE} - the source changed after the card was "
                "drawn"
            )

    if render:
        try:
            with tempfile.TemporaryDirectory(prefix="selftrade-card-") as tmp:
                fresh_path = Path(tmp) / "card.png"
                drawn = draw(source, fresh_path, chrome=chrome)
                fresh = fresh_path.read_bytes()
                document = text_of(source, chrome=chrome)
        except CardError as exc:
            problems.append(f"the source does not draw: {exc}")
        else:
            mine, theirs = strip_record(data), fresh
            drawn["identical"] = mine == theirs
            drawn["differing_bytes"] = abs(len(mine) - len(theirs)) + sum(
                1 for a, b in zip(mine, theirs) if a != b
            )
            if f"{facts.total}" not in document:
                problems.append(
                    f"the rendered source does not contain the measured count {facts.total}"
                )

    return {
        "ok": not problems,
        "card": card,
        "facts": facts.as_dict(),
        "expected": {
            "count": facts.total,
            "html": digest_of(source) if source.exists() else None,
        },
        "render": drawn,
        "problems": problems,
    }


def describe(report: dict) -> list[str]:
    """The report as lines for a human, in the house style of the other commands."""
    card = report["card"]
    facts = report["facts"]
    size = card["size"]
    lines = [
        "selftrade social card (the picture records the tree it was drawn from)",
        f"  card      {card['png']}"
        + (f", {size[0]}x{size[1]}, {card['bytes']} bytes" if size else ""),
        f"  suite     {facts['total']} tests in {facts['modules']} modules on Python "
        f"{facts['python']} ({facts['mode']}, {facts['seconds']:.1f} s, exit {facts['exit_code']})",
    ]
    record = card["record"]
    if record:
        lines.append(
            f"  record    {record.get('count', '?')} tests, {card['html']} "
            f"{str(record.get('html', '?'))[:12]}..., by {record.get('renderer', '?')} on "
            f"{record.get('date', '?')}"
        )
    else:
        lines.append("  record    none: nothing in the file says what it was drawn from")
    drawn = report.get("render") or {}
    if drawn:
        same = "identical" if drawn.get("identical") else f"{drawn.get('differing_bytes')} bytes differ"
        lines.append(
            f"  render    drawn again by {drawn['renderer']}: {same} (reported, not enforced: "
            "rasterisation is not reproducible, even here - the record is what is enforced)"
        )
    else:
        lines.append("  render    not attempted (--render draws it again and compares)")
    if report["ok"]:
        lines.append("  ok        the picture was drawn from this source, and it carries today's count")
    else:
        lines.append("  STALE     the card disagrees with the tree:")
        for problem in report["problems"]:
            lines.append(f"      {problem}")
        lines.append(
            "  fix       python -m selftrade verify --card --write   (draws the card from "
            f"{SOURCE})"
        )
    return lines


def stamp_report(report: dict) -> list[str]:
    """What ``--write`` did, in the same voice as the ledger's own ``write`` output."""
    return [
        f"  drew  {report['png']} ({report['size'][0]}x{report['size'][1]}, {report['bytes']} bytes)",
        f"  record count={report['count']} html={report['html'][:12]}... "
        f"renderer={report['renderer']}",
    ]
