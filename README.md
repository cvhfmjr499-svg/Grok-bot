# Grok Bot – Safe Desk (Paper-Trading für Solana-Memecoins)

Neu gebaut am 30.10.2026 aus allen brauchbaren Ideen der X-Posts, ohne die Fake-Teile (85 % Einsatz, „über Nacht 10-fach“, Wallet verbinden).
**Nur Spielgeld:** Der Bot hat keine Wallet und keinen Schlüssel und bewegt kein echtes Geld. Start mit **100 €**, läuft rund um die Uhr auf GitHub Actions. Das Dashboard ist auf GitHub Pages.

## Ablauf
1. **Markt-Regler (0–1)**, alle 2 Minuten neu: SOL-Trend, BTC-Trend, Meme-Volumen im Vergleich zum Schnitt und Anteil der Rug-Coins (6 h).
   - Regler unter 0,5: nur scannen, nichts kaufen.
   - **Markt-Alarm** (SOL −8 % oder BTC −5 % in 1 h, Stablecoin-Depeg): nur scannen, bis der Regler 60 Minuten lang wieder ≥ 0,5 ist.
2. **Scout:** Paare 30 min – 72 h alt, Marktwert 300k – 20 Mio. $ (DexScreener, GeckoTerminal, Jupiter, Pump.fun-Live-Stream).
3. **Filter (harte Kills, fehlt ein Wert → Kill):** Mint/Freeze nicht gesperrt, LP < 90 % gesperrt, Top-10 > 25 %, Dev > 3 % oder verkauft noch, Dev mit Rug-Vergangenheit oder Serien-Dev, Bundles/Insider > 10 %, Liquidität < 40k $, Volumen 1 h < 50k $, schon +80 % in 1 h, Nachahmer-Ticker, Fake-Volumen.
4. **Judge:** 4 feste Fragen, jeweils JA/NEIN mit Wahrscheinlichkeit, nur aus Daten:
   a) Wachsen die Käufer? b) Wachsen die Halter schneller als der Preis? c) Gibt es mehr Kauf- als Verkaufsvolumen? d) Hat eine bewährte Wallet gekauft und hält noch?
   Daraus entsteht ein Score von 0–100. Gekauft wird nur ab **70**. Die Sicherheit entscheidet, *ob* gekauft wird, nie *wie viel*.
5. **Executor:**
   - 6 % des Kontos pro Trade, höchstens 5 offene Positionen.
   - Pro Coin höchstens ein Kauf am Tag, nie nachkaufen.
   - Kein Kauf, wenn eine beobachtete Wallet den Coin schon verkauft hat.
   - Slippage mindestens 2 % plus Gebühren.
6. **Risk:**
   - Stop −12 %, steht vor dem Kauf fest und wird nie gesenkt.
   - Ab +20 % Trailing-Stop mit 10 % Abstand.
   - Sofort raus bei abgezogener Liquidität (−30 %), LP-Abzug oder wenn der Dev verkauft.
   - Zeit-Exit nach 90 Minuten ohne Fortschritt, wenn das Volumen stirbt.
   - **Tagesgrenze −10 %**: danach bis morgen keine Käufe.
   - **3 Verluste in Folge**: 60 Minuten Pause.
7. **Logger:** Jeder Kill mit Regel, jeder Trade mit Score, Slippage und echtem Ausstiegspreis. Es gibt eine Warnung, wenn eine Datenquelle 30 Minuten lang nichts liefert.

## Dashboard
- **Markt-Regler**
- **Judge** mit den 4 Antworten pro Coin
- **Morgen-Bericht** (24 h): Kills pro Regel, Trades, Gewinnquote, Ø Gewinn/Verlust, bester/schlechtester Trade, Drawdown, Tage ohne Rug
- **Ehrlichkeits-Check** (7 Tage): Gewinnen höhere Scores wirklich öfter? Wie schneidet das Ergebnis gegen „nichts tun“ ab?
- **Live-Bereitschaft:** Wird erst „bereit“ nach mindestens 7 Tagen und 50 Trades, mit Gewinn nach Kosten, Drawdown < 20 % und einem Ø Gewinn über dem 1,5-Fachen des Ø Verlusts.

Einstellungen: `config.json` (`desk`, `market`, `sniper`, `risk`, `go_live`).
Alte Versionen liegen in der Git-Historie: Grok Bot v1 bei Commit `69bd9ae`, Memecoin Desk bei `b7f6b18`.
