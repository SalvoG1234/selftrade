# Extending selftrade

## Add a strategy family

1. Declare its parameters in `selftrade/params.py`:

   ```python
   FAMILY_PARAMS["my_family"] = {
       "lookback": IntParam(5, 100, 20),
       "use_filter": BoolParam(False),
   }
   ```
   Add the name to `FAMILY_ORDER`.

2. Implement the strategy in `selftrade/strategies/my_family.py`. It must
   return four boolean series — `long_entry`, `long_exit`, `short_entry`,
   `short_exit` — via `signals_frame`. **Never** look ahead: everything at bar
   `i` may use only bars `<= i`.

3. Register it in `selftrade/strategies/__init__.py` (`STRATEGIES`).

4. If the family needs a constraint (like `ema_fast < ema_slow`), add it to
   `_fix_constraints` in `genome.py`.

5. Add a case to `_signal_body` in `freqtrade_export.py` so export parity
   holds, then run `pytest tests/test_freqtrade_export.py`.

6. The causality test in `tests/test_strategies.py` and the clamping tests in
   `tests/test_genome.py` cover all families automatically.

## Add a parameter

Add it to the family's dict in `params.py`. Sampling, clamping, mutation,
crossover, complexity and the LLM prompt ranges all pick it up automatically.

## Change the objective

`evaluation.metric` selects the walk-forward metric (`sharpe`, `sortino`,
`return`, `calmar`). To add one, extend `compute_metrics` in `backtest.py` and
the `_ENUMS["metric"]` set in `config.py`.

## Add a data source

1. Write a loader that ends with `ensure_ohlcv(...)`.
2. Dispatch on it in `data.load_dataframe`.
3. Add the value to `_ENUMS["source"]` in `config.py`.

## Add a control action

Add it to `Session` (`session.py`), to the validation set in `api_control`
(`dashboard.py`) and to the `session` subparser in `cli.py`. Honour it inside
`Session.checkpoint`, which is called at every generation boundary.

## Add a CLI command

```python
sp = sub.add_parser("mycmd", help="...")
sp.add_argument("--flag")
sp.set_defaults(func=cmd_mycmd)
```
Implement `cmd_mycmd(args) -> int` and return a non-zero exit code on refusal or
error. Print a readable message and support `--json` where it makes sense.

## Tests

Put one test module per module under `tests/`. External services (Ollama, REA,
MT5) must be stubbed locally so the suite runs offline. `tests/fake_mt5.py` is a
worked example of a fake terminal; `tests/test_llm.py` shows a local HTTP stub.
