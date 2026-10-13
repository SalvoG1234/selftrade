"""The verification ledger's two promises: it measures, and it notices.

A tool that keeps documentation honest has to be honest itself, so these tests are about the ways
a checker like this fails *silently* - which is worse than failing loudly, because the whole point
is that a human stops looking:

* the collection parser must not decide the suite is empty when pytest changes the shape of its
  output (a checker that counts zero tests passes forever);
* ``write`` must not touch a byte outside its markers, line endings included, or the first run
  would bury the real change in a whole-file diff;
* ``write`` must not overwrite a fragment recorded under another interpreter, because that would
  turn a record of a real run into a claim about a run that never happened;
* a count typed by hand somewhere the tool does not own must still fail the check, because the row
  a person forgets is never the row they remember.

The last test is the one that matters in CI: it runs the real check against this checkout, so
adding tests and forgetting the documents turns the build red instead of leaving five documents
quietly claiming an old number.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from selftrade import ledger

REPO = Path(__file__).resolve().parent.parent


def facts(total: int = 815, modules: int = 34, **over) -> ledger.SuiteFacts:
    base = dict(
        total=total,
        modules=modules,
        python="3.14.5",
        system="Windows",
        mode="run",
        seconds=219.0,
        exit_code=0,
        failed=0,
        errors=0,
        skipped=0,
    )
    base.update(over)
    return ledger.SuiteFacts(**base)


def repo_tree(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, body in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(textwrap.dedent(body).lstrip("\n"))
    return tmp_path


# --------------------------------------------------------------------------
# measuring
# --------------------------------------------------------------------------


def test_the_per_file_collection_shape_is_read():
    out = "tests/test_a.py: 3\ntests/test_b.py: 2\n"
    assert ledger.parse_collection(out) == (5, 2)


def test_the_node_id_collection_shape_is_read():
    out = "tests/test_a.py::test_one\ntests/test_a.py::test_two\ntests/test_b.py::test_three\n"
    assert ledger.parse_collection(out) == (3, 2)


def test_a_collection_output_that_changed_shape_raises_instead_of_counting_zero():
    """The dangerous failure: a parser that stops matching would report an empty suite."""
    with pytest.raises(ledger.LedgerError):
        ledger.parse_collection("no tests ran in 0.01s\n")


def test_a_junit_report_is_read_with_its_failures():
    xml = textwrap.dedent(
        """
        <testsuites><testsuite name="pytest" tests="4" failures="1" errors="0" skipped="1"
                               time="1.5">
          <testcase classname="tests.test_a" name="test_ok" file="tests/test_a.py"/>
          <testcase classname="tests.test_a" name="test_ok2" file="tests/test_a.py"/>
          <testcase classname="tests.test_b" name="test_ok3" file="tests/test_b.py"/>
          <testcase classname="tests.test_b" name="test_bad" file="tests/test_b.py"/>
        </testsuite></testsuites>
        """
    ).strip()
    assert ledger.parse_junit(xml) == {
        "tests": 4,
        "failures": 1,
        "errors": 0,
        "skipped": 1,
        "modules": 2,
    }


def test_a_junit_report_without_a_file_attribute_still_counts_modules():
    xml = (
        '<testsuites><testsuite tests="2" failures="0" errors="0" skipped="0">'
        '<testcase classname="tests.test_a" name="one"/>'
        '<testcase classname="tests.test_b" name="two"/>'
        "</testsuite></testsuites>"
    )
    assert ledger.parse_junit(xml)["modules"] == 2


# --------------------------------------------------------------------------
# the fragment
# --------------------------------------------------------------------------


def test_the_full_fragment_records_the_run():
    text = facts().fragment()
    assert "**815 pass, exit 0**" in text
    assert "34 test modules" in text
    assert "Python 3.14.5" in text


def test_the_count_fragment_is_only_the_number():
    assert facts().fragment("count") == "815"


def test_a_failing_run_says_so_rather_than_saying_pass():
    text = facts(total=10, failed=3, exit_code=1).fragment()
    assert "**3 of 10 failed, exit 1**" in text


def test_a_collection_fragment_never_claims_that_anything_passed():
    """A collection proves a count. It cannot support the word 'pass'."""
    text = facts(mode="collected", failed=None, errors=None, skipped=None).fragment()
    assert "collected" in text
    assert "pass" not in text


def test_a_count_is_read_out_of_both_fragment_shapes():
    assert ledger.count_in(facts().fragment()) == 815
    assert ledger.count_in(facts().fragment("count")) == 815
    assert ledger.count_in("nothing numeric here") is None


# --------------------------------------------------------------------------
# check
# --------------------------------------------------------------------------


def test_a_matching_document_passes(tmp_path):
    root = repo_tree(
        tmp_path,
        {
            "README.md": "| pytest | <!-- generated: suite -->**815 pass, exit 0**<!-- /generated: suite --> |\n",
        },
    )
    report = ledger.check(root, facts())
    assert report["ok"], report["problems"]


def test_a_marked_fragment_that_drifted_fails(tmp_path):
    root = repo_tree(
        tmp_path,
        {
            "README.md": "| pytest | <!-- generated: suite -->**809 pass, exit 0**<!-- /generated: suite --> |\n",
        },
    )
    report = ledger.check(root, facts())
    assert not report["ok"]
    assert any("809" in problem and "815" in problem for problem in report["problems"])


def test_a_count_typed_by_hand_fails_even_though_no_marker_owns_it(tmp_path):
    """The row somebody edits by hand is exactly the row this exists to catch."""
    root = repo_tree(
        tmp_path,
        {
            "README.md": "<!-- generated: suite -->**815 pass, exit 0**<!-- /generated: suite -->\n",
            "docs/LAUNCH.md": "a draft someone will paste: 785 tests, run in CI.\n",
        },
    )
    report = ledger.check(root, facts())
    assert not report["ok"]
    assert any(c["file"] == "docs/LAUNCH.md" and c["value"] == 785 for c in report["claims"])
    assert any("docs/LAUNCH.md:1" in problem for problem in report["problems"])


def test_a_module_level_count_is_not_mistaken_for_the_suite(tmp_path):
    """The ledger's own coverage rows say "31 tests" about one module. Those are not claims."""
    root = repo_tree(
        tmp_path,
        {
            "README.md": "<!-- generated: suite -->**815 pass, exit 0**<!-- /generated: suite -->\n",
            "docs/VERIFICATION.md": "| stress test module | 31 tests, seed reproducibility, clamping |\n",
        },
    )
    report = ledger.check(root, facts())
    assert report["ok"], report["problems"]
    # The only count in either document is the one the marker owns.
    assert [claim["value"] for claim in report["claims"]] == [815]


def test_a_fragment_naming_another_interpreter_is_reported_not_enforced(tmp_path):
    """A run recorded on 3.13 is evidence, not a claim about today: only 3.13's own job can
    refresh it, and holding a 3.14 checkout to it would make the check unsatisfiable."""
    root = repo_tree(
        tmp_path,
        {
            "README.md": "<!-- generated: suite -->**851 pass, exit 0**<!-- /generated: suite -->\n",
            "docs/VERIFICATION.md": "| 3.13 | <!-- generated: suite-py313 -->**815 pass, exit 0**, "
            "on Python 3.13.14<!-- /generated: suite-py313 --> |\n",
        },
    )
    report = ledger.check(root, facts(total=851, python="3.14.5"))
    assert report["ok"], report["problems"]
    assert [claim["slot"] for claim in report["recorded"]] == ["suite-py313"]
    assert any("3.13.14" in line for line in ledger.describe(report))


def test_that_fragment_is_enforced_when_the_run_is_on_that_interpreter(tmp_path):
    root = repo_tree(
        tmp_path,
        {
            "README.md": "<!-- generated: suite -->**851 pass, exit 0**<!-- /generated: suite -->\n",
            "docs/VERIFICATION.md": "| 3.13 | <!-- generated: suite-py313 -->**815 pass, exit 0**, "
            "on Python 3.13.14<!-- /generated: suite-py313 --> |\n",
        },
    )
    report = ledger.check(root, facts(total=851, python="3.13.14"))
    assert not report["ok"]
    assert any("815" in problem for problem in report["problems"])
    assert report["recorded"] == []


def test_a_checkout_with_no_marker_at_all_fails(tmp_path):
    root = repo_tree(tmp_path, {"README.md": "a readme with no generated slot\n"})
    report = ledger.check(root, facts())
    assert not report["ok"]
    assert any("no generated slot" in problem for problem in report["problems"])


def test_a_marker_that_carries_no_count_fails(tmp_path):
    root = repo_tree(
        tmp_path,
        {"README.md": "<!-- generated: suite -->the suite passes<!-- /generated: suite -->\n"},
    )
    report = ledger.check(root, facts())
    assert not report["ok"]
    assert any("carries no count" in problem for problem in report["problems"])


def test_an_unpaired_marker_is_an_error_not_a_silent_pass(tmp_path):
    root = repo_tree(
        tmp_path, {"README.md": "<!-- generated: suite -->815 pass, exit 0\n"}
    )
    with pytest.raises(ledger.LedgerError):
        ledger.check(root, facts())


def test_an_unsupported_claim_next_to_a_suite_scale_number_is_still_read(tmp_path):
    """``815 passed`` is the same claim as ``815 tests``; both have to be caught."""
    root = repo_tree(
        tmp_path,
        {
            "README.md": "<!-- generated: suite -->**815 pass, exit 0**<!-- /generated: suite -->\n",
            "CONTRIBUTING.md": "# 900 passed, exit 0\n",
        },
    )
    report = ledger.check(root, facts())
    assert not report["ok"]
    assert any(c["value"] == 900 for c in report["claims"])


# --------------------------------------------------------------------------
# exemptions
# --------------------------------------------------------------------------


def test_a_document_that_declares_itself_historical_is_skipped_and_reported(tmp_path):
    root = repo_tree(
        tmp_path,
        {
            "README.md": "<!-- generated: suite -->**815 pass, exit 0**<!-- /generated: suite -->\n",
            "docs/PLAN.md": textwrap.dedent(
                """
                # a plan written earlier

                <!-- ledger: historical: these numbers describe an older tree, not today's suite. -->

                At the time: 561 tests, all green.
                """
            ).lstrip("\n"),
        },
    )
    report = ledger.check(root, facts())
    assert report["ok"], report["problems"]
    assert [e["file"] for e in report["exemptions"]] == ["docs/PLAN.md"]
    assert not any(c["file"] == "docs/PLAN.md" for c in report["claims"])
    # Reported, never silent.
    assert any("historical" in line for line in ledger.describe(report))


def test_an_exemption_split_across_two_lines_still_counts(tmp_path):
    """It is an HTML comment: a comment may wrap, and an exemption must not stop working."""
    root = repo_tree(
        tmp_path,
        {
            "README.md": "<!-- generated: suite -->**815 pass, exit 0**<!-- /generated: suite -->\n",
            "docs/PLAN.md": "<!-- ledger: historical: it describes an\n     older tree. -->\n\n561 tests.\n",
        },
    )
    report = ledger.check(root, facts())
    assert report["ok"], report["problems"]
    assert [e["line"] for e in report["exemptions"]] == [1]


# --------------------------------------------------------------------------
# write
# --------------------------------------------------------------------------


def test_write_replaces_the_body_and_nothing_else(tmp_path):
    root = repo_tree(
        tmp_path,
        {
            "README.md": textwrap.dedent(
                """
                before
                <!-- generated: suite -->**809 pass, exit 0**<!-- /generated: suite -->
                after
                """
            ).lstrip("\n")
        },
    )
    ledger.write(root, facts())
    body = (root / "README.md").read_text(encoding="utf-8")
    assert body.startswith("before\n")
    assert body.endswith("after\n")
    assert "**815 pass, exit 0**" in body
    assert "809" not in body


def test_write_keeps_the_line_endings_it_found(tmp_path):
    """The marked files are CRLF on Windows. A rewrite must not reflow every line."""
    path = tmp_path / "README.md"
    path.write_bytes(
        b"<!-- generated: suite -->809 pass, exit 0<!-- /generated: suite -->\r\nsecond\r\n"
    )
    ledger.write(tmp_path, facts())
    raw = path.read_bytes()
    assert raw.count(b"\r\n") == 2
    assert raw.count(b"\n") == 2
    assert b"**815 pass, exit 0**" in raw


def test_write_can_put_a_bare_count_inside_a_sentence(tmp_path):
    root = repo_tree(
        tmp_path,
        {"docs/CARD.html": "<span><!-- generated: suite-count -->809<!-- /generated: suite-count --> tests</span>\n"},
    )
    ledger.write(root, facts())
    assert ">815<" in (root / "docs" / "CARD.html").read_text(encoding="utf-8")


def test_write_of_a_collection_never_writes_the_word_pass(tmp_path):
    """The wording has to follow the evidence: a collection proves a count, not a result."""
    root = repo_tree(
        tmp_path,
        {"README.md": "<!-- generated: suite -->809 pass<!-- /generated: suite -->\n"},
    )
    ledger.write(root, facts(mode="collected", failed=None, errors=None, skipped=None))
    body = (root / "README.md").read_text(encoding="utf-8")
    assert "847 tests collected" not in body  # this is a synthetic count of 815
    assert "**815 tests collected**" in body
    assert "pass" not in body


def test_write_leaves_a_fragment_bound_to_another_interpreter_alone_and_says_why(tmp_path):
    root = repo_tree(
        tmp_path,
        {
            "docs/VERIFICATION.md": textwrap.dedent(
                """
                <!-- generated: suite -->**815 pass, exit 0**<!-- /generated: suite -->
                <!-- generated: suite-py313 -->**815 pass, exit 0**, on Python 3.13.14<!-- /generated: suite-py313 -->
                """
            ).lstrip("\n")
        },
    )
    outcome = ledger.write(root, facts(python="3.14.5"))
    assert [w["slots"] for w in outcome["written"]] == [["suite"]]
    assert [s["slot"] for s in outcome["skipped"]] == ["suite-py313"]
    assert "3.13" in outcome["skipped"][0]["reason"]
    body = (root / "docs" / "VERIFICATION.md").read_text(encoding="utf-8")
    assert "suite-py313 -->**815 pass, exit 0**, on Python 3.13.14<!--" in body


def test_write_leaves_a_fragment_that_names_another_interpreter_alone(tmp_path):
    """The record names the interpreter it ran on, so only that interpreter may rewrite it."""
    root = repo_tree(
        tmp_path,
        {
            "docs/VERIFICATION.md": "<!-- generated: suite -->**815 pass, exit 0**, 34 test "
            "modules, 223 s, on Python 3.13.14<!-- /generated: suite -->\n"
        },
    )
    outcome = ledger.write(root, facts(python="3.14.5"))
    assert outcome["written"] == []
    assert "never happened" in outcome["skipped"][0]["reason"]
    assert "3.13.14" in (root / "docs" / "VERIFICATION.md").read_text(encoding="utf-8")


def test_the_same_fragment_is_rewritten_by_the_interpreter_it_names(tmp_path):
    root = repo_tree(
        tmp_path,
        {
            "docs/VERIFICATION.md": "<!-- generated: suite -->**815 pass, exit 0**, on "
            "Python 3.13.14<!-- /generated: suite -->\n"
        },
    )
    outcome = ledger.write(root, facts(python="3.13.14"))
    assert [w["slots"] for w in outcome["written"]] == [["suite"]]
    assert outcome["skipped"] == []


def test_a_slot_can_be_spelled_with_dots(tmp_path):
    """``suite-py3.13`` and ``suite-py313`` are the same binding, spelled differently."""
    root = repo_tree(
        tmp_path,
        {
            "docs/VERIFICATION.md": "<!-- generated: suite-py3.13 -->815 pass<!-- "
            "/generated: suite-py3.13 -->\n"
        },
    )
    assert ledger.write(root, facts(python="3.14.5"))["skipped"][0]["slot"] == "suite-py3.13"
    assert ledger.write(root, facts(python="3.13.14"))["written"][0]["slots"] == ["suite-py3.13"]


def test_force_overwrites_a_fragment_bound_to_another_interpreter(tmp_path):
    root = repo_tree(
        tmp_path,
        {"docs/VERIFICATION.md": "<!-- generated: suite-py313 -->809 pass<!-- /generated: suite-py313 -->\n"},
    )
    outcome = ledger.write(root, facts(python="3.14.5"), force=True)
    assert [w["slots"] for w in outcome["written"]] == [["suite-py313"]]
    assert outcome["skipped"] == []


def test_a_slot_this_package_does_not_own_is_left_alone(tmp_path):
    """Another tool's marker in the same document is its business, not this module's."""
    root = repo_tree(
        tmp_path,
        {
            "README.md": textwrap.dedent(
                """
                <!-- generated: whatever -->leave me<!-- /generated: whatever -->
                <!-- generated: suite -->809 pass<!-- /generated: suite -->
                """
            ).lstrip("\n")
        },
    )
    outcome = ledger.write(root, facts())
    body = (root / "README.md").read_text(encoding="utf-8")
    assert "leave me" in body
    assert "whatever" not in [slot for w in outcome["written"] for slot in w["slots"]]


def test_record_makes_the_count_current_before_it_runs_the_suite(tmp_path, monkeypatch):
    """The chicken and egg this exists to break.

    The suite contains the check that enforces these documents, so a run started while they are
    stale goes red - and a tool that waited for a green suite could never repair that state. The
    count is therefore repaired from a collection first, and the run sees documents that agree.
    """
    root = repo_tree(
        tmp_path,
        {"README.md": "<!-- generated: suite -->**815 pass, exit 0**<!-- /generated: suite -->\n"},
    )
    collected = lambda r, **kw: facts(  # noqa: E731 - a stub, not a style choice
        total=900, modules=40, mode="collected", failed=None, errors=None, skipped=None
    )
    seen = {}

    def fake_run(r, **kw):
        seen["docs"] = (root / "README.md").read_text(encoding="utf-8")
        return facts(total=900, modules=40)

    monkeypatch.setattr(ledger, "collect", collected)
    monkeypatch.setattr(ledger, "run_suite", fake_run)
    outcome = ledger.record(root)
    assert "**900 tests collected**" in seen["docs"], "the suite ran against stale documents"
    assert outcome["report"]["ok"]
    assert "**900 pass, exit 0**" in (root / "README.md").read_text(encoding="utf-8")


def test_record_does_not_write_a_failing_run_but_still_fixes_the_count(tmp_path, monkeypatch):
    root = repo_tree(
        tmp_path,
        {"README.md": "<!-- generated: suite -->**815 pass, exit 0**<!-- /generated: suite -->\n"},
    )
    monkeypatch.setattr(
        ledger,
        "collect",
        lambda r, **kw: facts(
            total=900, modules=40, mode="collected", failed=None, errors=None, skipped=None
        ),
    )
    monkeypatch.setattr(
        ledger, "run_suite", lambda r, **kw: facts(total=900, modules=40, failed=3, exit_code=1)
    )
    outcome = ledger.record(root)
    body = (root / "README.md").read_text(encoding="utf-8")
    assert "**900 tests collected**" in body
    assert "failed" not in body, "a red run must not be recorded as the normal state"
    assert outcome["report"]["ok"], "the count is still correct, and that is what the check reads"


def test_record_can_be_told_to_write_a_failing_run(tmp_path, monkeypatch):
    root = repo_tree(
        tmp_path,
        {"README.md": "<!-- generated: suite -->**815 pass, exit 0**<!-- /generated: suite -->\n"},
    )
    monkeypatch.setattr(
        ledger,
        "collect",
        lambda r, **kw: facts(
            total=900, modules=40, mode="collected", failed=None, errors=None, skipped=None
        ),
    )
    monkeypatch.setattr(
        ledger, "run_suite", lambda r, **kw: facts(total=900, modules=40, failed=3, exit_code=1)
    )
    ledger.record(root, force=True)
    assert "**3 of 900 failed, exit 1**" in (root / "README.md").read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# what is watched
# --------------------------------------------------------------------------


def test_scratch_copies_are_not_documentation(tmp_path):
    root = repo_tree(
        tmp_path,
        {
            "README.md": "<!-- generated: suite -->815 pass<!-- /generated: suite -->\n",
            ".tmp-l.md": "785 tests\n",
            ".tmp-shots/notes.md": "785 tests\n",
        },
    )
    names = [p.name for p in ledger.watched(root)]
    assert "README.md" in names
    assert ".tmp-l.md" not in names
    assert "notes.md" not in names
    assert ledger.check(root, facts())["ok"]


def test_documents_are_discovered_not_listed(tmp_path):
    """A document added tomorrow is covered tomorrow."""
    root = repo_tree(
        tmp_path,
        {
            "README.md": "<!-- generated: suite -->815 pass<!-- /generated: suite -->\n",
            "docs/deep/new/PLAN.md": "we have 700 tests\n",
        },
    )
    report = ledger.check(root, facts())
    assert any(c["file"] == "docs/deep/new/PLAN.md" for c in report["claims"])


# --------------------------------------------------------------------------
# this repository
# --------------------------------------------------------------------------


def test_this_repositorys_documents_agree_with_this_repositorys_suite():
    """The CI guard: every count in the docs must equal what the suite actually collects.

    This runs a real ``pytest --collect-only`` against the checkout (about two seconds, no tests
    executed), so it fails on the mistake this whole module exists for - adding tests and leaving
    five documents claiming the old number.
    """
    report = ledger.check(REPO, run=False)
    assert report["ok"], "\n".join(report["problems"])
    assert report["facts"]["total"] >= ledger.SUITE_FLOOR
    assert report["facts"]["modules"] >= 1
    assert report["slots"], "the marked fragments disappeared from the documentation"
