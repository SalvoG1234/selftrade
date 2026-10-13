# Capital: crediting simulated money the agents can work

`paper` answers *how* a strategy trades. This document answers the question that comes
right after it: **where does the money come from, and who is allowed to put it there?**

The answer is a **ledger**, not a number you edit:

```powershell
python -m selftrade capital status                       # the account, right now
python -m selftrade capital deposit 50000 --reason "seed capital"
python -m selftrade capital allocate 50000 --reason "first forward session"
python -m selftrade paper --create --fund 20000          # create AND fund in one step
python -m selftrade paper --all                          # the agents trade it
python -m selftrade capital ledger                       # every movement, newest first
```

The dashboard's **Capital** entry (`#/capital`) does the same four movements through the
same module: amount, reason, an explicit confirmation that reads both back before anything
is written, and the ledger below with the balance each row left behind. The page carries the
same sentence this document opens with - a credit is a ledger row, not a payment - so nobody
"tops up" expecting an exchange to notice. The two interfaces share one table, so
`capital status` in a terminal and the tiles in the browser cannot disagree.

`docs/PAPER-TRADING.md` describes what happens after the money is in the session; this
document describes the account, its rules and its audit trail.

---

## 1. What "crediting money" means here (and what it does not)

Every rail in this project is a **simulation** except the ones behind their own two-key
arming gates, and this one has no real counterpart at all:

* a credit is a row in a local SQLite table. Nothing is sent anywhere;
* `selftrade/capital.py` imports no HTTP client, no socket and no exchange module - a
  test parses the file's AST and fails if that changes;
* the real rails (`binance`, `mt5`) **do not read this ledger**. A Binance key's balance
  comes from Binance, and no deposit changes what an exchange will let you trade;
* what a credit *does* change is the capital a **paper session** trades, and therefore
  how large its positions are in absolute terms.

A test asserts the note the page shows: *"a simulated account: credits are recorded
locally and traded on paper. No exchange, no key and no order is involved anywhere in
this ledger."*

## 2. The rules of the ledger

`cash_ledger` is **append-only**. No row is ever updated or deleted:

| Column | Meaning |
| --- | --- |
| `id` | the audit reference (printed by every command and shown on the card) |
| `ts` / `created_at` | when the movement happened |
| `kind` | `deposit` \| `withdrawal` \| `allocation` \| `release` |
| `amount` | **signed**: the effect on the account's *free* balance |
| `currency` | the unit, from `funding.currency` unless overridden |
| `reason` | why the money exists - **required**, and it is what makes the row auditable |
| `source` | `cli` \| `dashboard` \| `test`: who moved it |
| `session_id` | the session an `allocation`/`release` belongs to |
| `balance_after` | the free balance after this row: the running total is stored, not only derivable |

The sign is the whole convention and it is always the same: `deposit` and `release` add
to the free balance, `withdrawal` and `allocation` take from it. One number, one meaning.

That gives the invariant the tests check after **every** step of a mixed sequence:

```
balance == credited - withdrawn - allocated
```

A mistake is corrected by a new row with a reason, never by editing history: that is
what `capital deposit 1 --reason "correcting the deposit above: I typed an extra zero"`
is for. A ledger you can rewrite is not an audit trail.

## 3. Who may credit (and who may not)

The movements are reachable from exactly three places:

1. `python -m selftrade capital deposit|withdraw|allocate|release`;
2. `paper --fund AMOUNT` / `paper --release`;
3. the dashboard's **Capital account** card, behind a confirmation.

**No strategy can fund itself.** The research side - `agent.py` (the Evolver),
`loop.py`, `propose.py`, `evaluate.py`, `critic.py`, `autopsy.py`, `backtest.py`,
`stress.py`, `invest.py`, `repro.py` - never imports the capital module and never
mentions the word. `tests/test_capital.py` pins that by reading the package's own
sources and failing if a new import appears: the modules allowed to move capital are
the ledger itself and the operator front ends. A genome cannot express "credit 50 000",
because there is no key to express it in and no code path to reach it.

This is the same shape as the seal on the risk limits: the guarantee is structural, not
a policy the model is asked to respect.

## 4. The policy caps (operator policy, not the sealed layer)

`funding:` in `config.yaml`:

| Key | Default | What it refuses |
| --- | --- | --- |
| `enabled` | `true` | every credit, when off - the ledger still reads |
| `currency` | `USDT` | the unit credits are recorded in; **no conversion is done or implied** |
| `max_deposit` | `100000.0` | a single credit above this (`0` or negative refuses every credit) |
| `max_total_capital` | `1000000.0` | the running total credited (deposits − withdrawals) |
| `allow_withdrawal` | `true` | taking capital back out |

These caps exist to stop a **typo** - an extra zero - from funding a fantasy account.
They are *not* the sealed layer and this document should not be read as claiming they
are: `risk_rules.py` holds the numbers that no config key, `--set`, genome or
environment variable can move (5% per position, 2% daily halt, 15% drawdown), and
**they still bind on funded capital**. Crediting more money changes how much capital
there is, never how much may be risked on one position.

## 5. Allocation: how the money gets to work

```
deposit ──► free balance ──allocate──► paper session's capital ──► orders through the gate ──► fills
                  ▲                                                                              │
                  └──────────────────── release (unfunded sessions only) ◄──────────────────────┘
```

An `allocation` sets the session's capital to its **total** allocated amount, so a
top-up before the session has traded simply raises it. It is **refused once the session
has traded**, and that is the one rule worth explaining:

* every position is sized as a fraction of the session's equity, and the session
  re-runs its window prefix from `initial_cash`. Adding capital late would therefore
  re-size **every trade already recorded** - the history would change because the
  balance did. The ledger would be telling the truth and the equity curve a different
  one;
* so the way to work more money is a **new session** (which is also the honest thing to
  measure: fresh capital, fresh results), or an explicit `paper --reset --session N`
  if you really mean to start that session over.

The guard is enforced by the **database**, not by a check in Python: the row, the ledger
entry and the capital update happen in one transaction
(`Memory.allocate_capital`), and the guard lives in its `WHERE`/preceding read under
`BEGIN IMMEDIATE`, so a session cannot start trading between the check and the write and
two credits cannot both claim the same balance. A test runs eight concurrent credits and
asserts eight distinct `balance_after` values.

`release` follows the same rule in reverse (unfunded sessions only, optionally partial),
because returning the capital while the recorded results stayed behind would leave a
session whose arithmetic no longer adds up.

## 6. What a funded session looks like

Nothing about the trading changes: the same engine, the same cost model, the same
pre-trade gate. What changes is the size, and every reader says where the capital came
from:

```console
$ python -m selftrade paper --status
paper session 1 'BTC/USDT-holdout' window=holdout status=done
  cursor 300/300 (100.0%) cash=49855.74 equity=49855.74 return=-0.29% trades=7
  capital 50000.00 funded from the ledger (50000.00 allocated)
```

* `paper_status` carries `funded_capital` and `capital_source` (`ledger` or `config`);
* `GET /api/paper` carries the same two fields, so the Trading page's *Paper session*
  card marks a funded session with a `from the ledger` chip and an unfunded one with
  *"(config initial_cash — nobody has credited this account)"*;
* `capital status` shows the session's capital next to the free balance and the
  allocation, so the reader can see both sides of the same movement.

### The measured effect

Five times the capital, the same trades, five times the size and the same return -
because the sealed 5% per position is what decides the size:

```
funded  (50 000): 7 trades, first notional 2500.00, equity 49855.74, return -0.2885%
control (10 000): 7 trades, first notional  500.00, equity  9971.15, return -0.2885%
```

`tests/test_capital.py::test_funded_capital_is_traded_at_the_same_relative_risk` asserts
exactly that, including that the entry/exit sequence is identical. It is the proof that
the money is *worked* and that working it does not bend the risk model.

## 7. The card (dashboard)

The **Trading** page carries a *Capital account* card:

* tiles: **free balance**, **credited** (minus withdrawals), **allocated** (working in a
  session), **session capital** (marked `from the ledger` or `config initial_cash`) and
  the **per-credit cap** from the policy;
* the ledger as a sortable, searchable table: movement, amount (signed), balance after,
  session, reason, source;
* a form with one movement at a time - *credit capital*, *withdraw*, *fund the session*,
  *release* - each of which needs an **amount** (except `release`, whose blank amount
  means "the whole allocation"), a **reason**, and then an explicit **confirm** whose
  sentence says what will happen. The buttons stay disabled until the form can produce a
  valid movement;
* a refusal comes back as the sentence the rule produced (the policy cap, the free
  balance, the session that already traded), never as a silent failure.

Endpoints:

| Method | Path | Returns |
| --- | --- | --- |
| GET | `/api/capital` | the account: `simulated`, `balance`, `credited`, `withdrawn`, `allocated`, `session` (`capital`, `funded`, `started`), `policy`, `ledger`, `note`, `checked_at`. `?limit=N` bounds the ledger (5–500) |
| POST | `/api/capital` | `{"action":"deposit\|withdraw\|allocate\|release","amount":N,"reason":"…","session_id":N,"confirm":true}` → `{ok, action, entry, capital, session_id, account}`; `400` with the refusal's own sentence, `confirm_required: true` when the confirmation is missing |

Both are behind the login like every other protected route, and the POST also passes the
same-origin check.

## 8. Files

| File | Role |
| --- | --- |
| `selftrade/capital.py` | the rules: `deposit`, `withdraw`, `allocate`, `release`, `summary`, `policy`, and the `capital` CLI |
| `selftrade/memory.py` | the `cash_ledger` table (schema v3), `add_ledger_entry`, `ledger_entries`, `ledger_totals`, `session_allocations`, `allocate_capital` (the transactional guard) |
| `selftrade/paper.py` | `--fund` / `--release`, and `funded_capital` / `capital_source` in the status |
| `selftrade/dashboard.py` | `CapitalPanel`, `GET`/`POST /api/capital` |
| `selftrade/dashboard_ui.py` | the *Capital account* card on the Trading page |
| `tests/test_capital.py` | the rules, the refusals, the invariants, the concurrency, the funding effect, and *"no agent can credit itself"* |

## 9. Upgrade notes

A database written before this feature gains the `cash_ledger` table on the next open;
`schema_meta.version` goes to `3`. No existing row is touched, and the ledger starts
empty - it does not back-fill a history it never recorded.
