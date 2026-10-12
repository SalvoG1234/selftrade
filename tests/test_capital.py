"""Tests for the capital account: crediting simulated money the agents can work.

The rules these tests pin, in the order they matter:

* a movement is one append-only ledger row with an amount, a reason, a source and the
  balance it left behind - and nothing else can produce capital;
* the account's balance is exactly ``credited - withdrawn - allocated`` after any
  sequence of movements, because that is what "a ledger" has to mean;
* every refusal (no reason, over a cap, more than the balance, a session that already
  traded, funding switched off) writes **nothing**;
* funded capital is actually *worked*: a session funded with five times the capital
  takes the same trades in five times the size, so the sealed 5% per position still
  binds and the return stays the same;
* the research side cannot reach any of this.  A strategy cannot fund itself.
"""

import re
import threading
from pathlib import Path

import numpy as np
import pytest

from selftrade import capital
from selftrade.config import load_settings
from selftrade.data import make_synthetic
from selftrade.genome import sample_genome
from selftrade.memory import LEDGER_KINDS, Memory, _ledger_row  # noqa: F401
from selftrade.paper import create_session, paper_status, run_session

#: A genome whose stop the sealed layer allows, so the rail will trade it.
GENOME = sample_genome("trend_momentum", np.random.default_rng(8))


@pytest.fixture(scope="module")
def df():
    return make_synthetic(1500, seed=21)


@pytest.fixture()
def settings(tmp_path):
    s = load_settings("config.yaml")
    s.memory.db = str(tmp_path / "cap.db")
    s.data.source = "synthetic"
    s.data.synthetic_rows = 1500
    return s


@pytest.fixture()
def memory(settings):
    return Memory(settings.memory.db)


def _session(settings, memory, *, deploy_gate=False, genome=None):
    return create_session(settings, memory, genome=genome or GENOME, deploy_gate=deploy_gate)


# ---------------------------------------------------------------- the read


def test_an_empty_account_says_so_and_credits_nothing(settings, memory):
    out = capital.summary(settings, memory)

    assert out["ok"] is True
    assert out["simulated"] is True  # never presented as a real account
    assert out["balance"] == 0.0 and out["credited"] == 0.0 and out["allocated"] == 0.0
    assert out["entries"] == 0 and out["ledger"] == []
    assert out["session"] is None  # no session holds capital
    assert out["currency"] == settings.funding.currency
    assert out["policy"]["max_deposit"] == settings.funding.max_deposit
    assert "simulated" in out["note"]


def test_the_account_reports_the_session_it_would_fund(settings, memory):
    sid = _session(settings, memory)
    capital.deposit(settings, 2_500, reason="seed", memory=memory)
    capital.allocate(settings, 2_500, reason="fund it", memory=memory, session_id=sid)

    out = capital.summary(settings, memory)
    assert out["session"]["session_id"] == sid
    assert out["session"]["capital"] == pytest.approx(2_500)
    assert out["session"]["funded"] == pytest.approx(2_500)
    assert out["session"]["started"] is False
    assert out["balance"] == 0.0 and out["allocated"] == pytest.approx(2_500)


# ---------------------------------------------------------------- deposits


def test_a_deposit_is_one_row_with_its_balance_and_its_reason(settings, memory):
    first = capital.deposit(settings, 1_000, reason="seed capital", memory=memory)
    second = capital.deposit(settings, 250, reason="top-up", memory=memory)

    assert first["entry"]["kind"] == "deposit"
    assert first["entry"]["amount"] == 1_000
    assert first["entry"]["balance_after"] == 1_000
    assert first["entry"]["reason"] == "seed capital"
    assert first["entry"]["source"] == "cli"  # the default caller is the CLI
    assert second["entry"]["balance_after"] == 1_250  # the running balance is stored

    rows = memory.ledger_entries(10)
    assert [r["id"] for r in rows] == [2, 1]  # newest first
    assert {r["kind"] for r in rows} == {"deposit"}
    totals = memory.ledger_totals()
    assert totals["credited"] == 1_250 and totals["balance"] == 1_250


def test_a_credit_without_a_reason_is_refused_and_writes_nothing(settings, memory):
    with pytest.raises(capital.CapitalError) as exc:
        capital.deposit(settings, 500, reason="   ", memory=memory)

    assert "reason" in str(exc.value)
    assert memory.ledger_entries(10) == []  # nothing was written
    assert capital.summary(settings, memory)["balance"] == 0.0


@pytest.mark.parametrize("bad", [0, -1, -0.5, float("nan"), float("inf"), "abc", None])
def test_a_deposit_must_be_a_positive_finite_number(settings, memory, bad):
    with pytest.raises(capital.CapitalError):
        capital.deposit(settings, bad, reason="trying", memory=memory)
    assert memory.ledger_entries(10) == []


def test_a_single_credit_is_capped_by_the_policy(settings, memory):
    settings.funding.max_deposit = 5_000
    with pytest.raises(capital.CapitalError) as exc:
        capital.deposit(settings, 5_001, reason="one zero too many", memory=memory)
    assert "max_deposit" in str(exc.value)
    assert memory.ledger_entries(10) == []

    out = capital.deposit(settings, 5_000, reason="exactly the cap", memory=memory)
    assert out["account"]["balance"] == pytest.approx(5_000)


def test_the_total_capital_ceiling_holds_across_credits(settings, memory):
    settings.funding.max_deposit = 10_000
    settings.funding.max_total_capital = 12_000
    capital.deposit(settings, 10_000, reason="first", memory=memory)

    with pytest.raises(capital.CapitalError) as exc:
        capital.deposit(settings, 10_000, reason="second", memory=memory)
    assert "max_total_capital" in str(exc.value)
    assert memory.ledger_totals()["credited"] == pytest.approx(10_000)

    # up to the ceiling is allowed
    capital.deposit(settings, 2_000, reason="to the ceiling", memory=memory)
    assert memory.ledger_totals()["credited"] == pytest.approx(12_000)


def test_crediting_can_be_switched_off_in_the_config(settings, memory):
    settings.funding.enabled = False
    with pytest.raises(capital.CapitalError) as exc:
        capital.deposit(settings, 100, reason="still trying", memory=memory)
    assert "disabled" in str(exc.value)
    assert memory.ledger_entries(10) == []


def test_a_zero_max_deposit_refuses_everything(settings, memory):
    settings.funding.max_deposit = 0
    with pytest.raises(capital.CapitalError):
        capital.deposit(settings, 0.01, reason="a penny", memory=memory)


def test_an_unknown_source_is_refused(settings, memory):
    with pytest.raises(capital.CapitalError) as exc:
        capital.deposit(settings, 100, reason="from nowhere", source="the model", memory=memory)
    assert "source" in str(exc.value)
    assert memory.ledger_entries(10) == []


# ---------------------------------------------------------------- withdrawals


def test_a_withdrawal_moves_capital_back_out_and_is_ledgered(settings, memory):
    capital.deposit(settings, 1_000, reason="seed", memory=memory)
    out = capital.withdraw(settings, 400, reason="not needed", memory=memory)

    assert out["entry"]["kind"] == "withdrawal"
    assert out["entry"]["amount"] == -400  # the sign is the effect on the free balance
    assert out["entry"]["balance_after"] == 600
    totals = memory.ledger_totals()
    assert totals["credited"] == 1_000 and totals["withdrawn"] == 400
    assert totals["balance"] == 600


def test_a_withdrawal_bigger_than_the_free_balance_is_refused(settings, memory):
    capital.deposit(settings, 100, reason="seed", memory=memory)
    with pytest.raises(capital.CapitalError) as exc:
        capital.withdraw(settings, 101, reason="too much", memory=memory)
    assert "free balance" in str(exc.value)
    assert memory.ledger_totals()["balance"] == 100  # unchanged

    # allocated capital is not free capital
    sid = _session(settings, memory)
    capital.allocate(settings, 100, reason="fund", memory=memory, session_id=sid)
    with pytest.raises(capital.CapitalError):
        capital.withdraw(settings, 1, reason="mine?", memory=memory)


def test_withdrawals_can_be_switched_off(settings, memory):
    capital.deposit(settings, 100, reason="seed", memory=memory)
    settings.funding.allow_withdrawal = False
    with pytest.raises(capital.CapitalError) as exc:
        capital.withdraw(settings, 10, reason="please", memory=memory)
    assert "withdrawals are disabled" in str(exc.value)


# ---------------------------------------------------------------- allocation


def test_allocation_funds_the_session_and_keeps_the_ledger_balanced(settings, memory):
    sid = _session(settings, memory)
    capital.deposit(settings, 30_000, reason="seed capital", memory=memory)
    out = capital.allocate(settings, 30_000, reason="hand it to the session", memory=memory,
                           session_id=sid)

    assert out["entry"]["kind"] == "allocation"
    assert out["entry"]["amount"] == -30_000 and out["entry"]["session_id"] == sid
    assert out["capital"] == pytest.approx(30_000)
    session = memory.get_paper_session(sid)
    assert session["initial_cash"] == pytest.approx(30_000)
    assert session["cash"] == pytest.approx(30_000)

    totals = memory.ledger_totals()
    assert totals["balance"] == 0.0
    assert totals["balance"] == pytest.approx(
        totals["credited"] - totals["withdrawn"] - totals["allocated"]
    )


def test_allocation_is_capped_by_the_free_balance(settings, memory):
    sid = _session(settings, memory)
    capital.deposit(settings, 1_000, reason="seed", memory=memory)
    with pytest.raises(capital.CapitalError) as exc:
        capital.allocate(settings, 1_001, reason="more than we have", memory=memory, session_id=sid)
    assert "free balance" in str(exc.value)
    assert memory.ledger_entries(5)[0]["kind"] == "deposit"  # only the deposit is on record
    assert memory.get_paper_session(sid)["initial_cash"] == settings.backtest.initial_cash


def test_a_top_up_before_trading_raises_the_capital(settings, memory):
    sid = _session(settings, memory)
    capital.deposit(settings, 5_000, reason="seed", memory=memory)
    capital.allocate(settings, 3_000, reason="first tranche", memory=memory, session_id=sid)
    out = capital.allocate(settings, 2_000, reason="second tranche", memory=memory, session_id=sid)

    assert out["capital"] == pytest.approx(5_000)  # the total, not just the last movement
    assert memory.get_paper_session(sid)["initial_cash"] == pytest.approx(5_000)
    assert memory.session_allocations()[sid] == pytest.approx(5_000)


def test_a_top_up_after_the_session_traded_is_refused(settings, memory, df):
    sid = _session(settings, memory)
    capital.deposit(settings, 60_000, reason="seed", memory=memory)
    capital.allocate(settings, 10_000, reason="fund", memory=memory, session_id=sid)
    status = run_session(settings, memory, sid, mode="all", df=df)
    assert status["n_trades"] > 0  # the session really traded

    before = memory.get_paper_session(sid)["initial_cash"]
    with pytest.raises(capital.CapitalError) as exc:
        capital.allocate(settings, 50_000, reason="late top-up", memory=memory, session_id=sid)

    assert "already traded" in str(exc.value)
    assert "reset" in str(exc.value)  # the message names the way out
    assert memory.get_paper_session(sid)["initial_cash"] == before  # untouched
    assert memory.ledger_totals()["balance"] == pytest.approx(50_000)  # still free


def test_funding_needs_a_session(settings, memory):
    capital.deposit(settings, 1_000, reason="seed", memory=memory)
    with pytest.raises(capital.CapitalError) as exc:
        capital.allocate(settings, 500, reason="fund nobody", memory=memory)
    assert "no paper session" in str(exc.value)

    with pytest.raises(capital.CapitalError) as exc:
        capital.allocate(settings, 500, reason="fund a ghost", memory=memory, session_id=4242)
    assert "no paper session with id" in str(exc.value)


def test_a_release_returns_unallocated_capital(settings, memory):
    sid = _session(settings, memory)
    capital.deposit(settings, 4_000, reason="seed", memory=memory)
    capital.allocate(settings, 4_000, reason="fund", memory=memory, session_id=sid)

    out = capital.release(settings, reason="take it back", memory=memory, session_id=sid)
    assert out["entry"]["kind"] == "release"
    assert out["entry"]["amount"] == 4_000  # back into the free balance
    assert out["capital"] == 0.0
    assert memory.get_paper_session(sid)["initial_cash"] == 0.0
    assert memory.ledger_totals()["balance"] == pytest.approx(4_000)
    assert memory.ledger_totals()["allocated"] == 0.0


def test_a_release_needs_something_to_release(settings, memory):
    sid = _session(settings, memory)
    with pytest.raises(capital.CapitalError) as exc:
        capital.release(settings, reason="nothing there", memory=memory, session_id=sid)
    assert "no allocated capital" in str(exc.value)


def test_a_partial_release_leaves_the_rest_allocated(settings, memory):
    sid = _session(settings, memory)
    capital.deposit(settings, 4_000, reason="seed", memory=memory)
    capital.allocate(settings, 4_000, reason="fund", memory=memory, session_id=sid)

    out = capital.release(settings, reason="half back", memory=memory, session_id=sid, amount=1_500)
    assert out["capital"] == pytest.approx(2_500)
    assert memory.ledger_totals()["balance"] == pytest.approx(1_500)
    assert memory.ledger_totals()["allocated"] == pytest.approx(2_500)

    with pytest.raises(capital.CapitalError):
        capital.release(settings, reason="too much", memory=memory, session_id=sid, amount=9_000)


# ------------------------------------------------------- the money is worked


def test_funded_capital_is_traded_at_the_same_relative_risk(settings, memory, df):
    """5x the capital -> 5x the notional, the same trades and the same return.

    This is what "the agents work the credited money" has to mean: the money is traded,
    the position sizing scales with it, and the sealed 5%-per-position cap still decides
    the size - so the *relative* risk does not change with the deposit.
    """
    capital.deposit(settings, 50_000, reason="seed the account", memory=memory)
    funded = _session(settings, memory)
    capital.allocate(settings, 50_000, reason="fund the session", memory=memory, session_id=funded)
    control = _session(settings, memory)

    a = run_session(settings, memory, funded, mode="all", df=df)
    b = run_session(settings, memory, control, mode="all", df=df)

    assert a["capital_source"] == "ledger" and a["funded_capital"] == pytest.approx(50_000)
    assert b["capital_source"] == "config" and b["funded_capital"] == 0.0
    assert a["initial_cash"] == pytest.approx(50_000)
    assert b["initial_cash"] == pytest.approx(settings.backtest.initial_cash)

    ta, tb = memory.paper_trades(funded), memory.paper_trades(control)
    assert a["n_trades"] > 0 and a["n_trades"] == b["n_trades"]
    assert [(t["entry_time"], t["direction"], t["exit_reason"]) for t in ta] == [
        (t["entry_time"], t["direction"], t["exit_reason"]) for t in tb
    ]
    notional_a = ta[0]["size"] * ta[0]["entry_price"]
    notional_b = tb[0]["size"] * tb[0]["entry_price"]
    assert notional_a / notional_b == pytest.approx(50_000 / settings.backtest.initial_cash)
    assert a["total_return"] == pytest.approx(b["total_return"])
    assert a["equity"] / b["equity"] == pytest.approx(50_000 / settings.backtest.initial_cash)


def test_the_session_names_where_its_capital_came_from(settings, memory, df):
    plain = _session(settings, memory)
    st = paper_status(settings, memory, plain)
    assert st["capital_source"] == "config" and st["funded_capital"] == 0.0

    capital.deposit(settings, 7_000, reason="seed", memory=memory)
    funded = _session(settings, memory)
    capital.allocate(settings, 7_000, reason="fund", memory=memory, session_id=funded)
    st = paper_status(settings, memory, funded)
    assert st["capital_source"] == "ledger" and st["funded_capital"] == pytest.approx(7_000)


# ------------------------------------------------------------- the ledger


def test_the_balance_is_always_credited_minus_withdrawn_minus_allocated(settings, memory):
    sid = _session(settings, memory)
    steps = [
        ("deposit", 10_000),
        ("deposit", 5_000),
        ("withdraw", 2_000),
        ("allocate", 8_000),
        ("release", 3_000),
        ("deposit", 1_000),
    ]
    for action, amount in steps:
        if action == "deposit":
            capital.deposit(settings, amount, reason=f"{action} {amount}", memory=memory)
        elif action == "withdraw":
            capital.withdraw(settings, amount, reason=f"{action} {amount}", memory=memory)
        elif action == "allocate":
            capital.allocate(settings, amount, reason=f"{action} {amount}", memory=memory,
                             session_id=sid)
        else:
            capital.release(settings, reason=f"{action} {amount}", memory=memory,
                            session_id=sid, amount=amount)
        totals = memory.ledger_totals()
        assert totals["balance"] == pytest.approx(
            totals["credited"] - totals["withdrawn"] - totals["allocated"]
        )

    totals = memory.ledger_totals()
    assert totals["credited"] == pytest.approx(16_000)
    assert totals["withdrawn"] == pytest.approx(2_000)
    assert totals["allocated"] == pytest.approx(5_000)
    assert totals["balance"] == pytest.approx(9_000)
    assert totals["entries"] == len(steps)
    # and the last row's balance_after is the balance
    assert memory.ledger_entries(1)[0]["balance_after"] == pytest.approx(totals["balance"])


def test_the_ledger_is_append_only(settings, memory):
    capital.deposit(settings, 100, reason="a mistake", memory=memory)
    first = dict(memory.ledger_entries(1)[0])

    capital.deposit(settings, 1, reason="the correction", memory=memory)
    again = memory.ledger_entries(50)
    assert len(again) == 2
    assert next(r for r in again if r["id"] == first["id"]) == first  # the old row is intact
    # nothing in the ledger can be rewritten: the module exposes no update or delete
    assert not [m for m in dir(memory) if re.match(r"(update|delete)_ledger", m)]


def test_every_kind_of_movement_is_a_known_kind(settings, memory):
    sid = _session(settings, memory)
    capital.deposit(settings, 100, reason="seed", memory=memory)
    capital.allocate(settings, 100, reason="fund", memory=memory, session_id=sid)
    capital.release(settings, reason="back", memory=memory, session_id=sid)
    capital.withdraw(settings, 100, reason="out", memory=memory)

    kinds = {r["kind"] for r in memory.ledger_entries(10)}
    assert kinds <= set(LEDGER_KINDS)
    assert kinds == {"deposit", "allocation", "release", "withdrawal"}

    with pytest.raises(ValueError):
        memory.add_ledger_entry(kind="transfer", amount=1.0, reason="invented")


def test_concurrent_credits_cannot_claim_the_same_balance(settings, memory):
    """Eight credits landing at once must produce eight different balances."""
    errors: list[Exception] = []

    def credit(i: int) -> None:
        try:
            capital.deposit(settings, 10, reason=f"thread {i}", memory=memory)
        except Exception as exc:  # noqa: BLE001 - reported by the assert below
            errors.append(exc)

    threads = [threading.Thread(target=credit, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    balances = sorted(r["balance_after"] for r in memory.ledger_entries(50))
    assert balances == [10.0 * i for i in range(1, 9)]
    assert memory.ledger_totals()["balance"] == pytest.approx(80)


def test_an_existing_database_gains_the_ledger_without_losing_anything(tmp_path):
    path = tmp_path / "old.db"
    mem = Memory(path)
    run_id = mem.start_run("a run from before", {})
    mem.add_lesson(run_id, "a lesson that must survive the migration")
    assert mem.schema_version() == 3

    reopened = Memory(path)
    assert reopened.schema_version() == 3
    assert [lesson["text"] for lesson in reopened.lessons(limit=10)] == [
        "a lesson that must survive the migration"
    ]
    assert reopened.ledger_entries(10) == []
    capital.deposit(load_settings("config.yaml"), 5, reason="after the migration", memory=reopened)
    assert reopened.ledger_totals()["credited"] == pytest.approx(5)


# -------------------------------------------------- nothing can self-fund


def test_only_the_operator_paths_can_move_capital():
    """The model side has no route to the ledger - this is the real guarantee.

    ``deposit``, ``withdraw``, ``allocate`` and ``release`` change what the agents may
    trade, so the modules that run the search must not be able to reach them.  This test
    reads the package's own sources: the only files allowed to mention the capital module
    are the ledger itself, the two operator front ends (the CLI and the dashboard) and the
    paper CLI, which only passes ``--fund`` through.
    """
    root: Path = Path(capital.__file__).resolve().parent
    allowed = {"capital.py", "cli.py", "dashboard.py", "paper.py"}
    pattern = re.compile(r"(from \. import capital|from \.capital import|import capital|capital_mod\.)")
    offenders = []
    for path in sorted(root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if pattern.search(text) and path.name not in allowed:
            offenders.append(path.name)
    assert not offenders, f"these modules must not be able to move capital: {offenders}"

    for name in ("agent.py", "loop.py", "propose.py", "evaluate.py", "critic.py",
                 "autopsy.py", "backtest.py", "repro.py", "stress.py", "invest.py"):
        text = (root / name).read_text(encoding="utf-8")
        assert "capital" not in text, f"{name} must have no idea capital exists"


def test_the_capital_module_touches_no_network_and_no_exchange():
    """A credit is a local ledger row: the module imports nothing that can reach out.

    The module's *docstring* may name the real rails (to say they do not read this
    ledger); what is checked here is the import list, which is what decides.
    """
    import ast

    tree = ast.parse(Path(capital.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    forbidden_modules = {"requests", "urllib", "urllib3", "socket", "http", "ssl",
                         "binance", "mt5", "websocket", "aiohttp"}
    assert not (imported & forbidden_modules), sorted(imported & forbidden_modules)
    # and the only project modules it pulls in are the store and nothing else
    assert {m for m in imported if m in {"execution", "binance", "mt5", "notify"}} == set()


def test_the_cli_only_writes_with_an_amount_and_a_reason(tmp_path, capsys):
    """End to end through the real CLI: the ledger, the refusals and the exit codes."""
    from selftrade.cli import main

    db = tmp_path / "cli.db"
    base = ["--set", f"memory.db={db}"]

    assert main(["capital", "status"] + base) == 0
    assert "free balance 0.00" in capsys.readouterr().out

    assert main(["capital", "deposit"] + base + ["500"]) == 1  # no reason
    assert "reason" in capsys.readouterr().err

    assert main(["capital", "deposit"] + base + ["500", "--reason", "seed"]) == 0
    out = capsys.readouterr().out
    assert "credited 500.00" in out and "balance 500.00" in out

    assert main(["capital", "ledger"] + base) == 0
    assert "seed" in capsys.readouterr().out

    assert main(["capital", "deposit"] + base + ["999999999", "--reason", "typo"]) == 1
    assert "max_deposit" in capsys.readouterr().err

    assert main(["capital", "policy"] + base) == 0
    assert "max_deposit" in capsys.readouterr().out

    with pytest.raises(SystemExit) as exc:  # argparse rejects an unknown action
        main(["capital", "nonsense"])
    assert exc.value.code == 2
