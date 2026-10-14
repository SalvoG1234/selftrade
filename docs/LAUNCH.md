# Getting outside eyes on this

The project's most valuable input is not a star. It is a stranger telling us the numbers do
not hold up, that the safety story has a hole, or that the README over-claims. GitHub alone
rarely produces that; a few communities do, and they each have rules about being posted to.

This file is the launch kit: what we may claim (with the numbers), where the project actually
fits, the rules to respect in each place, ready-to-paste drafts, and what to do with the
replies. It is deliberately not an automation playbook.

## 0. What this kit cannot do, and why

**The accounts are created and operated by a person, not by this repository.** No tool here
signs up anywhere, and none posts. That is not a limitation of effort; it is where the
platforms draw the line, read at the source:

* Instagram's Terms, as published: *"You can't attempt to create accounts or access or
  collect information in unauthorized ways"*, which the same clause extends to doing it *"in an
  automated way without our express permission"*, and *"You can't create an account for
  someone else unless you have their express permission"*
  ([Terms of Use](https://help.instagram.com/581066165581870/)). *The
  researcher could only read these clauses from the official page's search listing, not the
  fetched page text - verify them yourself before relying on the wording.*
* X's Authenticity policy requires accounts to be *"legitimate, genuine and transparent"* and
  prohibits *"automated or scripted accounts that do not comply with our Developer Policy"*;
  it **does** allow controlling multiple accounts for a third party (a social-media manager)
  and X's Automation rules allow automated posting when the user has given express, informed
  consent - while forbidding non-API automation such as scripting the website
  ([Authenticity](https://help.x.com/en/rules-and-policies/authenticity),
  [Automation](https://help.x.com/en/rules-and-policies/x-automation)).
* Reddit's Rules require participating *"authentically in communities where you have a personal
  interest"* and prohibit spam; the older site-wide self-promotion guide's *"10% or less"* is a
  rule of thumb, not permission to post in a community whose own rules forbid promotion
  ([Rules](https://redditinc.com/policies/reddit-rules),
  [self-promotion guide](https://www.reddit.com/r/reddit.com/wiki/selfpromotion/)).

So the honest split is: **the drafts and the rules live here; the human presses publish**, in
their own name, with their own account, and answers the thread. Anything else would be both a
terms violation and the operator speaking with a borrowed voice.

One useful consequence: because the posting is manual, this file can be a genuine checklist
rather than a script.

## 1. What we may claim, and the four things we must never claim

Every number below is measured in this repository and reproducible with the command beside it.
Quote these, and say where they come from: a claim with a command behind it survives the first
hostile reply; a round number does not.

| Claim | The number | How to check it |
| --- | --- | --- |
| The search publishes its own failure | The champion's fold-mean Sharpe is **0.885**, and the untouched holdout read it once and returned **−0.545**; the market's own buy & hold was **0.009** on that slice, so the strategy lost to holding by **0.554** | `python -m selftrade backtest --champion --holdout` |
| The benchmark is in the report, not in a footnote | every fold carries its own buy & hold; `metric_excess = metric_mean - baseline_mean` | same command, and `docs/ARCHITECTURE.md` |
| A simulated account, honestly labelled | the ledger says on the page what it is: *"A credit here is a row in a local ledger, not a payment"* | `python -m selftrade capital status`, `docs/CAPITAL.md` |
| Costs are charged, not assumed away | 30 bps round trip: the holdout took **2 472 USDT** of costs on 10 000; with costs set to zero the same trades return **+9.3%**, so the edge lived in the fees | `python -m selftrade invest --champion --window holdout` |
| The critic refuses the champion | *"28.4% drawdown breaks the sealed 15.0% limit; the worst day lost 4.03%, past the sealed 2.0% daily halt. I will not deploy it."* | `python -m selftrade critic --champion` |
| The risk layer is sealed, and that is testable | pinned SHA-256 over the caps, unreachable by config, environment, genome or LLM, plus a file-backed kill switch that stops paper and exchange orders alike | `python -m selftrade risk verify`, `docs/EXECUTION-AND-RISK-GATE.md` |
| The lock survives contention, and the tool can prove its own test bit | `lock-stress`: 968 writes, 365 create races injected, **0 stops overwritten**; `--compare` shows the shipped lock **clean** against a deliberately pre-fix reading that **loses 2 stops** | `python -m selftrade lock-stress`, `docs/SESSION-LOCK.md` |
| It is tested on two platforms | **<!-- generated: suite-count -->832<!-- /generated: suite-count --> tests** pass locally (Python 3.14, Windows 11) and in CI on Windows *and* Ubuntu | `python -m pytest` |

**Never claim**:

1. **Profit, or an edge.** The out-of-sample result on the default data is a loss that trails
   buy & hold. Say that first; it is the most interesting thing about the project.
2. **That it trades real money.** It does not: real order placement needs its own two-key gate
   and is disabled in the shipped config.
3. **That the AI finds strategies.** No model writes trading logic; proposals are bounded genome
   changes, and with no local Ollama the run is deterministic - which the report states.
4. **Any in-sample number without its label.** The full-history figure (10 000 → 70 386) is
   *in-sample and therefore optimistic*; quoting it alone is the exact over-claim the README's
   feedback section asks people to hunt for.

## 2. Where this actually fits (in order)

| Venue | Fit | The rule that governs it |
| --- | --- | --- |
| **Hacker News — Show HN** | **Best fit.** Show HN is for *"something you've made that other people can play with"*, where readers can *"try it out, give you feedback, and ask questions in the thread"* - a feedback request is expected, not tolerated. No signup is needed to run this: it reads public market data | Title must begin with `Show HN`; *"Please don't ask friends to upvote or comment"*; don't use HN *"primarily for promotion"* ([Show HN](https://news.ycombinator.com/showhn.html), [guidelines](https://news.ycombinator.com/newsguidelines.html)) |
| **r/Python** | Good fit: the whole project is standard-library Python with no build step | Project posts must use the **Showcase flair** and be a **text post**, not an image or video post; media may be embedded in the body ([rules](https://www.reddit.com/r/Python/about/rules/)). Read the sidebar at the moment you post |
| **X** | Fine for a short thread pointing at the negative result; manual, from your own account | Post your own work; automated posts need express consent and API use - no scripting the website; no unsolicited automated replies or DMs ([Automation](https://help.x.com/en/rules-and-policies/x-automation)) |
| **Instagram** | Weak fit for a CLI project; only as a by-hand build log with real screenshots | Programmatic account creation and creating an account for someone else are prohibited; there is no verified statement permitting third-party automation to post ([Terms](https://help.instagram.com/581066165581870/)) |
| **r/algotrading** | **Do not post it as promotion.** As currently indexed, the rule reads: *"No Promotional Activity. This community is not a platform for marketing or self-promotion"*, and product or service announcements are prohibited | Asking for critique does not exempt a post about your own project. Participate there by answering other people's questions; that is where the useful conversations happen anyway |
| **r/quant** | Possibly, later: self-promotion *"is allowed on the sub to an extent if it is relevant to institutional quants"*, and repeated promotion *"even for a free resource, is discouraged"* | Only if the discussion is genuinely about method (walk-forward discipline, the sealed gate, the cost model), never as a drive-by link |

*Caveat on the Reddit quotes: the researcher could not read the subreddit rule pages directly
(Reddit served no readable text), so r/algotrading and r/quant are quoted from indexed rule
excerpts and r/Python from a page reproducing its rule. **Before posting, open the subreddit
sidebar and confirm the current wording, including the flair and any day-of-week rule.***

## 3. Drafts, ready to paste

Adjust the numbers only by re-running the commands in §1. Everything else is written to be
publishable as-is.

### 3.1 Show HN

**Title**

```
Show HN: Self-trade – an evolutionary strategy search that publishes its own failed holdout
```

**First comment** (post it immediately, from the same account)

```
Author here. It is a research bot for Windows that evolves trading strategies on real BTC/USDT
candles, and the reason I am posting it is the result it produced rather than the code.

The search optimises on walk-forward folds. Its best candidate reached a fold-mean Sharpe of
0.885. The holdout tail is read exactly once, at the end, and it came back at -0.545 - against
0.009 for simply holding the asset over the same slice. So the selection advantage did not
survive the one read the search never saw, and the README says so in the table that shows both.

What I would most like you to attack:

1. Can you make it trade when it should not? The risk limits are pinned by SHA-256 and
   unreachable from config, environment, genome or LLM, with a file-backed kill switch. A
   reproducible counterexample is the most valuable thing you could send me.
2. Where does the README over-claim? The numbers are deliberately unflattering, but it is easy
   to flatter yourself without noticing which sentence does it.
3. Can you reproduce a run from a stored report and get the same digits?

No API keys needed and nothing trades real money by default: `python -m selftrade data --fetch`
pulls keyless public klines, then `evolve --generations 12 --no-llm` runs the search and
`report` prints what happened. The dashboard is standard-library only, no build step.

It is not financial advice and it does not promise profit - the honest out-of-sample result on
the default data is a loss. Issues and discussions are open.
```

### 3.2 r/Python (Showcase flair, text post)

**Title**

```
Showcase: a research bot that kept its out-of-sample failure in the README
```

**Body**

```
A self-improving strategy search for trading, in pure standard library Python - no build step,
no remote assets in the dashboard, no API keys required to try it. I am posting it because of a
result rather than a feature.

What it does: loads OHLCV (18k real BTC/USDT 4h candles from a public keyless mirror), evolves
strategy hypotheses, evaluates them with walk-forward folds and realistic costs, promotes only
what clears hard gates and an untouched holdout, and paper-trades the champion.

The interesting part is what the numbers did. The best candidate scored 0.885 fold-mean Sharpe,
and the holdout - read exactly once, at the end - returned -0.545, against 0.009 for buy & hold
on the same slice. It lost to simply holding the asset, and the failing number is what the
README leads with. Every evaluation now also reports the buy & hold benchmark per fold, so a
metric reads as excess rather than as skill.

Engineering bits that might interest this subreddit specifically:

- one inline HTML/CSS/JS dashboard served by http.server, dark theme, hash routing, ten panels
  grouped by subject, no framework and no CDN;
- a sealed risk layer: SHA-256 pinned caps that no config key, environment variable or generated
  parameter can widen, plus a kill switch that stops paper and exchange orders alike;
- a session lock stress tool that injects create races and reports 0 stops overwritten over 968
  writes, and which refuses to call a run clean when its own injection never bit;
- <!-- generated: suite-count -->832<!-- /generated: suite-count --> tests, run in CI on Windows and Ubuntu.

I am looking for criticism, especially: where the reporting is self-serving, what the backtest
is still pretending not to know, and where the code is over-engineered.

    powershell -ExecutionPolicy Bypass -File .\setup.ps1
    .\run.ps1 data --fetch --rows 18000
    .\run.ps1 evolve --generations 12 --no-llm
    .\run.ps1 report
    .\dashboard.ps1
```

### 3.3 X thread (five posts, manual posting)

```
1/ I built a strategy search, then published the result that says it failed.

Fold-mean Sharpe 0.885. Holdout, read once: -0.545. Buy & hold on the same slice: 0.009.

The selection advantage did not survive the one read the search never saw.
```

```
2/ So every evaluation now reports the benchmark beside the result: per fold, buy & hold with
the same cash and the same entry cost.

metric_excess = metric_mean - baseline_mean.

A metric with no reference reads like skill when the asset simply rose.
```

```
3/ The costs are in the report too: 2,472 of 10,000 went to fees on the holdout, and with costs
set to zero the same trades return +9.3%.

The edge lived in the fees - which is worth knowing before anything else.
```

```
4/ The critic refuses its own champion: "28.4% drawdown breaks the sealed 15.0% limit. I will
not deploy it."

The caps are pinned by SHA-256 and unreachable from config, environment or generated
parameters. A kill switch stops paper and real orders alike.
```

```
5/ No API keys needed to try it, nothing trades real money by default, <!-- generated: suite-count -->832<!-- /generated: suite-count --> tests on Windows and
Ubuntu.

I want the criticism more than the stars: tell me where the README over-claims or where you can
make it trade when it should not. <link>
```

Do not reply to strangers from a script, and do not send unsolicited DMs with the link: X's
automation rules forbid exactly that.

### 3.4 Instagram (optional, by hand)

A CLI research project does not belong in a photo feed, and automated account creation there is
prohibited. If you want a visual log anyway, use it only for screenshots of the real dashboard
(Overview, Market, Risk Center, Strategy Lab) with a caption that repeats the failure:

```
The champion's out-of-sample result: -0.545 Sharpe. Buy & hold on the same slice: 0.009.
It lost to holding. That is the number in the README, not a footnote.

An evolutionary strategy search for Windows: walk-forward folds, one read of an untouched
holdout, a sealed risk layer, and a dashboard that says N/D when it has not measured
something. No API keys needed to try it.

Not financial advice, no profit promised. Feedback wanted - link in bio.
```

## 4. Answering the three predictable objections

* **"This is curve-fitted; backtests lie."** Agree, and then show the discipline: the holdout is
  read once per run and a test asserts zero reads during evolution; the metric is measured on
  each fold's test slice; the benchmark is now reported per fold; costs are 30 bps round trip
  with a zero-cost counterfactual in the report. The failure is published as the headline.
* **"Why would you share a losing strategy?"** Because the interesting artefact is the
  discipline, not the strategy: the gates that refused it, the sealed caps, the reproducibility
  record, the memory of failures. A strategy that loses money is a fine demo of a process that
  says so.
* **"Not production-grade."** Correct, and the README's first line says so. The ask is narrow on
  purpose: break the safety claim, find the over-claim, reproduce a run.

## 5. A four-week cadence, and where the feedback lands

| When | What | Why that order |
| --- | --- | --- |
| Week 1 | Show HN (§3.1), then answer every comment the same day | The audience that rewards an honest negative result, and the only one where the "try it and tell me" framing is explicitly invited |
| Week 2 | r/Python Showcase post (§3.2), with the same numbers | A different readership: the engineering (stdlib dashboard, race-injecting lock tool) rather than the trading |
| Week 3 | X thread (§3.3), linking the Show HN thread once it has replies | The short version travels; the long argument stays where it can be read |
| Week 4 | A follow-up comment **in the original HN thread** - "here is what your feedback changed" | A new post would be promotion; a comment with the diff is a contribution |

Rules of engagement:

* every reproducible bug becomes an issue, and the reporter is credited in the commit that fixes
  it;
* never argue with a negative number about the strategy - quote the benchmark and move on;
* never ask for votes, comments or submissions anywhere (HN's guideline, and the fastest way to
  get a domain banned);
* if a community's rules forbid the post, do not post it. r/algotrading is on that list today,
  and answering questions there is worth more than a link.

## 6. Before you press publish

```powershell
python -m pytest -q                                # <!-- generated: suite-count -->832<!-- /generated: suite-count --> pass, exit 0
python -m selftrade backtest --champion --holdout  # fold mean, benchmark, excess, holdout
python -m selftrade critic --champion              # the refusal, exit 1 - quote it exactly
python -m selftrade risk verify                    # the seal checks
```

* re-read the README's first screen: if any sentence claims more than §1 does, fix the sentence
  before promoting the page;
* open the target community's sidebar and confirm the current rule and flair (the Reddit quotes
  above are indexed excerpts - read the live rule);
* use the real dashboard screenshots, not mockups; the tooltips and the `N/D` states are part of
  the honesty story;
* check the post contains no key, no path with a username in it, and no environment value;
* make sure you can be in the thread for the next few hours: an unanswered Show HN is a failed
  one.
