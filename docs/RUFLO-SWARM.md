# Ruflo swarm / MCP

`python -m selftrade mcp` writes an MCP config for REA and Ruflo together with a
roster of agents and ready missions. No agent is decorative: every role declares
inputs, outputs and acceptance criteria.

```powershell
.\run.ps1 mcp --out .mcp.json
```

`.mcp.json` is git-ignored.

## Servers

```json
{
  "mcpServers": {
    "rea":   {"command": "npx", "args": ["-y", "rea-agents@latest", "mcp"]},
    "ruflo": {"command": "npx", "args": ["-y", "ruflo@latest", "mcp"]}
  }
}
```

`node_cmd` and the REA package come from `rea.node_cmd` / `rea.package`.

## Roster

| Agent | Inputs | Outputs | Acceptance |
| --- | --- | --- | --- |
| `rea-analyst` | authorized target, question | evidence saved to memory (tag `rea`) | unauthorized targets refused; evidence stored with target and question; dry run honoured |
| `strategy-proposer` | champion genome, lessons, REA evidence | one JSON genome (`origin=llm`) | `validate_genome` passes; a single bounded change; deterministic fallback |
| `backtest-auditor` | candidate genome, development series | per-fold metrics, gate verdict | holdout never read during evolution; rejects recorded with a reason |
| `mt5-bridge-guard` | order request | dry-run request or readable rejection list | `order_send` never called without both keys; real accounts refused; password never in output |

## Missions

- **evidence-sweep** (`rea-analyst`) — collect evidence for each authorized
  target and store limits in memory.
- **guided-evolution** (`strategy-proposer`) — run `evolve` with LLM proposals
  seeded by REA evidence.
- **gate-audit** (`backtest-auditor`) — verify the hall contains only
  gate-passing champions.
- **fx-safety-drill** (`mt5-bridge-guard`) — exercise the MT5 dry run and
  rejection paths against a fake terminal.

## Notes

MCP is optional. The operator CLI path (`python -m selftrade …`) is always
available and is what the tests exercise.
