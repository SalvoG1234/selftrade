# LOOP AI — FASE 1 (ispezione) e FASE 2 (piano operativo)

Documento di lavoro. Il codice e la documentazione del repository restano in inglese;
questo piano è in italiano perché è una decisione da prendere, non codice.

<!-- ledger: historical: i numeri qui sotto fotografano l'albero al momento in cui il piano è
     stato scritto, non la suite di oggi, e il controllo del ledger li ignora di proposito. -->

## FASE 1 — Ispezione: cosa c'è davvero

Ambiente verificato: Windows 11 (MINGW64_NT-10.0-26300), venv con **Python 3.14.5**,
Git Bash. Dipendenze core installate e dichiarate: `numpy`, `pandas`, `requests`,
`PyYAML` (+ `pytest` per i test, `MetaTrader5` opzionale Windows-only).

Il progetto **non è vuoto**: esiste un sistema completo chiamato `selftrade`, con nomi
diversi da quelli del brief ma gli stessi componenti.

| Misura | Valore |
| --- | --- |
| Pacchetto | `selftrade/` — 39 file `.py`, **15.660 righe** (include `execution/`) |
| Test | `tests/` — 28 file, **9.120 righe, 561 test, tutti verdi** |
| Git | 6 commit su `master` (`1fecb34`), lavoro non committato nel worktree |
| Persistenza | SQLite `state/selftrade.db`: `runs` (3), `candidates` (253), `hall_of_fame` (3), `lessons` (29), `paper_sessions` (1), `paper_trades` (90), `paper_equity` (3600), `schema_meta` |
| Dati reali | `data/BTCUSDT_4h.csv` — 18.000 candele BTC/USDT 4h (2018-07-23 → 2026-10-10) |
| CLI | `doctor`, `data`, `backtest`, `evolve`, `report`, `hall`, `export`, `invest`, `autopsy`, `critic`, `stress`, `risk`, `paper`, `mt5`, `rea`, `mcp`, `serve`, `session` |
| Dashboard | `http.server` su 127.0.0.1, endpoint (`/api/health|overview|paper|equity|hall|experiments|trades|lessons|history|session|control|market|binance|killswitch`) |

Nessuna cartella `loop_ai/` esiste: il brief propone una struttura *indicativa* e vieta
esplicitamente di duplicare funzionalità già presenti. Quindi la scelta consigliata è
**estendere `selftrade`**, non ricominciare: si perderebbero 438 test, il motore
verificato, la memoria storica e la coerenza backtest/paper dimostrata da test.

### Mappa dei 12 componenti richiesti

| # | Componente del brief | Modulo reale | Stato | Cosa manca davvero |
| --- | --- | --- | --- | --- |
| 1 | Raccolta dati di mercato | `data.py` | **ok** | fetch reale senza chiave (paginato), CSV, freqtrade JSON, MT5; validazione OHLCV stretta, UTC, duplicati, prezzi anomali, avviso di spaziatura. Manca: split/eventi societari (irrilevante per crypto, necessario se si passa ad azioni). |
| 2 | Analisi quantitativa | `indicators.py`, `strategies/` | **ok** | EMA/RSI/ATR/Bollinger/Donchian causali + 4 famiglie interpretabili. |
| 3 | Generazione strategie | `genome.py`, `params.py`, `propose.py`, `llm.py` | **ok** | L'LLM propone **solo** genomi bounded e non scrive mai logica di trading (requisito §3 rispettato). |
| 4 | Backtesting | `backtest.py`, `evaluate.py` | **ok** | Fill all'apertura successiva, costi su entrambi i lati, stop intrabar, walk-forward, holdout letto una volta, nessun lookahead (testato). |
| 5 | Simulazione di mercato e **stress test** | `paper.py`, `invest.py`, `execution/runner.py`, `stress.py` | **buono** | Il paper in avanti riavviabile manda **ordini** al rail e passa dal gate (equivalenza col motore a barre asserita in `tests/test_execution_runner.py`); c'è la sensibilità ai costi (0x–3x); `stress.py` fa il **Monte Carlo** sulle sequenze di trade (bootstrap/shuffle/block, percentili riproducibili per seed, digest) e la **sensibilità ai parametri** (±δ su ogni parametro numerico, clamp dichiarato, degrado misurato) — `docs/STRESS.md`. Manca: shock espliciti di volatilità/regime. |
| 6 | Risk management | `risk_rules.py`, `critic.py`, `mt5.py` | **buono, incompleto** | Limiti sigillati (frozen + SHA-256 + `MODEL_CAN_OVERRIDE=False`, nessuna via da config/genoma/env), critico con 5 controlli duri, gate MT5 (demo, stop, simboli, spread, lotti, posizioni). Mancano: **kill switch** con stato, **registro eventi di rischio** nel DB, cap di esposizione totale/per strumento, frequenza massima ordini, cap di leva. |
| 7 | Esecuzione ordini | `execution/`, `binance.py`, `mt5.py` | **buono** | L'astrazione `BrokerAdapter` e il broker simulato a livello di ordine esistono (`submit_order`, `cancel_order`, `get_order_status`, `get_positions`, `get_account_balance` + `reconcile`), e il paper li usa davvero. Il rail Binance ora arriva anche a un conto reale, ma solo con `allow_production: true` e con il **gate sigillato + kill switch dentro l'adattatore** su ogni ordine inviato (un rifiuto è registrato e non parte nulla), piu' una sonda **sola lettura** (`binance check`) che valida orologio, firma, permessi, saldo e ordini aperti di una chiave nuova senza poter inviare nulla, e lo **stop protettivo automatico**: lo stop implicito dal piano viene validato contro i filtri dell'exchange *prima* che l'entry parta (se lo stop sarebbe rifiutato, non si apre la posizione), poi viene piazzato come `SELL STOP_LOSS_LIMIT` appena il fill arriva, dimensionato sul saldo libero e prezzato sul prezzo medio di riempimento; se non riesce, l'ordine torna con `UNPROTECTED` e un evento registrato. Cosi' `require_stop: true` (default) e' soddisfacibile su un ordine Spot vero. Restano fuori: take profit e trailing (che richiedono un OCO o un watcher) e i fill parziali. |
| 8 | Monitoraggio portafoglio | `execution/`, `paper.py`, `dashboard.py` | **parziale** | Stato sessione, equity, trade e il registro `risk_events`; il rail espone posizioni e ordini pendenti, e il pannello della chiave porta in pagina orologio, permessi, saldi e ordini aperti. Manca: posizioni aperte e ordini pendenti degli **altri** rail (MT5) nel pannello, un pannello dei limiti di rischio e un monitor in tempo reale (i rail sono interrogati a comando, non in streaming). |
| 9 | Analisi automatica degli errori | `autopsy.py`, `stress.py` | **buono** | Peggiori perdite con contesto causale all'ingresso, cluster dei modi di fallire con contrasto vincenti, avvisi di overfitting, lezione in memoria, benchmark buy&hold in `invest`; la sensibilità ai parametri e la fragilità a un ±δ ora sono misurate da `stress`. Mancano: "errori di implementazione", sezione differenze-vs-benchmark dentro l'autopsia. |
| 10 | Miglioramento iterativo | `agent.py` (Evolver) | **buono** | Generazioni, élite, proposte (LLM o deterministiche), lezioni con embedding. Manca un record esplicito **versione prima → candidata** con diff dei parametri e motivazione. |
| 11 | Logging e audit | `memory.py`, report md/json | **parziale** | Ogni candidato con gate e motivo. Mancano: log di audit dedicato e, per la riproducibilità §10, **SHA del commit e hash del dataset** registrati per run. |
| 12 | Dashboard | `dashboard.py` | **buono** | Endpoint e controllo start/pausa/riprendi/stop, **login** (cookie firmato HttpOnly/SameSite, obbligatorio per un bind non-loopback, password da env o generata), **grafico live a candele** dai dati pubblici Binance (chiave non necessaria, cache, errori riportati e non sollevati), **pannello chiave Binance** (lo stesso report sola-lettura di `binance check`, su un adapter disarmato) e **arresto d'emergenza**: `GET/POST /api/killswitch` scrive lo **stesso** `state/kill_switch.json` che gate, loop paper e rail leggono, con conferma esplicita verificata dal server e ogni azione registrata in `risk_events` — `docs/DASHBOARD.md`. Mancano: pannello **stato dei limiti di rischio**, esposizione/posizioni nel pannello, ultimo ciclo LOOP, confronto versioni di strategia, cronologia esperimenti navigabile.
  → **Aggiornamento:** la dashboard è stata riprogettata (modulo di presentazione `dashboard_ui.py`, design system, 8 viste) e ora ha il pannello dei limiti di rischio, l'ultimo ciclo LOOP, la **cronologia esperimenti navigabile** e lo **storico delle versioni di strategia** con il diff, più il record di riproducibilità — `docs/DASHBOARD.md`. |

### I 10 gap da colmare (in ordine di dipendenza)

1. `BrokerAdapter` + `PaperBroker` a livello di ordine (blocca i punti 7, 8, 12) — **fatto**: `selftrade/execution/`, e il paper ora ci passa davvero.
2. Kill switch con stato + registro eventi di rischio nel DB — **fatto**: `execution/kill_switch.py`, tabella `risk_events`.
3. Pre-trade gate unificato: esposizione totale/per strumento, frequenza ordini, leva, numero posizioni — **fatto**: `execution/risk_gate.py`, limiti sigillati inclusi; un ordine che **riduce** la posizione non è nuova esposizione.
4. Stress test e sensibilità ai parametri (Monte Carlo sulle sequenze di trade, shock di regime, ±X% sui parametri).
5. Tracciabilità delle modifiche: versione prima/dopo, diff, motivazione, esito — **fatto**:
   tabella `strategy_versions` (`memory.py`), scritta dall'`Evolver` per ogni candidato
   (`_record_version`) con il genoma del **genitore reale**, il diff, la motivazione e
   l'esito (`champion`/`kept`/`dropped`); il diff è `genome.genome_diff`, l'unica
   primitiva, riusata dal dashboard.
6. Riproducibilità per run: SHA del commit, hash del dataset, versione del codice —
   **fatto**: `selftrade/repro.py` produce `repro_id` (= `sha256(commit + sha256(dataset))[:16]`),
   commit/branch/**dirty**, e l'hash dei byte del file dati (o del frame, se i prezzi non
   sono mai passati da un file); l'id è salvato su `runs` (migrazione in place, schema v2).
7. Autopsia estesa: sensibilità, errori di implementazione, confronto benchmark nel report.
8. Dashboard: limiti di rischio, ultimo ciclo, confronto versioni, cronologia esperimenti
   — **fatto**: la pagina AI Lab mostra ora il ciclo, il record di riproducibilità, lo
   storico versioni navigabile con il diff (ricerca per nome di parametro, filtro per
   esito/origine, riga espandibile con i due genomi) e la tabella esperimenti.
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
| `dashboard/` | `dashboard.py` (stdlib `http.server`, nessuna dipendenza nuova; login + grafico live Binance) |
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

**WP1 esteso: adattatore Binance Spot Testnet.** `selftrade/binance.py` implementa
`BrokerAdapter` contro l'API documentata del testnet: firma HMAC-SHA-256 con
`timestamp` in millisecondi e `recvWindow` (default documentato 5000), `GET /api/v3/time`
per l'orologio, filtri `LOT_SIZE`/`NOTIONAL`/`PRICE_FILTER` da `exchangeInfo`,
idempotenza su `clientOrderId`, tetto locale sul peso e cooldown da `Retry-After`,
riconciliazione degli ordini, e **ogni chiamata ed errore registrati** nella nuova
tabella `api_calls`. Due chiavi per inviare (`binance.enabled` + `binance.live` +
`--live`); senza di esse l'ordine è `dry_run`. Un host di produzione è rifiutato sia
in costruzione sia dalla config (exit 3). Verificato contro il testnet reale senza
chiavi (time/exchangeInfo/klines pubblici) e con 77 test a trasporto iniettato. Il
primo comando da usare con una chiave nuova è `binance check`, una sonda **sola
lettura** (orologio, firma, permessi, saldo, ordini aperti) che stampa quanti `POST`
ha fatto: zero, ed è la stessa cosa che il test verifica. Una richiesta **firmata** è
stata fatta una volta sola, con una chiave volutamente finta, e il testnet reale l'ha
rifiutata con `-2014 API-key format invalid.` (0 POST) — quindi la firma arriva
all'exchange, ma **nessun `200` firmato** è ancora stato visto (le chiavi vere le crea
l'utente con login GitHub). Lo stop protettivo e' verificato con trasporto iniettato e,
per la parte che non richiede chiavi, contro i filtri **veri** del testnet: un piano con
stop a -30% viene rifiutato prima di firmare qualunque cosa (limite documentato
`askMultiplierDown 0.8`); **nessun ordine reale e' mai stato inviato**, quindi nessuno
stop e' mai stato piazzato su un exchange. Dettagli: `docs/BINANCE-TESTNET.md`.

**WP4 e WP5 fatti.** `repro.py` + `strategy_versions` + le due card in AI Lab sono
verificate da test (`tests/test_repro.py`, `tests/test_memory.py`,
`tests/test_agent.py`, `tests/test_dashboard.py`) e in un'esecuzione reale: il run #6
(commit `1fecb34`, dataset `6f7bd0e4b9a4`, 18 000 righe) ha registrato 8 versioni, con
il campione marcato `champion`, i carry-over dell'elite a diff vuoto e due mutazioni
con il diff `bb_period 54→61`, `bb_std 3.2430→3.1722` — e `reproducible: false`,
perché il worktree era sporco.

Restano WP3, WP7 e WP8.

### Pacchetti di lavoro e test di accettazione

| WP | Contenuto | Test di accettazione |
| --- | --- | --- |
| WP1 | `execution/broker.py`: interfaccia `BrokerAdapter` e `PaperBroker` a livello di ordine, riusando `backtest.simulate` per non rompere la coerenza backtest/paper | ordine market/limit, cancel, stato, posizioni, saldo; **nessun** ordine reale; riavvio della sessione → stessa equity (coerenza già garantita dai test di `paper.py`) |
| WP2 | `risk/kill_switch.py` + `risk_events` nel DB + pre-trade gate unificato (esposizione, frequenza, leva, posizioni) | al superamento di un limite: ordine **bloccato**, motivo registrato, evento in tabella, stato del kill switch persistente al riavvio |
| WP3 | `stress.py`: Monte Carlo sulle sequenze di trade, shock di volatilità/regime, sensibilità ai parametri ±X% | su dati sintetici noti: percentili riproducibili con lo stesso seed; il degrado della sensibilità è misurato, non stimato |
| WP4 **fatto** | Versioni e riproducibilità: `strategy_versions` (prima/dopo, diff, motivo) + SHA commit e hash dati per run | due run con lo stesso commit e dataset → stesso ID di riproducibilità; diff leggibile tra versione precedente e candidata |
| WP5 **fatto** | Dashboard: pannello limiti di rischio, ultimo ciclo LOOP, confronto versioni, cronologia esperimenti | endpoint nuovi con dati attesi via HTTP reale; la pagina dichiara cosa è simulato |
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
  broker generico, e l'adattatore Binance Spot Testnet esiste
  (`selftrade/binance.py`) ed è verificato contro gli endpoint **pubblici** del
  testnet. Resta vero che **nessun ordine reale** è mai stato inviato, che nessuna
  richiesta **firmata** ha mai avuto successo (un `200` firmato richiede chiavi
  testnet dell'utente: l'unico tentativo è quello con la chiave finta, rifiutato con
  `-2014`) e che l'adattatore non è ancora agganciato alla sessione paper.
- L'**arresto d'emergenza** dal dashboard è stato provato su **loopback**, con il file di stato in una directory di appoggio: ferma davvero la cosa giusta (lo stesso `state/kill_switch.json` che gate, loop paper e rail Binance leggono, verificato con `switch show` in un secondo processo e con un test che interroga il `RiskGate`), ma armarlo **rifiuta** ordini: **non** chiude una posizione aperta.
- Stress test e sensibilità ai parametri non esistono oggi: `grep` su `selftrade/` non trova né `stress` né `monte`.
  → **Superato:** `selftrade/stress.py` esiste (`docs/STRESS.md`), quindi WP3 non è più
  un vuoto; resta da rifare la verifica finale su un caso reale.
- **Versioni e riproducibilità (WP4): cosa resta non verificato.** Il run #6 è stato
  eseguito da un worktree **sporco**, quindi il suo `repro_id` c'è ma `reproducible` è
  `false`: due run dello stesso commit e dello stesso dataset condividono l'id (provato
  da test con git vero e con il transport iniettato), ma **non** è stato ancora possibile
  verificare una riproduzione da un albero pulito committato, perché le modifiche sono
  non committate. La sostituibilità dei dati è provata su CSV e su frame sintetici; per
  MT5 il fingerprint ricade sul frame (nessun file), e non è stato provato su un
  terminale vero.
