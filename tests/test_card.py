"""The card's record: the file says what it was drawn from, and a machine checks that it still is.

``docs/img/social-preview.png`` is the one copy of the suite count a reader cannot check - it is a
picture of the number. These tests are about the ways that record fails *silently*, because the
whole reason it exists is that a human stops looking at the card once it is uploaded:

* the record must survive a round trip and must be *replaced* rather than joined by a second one:
  a file carrying two answers to the same question is worse than a file carrying none;
* every chunk the renderer wrote must come back byte for byte, because a record that quietly
  re-encoded the drawing would change the picture it claims to describe;
* a record that is corrupt - a bad CRC, a truncated file, a field that is not a number - has to be
  refused rather than read as agreement;
* a card drawn from an older source, or carrying an older count, has to fail; those are the two
  ways the picture goes stale, and neither of them touches the file's name or its size.

The drawing itself is exercised only where a browser exists (the same way the MetaTrader5 tests
assert the "package missing" path explicitly), and the last test is the CI guard: it holds *this*
repository's card to *this* repository's suite, so repairing the documents and forgetting the card
turns the build red instead of leaving a screenshot of last week's number on the front page.
"""

from __future__ import annotations

import re
import struct
import zlib
from pathlib import Path

import pytest

from selftrade import card, ledger

REPO = Path(__file__).resolve().parent.parent

#: The browser the local runs drew the committed card with, when it can be found at all.
try:
    BROWSER = card.find_chromium()
except card.CardError:  # pragma: no cover - a machine without a browser skips the drawing tests
    BROWSER = None

needs_browser = pytest.mark.skipif(BROWSER is None, reason="no Chromium to draw with")


# --------------------------------------------------------------------------
# a real, tiny PNG: the format is what is under test, not the drawing
# --------------------------------------------------------------------------


def chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def png(width: int = 4, height: int = 4, seed: int = 1) -> bytes:
    rows = []
    for y in range(height):
        row = bytearray([0])  # filter type 0: none
        for x in range(width):
            row += bytes(((x * 7 + y * 13 + seed) % 256, 12, 20))
        rows.append(bytes(row))
    return (
        card.PNG_MAGIC
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
        + chunk(b"IEND", b"")
    )


def a_record(count: int = 815, html: str = "a" * 64, **over) -> str:
    return card.record_text(
        count=count,
        source_digest=over.pop("source_digest", html),
        renderer=over.pop("renderer", "Chromium 153.0.8010.12"),
        when=over.pop("when", "2026-10-14T09:20Z"),
    )


#: The chip in the source: `<!-- generated: suite-count -->869<!-- /generated: suite-count -->`.
CHIP = re.compile(r"(generated: suite-count -->)\s*\d+\s*(<!--)")


def with_count(html: str, count: int) -> str:
    """The real card's source carrying a different number, for a test that needs one.

    Rewritten rather than typed, so the tests do not care what the suite actually collects.
    """
    return CHIP.sub(rf"\g<1>{count}\g<2>", html)


def facts(total: int = 815) -> ledger.SuiteFacts:
    return ledger.SuiteFacts(
        total=total,
        modules=34,
        python="3.14.5",
        system="Windows",
        mode="run",
        seconds=246.0,
        exit_code=0,
        failed=0,
        errors=0,
        skipped=0,
    )


@pytest.fixture
def made(tmp_path, monkeypatch):
    """A checkout in miniature: a source, a card, and a record that ties them together.

    The declared size and the byte floor are shrunk by default so the tests draw a 4x4 PNG instead
    of 1280x640; the tests that are *about* those two numbers put the real values back.
    """

    def build(
        *,
        count: int = 815,
        source: str = "<p>815 tests</p>",
        stamped: bool = True,
        drawn: tuple[int, int] = (4, 4),
        declared: tuple[int, int] | None = None,
        floor: int = 0,
        with_source: bool = True,
        with_card: bool = True,
        record_text: str | None = None,
        record_fields: dict | None = None,
    ) -> Path:
        monkeypatch.setattr(card, "SIZE", declared or drawn)
        monkeypatch.setattr(card, "FLOOR", floor)
        root = tmp_path
        (root / "docs" / "img").mkdir(parents=True, exist_ok=True)
        if with_source:
            (root / card.SOURCE).write_text(source, encoding="utf-8")
        if with_card:
            data = png(*drawn)
            if record_text is None:
                text = a_record(
                    count=count,
                    html=card.digest_of(root / card.SOURCE) if with_source else "b" * 64,
                    **(record_fields or {}),
                )
            else:
                text = record_text
            if stamped:
                data = card.stamp(data, text)
            (root / card.CARD).write_bytes(data)
        return root

    return build


# --------------------------------------------------------------------------
# the record itself
# --------------------------------------------------------------------------


def test_a_record_round_trips_through_the_file():
    data = card.stamp(png(), a_record(count=852, html="c" * 64))
    assert card.read_record(data) == {
        "count": "852",
        "html": "c" * 64,
        "renderer": "Chromium 153.0.8010.12",
        "date": "2026-10-14T09:20Z",
    }


def test_a_second_stamp_replaces_the_first_one():
    """Two records in one file is two answers to the same question; the newer one wins."""
    data = card.stamp(png(), a_record(count=809))
    data = card.stamp(data, a_record(count=815))
    texts = [
        payload
        for _, kind, payload in card.chunks(data)
        if kind == b"tEXt" and payload.startswith(card.RECORD_KEY.encode())
    ]
    assert len(texts) == 1, "the file carries more than one record"
    assert card.read_record(data)["count"] == "815"


def test_the_drawing_comes_back_byte_for_byte():
    """The record describes the drawing; it must not re-encode it."""
    drawn = png(seed=5)
    stamped = card.stamp(drawn, a_record())
    assert card.strip_record(stamped) == drawn
    original = [(kind, payload) for _, kind, payload in card.chunks(drawn)]
    after = [
        (kind, payload)
        for _, kind, payload in card.chunks(stamped)
        if not payload.startswith(card.RECORD_KEY.encode())
    ]
    assert after == original


def test_a_file_that_is_not_a_png_is_refused():
    with pytest.raises(card.CardError, match="not a PNG"):
        card.chunks(b"GIF89a\x00\x00\x00\x00")


def test_a_corrupted_chunk_is_refused():
    """A card edited by hand, or copied badly, must not be read as evidence."""
    data = bytearray(png())
    data[40] ^= 0xFF  # inside the IDAT payload
    with pytest.raises(card.CardError, match="CRC"):
        card.chunks(bytes(data))


def test_a_truncated_file_is_refused():
    with pytest.raises(card.CardError, match="truncated|no IEND"):
        card.chunks(png()[:-5])


def test_a_card_with_no_record_reads_as_none():
    assert card.read_record(png()) is None


def test_the_declared_size_comes_from_the_header():
    assert card.png_size(png(width=7, height=3)) == (7, 3)


def test_an_ihdr_that_declares_no_size_is_refused():
    """A truncated header is a broken file, not a crash in the checker."""
    broken = card.PNG_MAGIC + chunk(b"IHDR", b"\x00\x00\x02\x80") + chunk(b"IEND", b"")
    with pytest.raises(card.CardError, match="IHDR"):
        card.png_size(broken)


def test_write_refuses_a_source_whose_number_has_moved(made):
    """The ledger moves the count and the HTML is not repaired: drawing now would stamp a record of
    one number onto a picture of another, which is the failure this module exists to prevent."""
    root = made(source="<!-- generated: suite-count -->809<!-- /generated: suite-count --> tests")
    with pytest.raises(card.CardError, match="still says 809"):
        card.write(root, facts=facts(815))


def test_a_record_that_is_not_a_record_is_returned_as_it_is():
    """The reader does not validate; ``check`` reports. A reader that hid a broken record would be
    the silent failure this module exists to prevent."""
    data = card.stamp(png(), "count=eight\nrenderer=\n")
    assert card.read_record(data) == {"count": "eight", "renderer": ""}


# --------------------------------------------------------------------------
# holding the card to the tree
# --------------------------------------------------------------------------


def test_a_freshly_recorded_card_is_green(made):
    root = made(count=815)
    report = card.check(root, facts=facts(815))
    assert report["ok"], report["problems"]


def test_a_card_whose_count_has_moved_is_stale(made):
    root = made(count=809)
    report = card.check(root, facts=facts(815))
    assert not report["ok"]
    assert any("says 809 tests" in p and "collects 815" in p for p in report["problems"])


def test_a_card_drawn_from_an_older_source_is_stale(made):
    """The way this actually happens: the ledger rewrites the HTML, the PNG is left alone."""
    root = made(count=815)
    (root / card.SOURCE).write_text("<p>815 tests, revised</p>", encoding="utf-8")
    report = card.check(root, facts=facts(815))
    assert not report["ok"]
    assert any("drawn from a different" in p for p in report["problems"])


def test_a_card_without_a_record_is_stale(made):
    root = made(stamped=False)
    report = card.check(root, facts=facts(815))
    assert not report["ok"]
    assert any("carries no record" in p for p in report["problems"])


def test_a_record_with_a_missing_field_is_stale(made):
    root = made(record_text="count=815\nrenderer=Chromium\n")
    report = card.check(root, facts=facts(815))
    assert not report["ok"]
    assert any("no html or date" in p for p in report["problems"])


def test_a_count_that_is_not_a_number_is_stale(made):
    root = made(record_text="count=eight\nhtml=" + "c" * 64 + "\nrenderer=x\ndate=2026-10-14\n")
    report = card.check(root, facts=facts(815))
    assert not report["ok"]
    assert any("is not a number" in p for p in report["problems"])


def test_the_wrong_size_is_reported_against_githubs_number(made):
    root = made(drawn=(4, 4), declared=(1280, 640))
    report = card.check(root, facts=facts(815))
    assert not report["ok"]
    assert any("is 4x4" in p and "1280x640" in p for p in report["problems"])


def test_a_blank_picture_fails_the_byte_floor(made):
    """A white 1280x640 PNG is a few hundred bytes; the floor is there so a broken render cannot
    pass as a drawing."""
    root = made(floor=card.FLOOR)
    report = card.check(root, facts=facts(815))
    assert not report["ok"]
    assert any("floor" in p for p in report["problems"])


def test_a_file_that_is_not_a_png_is_a_problem_not_a_crash(made):
    root = made(with_card=False)
    (root / card.CARD).write_bytes(b"not a picture at all")
    report = card.check(root, facts=facts(815))
    assert not report["ok"]
    assert any("not a PNG" in p for p in report["problems"])


def test_a_missing_card_is_reported_by_name(made):
    root = made(with_card=False)
    report = card.check(root, facts=facts(815))
    assert not report["ok"]
    assert any(card.CARD in p for p in report["problems"])


def test_a_missing_source_is_reported(made):
    root = made(with_source=False)
    report = card.check(root, facts=facts(815))
    assert not report["ok"]
    assert any(card.SOURCE in p for p in report["problems"])


def test_the_fix_a_reader_is_given_names_the_command_that_draws_it(made):
    root = made(count=809)
    lines = card.describe(card.check(root, facts=facts(815)))
    assert any("verify --card --write" in line for line in lines)
    assert any(card.SOURCE in line for line in lines)


# --------------------------------------------------------------------------
# naming a browser
# --------------------------------------------------------------------------


def test_the_browser_that_is_named_is_the_browser_that_is_used(tmp_path):
    fake = tmp_path / "chrome.exe"
    fake.write_bytes(b"")
    assert card.find_chromium(str(fake)) == str(fake)


def test_the_environment_can_name_it_too(tmp_path, monkeypatch):
    fake = tmp_path / "somewhere" / "chromium"
    fake.parent.mkdir(parents=True)
    fake.write_bytes(b"")
    monkeypatch.setenv(card.CHROME_ENV, str(fake))
    assert card.find_chromium() == str(fake)


def test_the_environment_never_wins_over_an_explicit_path(tmp_path, monkeypatch):
    named = tmp_path / "named.exe"
    named.write_bytes(b"")
    monkeypatch.setenv(card.CHROME_ENV, str(tmp_path / "elsewhere.exe"))
    assert card.find_chromium(str(named)) == str(named)


def test_a_named_browser_that_is_not_there_is_refused(tmp_path):
    with pytest.raises(card.CardError, match="not a browser"):
        card.find_chromium(str(tmp_path / "missing.exe"))


def test_the_renderer_is_named_from_its_own_report(monkeypatch):
    """``--version`` is parsed, not trusted: Chrome prints a product line, a bundled build prints
    nothing and gets identified by its file name instead."""

    class Proc:
        stdout = "Google Chrome 143.0.3650.96\n"
        stderr = ""

    monkeypatch.setattr(card.subprocess, "run", lambda *a, **k: Proc())
    assert card.renderer_name("whatever.exe") == "Google Chrome 143.0.3650.96"


def test_a_browser_that_will_not_say_its_version_is_still_usable(monkeypatch):
    """The probe must not hang the command: some builds launch a window instead of answering."""

    def hang(*args, **kwargs):
        raise card.subprocess.TimeoutExpired(cmd=args[0], timeout=5)

    monkeypatch.setattr(card.subprocess, "run", hang)
    assert card.renderer_name("/somewhere/chrome.exe") == "chrome.exe"


def test_the_message_for_no_browser_says_how_to_name_one(monkeypatch):
    monkeypatch.delenv(card.CHROME_ENV, raising=False)
    monkeypatch.setattr(card.shutil, "which", lambda name: None)
    monkeypatch.setattr(card, "WELL_KNOWN", ())
    with pytest.raises(card.CardError, match=card.CHROME_ENV):
        card.find_chromium()


def test_asking_for_a_render_without_a_browser_is_unmeasurable(monkeypatch, tmp_path):
    """``verify --card --render`` has to say "no browser" (exit 2), not "the card drifted" (exit 1)."""
    from selftrade.cli import main

    monkeypatch.setattr(card.shutil, "which", lambda name: None)
    monkeypatch.setattr(card, "WELL_KNOWN", ())
    monkeypatch.delenv(card.CHROME_ENV, raising=False)
    assert main(["verify", "--card", "--render"]) == 2


# --------------------------------------------------------------------------
# drawing it, where there is something to draw with
# --------------------------------------------------------------------------


@needs_browser
def test_the_source_draws_at_the_declared_size(tmp_path):
    out = tmp_path / "card.png"
    drawn = card.draw(REPO / card.SOURCE, out)
    assert drawn["size"] == list(card.SIZE)
    assert drawn["bytes"] >= card.FLOOR


@needs_browser
def test_write_records_the_count_the_source_and_the_renderer(tmp_path):
    root = tmp_path
    (root / "docs" / "img").mkdir(parents=True)
    (root / card.SOURCE).write_text(
        with_count((REPO / card.SOURCE).read_text(encoding="utf-8"), 815), encoding="utf-8"
    )
    written = card.write(root, facts=facts(815))
    assert written["count"] == 815 and written["bytes"] > card.FLOOR
    stored = card.read_record((root / card.CARD).read_bytes())
    assert stored["count"] == "815"
    assert stored["html"] == card.digest_of(root / card.SOURCE)
    assert stored["renderer"] and re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}Z", stored["date"])
    assert card.check(root, facts=facts(815))["ok"]


@needs_browser
def test_a_re_drawing_is_compared_and_reported_not_required(tmp_path):
    """The measurement that removed the equality flag: this browser on this machine drew the same
    source into two files that differed in 173 060 bytes in one run and matched byte for byte in
    another. So a re-drawing is *compared* rather than required: whatever the pixels do, the count
    and the digest are what can fail a build."""
    root = tmp_path
    (root / "docs" / "img").mkdir(parents=True)
    source = with_count((REPO / card.SOURCE).read_text(encoding="utf-8"), 815)
    (root / card.SOURCE).write_text(source, encoding="utf-8")
    card.write(root, facts=facts(815))
    report = card.check(root, facts=facts(815), render=True)
    assert report["ok"], report["problems"]
    assert report["render"]["size"] == list(card.SIZE)
    assert isinstance(report["render"]["identical"], bool)
    assert report["render"]["differing_bytes"] >= 0


def test_a_byte_difference_in_a_fresh_drawing_is_reported_and_not_fatal(made, monkeypatch, tmp_path):
    """The half of the comparison that has nothing to do with drawing: whatever the pixels do, the
    answer carries the difference and the check still turns on the record."""
    root = made(count=815)
    elsewhere = png(seed=99)  # a different drawing of the same size, which is all it has to be

    def draw_differently(source, out, **kwargs):
        out.write_bytes(elsewhere)
        return {
            "bytes": len(elsewhere),
            "size": [4, 4],
            "renderer": "elsewhere",
            "exe": "elsewhere",
        }

    monkeypatch.setattr(card, "draw", draw_differently)
    monkeypatch.setattr(card, "text_of", lambda source, **kwargs: "815 tests")
    report = card.check(root, facts=facts(815), render=True)
    assert report["ok"], report["problems"]
    assert report["render"]["identical"] is False
    assert report["render"]["differing_bytes"] > 0
    assert any("bytes differ" in line for line in card.describe(report))


# --------------------------------------------------------------------------
# this repository
# --------------------------------------------------------------------------


def test_this_repositorys_card_carries_this_repositorys_count():
    """The CI guard. Adding tests moves the suite count, so the card has to be drawn again: this is
    the test that says so, in the same way the ledger's own guard does for the text."""
    facts_here = ledger.collect(REPO)
    report = card.check(REPO, facts=facts_here)
    assert report["ok"], "\n".join(report["problems"])
    assert report["card"]["record"]["count"] == str(facts_here.total)
    assert report["card"]["size"] == list(card.SIZE)
