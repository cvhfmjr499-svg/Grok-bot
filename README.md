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
- **Gewinner laufen lassen:** keine feste Gewinnmitnahme. Stattdessen Teilgewinne: bei +50 % werden 25 % verkauft, bei +100 % 20 %, danach bei jedem weiteren +50 % wieder 20 % (`risk.partial_tp`). Die Haltezeit gilt nur für Coins im Minus. Der Trailing-Stop greift erst ab +50 % und verkauft, wenn der Coin 25 % vom Höchststand fällt.
- **Live-Charts:** Jede offene Position hat einen Kerzen-Chart mit TradingViews Chart-Engine (Lightweight Charts), Kurse im Sekundentakt. Die gelbe Linie ist dein Entry (Ø-Kaufpreis) und steigt nach jedem Nachkauf. Pfeile markieren Kauf, Nachkäufe und Teilgewinne. Braucht Internet (die Chart-Bibliothek wird geladen).
- **Kontostand-Chart wie in der FOMO-App:** Zeiträume 1h, 24h, 7d, 30d und All. Fahr mit dem Finger über die Linie, dann siehst du den Kontostand zu jeder Uhrzeit und wie viel seit Beginn des Zeitraums dazugekommen ist. Grün heißt im Plus, rot im Minus. Die Linie ist weich gezeichnet, und beim Wischen läuft die Zahl flüssig mit.
- **Bessere Rug-Erkennung (nach $USDF):** kein Kauf, wenn der Coin in 6 h mehr als 60 % oder in 24 h mehr als 75 % gefallen ist (schon gerugt). Kein Kauf bei Fake-Handel, also weniger als 8 $ pro Trade bei mindestens 300 Trades pro Stunde (Bots). Kein Kauf bei Nachahmern mit gleichem Namen wie ein gemeldeter Rug. Offene Positionen mit Fake-Handel oder Nachahmer-Namen werden sofort verkauft.
- **Kompletter Chart wird geprüft:** Vor jedem Kauf lädt der Bot den ganzen Chart seit dem Start des Coins (15-Minuten-Kerzen, bei jungen Coins 1-Minuten-Kerzen von GeckoTerminal). Kein Kauf, wenn es irgendwann eine Rug-Kerze gab (−90 % in einer Kerze), wenn der Kurs mehr als 95 % unter dem Höchststand liegt oder wenn der Coin tot ist (mehr als 80 % unter dem Hoch und kaum noch Volumen). Ältere Coins ohne prüfbaren Chart kauft er nicht. Junge Coins unter 3 h, die GeckoTerminal noch nicht kennt, darf er kaufen, dann passen die Live-Wächter auf. Im Test mit den echten Coins des Bots hätte das den USDF-Rug und 6 Verlierer verhindert und nur 2 Mini-Gewinner gekostet. Die Werte stehen in jeder Positions-Karte („Chart-Check ✓“).
- **Absturz-Wächter:** Fällt ein gehaltener Coin innerhalb von 2 Minuten um 50 % oder mehr, verkauft der Bot sofort (Rug-Kerze). Das gilt auch, wenn der Coin vorher im Plus war.
- **Rug selbst melden:** Unter jedem Chart gibt es den Knopf **„🚩 Rug melden“**. Der Bot verkauft die Position sofort und sperrt die Dev-Wallet. Auf der GitHub-Seite öffnet der Knopf ein vorausgefülltes GitHub-Issue („RUG <Adresse>“). Du tippst nur noch auf „Create“, der laufende Bot liest es innerhalb von ca. 30 Sekunden, antwortet und schließt es (nur Meldungen des Repo-Besitzers zählen). Ohne Knopf geht auch `python3 bot.py --rug <Coin-Adresse>`.
- **Kein Positionslimit:** Der Bot kauft jeden Coin, der alle Regeln besteht (5 % pro Trade), solange Geld da ist.
- **Quellen:** Live-Stream aller neuen Pump.fun-Coins und Abschlüsse, 17 Solana-Launchpads, GeckoTerminal, DexScreener, Jupiter. FOMO ist aus.

## Auf dem iPhone (a-Shell)
1. ZIP in der App **Dateien** antippen, damit sie entpackt wird.
2. a-Shell öffnen, `pickFolder` eingeben und den Ordner **grokbot** wählen.
3. `python3 bot.py --reset` (erster Start mit frischen 1.000 €), später nur `python3 bot.py`.
   Zweite Strategie: `python3 bot.py --config config_abschluss.json --reset` (eigener Spielstand).
4. In Safari `localhost:8080` öffnen.
5. Stoppen: `q` und Enter.
6. **Neustart: `j` und Enter**, danach nur Enter = Neustart mit deinem Stand (lädt geänderte `config.json` neu), oder `n` und Enter = komplett neu mit frischem Startkapital.

## Auf GitHub (läuft ohne Handy und PC)
- **GitHub Actions** (`.github/workflows/bot-run.yml`) startet den Bot automatisch. Jeder Lauf dauert ca. 55 Minuten, danach startet sofort der nächste. Jede Minute speichert er seinen Stand (`state.json`, `dev_blacklist.json`) im Repo.
- **Handy-Seite:** https://cvhfmjr499-svg.github.io/Grok-bot/ (GitHub Pages aus dem Ordner `docs/`). Sie zeigt den gespeicherten Stand, also mit ca. 1–2 Minuten Verzögerung, und lädt sich alle 15 Sekunden neu.
- **Screenshots:** Nach jedem Lauf speichert der Bot ein Bild der Handy-Seite in `docs/screenshots/` (Name = Datum und Uhrzeit in UTC, die letzten 72 bleiben). Ansehen: https://github.com/cvhfmjr499-svg/Grok-bot/tree/main/docs/screenshots
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
