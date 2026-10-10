# LOOP AI — FASE 1 (ispezione) e FASE 2 (piano operativo)

Documento di lavoro. Il codice e la documentazione del repository restano in inglese;
questo piano è in italiano perché è una decisione da prendere, non codice.

## FASE 1 — Ispezione: cosa c'è davvero

Ambiente verificato: Windows 11 (MINGW64_NT-10.0-26300), venv con **Python 3.14.5**,
Git Bash. Dipendenze core installate e dichiarate: `numpy`, `pandas`, `requests`,
`PyYAML` (+ `pytest` per i test, `MetaTrader5` opzionale Windows-only).

Il progetto **non è vuoto**: esiste un sistema completo chiamato `selftrade`, con nomi
diversi da quelli del brief ma gli stessi componenti.

| Misura | Valore |
| --- | --- |
| Pacchetto | `selftrade/` — 24 moduli, **8.819 righe** |
| Test | `tests/` — 21 file, **4.462 righe, 330 test, tutti verdi** |
| Git | 5 commit, worktree pulita (`c37d0d5`) |
| Persistenza | SQLite `state/selftrade.db`: `runs` (3), `candidates` (253), `hall_of_fame` (3), `lessons` (29), `paper_sessions` (1), `paper_trades` (90), `paper_equity` (3600), `schema_meta` |
| Dati reali | `data/BTCUSDT_4h.csv` — 18.000 candele BTC/USDT 4h (2018-07-23 → 2026-10-10) |
| CLI | `doctor`, `data`, `backtest`, `evolve`, `report`, `hall`, `export`, `invest`, `autopsy`, `critic`, `risk`, `paper`, `mt5`, `rea`, `mcp`, `serve`, `session` |
| Dashboard | `http.server` su 127.0.0.1, 11 endpoint (`/api/health|overview|paper|equity|hall|experiments|trades|lessons|history|session|control`) |

Nessuna cartella `loop_ai/` esiste: il brief propone una struttura *indicativa* e vieta
esplicitamente di duplicare funzionalità già presenti. Quindi la scelta consigliata è
**estendere `selftrade`**, non ricominciare: si perderebbero 330 test, il motore
verificato, la memoria storica e la coerenza backtest/paper dimostrata da test.

### Mappa dei 12 componenti richiesti

| # | Componente del brief | Modulo reale | Stato | Cosa manca davvero |
| --- | --- | --- | --- | --- |
| 1 | Raccolta dati di mercato | `data.py` | **ok** | fetch reale senza chiave (paginato), CSV, freqtrade JSON, MT5; validazione OHLCV stretta, UTC, duplicati, prezzi anomali, avviso di spaziatura. Manca: split/eventi societari (irrilevante per crypto, necessario se si passa ad azioni). |
| 2 | Analisi quantitativa | `indicators.py`, `strategies/` | **ok** | EMA/RSI/ATR/Bollinger/Donchian causali + 4 famiglie interpretabili. |
| 3 | Generazione strategie | `genome.py`, `params.py`, `propose.py`, `llm.py` | **ok** | L'LLM propone **solo** genomi bounded e non scrive mai logica di trading (requisito §3 rispettato). |
| 4 | Backtesting | `backtest.py`, `evaluate.py` | **ok** | Fill all'apertura successiva, costi su entrambi i lati, stop intrabar, walk-forward, holdout letto una volta, nessun lookahead (testato). |
| 5 | Simulazione di mercato e **stress test** | `paper.py`, `invest.py` | **parziale** | C'è il paper in avanti riavviabile e la sensibilità ai costi (0x–3x). Manca: stress test (Monte Carlo sulle sequenze di trade, shock di volatilità/regime) e sensibilità ai parametri. |
| 6 | Risk management | `risk_rules.py`, `critic.py`, `mt5.py` | **buono, incompleto** | Limiti sigillati (frozen + SHA-256 + `MODEL_CAN_OVERRIDE=False`, nessuna via da config/genoma/env), critico con 5 controlli duri, gate MT5 (demo, stop, simboli, spread, lotti, posizioni). Mancano: **kill switch** con stato, **registro eventi di rischio** nel DB, cap di esposizione totale/per strumento, frequenza massima ordini, cap di leva. |
| 7 | Esecuzione ordini | `mt5.py` | **parziale** | Il rail live esiste ma **manca l'astrazione `BrokerAdapter` e un broker simulato a livello di ordine** (`submit_order`, `cancel_order`, `get_order_status`, `get_positions`, `get_account_balance`). Oggi `paper.py` simula *barre*, non ordini. È il gap strutturale più grande. |
| 8 | Monitoraggio portafoglio | `paper.py`, `dashboard.py` | **parziale** | Stato sessione, equity, trade. Mancano posizioni aperte in tempo reale e ordini pendenti perché non c'è ancora il livello ordine. |
| 9 | Analisi automatica degli errori | `autopsy.py` | **buono** | Peggiori perdite con contesto causale all'ingresso, cluster dei modi di fallire con contrasto vincenti, avvisi di overfitting, lezione in memoria, benchmark buy&hold in `invest`. Mancano: sensibilità ai parametri, "errori di implementazione", sezione differenze-vs-benchmark dentro l'autopsia. |
| 10 | Miglioramento iterativo | `agent.py` (Evolver) | **buono** | Generazioni, élite, proposte (LLM o deterministiche), lezioni con embedding. Manca un record esplicito **versione prima → candidata** con diff dei parametri e motivazione. |
| 11 | Logging e audit | `memory.py`, report md/json | **parziale** | Ogni candidato con gate e motivo. Mancano: log di audit dedicato e, per la riproducibilità §10, **SHA del commit e hash del dataset** registrati per run. |
| 12 | Dashboard | `dashboard.py` | **parziale** | Endpoint e controllo start/pausa/riprendi/stop. Mancano: pannello **stato dei limiti di rischio**, ultimo ciclo LOOP, confronto versioni di strategia, cronologia esperimenti navigabile. |

### I 10 gap da colmare (in ordine di dipendenza)

1. `BrokerAdapter` + `PaperBroker` a livello di ordine (blocca i punti 7, 8, 12).
2. Kill switch con stato + registro eventi di rischio nel DB (dipende da 1 per avere senso).
3. Pre-trade gate unificato: esposizione totale/per strumento, frequenza ordini, leva, numero posizioni.
4. Stress test e sensibilità ai parametri (Monte Carlo sulle sequenze di trade, shock di regime, ±X% sui parametri).
5. Tracciabilità delle modifiche: versione prima/dopo, diff, motivazione, esito.
6. Riproducibilità per run: SHA del commit, hash del dataset, versione del codice.
7. Autopsia estesa: sensibilità, errori di implementazione, confronto benchmark nel report.
8. Dashboard: limiti di rischio, ultimo ciclo, confronto versioni, cronologia esperimenti.
9. Analisi statica e confini di esecuzione (§11): `ruff`/`mypy` in dev-deps, backup prima delle modifiche, ripristino dell'ultima versione stabile.
10. Documentazione finale + report di verifica (FASE 7 e 8).

## FASE 2 — Piano operativo proposto

### Decisione di architettura

Estendere `selftrade` mantenendo i nomi dei moduli esistenti e aggiungendo i moduli
mancanti, senza toccare ciò che è verificato. La struttura `loop_ai/` del brief viene
mappata così (nessun file duplicato):

| Brief | Reale |
| --- | --- |
| `app.py` | `cli.py` (`python -m selftrade`) |
| `config/settings.py`, `config/risk_limits.py` | `config.py` + `risk_rules.py` (già sigillato) |
| `data/` | `data.py` |
| `strategies/` | `strategies/`, `genome.py`, `params.py` |
| `research/` | `agent.py`, `propose.py`, `evaluate.py` |
| `backtesting/` | `backtest.py` (+ `stress.py` da creare) |
| `risk/` | `risk_rules.py`, `critic.py`, `risk_gate.py` (creato) |
| `execution/` | creato: `execution/base.py` (interfaccia), `paper_broker.py`, `risk_gate.py`, `kill_switch.py`; l'attuale `mt5.py` resta l'adattatore live |
| `analysis/` | `autopsy.py`, `invest.py` |
| `dashboard/` | `dashboard.py` (stdlib `http.server`, nessuna dipendenza nuova) |
| `logging/audit.py` | da creare, sopra le tabelle esistenti |

### Stato dei pacchetti di lavoro

**WP1 e WP2 fatti** (commit su `selftrade/execution/` + CLI `broker`/`switch`).

- WP1 — `BrokerAdapter` con i sette metodi richiesti e `PaperBroker` a livello di
  ordine: fill all'apertura successiva, costi su entrambi i lati, stop intrabar con
  gap, idempotenza su `client_id`, ricostruzione del libro dall'elenco dei fill
  (`reconcile`). Nessuna importazione di rete nel pacchetto (verificato con `ast`).
- WP2 — `RiskGate` deterministico con i controlli del brief (esposizione per
  strumento e complessiva, leva, numero posizioni, frequenza, rischio per
  operazione, stop massimo, drawdown, perdita giornaliera) sopra i limiti sigillati;
  rifiuto registrato in `risk_events` (nuova tabella SQLite); il superamento di un
  limite di conto **arma il kill switch**; `KillSwitch` persistente e fail-safe.
- Evidenza reale: `broker --bars 600` sui prezzi BTC/USDT (ordine al 50% rifiutato
  dal cap sigillato al 5%, ordine al 4,5% eseguito a 62 677,13, stop al 2% uscito a
  bar 24 per −9,68, `reconcile: OK`); con lo switch armato entrambi gli ordini
  vengono rifiutati e non si apre nulla. Suite: **372 test**.

Restano WP3-WP8.

### Pacchetti di lavoro e test di accettazione

| WP | Contenuto | Test di accettazione |
| --- | --- | --- |
| WP1 | `execution/broker.py`: interfaccia `BrokerAdapter` e `PaperBroker` a livello di ordine, riusando `backtest.simulate` per non rompere la coerenza backtest/paper | ordine market/limit, cancel, stato, posizioni, saldo; **nessun** ordine reale; riavvio della sessione → stessa equity (coerenza già garantita dai test di `paper.py`) |
| WP2 | `risk/kill_switch.py` + `risk_events` nel DB + pre-trade gate unificato (esposizione, frequenza, leva, posizioni) | al superamento di un limite: ordine **bloccato**, motivo registrato, evento in tabella, stato del kill switch persistente al riavvio |
| WP3 | `stress.py`: Monte Carlo sulle sequenze di trade, shock di volatilità/regime, sensibilità ai parametri ±X% | su dati sintetici noti: percentili riproducibili con lo stesso seed; il degrado della sensibilità è misurato, non stimato |
| WP4 | Versioni e riproducibilità: `strategy_versions` (prima/dopo, diff, motivo) + SHA commit e hash dati per run | due run con lo stesso commit e dataset → stesso ID di riproducibilità; diff leggibile tra versione precedente e candidata |
| WP5 | Dashboard: pannello limiti di rischio, ultimo ciclo LOOP, confronto versioni, cronologia esperimenti | endpoint nuovi con dati attesi via HTTP reale; la pagina dichiara cosa è simulato |
| WP6 | §11 sicurezza: `ruff` + `mypy` in dev-deps, backup prima delle modifiche, ripristino dell'ultima versione stabile | analisi statica pulita sui moduli toccati; il backup viene creato e il ripristino è provato |
| WP7 | Autopsia estesa (sensibilità, errori di implementazione, differenze vs benchmark) | report con le sezioni nuove su un caso reale, numeri non inventati |
| WP8 | Docs e verifica finale (FASE 7-8) | README, docs aggiornati, riepilogo di cosa è verificato / non verificato |

### Rischi tecnici e mitigazioni

1. **Rompete la coerenza backtest/paper** — esiste un test che pretende che un colpo solo, molti passi e un riavvio diano gli stessi trade ed equity. Il `PaperBroker` deve riusare `backtest.simulate`, non reimplementare i fill.
2. **Dipendenze** — il brief §3 vieta nuove dipendenze core senza motivo scritto. La dashboard resta `http.server`; i broker useranno `requests` (già presente) o l'SDK ufficiale **solo** se scelto esplicitamente; `ruff`/`mypy` solo dev-deps.
3. **Python 3.14** — verifica di compatibilità degli SDK (già escluso `MetaTrader5` per questo motivo). Se un SDK non supporta 3.14, si usa l'API REST via `requests` e lo si dichiara.
4. **Kill switch ≠ chiudere le posizioni** — il kill switch blocca nuovi ordini; la gestione delle posizioni aperte è una procedura documentata, non un automatismo magico.
5. **Mercato e broker** — orari, spread, margine, volumi minimi e `trade_mode` variano; i vincoli vanno letti dal broker e i rifiuti devono essere espliciti (come già fa il gate MT5).

### Ordine di esecuzione proposto

WP1 → WP2 → WP3 → WP4 → WP5 → WP7 → WP6 → WP8. Ogni WP si chiude con test verdi e
un comando reale eseguito, secondo la regola «fatto = eseguito».

## Decisioni che servono da te

1. **Broker** (obbligatorio: il brief vieta di inventare endpoint): quale adattatore reale vuoi, dopo il broker simulato? Vedi la domanda dedicata.
2. **Layout**: confermi di estendere `selftrade` invece di creare `loop_ai/` da zero?
3. **Dashboard**: resta `http.server` senza dipendenze nuove (consigliato, coerente col progetto) o vuoi Streamlit come suggerisce il §9?

## Cosa NON è ancora stato verificato (onestà)

- Nessun ordine reale è mai stato inviato: il terminale MT5 e il conto Trading.com non esistono in questa macchina.
- Il pacchetto `MetaTrader5` è Windows-only e non è installato; la logica è provata con un terminale finto.
- Non esiste ancora un broker simulato a livello di ordine (WP1): quindi i criteri §14.5 («simula ordini e posizioni») e §14.6 («blocca gli ordini oltre i limiti») sono soddisfatti solo per il rail MT5, non per un broker generico.
  → **Aggiornamento:** WP1 e WP2 sono fatti, quindi §14.5 e §14.6 ora valgono per un
  broker generico. Resta vero che **nessun ordine reale** è mai stato inviato e che
  l'adattatore live (Binance Spot Testnet, scelto da te) non esiste ancora.
- Stress test e sensibilità ai parametri non esistono oggi: `grep` su `selftrade/` non trova né `stress` né `monte`.
