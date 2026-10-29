# Grok Bot – Paper-Trading für Solana-Memecoins

Ein 5-Agenten-Bot wie in den TikTok-Videos, aber **ehrlich**: Er nutzt echte Live-Kurse von DexScreener und echte Rug-Checks von RugCheck. Gehandelt wird nur mit **Spielgeld**. Er braucht keine Wallet, keinen API-Key und kein echtes Geld.

## Diese Version (Challenge Runde 2)
- **Startkapital: 1.000 € Spielgeld**, Anzeige in Euro (Kurs live von der EZB).
- **Strategie „Kill-first“** (aus dem Video, in Runde 1 im Plus): kauft nur Coins mit gesperrter Liquidität (≥ 90 %), Top-10-Halter höchstens 30 %, mindestens 40.000 $ Volumen in 24 h, nicht schon +80 % in der letzten Stunde, und nie wieder nach 2 Verlusten mit demselben Coin. 10 % pro Trade, höchstens 6 Coins gleichzeitig. Verkauf: -25 % Stop-Loss (bei Rug-Anzeichen sofort raus), Teilgewinne ab +50 % (siehe unten), Trailing-Stop ab +50 % mit 25 % Abstand.
- **Optional zweite Strategie „Abschluss+Kill-first“** (`config_abschluss.json`): dieselben Regeln, aber nur für Coins, die gerade die Pump.fun-Bonding-Kurve verlassen.
- Kurse werden **alle 4 Sekunden** geprüft.
- **Schutz vor Soft-Rugs:** kein Kauf, wenn Insider-Netzwerke mehr als 10 % halten (RugCheck) oder es in der letzten Stunde mehr als 3× so viele Verkäufe wie Käufe gab.
- **Dev-Blacklist:** Nach einem Rug wird die Wallet des Erstellers gesperrt (`dev_blacklist.json`). Der Bot kauft nie wieder einen Coin dieser Wallet und verkauft offene Positionen sofort.
- **Rug oder nur Ausstieg? (Pool-Tiefe):** Wenn ein großer Trader mit vielen Copytradern verkauft, fällt der Kurs, und die Liquidität in $ sinkt automatisch mit. Das ist **kein Rug**. Der Bot misst deshalb die Tiefe des Pools (Coin-Menge × SOL-Menge). Die bleibt bei Käufen und Verkäufen gleich und sinkt nur, wenn jemand Liquidität **abzieht**, also bei einem echten Rug. Verkauft wird nur bei abgezogener Liquidität (−15 % in 90 min oder −25 % seit Kauf), bei Insider-Verkäufen (RugCheck), bei einer Verkaufspanik **mit** abgezogener Liquidität oder bei einem Absturz von −50 % in 2 Minuten. Normale Kursrückgänge regelt der Stop-Loss (−25 %).
- **Nachkauf, wenn ein Coin gut läuft:** bei +25 %, +50 % und +75 % über dem ersten Kaufpreis kauft der Bot jeweils 25 % des ersten Einsatzes nach (`sniper.pyramid`). Stops und Gewinnmitnahme rechnen danach mit dem Durchschnittspreis.
- **Dips kaufen (Entry senken):** Fällt ein Coin in einer roten Kerze (mind. −6 % in 2 Minuten) 8–18 % unter deinen Entry, kauft der Bot 25 % des ersten Einsatzes nach, höchstens 2× pro Coin und mit mindestens 3 Minuten Abstand. Der Durchschnitts-Entry sinkt dadurch. Nie bei Rug-Anzeichen, wenn die Verkäufer überwiegen oder die Liquidität schrumpft. Dafür hält der Bot 15 % Bargeld als Reserve frei (`sniper.dip_buy`). Im Chart sind Dip-Käufe blaue Pfeile.
- **Gewinner laufen lassen:** keine feste Gewinnmitnahme. Stattdessen Teilgewinne: bei **+20 % werden 25 % verkauft**, bei +50 % 25 %, bei +100 % 20 %, danach bei jedem weiteren +50 % wieder 20 % (`risk.partial_tp`). **Break-even-Stop:** War ein Coin einmal +20 % im Plus, verkauft der Bot spätestens bei +3 %, damit aus einem Gewinner kein Verlierer wird (`risk.breakeven`). Der Trailing-Stop greift ab +50 % mit 25 % Abstand, nach dem ersten Teilgewinn mit 15 %. Es gibt keine Haltezeit mehr, Coins im Minus schützt der Stop-Loss (−25 %).
- **Läufer laufen lassen (z. B. $DUBBLE):** Nach dem zweiten Teilgewinn gilt ein Coin als Läufer. Der Trailing-Stop bekommt dann wieder mehr Luft (30 % statt 15 %). Bei roten Kerzen (−8 bis −20 % in 3 Minuten) kauft der Bot nach, solange der Coin mindestens 30 % im Plus ist: 25 % des ersten Einsatzes, höchstens 2-mal, mit mindestens 5 Minuten Abstand. Der Entry steigt dadurch, das ist gewollt. Nicht bei Rug-Anzeichen, wenn die Verkäufer überwiegen oder die Liquidität abgezogen wird (`risk.runner`). **Läufer werden nicht komplett geschlossen, nur weil der Chart gerade schwach aussieht:** Greift der Trailing-Stop, verkauft der Bot nur die Hälfte und misst danach neu. Beim zweiten Mal verkauft er wieder die Hälfte. Das letzte Viertel (Moonbag) läuft mit 50 % Abstand weiter. Ganz raus geht er nur bei Rug-Anzeichen, beim Break-even-Stop oder wenn kein Volumen mehr da ist.
- **Live-Kurse von Jupiter:** Für Solana-Coins holt der Risk-Agent den Preis alle 3 Sekunden von Jupiter. Das ist etwa 4-mal aktueller als DexScreener, damit greifen Stop-Loss und Rug-Schutz früher. Liquidität, Trades und Volumen kommen weiter von DexScreener.
- **Lern-Log:** Nach jedem Verkauf verfolgt der Bot den Coin noch 6 Stunden weiter und zeigt in der Trade-Liste unter „Danach“, wie hoch und tief er noch ging. So lässt sich erkennen, welche Verkaufsregeln zu früh greifen.
- **Comeback-Ausbruch (Coins wie $GTA6):** Ältere Coins (2 bis 365 Tage) dürfen gekauft werden, wenn sie gerade wie $GTA6 ausbrechen. Bedingungen: mindestens 3.000 Halter und +10 % neue Halter in 24 h, Organic Score ab 60, Preis +20 % in 24 h, wachsende Liquidität, mehr Käufer als Verkäufer, Mint und Freeze gesperrt, Top-Halter höchstens 25 %. Im Tageschart: höchstens 15 % unter dem Allzeithoch, Volumen mindestens 1,5× der Vortage und höhere Tiefs. Alle Rug-Prüfungen gelten weiter, auch Insider-Netzwerke (`analyst.comeback`).
- **Nur frische Coins mit Schwung:** Gekauft werden nur Coins, die jünger als 24 h sind und in der letzten Stunde nicht mehr als 10 % gefallen sind (`analyst.max_age_hours`, `analyst.min_change_h1_pct`). In den ersten 25 Trades haben Coins älter als 24 h nur 1 von 14 gewonnen. Im Backtest mit 62 Coins aus dem Test-Labor stieg die Gewinnquote mit diesen Regeln von 44 % auf 65 %.
- **Tote Coins meiden und verkaufen:** Kein Kauf, wenn gerade kaum gehandelt wird (unter 5 Trades in 5 min), wenn das Volumen stirbt (aktuelle Stunde unter 30 % einer normalen Stunde, ein starker 5-Minuten-Anstieg zählt auch) oder wenn das Volumen im Chart unter 2 % vom Maximum liegt. Ein gehaltener Coin wird verkauft, wenn 10 Minuten lang niemand mehr handelt (höchstens 3 Trades in 5 min und unter 3.000 $ in 1 h). Das gilt auch, wenn er im Plus ist (`risk.dead_volume_exit`). Im Backtest stieg die Gewinnquote mit dem Volumen-Trend von 65 % auf 71 %.
- **Chart-Check für gehaltene Coins:** Alle 30 Minuten prüft der Bot den kompletten Chart jedes gehaltenen Coins. Bei einer Rug-Kerze verkauft er sofort und sperrt den Dev. Bei einem toten Coin (weit unter dem Hoch, kaum Volumen) verkauft er nur, wenn der Coin im Minus ist. Im Plus darf er weiterlaufen.
- **Live-Charts:** Jede offene Position hat einen Kerzen-Chart mit TradingViews Chart-Engine (Lightweight Charts), Kurse im Sekundentakt. Die gelbe Linie ist dein Entry (Ø-Kaufpreis) und steigt nach jedem Nachkauf. Pfeile markieren Kauf, Nachkäufe und Teilgewinne. Braucht Internet (die Chart-Bibliothek wird geladen).
- **Kontostand-Chart wie in der FOMO-App:** Zeiträume 1h, 24h, 7d, 30d und All. Fahr mit dem Finger über die Linie, dann siehst du den Kontostand zu jeder Uhrzeit und wie viel seit Beginn des Zeitraums dazugekommen ist. Grün heißt im Plus, rot im Minus. Die Linie ist weich gezeichnet, und beim Wischen läuft die Zahl flüssig mit.
- **Bessere Rug-Erkennung (nach $USDF):** kein Kauf, wenn der Coin in 6 h mehr als 60 % oder in 24 h mehr als 75 % gefallen ist (schon gerugt). Kein Kauf bei Fake-Handel, also weniger als 8 $ pro Trade bei mindestens 300 Trades pro Stunde (Bots). Kein Kauf bei Nachahmern mit gleichem Namen wie ein gemeldeter Rug. Offene Positionen mit Fake-Handel oder Nachahmer-Namen werden sofort verkauft.
- **Kompletter Chart wird geprüft:** Vor jedem Kauf lädt der Bot den ganzen Chart seit dem Start des Coins (15-Minuten-Kerzen, bei jungen Coins 1-Minuten-Kerzen von GeckoTerminal). Kein Kauf, wenn es irgendwann eine Rug-Kerze gab (−90 % in einer Kerze), wenn der Kurs mehr als 95 % unter dem Höchststand liegt oder wenn der Coin tot ist (mehr als 80 % unter dem Hoch und kaum noch Volumen). Ältere Coins ohne prüfbaren Chart kauft er nicht. Junge Coins unter 3 h, die GeckoTerminal noch nicht kennt, darf er kaufen, dann passen die Live-Wächter auf. Im Test mit den echten Coins des Bots hätte das den USDF-Rug und 6 Verlierer verhindert und nur 2 Mini-Gewinner gekostet. Die Werte stehen in jeder Positions-Karte („Chart-Check ✓“).
- **Absturz-Wächter:** Fällt ein gehaltener Coin innerhalb von 2 Minuten um 50 % oder mehr, verkauft der Bot sofort (Rug-Kerze). Das gilt auch, wenn der Coin vorher im Plus war.
- **Rug selbst melden:** Unter jedem Chart gibt es den Knopf **„🚩 Rug melden“**. Der Bot verkauft die Position sofort und sperrt die Dev-Wallet. Auf der GitHub-Seite öffnet der Knopf ein vorausgefülltes GitHub-Issue („RUG <Adresse>“). Du tippst nur noch auf „Create“, der laufende Bot liest es innerhalb von ca. 30 Sekunden, antwortet und schließt es (nur Meldungen des Repo-Besitzers zählen). Ohne Knopf geht auch `python3 bot.py --rug <Coin-Adresse>`.
- **Kein Positionslimit:** Der Bot kauft jeden Coin, der alle Regeln besteht (5 % pro Trade), solange Geld da ist.
- **Quellen:** Live-Stream aller neuen Pump.fun-Coins und Abschlüsse, Pump.fun-Heiß-Liste, FOMO, 17 Solana-Launchpads, GeckoTerminal, DexScreener, Jupiter.
- **Robinhood Chain im selben Bot:** Neben Solana scannt der Bot auch die Robinhood Chain (Pons, Uniswap v4/Pools.trade/Flap, GeckoTerminal, DexScreener), mit **derselben Kasse** und denselben Regeln. Robinhood-Coins tragen auf der Seite ein grünes „Robinhood“-Schild. Statt RugCheck prüft dort GoPlus den Vertrag (z. B. kein Kauf, wenn der Handel pausierbar ist). Einstellung: `"extra_chains": ["robinhood"]` in `config.json`, der Stand liegt in `state_robinhood.json`.
- **Pump.fun „Heiß“:** Der Bot rechnet die Heiß-Liste wie in der Pump.fun-App selbst aus. Jede Minute fragt er alle lebenden Pump.fun-Coins der letzten 6 Stunden bei DexScreener ab (aus dem Live-Stream, Coins ohne Handel fallen raus) und sortiert sie nach Volumen und Käufen der letzten 5 Minuten. Die heißesten Coins an der Börse prüft der Analyst sofort. Coins auf der Bonding-Kurve beobachtet er, bis sie an die Börse wechseln. Die Liste steht auf der Seite unter „Pump.fun heiß“. Die Pump.fun-API selbst ist durch Cloudflare gesperrt, und der Live-Handel von PumpPortal braucht einen bezahlten Schlüssel, deshalb dieser Weg (`pumpfun_hot`).
- **FOMO wieder an:** Der FOMO-Agent liest die FOMO-Käufe auf der Blockchain. Kaufen mindestens 3 FOMO-Nutzer in 10 Minuten denselben Coin, prüft der Analyst ihn sofort (`fomo`).
- **Trader-Wallets beobachten:** In `config.json` unter `watch_wallets` stehen Wallets bekannter Trader (Name und Adresse) und ein Enddatum. Der Bot liest alle 20 Sekunden ihre eigenen Käufe und Verkäufe von der Blockchain. Transfers und Airdrops zählen nicht. Die Trades stehen auf der Seite unter „Beobachtete Wallets“, und gekaufte Coins prüft der Analyst sofort. Gekauft wird nur, wenn alle Regeln passen. Aktuell: Numerooo0 (Wallet laut Kolscan) bis 30.10. 12:00 Uhr.

## Auf dem iPhone (a-Shell)
1. ZIP in der App **Dateien** antippen, damit sie entpackt wird.
2. a-Shell öffnen, `pickFolder` eingeben und den Ordner **grokbot** wählen.
3. `python3 bot.py --reset` (erster Start mit frischen 1.000 €), später nur `python3 bot.py`.
   Zweite Strategie: `python3 bot.py --config config_abschluss.json --reset` (eigener Spielstand).
4. In Safari `localhost:8080` öffnen.
5. Stoppen: `q` und Enter.
6. **Neustart: `j` und Enter**, danach nur Enter = Neustart mit deinem Stand (lädt geänderte `config.json` neu), oder `n` und Enter = komplett neu mit frischem Startkapital.

## Auf GitHub (läuft ohne Handy und PC)
- **GitHub Actions** (`.github/workflows/bot-run.yml`) startet den Bot automatisch. Jeder Lauf dauert ca. 5 h 40 min (GitHub erlaubt höchstens 6 h), danach startet sofort der nächste. Jede Minute speichert er seinen Stand (`state.json`, `dev_blacklist.json`) im Repo.
- **Handy-Seite:** https://cvhfmjr499-svg.github.io/Grok-bot/ (GitHub Pages aus dem Ordner `docs/`). Sie zeigt den gespeicherten Stand, also mit ca. 1–2 Minuten Verzögerung, und lädt sich alle 15 Sekunden neu.
- **Screenshots:** Jede Stunde und nach jedem Lauf speichert der Bot ein Bild der Handy-Seite in `docs/screenshots/` (Name = Datum und Uhrzeit in UTC, die letzten 72 bleiben). Ansehen: https://github.com/cvhfmjr499-svg/Grok-bot/tree/main/docs/screenshots
- **Einstellungen ändern:** `config.json` im Repo bearbeiten. Der nächste Lauf übernimmt sie.
- **Neu starten mit 1.000 €:** `state.json` im Repo löschen.
- **Anhalten:** auf GitHub unter Actions → „Grok Bot (Dauerbetrieb)“ → „…“ → „Disable workflow“.

## Starten

Voraussetzung ist Python 3.9 oder neuer. Zusätzliche Pakete braucht man nicht.

```bash
python3 bot.py            # Dashboard: http://localhost:8080
python3 bot.py --reset    # Neustart mit frischem Startkapital
python3 bot.py --host 0.0.0.0 --port 8080   # vom Handy im selben WLAN aufrufbar
```

Stoppen: `q` eintippen und Enter drücken (auf dem PC geht auch `Strg+C`). Der Stand liegt in `state.json` und wird beim nächsten Start fortgesetzt.

## Die 5 Agenten

| Agent   | Aufgabe |
|---------|---------|
| Scout   | Holt alle 20 s neue Solana-Coins: DexScreener (neue Profile, Boosts, Community-Takeovers) und GeckoTerminal (brandneue und trendende Pools, ca. 20 neue pro Minute, dazu reihum die aktivsten Pools von Pump.fun, PumpSwap, StonkFun, LetsBonk, Bags, Raydium LaunchLab, Boop und Moonit). Coins, die nur „zu jung“ sind, kommen auf eine Beobachtungsliste. Abgelehnte Coins werden je nach Grund erst nach 10 min bis 7 Tagen erneut geprüft. |
| Analyst | Filtert nach Liquidität, Alter, Volumen, Marktkapitalisierung und Käufer/Verkäufer-Verhältnis, sortiert bereits gepumpte Tokens aus und macht einen RugCheck (Score, „danger“-Risiken). |
| Sniper  | Kauft mit x % des Kapitals. Slippage (abhängig von der Liquidität) und Gebühren werden simuliert. |
| Risk    | Prüft alle 8 s die Kurse: Stop-Loss, Trailing-Stop, Take-Profit, maximale Haltezeit und Notausstieg, falls die Liquidität abgezogen wird (Rug). |
| Banker  | Verkauft und verbucht Gewinn oder Verlust nach Slippage und Gebühren. |
| FOMO    | Liest live die Trades der FOMO-App von der Solana-Blockchain. Alle FOMO-Trades werden von der FOMO-Wallet `AgmLJBMD…zN51` mitsigniert, der zweite Signer ist der Nutzer. Kaufen mindestens `min_wallets` verschiedene FOMO-Nutzer innerhalb von `window_minutes` denselben Coin, prüft der Analyst ihn sofort. |

## Einstellungen (`config.json`)

- `start_balance_usd`: Startkapital (Spielgeld)
- `analyst.*`: Filterregeln. Strenger heißt weniger, aber sicherere Trades.
- `sniper.position_pct_of_equity`, `max_open_positions`: Positionsgröße und Anzahl gleichzeitiger Positionen
- `risk.*`: Stop-Loss, Trailing-Stop, Take-Profit, Haltezeit
- `start_kapital_eur`: Startkapital in Euro (wirkt nach `--reset`); `anzeige_waehrung`: `"EUR"` oder `"USD"`
- `analyst.min_organic_score`: nur Coins mit mindestens diesem Jupiter Organic Score kaufen (0–100, 0 = aus). Hoch = echter Handel, niedrig = viel Bot-Handel.
- `jupiter.enabled`: Jupiter als Quelle an/aus (nur Solana)
- `chain`: `solana`, `bsc`, `base` oder `robinhood`
- `pumpportal.enabled`: Live-Stream neuer Pump.fun-Coins an/aus (nur Solana); `gecko_new_pages`: wie viele Seiten neuer Pools pro Minute
- Kill-first-Regeln: `analyst.min_volume_h24_usd`, `analyst.max_top10_pct` (Top-10-Halter), `analyst.min_lp_locked_pct`, `analyst.max_losses_per_token`, `analyst.max_tax_pct` (EVM)
- `fomo.enabled`: FOMO-Agent an/aus; `fomo.min_wallets`: ab wie vielen FOMO-Käufern ein Coin geprüft wird
- `fomo.rpc_url`: Solana-RPC. Der kostenlose öffentliche RPC ist streng limitiert, deshalb wertet der Bot nur einen Teil der FOMO-Trades aus (etwa 10 %). Mit einem eigenen kostenlosen RPC-Zugang (z. B. von Helius) kannst du `tx_per_cycle` erhöhen.

Nach einer Änderung den Bot neu starten. Mit `--reset` beginnt der Test sauber von vorn.

## Wichtig

- Lass den Bot **mehrere Tage** laufen, bevor du die Ergebnisse bewertest. Ein paar Gewinne in der ersten Stunde sagen nichts aus.
- Paper-Ergebnisse sind **zu optimistisch**: Echte Fills sind langsamer, MEV-Bots springen vor deine Order, manche Tokens lassen sich gar nicht verkaufen (Honeypots), und DexScreener-Kurse kommen leicht verzögert.
- Die Gewinne aus den Videos (z. B. 4,9× in 25 Sekunden) waren laut „SIM“-Anzeige simuliert. Echte Memecoin-Bots verlieren oft Geld.
