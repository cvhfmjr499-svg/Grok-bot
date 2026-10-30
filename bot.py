#!/usr/bin/env python3
"""
Grok Bot - Paper-Trading-Bot fuer Solana-Memecoins ("Safe Desk").

Markt-Regler + 6 Stufen:
  Markt    -> SOL/BTC-Trend, Meme-Volumen, Rug-Anteil; Alarm = nur scannen
  Scout    -> neue Paare (DexScreener, GeckoTerminal, Jupiter, Pump.fun-Stream)
  Filter   -> harte Kills (Mint/Freeze, LP, Top-10, Dev, Bundles, Fake-Volumen, Nachahmer ...)
  Judge    -> 4 geschlossene Fragen mit Wahrscheinlichkeit -> Score 0-100, ab 70
  Executor -> 6 % pro Trade, max. 5 offen, Slippage mind. 2 %
  Risk     -> Stop -12 %, Trailing ab +20 % (10 %), Rug-/Zeit-Exit, Tagesgrenze -10 %
  Logger   -> jeder Kill, jeder Trade, Morgen-Bericht, Ehrlichkeits-Check

Es wird KEIN echtes Geld bewegt und keine Wallet benoetigt.
Start:  python3 bot.py            -> Dashboard auf http://localhost:8080
"""
import argparse
from datetime import datetime
import base64
import json
import os
import re
import socket
import ssl
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
STATE_PATH = os.path.join(HERE, "state.json")
STATIC_DIR = os.path.join(HERE, "static")

DEX = "https://api.dexscreener.com"
GECKO_BASE = "https://api.geckoterminal.com/api/v2/networks/"
GOPLUS = "https://api.gopluslabs.io/api/v1/token_security/{}?contract_addresses={}"
# DexScreener-chainId -> GoPlus-Chain-ID (EVM); Solana nutzt RugCheck
CHAIN_NAMES = {"solana": "Solana", "bsc": "BNB Chain", "base": "Base", "robinhood": "Robinhood"}
GOPLUS_CHAIN = {"bsc": "56", "base": "8453", "robinhood": "4663"}
QUOTE_SYMBOLS = {"WETH", "ETH", "WBNB", "BNB", "USDC", "USDT", "DAI", "USD1", "CBBTC", "WBTC", "SOL", "WSOL", "USDE"}
JUP = "https://lite-api.jup.ag/tokens/v2"
# Jupiter-Listen: (Pfad, Abfrage alle x Sekunden, Quellen-Name)
JUP_LISTS = [("/toptrending/1h?limit=100", 120, "JUP-Trend"), ("/toporganicscore/1h?limit=100", 120, "JUP-Organic"),
             ("/toptraded/1h?limit=100", 180, "JUP-Traded"), ("/recent?limit=100", 90, "JUP-Neu"),
             ("/toptrending/24h?limit=100", 600, "JUP-Trend24h"), ("/toporganicscore/24h?limit=100", 600, "JUP-Organic24h")]
# Solana-Launchpads, deren aktivste Pools der Scout reihum bei GeckoTerminal abfragt
LAUNCHPADS_BY_CHAIN = {
    "solana": [
        ("pump-fun", "Pump.fun"), ("pumpswap", "PumpSwap"), ("stonkfun", "StonkFun"),
        ("letsbonk-fun", "LetsBonk"), ("bags-fm", "Bags"), ("raydium-launchlab", "LaunchLab"),
        ("meteora-dbc", "Meteora DBC (Believe/Bags/Jup Studio)"), ("moonshot", "Moonshot"),
        ("boop-fun", "Boop"), ("moonit", "Moonit"), ("heaven", "Heaven"), ("daos-fun", "Daos.fun"),
        ("wavebreak", "Wavebreak"), ("token-mill", "Token Mill"), ("memefun", "Meme.fun"),
        ("meteora-damm-v2", "Meteora DAMM v2"), ("clanker-solana", "Clanker (Solana)"),
    ],
    "bsc": [("four-meme", "Four.meme"), ("tiktokfun", "TikTokFun")],
    "base": [("uniswap-v4-base", "Uniswap v4 (Zora/Clanker)"), ("bankr", "Bankr"), ("virtuals-base", "Virtuals"),
             ("o1-launchpad", "o1 Launchpad"), ("memebox-base", "Memebox")],
    "robinhood": [("pons-v2", "Pons"), ("uniswap-v4-robinhood", "Uniswap v4 (Pools.trade/Flap)")],
}


# FOMO-App: alle Trades werden von dieser FOMO-Wallet mitsigniert (laut Bitquery, on-chain geprueft).
# Die Nutzer-Wallet ist der zweite Signer der Transaktion.
WSOL = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
FOMO_SIGNER = "AgmLJBMDCqWynYnQiPCuj9ewsNNsBJXyzoUhD9LJzN51"


def rpc_call(url, method, params, timeout=20):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "User-Agent": "grokbot-paper/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


class MiniWebSocket:
    """Minimaler WebSocket-Client (nur Standardbibliothek, laeuft auch in a-Shell); nutzt HTTPS_PROXY falls gesetzt."""

    def __init__(self, host, path, timeout=120):
        px = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        if px:
            u = urllib.parse.urlparse(px)
            raw = socket.create_connection((u.hostname, u.port or 80), timeout=timeout)
            hdr = f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n"
            if u.username:
                cred = f"{urllib.parse.unquote(u.username)}:{urllib.parse.unquote(u.password or '')}"
                hdr += "Proxy-Authorization: Basic " + base64.b64encode(cred.encode()).decode() + "\r\n"
            raw.sendall((hdr + "\r\n").encode())
            if b" 200" not in raw.recv(4096).split(b"\r\n")[0]:
                raise ConnectionError("Proxy-Tunnel abgelehnt")
        else:
            raw = socket.create_connection((host, 443), timeout=timeout)
        cafile = os.environ.get("SSL_CERT_FILE")
        ctx = ssl.create_default_context(cafile=cafile if cafile and os.path.exists(cafile) else None)
        self.sock = ctx.wrap_socket(raw, server_hostname=host)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("WebSocket-Handshake abgebrochen")
            resp += chunk
        if b" 101" not in resp.split(b"\r\n")[0]:
            raise ConnectionError(resp.split(b"\r\n")[0].decode(errors="replace"))
        self.buf = resp.split(b"\r\n\r\n", 1)[1]

    def _send(self, opcode, data):
        mask = os.urandom(4)
        n = len(data)
        head = bytes([0x80 | opcode]) + (bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack(">H", n))
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def send_json(self, obj):
        self._send(1, json.dumps(obj).encode())

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("Verbindung geschlossen")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv_json(self):
        while True:
            h = self._read(2)
            op, n = h[0] & 0x0F, h[1] & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            data = self._read(n)
            if op == 9:
                self._send(10, data)  # Ping -> Pong
            elif op == 8:
                raise ConnectionError("Server hat geschlossen")
            elif op == 1:
                return json.loads(data)

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


_BLACKLISTS = {}
_BL_LOCK = threading.Lock()


def dev_blacklist(path):
    """Wallets von Coin-Erstellern, deren Coins schon einmal gerugt haben. Datei wird zwischen Bots geteilt."""
    with _BL_LOCK:
        if path not in _BLACKLISTS:
            data = {}
            if os.path.exists(path):
                try:
                    with open(path, encoding="utf-8") as f:
                        data = json.load(f)
                except Exception:
                    data = {}
            _BLACKLISTS[path] = data
        return _BLACKLISTS[path]


def dev_blacklist_add(path, wallet, entry):
    with _BL_LOCK:
        data = _BLACKLISTS.setdefault(path, {})
        if wallet in data:
            return False
        data[wallet] = entry
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1, ensure_ascii=False)
        os.replace(tmp, path)
        return True


def downsample(points, n):
    """Hoechstens n Punkte, letzter Punkt immer dabei."""
    if len(points) <= n:
        return points
    step = len(points) / n
    out = [points[int(i * step)] for i in range(n)]
    out[-1] = points[-1]
    return out


def kill_label(reason):
    """Ablehnungsgrund ohne konkrete Zahlen, z. B. 'schon gepumpt (+58% 5m)' -> 'schon gepumpt'."""
    r = re.sub(r"\(.*?\)", "", reason)
    r = re.sub(r"\$[\d,.]+", "", r)
    r = re.sub(r"(?<![\w-])[+-]?[\d.,]+%?(?![\w])", "", r)
    return re.sub(r"\s+", " ", r).strip(" :")


RUG_WORDS = ("gerugt", "Mint", "Freeze", "LP ", "Dev ", "Serien-Dev", "Bundle", "Nachahmer", "Fake")


def kill_is_rug(reason):
    """Filter-Kill wegen Rug-Merkmalen (nicht wegen zu wenig Volumen o. ae.) - fuer den Markt-Regler."""
    return any(w in reason for w in RUG_WORDS)


def launchpad_of(token, dex_id):
    """Grobe Zuordnung zum Launchpad ueber Adress-Endung bzw. DEX."""
    if token.endswith("pump"):
        return "Pump.fun"
    if token.endswith("bonk"):
        return "LetsBonk"
    return {"pumpfun": "Pump.fun", "pumpswap": "Pump.fun"}.get(dex_id or "", dex_id or "?")
RUGCHECK = "https://api.rugcheck.xyz/v1/tokens/{}/report"
# Stablecoins / wrapped SOL nie als "Memecoin" behandeln
IGNORE_TOKENS = {
    "So11111111111111111111111111111111111111112",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
}


def now():
    return time.time()


def http_json(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "grokbot-paper/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


_GECKO_LOCK = threading.Lock()
_GECKO_LAST = [0.0]


def gecko_json(url, gap_s=2.2):
    """GeckoTerminal erlaubt ca. 30 Abfragen pro Minute -> alle Abfragen (Scout + Chart-Check) nacheinander mit Abstand."""
    with _GECKO_LOCK:
        wait = _GECKO_LAST[0] + gap_s - time.time()
        if wait > 0:
            time.sleep(wait)
        try:
            return http_json(url)
        finally:
            _GECKO_LAST[0] = time.time()


def pool_depth(pair):
    """Tiefe des Pools, die sich durch Kaeufe/Verkaeufe NICHT aendert, nur wenn Liquiditaet abgezogen wird (echter Rug).
    Konstant-Produkt-Pool: sqrt(Coin-Menge x SOL-Menge) bleibt bei jedem Trade gleich. Faellt der Kurs, weil ein grosser
    Trader und seine Copytrader verkaufen, sinkt zwar die Liquiditaet in $, die Tiefe aber nicht."""
    lq = pair.get("liquidity") or {}
    base, quote = fnum(lq.get("base")), fnum(lq.get("quote"))
    if base > 0 and quote > 0:
        return (base * quote) ** 0.5
    price = fnum(pair.get("priceUsd"))
    return fnum(lq.get("usd")) / price ** 0.5 if price > 0 else 0  # Naeherung, falls Mengen fehlen


def chart_stats(candles):
    """Kompletter Chart seit Start: candles = [[ts, open, high, low, close, volume], ...] (Reihenfolge egal).
    -> Abstand zum Allzeithoch, groesster Absturz in einer Kerze (+ wann), aktuelles Volumen im Vergleich zum Maximum."""
    c = sorted(x for x in candles if fnum(x[4]) > 0)
    if len(c) < 4:
        return None
    t0 = c[0][0]
    body = [x for x in c if x[0] >= t0 + 1800] or c  # Start-Spike der ersten 30 min zaehlt nicht als Hoch
    ath = max(fnum(x[2]) for x in body)
    last = fnum(c[-1][4])
    crash, crash_ts = 0.0, None
    for prev, cur in zip(c, c[1:]):
        d = (fnum(cur[3]) / fnum(prev[4]) - 1) * 100 if fnum(prev[4]) > 0 else 0
        if d < crash:
            crash, crash_ts = d, cur[0]
    vmax = max(fnum(x[5]) for x in c)
    vnow = sum(fnum(x[5]) for x in c[-4:]) / 4
    return {"from_ath_pct": round((last / ath - 1) * 100, 1) if ath else 0, "worst_candle_pct": round(crash, 1),
            "worst_candle_ts": crash_ts, "vol_vs_max_pct": round(vnow / vmax * 100, 1) if vmax else 0,
            "hours": round((c[-1][0] - t0) / 3600, 1), "candles": len(c)}


def clean_symbol(sym):
    """Unsichtbare Steuerzeichen entfernen (z. B. Rechts-nach-links-Zeichen, mit denen Betrueger Namen tarnen)."""
    return re.sub(r"[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]", "", str(sym or "?")) or "?"


def fnum(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


class Bot:
    @property
    def cash(self):
        return self.wallet["cash"]

    @cash.setter
    def cash(self, v):
        self.wallet["cash"] = v

    def __init__(self, cfg, state_path=STATE_PATH, name=""):
        self.wallet = {"cash": 0.0}  # bei mehreren Chains teilen sich alle Bots eine Kasse (join_chain)
        self.family = [self]
        self.cfg = cfg
        self.state_path = state_path
        self.name = name
        self.lock = threading.RLock()
        self.save_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.activity = deque(maxlen=200)
        self.first_seen = {}      # token -> erstmals gesehen (ts)
        self.next_check = {}      # token -> fruehestens wieder pruefen (ts)
        self.watch = {}           # token -> beobachten bis (ts), auch wenn keine Quelle ihn mehr listet
        self.gecko_next = {}      # GeckoTerminal-Pfad -> naechste Abfrage (ts)
        self.gecko_warned = 0
        self.launchpad_idx = 0
        self.chain = cfg.get("chain", "solana")
        self.gecko = GECKO_BASE + self.chain
        self.launchpads = LAUNCHPADS_BY_CHAIN.get(self.chain, [])
        self.goplus_cache = {}      # token -> (ts, ergebnis)
        self.creators = {}          # token -> Dev-Wallet (aus Live-Stream, RugCheck oder GoPlus)
        self.rug_alarm = {}         # token -> Grund (vom Rug-Waechter gesetzt, Risk verkauft)
        self.rugwatch_next = {}     # token -> naechster RugCheck-Nachtest (ts)
        self.liq_hist = {}          # token -> [(ts, liq)] aus den Analysen (fuer langsam schrumpfende Liquiditaet)
        self.blacklist_path = cfg.get("dev_blacklist_file") or os.path.join(HERE, "dev_blacklist.json")
        self.blacklist = dev_blacklist(self.blacklist_path)
        self.token_losses = {}      # token -> Anzahl Verlust-Trades
        self.run_started = now()
        self.run_seen = 0           # in diesem Lauf gesehene Coins (fuer Scans/Minute)
        self.jup_next = {}          # Jupiter-Pfad -> naechste Abfrage (ts)
        self.organic = {}           # mint -> {"ts", "score", "label", "top_holders"} von Jupiter
        self.fomo_events = deque()  # (ts, mint, wallet, +1 Kauf / -1 Verkauf)
        self.fomo_last_sig = None
        self.fomo_stats = {"tx_seen": 0, "tx_parsed": 0, "wallets": set()}
        self.fomo_checked = {}      # mint -> zuletzt wegen FOMO-Signal geprueft (ts)
        self.pf_hot = []            # Pump.fun "Heiss": gerade meistgekaufte Coins (hot_loop)
        self.pf_hot_view = []       # fuer die Seite: [symbol, mint, Volumen 5 min, Kaeufe 5 min, MCap, an der Boerse?]
        self.pf_checked = {}
        self.pf_dead = set()        # Pump.fun-Coins ohne Handel -> nicht mehr abfragen
        self.jup_price_next, self.jup_prices, self.jup_prices_at = 0, {}, 0
        self.ww_last_sig, self.ww_trades = {}, []  # beobachtete Trader-Wallets
        self.kill_log, self.desk_scores = [], []  # Memecoin Desk: jeder Kill + letzte Analyst-Scores
        # Safe Desk: Markt-Regler, Tagesgrenze, Verlustserie, Datenquellen
        self.market = {"dial": None, "parts": {}, "alert": None, "alert_since": 0, "good_since": 0, "ts": 0, "info": {}}
        self.meme_samples = []      # [ts, Summe 1h-Volumen der gepruefter Coins] alle 60 min, 7 Tage
        self.vol_window = {}        # mint -> (ts, Volumen 1h) aus der letzten Stunde
        self.range_ts = deque()     # Zeitpunkte, an denen ein Coin im Suchbereich war (6 h)
        self.day = {"date": "", "start_equity": 0.0}
        self.loss_streak, self.pause_until = 0, 0
        self.dev_seen = {}          # mint -> erster Dev-Anteil (%), um "Dev verkauft noch" zu erkennen
        self.sym_best = {}          # Ticker -> (mint, MCap) des groessten Coins mit diesem Namen (Nachahmer)
        self.source_last = {}       # Quelle -> zuletzt etwas geliefert (ts)
        self.health_warned = 0
        self.manual_live = {}       # Hand-Trades (Spielgeld, getrennt vom Bot): id -> {"price", "peak", "hist", ...}
        self.symbols = {}           # mint -> Symbol (fuers Dashboard)
        self.source = {}          # token -> Quelle, ueber die der Scout ihn gefunden hat
        self.cooldown = {}        # token -> nicht vor ts erneut kaufen
        self.rug_cache = {}       # token -> (ts, ergebnis)
        self.analyst_view = None  # zuletzt gepruefter Kandidat fuers Dashboard
        self.stats = {"scanned": 0, "unique": 0, "analyzed": 0, "passed": 0, "rejected": 0,
                      "kills": {}, "stream_new": 0, "stream_grad": 0}
        self.last_msg = {}        # agent -> letzte Meldung fuers Dashboard
        self.errors = 0
        self.started_at = now()
        self.cash = cfg["start_balance_usd"]
        self.positions = {}       # token -> dict
        self.closed = []          # abgeschlossene Trades
        self.equity_history = []  # [ts, equity], fein: letzte 2 h
        self.equity_long = []  # [ts, equity], alle 5 min, 60 Tage
        self.chart_cache = {}  # Pool -> (Zeit, chart_stats)
        self.chart_exit = {}  # Token -> Verkaufsgrund aus dem Chart-Check gehaltener Coins
        self.load_state()

    # ---------- Persistenz ----------
    def load_state(self):
        if not os.path.exists(self.state_path):
            self.log("system", f"Neuer Lauf - Startkapital ${self.cash:,.2f} (Spielgeld)")
            return
        try:
            with open(self.state_path, encoding="utf-8") as f:
                s = json.load(f)
            self.cash = s["cash"]
            self.positions = s["positions"]
            for pos in self.positions.values():
                if not pos.get("ladder2"):  # alte Leiter ohne +20%-Stufe: erledigte Stufen eins weiter schieben
                    n = pos.get("partial_level", 1 if pos.get("partial_done") else 0)
                    pos["partial_level"] = n + 1 if n >= 1 else 0
                    pos["ladder2"] = True
            self.closed = s["closed"]
            self.equity_history = s.get("equity_history", [])[-2000:]
            self.equity_long = s.get("equity_long") or downsample(self.equity_history, 200)
            self.ww_trades = s.get("wallet_trades") or []
            self.kill_log = s.get("kill_log") or []
            self.desk_scores = s.get("desk_scores") or []
            self.meme_samples = s.get("meme_samples") or []
            self.manual_live = s.get("manual_live") or {}
            self.day = s.get("day") or self.day
            self.loss_streak, self.pause_until = s.get("loss_streak", 0), s.get("pause_until", 0)
            m = s.get("market") or {}
            for k in ("alert", "alert_since", "good_since"):
                if k in m:
                    self.market[k] = m[k]
            self.ww_last_sig = s.get("wallet_last_sig") or {}
            for m, ts in (s.get("pf_track") or {}).items():  # Pump.fun-Coins der letzten Stunden weiter verfolgen
                self.first_seen.setdefault(m, ts)
                self.source.setdefault(m, "PP-Neu")
            self.cooldown = s.get("cooldown", {})
            self.token_losses = s.get("token_losses", {})
            self.started_at = s.get("started_at", self.started_at)
            self.cfg["start_balance_usd"] = s.get("start_balance_usd", self.cfg["start_balance_usd"])
            self.log("system", f"Stand geladen: {len(self.positions)} offen, {len(self.closed)} Trades")
        except Exception as e:
            self.log("system", f"state.json unlesbar ({e}) - starte neu")

    def save_state(self):
        with self.lock:
            s = {
                "cash": self.cash, "positions": self.positions, "closed": self.closed[-1000:],
                "equity_history": self.equity_history[-2000:], "equity_long": self.equity_long, "cooldown": self.cooldown,
                "wallet_trades": self.ww_trades[-200:], "wallet_last_sig": self.ww_last_sig,
                "kill_log": self.kill_log[-5000:], "desk_scores": self.desk_scores[-200:],
                "meme_samples": self.meme_samples[-200:], "manual_live": self.manual_live, "day": self.day, "loss_streak": self.loss_streak,
                "pause_until": self.pause_until,
                "market": {k: self.market[k] for k in ("alert", "alert_since", "good_since")},
                "pf_track": {m: ts for m, ts in self.first_seen.items() if now() - ts <= 6 * 3600 and m not in self.pf_dead
                             and str(self.source.get(m, "")).startswith(("PP", "PF"))},
                "token_losses": self.token_losses,
                "started_at": self.started_at, "start_balance_usd": self.cfg["start_balance_usd"],
            }
        # mehrere Threads speichern -> eigene Zwischendatei je Thread und nacheinander ersetzen (sonst FileNotFoundError)
        with self.save_lock:
            tmp = f"{self.state_path}.{threading.get_ident()}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(s, f)
            os.replace(tmp, self.state_path)

    # ---------- Hilfen ----------
    def log(self, agent, msg, kind="info"):
        entry = {"ts": now(), "agent": agent, "msg": (f"[{self.name}] " if self.name else "") + msg, "kind": kind}
        with self.lock:
            self.activity.appendleft(entry)
            self.last_msg[agent] = entry
        print(f"[{time.strftime('%H:%M:%S')}] {self.name + ' ' if self.name else ''}{agent:<8} {msg}", flush=True)

    def equity(self):
        with self.lock:
            return self.cash + sum(p["qty"] * p["last_price"] for bot in self.family for p in bot.positions.values())

    def join_chain(self, sub):
        """Weitere Chain (z. B. Robinhood) in denselben Bot: gleiche Kasse, gleiches Lock, gleicher Stopp, eine Seite."""
        sub.wallet, sub.lock, sub.stop_event = self.wallet, self.lock, self.stop_event
        self.family.append(sub)
        sub.family = self.family

    def jupiter_prices(self, tokens):
        """Aktuelle Preise von Jupiter (Solana), hoechstens alle 3 s, bei Rate-Limit 30 s Pause. -> {mint: usd}"""
        c = self.cfg.get("jupiter", {})
        if self.chain != "solana" or not c.get("enabled", True) or not c.get("live_prices", True) or not tokens:
            return {}
        if now() < self.jup_price_next:
            return self.jup_prices if now() - self.jup_prices_at < 10 else {}
        self.jup_price_next = now() + c.get("price_interval_s", 3)
        out = {}
        try:
            for i in range(0, len(tokens), 50):
                d = http_json("https://lite-api.jup.ag/price/v3?ids=" + ",".join(tokens[i:i + 50]), timeout=5) or {}
                out.update({m: fnum(v.get("usdPrice")) for m, v in d.items() if isinstance(v, dict) and fnum(v.get("usdPrice")) > 0})
        except urllib.error.HTTPError as e:
            if e.code == 429:
                self.jup_price_next = now() + 30
            return {}
        except Exception:
            return {}
        self.jup_prices, self.jup_prices_at = out, now()
        return out

    def fetch_pairs(self, tokens):
        """Beste Pair (hoechste Liquiditaet) je Token, in 30er-Paketen."""
        best = {}
        tokens = list(tokens)
        for i in range(0, len(tokens), 30):
            chunk = tokens[i:i + 30]
            by_lower = {t.lower(): t for t in chunk}
            try:
                pairs = http_json(f"{DEX}/tokens/v1/{self.chain}/{','.join(chunk)}")
            except Exception as e:
                self.errors += 1
                self.log("system", f"DexScreener-Fehler: {e}", "error")
                continue
            for p in pairs or []:
                if p.get("chainId") != self.chain:
                    continue
                addr = by_lower.get((p.get("baseToken", {}).get("address") or "").lower())
                if not addr:
                    continue
                liq = fnum((p.get("liquidity") or {}).get("usd"))
                if addr not in best or liq > fnum((best[addr].get("liquidity") or {}).get("usd")):
                    best[addr] = p
        return best

    def fetch_by_pair(self, pair_addrs):
        """Kurs genau der Pools, in denen gekauft wurde (verhindert Fake-Spruenge durch Pool-Wechsel)."""
        out = {}
        addrs = [a for a in dict.fromkeys(pair_addrs) if a]
        for i in range(0, len(addrs), 30):
            try:
                d = http_json(f"{DEX}/latest/dex/pairs/{self.chain}/{','.join(addrs[i:i + 30])}")
            except Exception as e:
                self.errors += 1
                self.log("system", f"DexScreener-Fehler: {e}", "error")
                continue
            for p in (d.get("pairs") or ([d["pair"]] if d.get("pair") else [])):
                out[p.get("pairAddress")] = p
        return out

    # ---------- Agent 1: Scout ----------
    def gecko_tokens(self, path, every_s):
        """Basis-Tokens aus GeckoTerminal-Pools; sparsam abfragen (strenges Rate-Limit)."""
        if now() < self.gecko_next.get(path, 0):
            return []
        try:
            data = gecko_json(self.gecko + path).get("data", [])
            self.gecko_next[path] = now() + every_s
        except urllib.error.HTTPError as e:
            self.gecko_next[path] = now() + (30 if e.code == 429 else every_s)
            if now() - self.gecko_warned > 300:  # hoechstens alle 5 min melden
                self.gecko_warned = now()
                self.log("scout", f"GeckoTerminal: {'Rate-Limit erreicht' if e.code == 429 else f'Fehler {e.code}'} - neuer Versuch gleich", "warn")
            return []
        except Exception:
            self.gecko_next[path] = now() + every_s
            return []
        out = []
        for pool in data:
            tid = (((pool.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id", "")
            if tid.startswith(self.chain + "_"):
                out.append(tid[len(self.chain) + 1:])
        return out

    def scout(self):
        found = {}
        for path, label in (("/token-profiles/latest/v1", "DS-Profil"), ("/token-boosts/latest/v1", "DS-Boost"),
                            ("/token-boosts/top/v1", "DS-TopBoost"), ("/community-takeovers/latest/v1", "DS-CTO")):
            try:
                for item in http_json(DEX + path) or []:
                    if item.get("chainId") == self.chain and item.get("tokenAddress"):
                        found.setdefault(item["tokenAddress"], label)
            except Exception as e:
                self.errors += 1
                self.log("scout", f"Quelle {path} nicht erreichbar: {e}", "error")
        gt_lists = [("/new_pools?page=1", 60, "GT-Neu"), ("/trending_pools?page=1&duration=1h", 300, "GT-Trend")]
        gt_lists += [(f"/new_pools?page={n}", 120, "GT-Neu") for n in range(2, self.cfg.get("gecko_new_pages", 1) + 1)]
        for path, every, label in gt_lists:
            for a in self.gecko_tokens(path, every):
                found.setdefault(a, label)
        if self.chain == "solana" and self.cfg.get("jupiter", {}).get("enabled", True):
            # pro Durchlauf hoechstens eine Jupiter-Liste (Rate-Limit), bei 429 eine Minute Pause
            for path, every, label in JUP_LISTS:
                if now() < self.jup_next.get(path, 0):
                    continue
                try:
                    for tok in http_json(JUP + path) or []:
                        mint = tok.get("id")
                        if mint and (tok.get("mcap") or 0) < self.cfg["analyst"].get("max_mcap_usd", 2e7) * 2:
                            found.setdefault(mint, label)
                            self.remember_organic(tok)
                    self.jup_next[path] = now() + every
                except urllib.error.HTTPError as e:
                    self.jup_next[path] = now() + 60
                    if e.code != 429:
                        self.log("scout", f"Jupiter {label}: Fehler {e.code}", "warn")
                except Exception as e:
                    self.jup_next[path] = now() + 60
                    self.log("scout", f"Jupiter {label} nicht erreichbar: {e}", "warn")
                break
        # pro Durchlauf hoechstens ein Launchpad abfragen (GeckoTerminal-Limit schonen)
        for _ in range(len(self.launchpads)):
            dex, name = self.launchpads[self.launchpad_idx % len(self.launchpads)]
            path = f"/dexes/{dex}/pools?page=1"
            if now() < self.gecko_next.get(path, 0):
                self.launchpad_idx += 1
                continue
            for a in self.gecko_tokens(path, 480):
                found.setdefault(a, name)
            self.launchpad_idx += 1
            break

        t = now()
        for label in set(found.values()):  # Datenquellen-Gesundheit
            self.source_last[label] = t
        # Beobachtungsliste: z. B. "zu junge" Tokens, auch wenn keine Quelle sie mehr listet
        for a, until in list(self.watch.items()):
            if t > until:
                del self.watch[a]
            else:
                found.setdefault(a, "Watchlist")

        fomo_cfg = self.cfg.get("fomo", {})
        if fomo_cfg.get("enabled"):
            for a, buyers, _ in self.fomo_hot():
                if buyers >= fomo_cfg.get("min_wallets", 3) and t - self.fomo_checked.get(a, 0) > 120:
                    found[a] = "FOMO"
                    self.fomo_checked[a] = t
                    self.next_check[a] = 0  # FOMO-Signal ist neu -> sofort pruefen

        # Pump.fun "Heiss": die Coins mit den meisten Kaeufen/Volumen gerade (hot_loop) jede Minute sofort pruefen
        for a in list(self.pf_hot):
            if t - self.pf_checked.get(a, 0) >= 60:
                found[a] = "PF-Heiss"
                self.pf_checked[a] = t
                self.next_check[a] = 0

        new, again = [], []
        for a in found:
            if a in IGNORE_TOKENS or a in self.positions or t < self.next_check.get(a, 0):
                continue
            (again if a in self.first_seen else new).append(a)
            if a not in self.first_seen:
                self.run_seen += 1
            if found[a] != "Watchlist":
                self.source[a] = found[a]
            self.first_seen.setdefault(a, t)
            self.next_check[a] = t + 300  # Standard; der Analyst setzt je nach Ergebnis neu
        if len(self.next_check) > 20000:  # Speicher begrenzen
            for a in [a for a, ts in self.next_check.items() if ts < t - 86400]:
                self.next_check.pop(a, None)
                self.first_seen.pop(a, None)
        with self.lock:
            self.stats["scanned"] += len(new) + len(again)
            self.stats["unique"] = len(self.first_seen)
        if new or again:
            srcs = {}
            for a in new:
                srcs[found[a]] = srcs.get(found[a], 0) + 1
            src_txt = ", ".join(f"{k} {v}" for k, v in srcs.items())
            self.log("scout", f"{len(new)} neue Coins" + (f" ({src_txt})" if src_txt else "") + f", {len(again)} erneut geprueft")
        return new + again

    def remember_organic(self, tok):
        a, s24, s1 = tok.get("audit") or {}, tok.get("stats24h") or {}, tok.get("stats1h") or {}
        try:
            created = datetime.fromisoformat(str(tok.get("createdAt")).replace("Z", "+00:00")).timestamp()
        except ValueError:
            created = None
        self.organic[tok["id"]] = {"ts": now(), "score": fnum(tok.get("organicScore")),
                                   "label": tok.get("organicScoreLabel"),
                                   "top_holders": fnum(a.get("topHoldersPercentage"), None),
                                   # fuer die Comeback-Suche (Coins wie $GTA6)
                                   "holders": fnum(tok.get("holderCount")), "holder_change_24h": fnum(s24.get("holderChange")),
                                   "liq_change_24h": fnum(s24.get("liquidityChange")), "price_change_24h": fnum(s24.get("priceChange")),
                                   "net_buyers_1h": fnum(s1.get("numNetBuyers")), "created": created,
                                   "safe_auth": bool(a.get("mintAuthorityDisabled") and a.get("freezeAuthorityDisabled")),
                                   # fuer den Memecoin Desk
                                   "mint_off": a.get("mintAuthorityDisabled"), "freeze_off": a.get("freezeAuthorityDisabled"),
                                   "dev_pct": fnum(a.get("devBalancePercentage"), None), "dev_mints": fnum(a.get("devMints"), None),
                                   "dev_migrations": fnum(a.get("devMigrations"), None),
                                   "traders_1h": fnum(s1.get("numTraders")), "organic_buyers_1h": fnum(s1.get("numOrganicBuyers")),
                                   "holder_change_1h": fnum(s1.get("holderChange"), None), "dev": tok.get("dev"),
                                   "buy_vol_1h": fnum(s1.get("buyVolume"), None), "sell_vol_1h": fnum(s1.get("sellVolume"), None),
                                   "price_change_1h": fnum(s1.get("priceChange"), None),
                                   "net_buyers_1h_raw": fnum(s1.get("numNetBuyers"), None), "traders_1h_raw": fnum(s1.get("numTraders"), None)}

    def fetch_organic(self, tokens):
        """Organic Score (echter vs. Bot-Handel) von Jupiter, bis zu 100 Coins pro Abfrage, 10 min Cache."""
        todo = [t for t in tokens if now() - self.organic.get(t, {}).get("ts", 0) > 600]
        for i in range(0, len(todo), 100):
            try:
                for tok in http_json(f"{JUP}/search?query={','.join(todo[i:i + 100])}") or []:
                    if tok.get("id"):
                        self.remember_organic(tok)
            except urllib.error.HTTPError as e:
                if e.code == 429:  # beim naechsten Durchlauf erneut versuchen
                    return
                self.log("analyst", f"Jupiter Organic Score: Fehler {e.code}", "warn")
            except Exception as e:
                self.log("analyst", f"Jupiter Organic Score nicht abrufbar: {e}", "warn")
            time.sleep(1)

    # ---------- Agent 2: Analyst ----------
    def rugcheck(self, token):
        cached = self.rug_cache.get(token)
        if cached and now() - cached[0] < 1800:
            return cached[1]
        try:
            res = http_json(RUGCHECK.format(token), timeout=10)
        except Exception:
            res = None
        self.rug_cache[token] = (now(), res)
        time.sleep(0.4)  # RugCheck nicht ueberlasten
        return res

    def creator_of(self, token):
        if token in self.creators:
            return self.creators[token]
        dev = None
        if self.chain == "solana":
            rc = self.rugcheck(token)
            dev = (rc or {}).get("creator")
        else:
            g = self.goplus(token) or {}
            dev = g.get("creator_address") or g.get("owner_address")
            if dev and dev.lower() in ("0x0000000000000000000000000000000000000000", "0x000000000000000000000000000000000000dead"):
                dev = None
        if dev:
            self.creators[token] = dev
        return dev

    def goplus(self, token):
        cached = self.goplus_cache.get(token)
        if cached and now() - cached[0] < 1800:
            return cached[1]
        try:
            d = http_json(GOPLUS.format(GOPLUS_CHAIN[self.chain], token), timeout=12)
            res = (d.get("result") or {}).get(token.lower())
        except Exception:
            res = None
        self.goplus_cache[token] = (now(), res)
        time.sleep(1)  # kostenloses Limit schonen
        return res

    def goplus_check(self, token, c):
        """Sicherheits-Check fuer EVM-Chains (BNB, Base, Robinhood): Honeypot, Steuern, Sperren, Halter, LP-Lock."""
        g = self.goplus(token)
        if not g:
            return False, "Sicherheitscheck (GoPlus) nicht verfuegbar", {"recheck_min": 10}
        flag = lambda k: str(g.get(k, "0")) == "1"
        if flag("is_honeypot") or flag("cannot_sell_all"):
            return False, "Honeypot (nicht verkaufbar)", {}
        for k, txt in (("owner_change_balance", "Owner kann Guthaben aendern"), ("hidden_owner", "versteckter Owner"),
                       ("transfer_pausable", "Handel pausierbar"), ("is_blacklisted", "Blacklist-Funktion")):
            if flag(k):
                return False, txt, {}
        tax = max(fnum(g.get("buy_tax")), fnum(g.get("sell_tax"))) * 100
        if tax > c.get("max_tax_pct", 10):
            return False, f"Steuer {tax:.0f}% zu hoch", {}
        holders = [h for h in g.get("holders") or [] if str(h.get("is_locked")) != "1"]
        top10 = sum(fnum(h.get("percent")) for h in holders[:10]) * 100
        lp = g.get("lp_holders") or []
        lp_total = sum(fnum(h.get("percent")) for h in lp)
        lp_locked = 100 * sum(fnum(h.get("percent")) for h in lp if str(h.get("is_locked")) == "1") / lp_total if lp_total else 0
        extra = {"top10_pct": top10, "lp_locked": lp_locked, "tax_pct": tax}
        if lp_locked < c.get("min_lp_locked_pct", 0):
            return False, f"LP nur {lp_locked:.0f}% gesperrt", dict(extra, recheck_min=60)
        return True, "ok", extra

    def chart_watch_loop(self):
        """Gehaltene Coins alle 30 min mit dem kompletten Chart pruefen (eigener Thread, GeckoTerminal ist langsam).
        Rug-Kerze (-90 % in einer Kerze) -> sofort raus und Dev sperren. Toter Coin -> raus, aber nur wenn er im Minus ist."""
        while not self.stop_event.is_set():
            c = self.cfg["analyst"]
            if c.get("chart_check", True) and self.cfg["risk"].get("chart_watch_minutes", 30):
                with self.lock:
                    due = [(t, p.get("pair"), p["symbol"]) for t, p in self.positions.items()
                           if now() - p.get("chart_checked_at", 0) >= self.cfg["risk"].get("chart_watch_minutes", 30) * 60]
                for token, pool, sym in due:
                    if self.stop_event.is_set() or not pool:
                        break
                    ok, reason, st = self.chart_check({"pairAddress": pool}, c)
                    with self.lock:
                        pos = self.positions.get(token)
                        if not pos:
                            continue
                        if ok is None and reason == "Chart nicht abrufbar":
                            continue  # spaeter nochmal
                        pos["chart_checked_at"] = now()
                        if st:
                            pos.setdefault("info", {})["chart"] = st
                        if ok is False:
                            losing = pos["last_price"] < pos["entry_price"]
                            if reason.startswith("Rug im Chart"):
                                self.rug_alarm.setdefault(token, reason)
                            elif losing:
                                self.chart_exit[token] = f"Chart-Check: {reason}"
                            else:
                                self.log("risk", f"${sym}: {reason}, aber im Plus -> gehalten", "info")
            self.stop_event.wait(60)

    def chart_check(self, pair, c):
        """Kompletten Chart pruefen (15-min-Kerzen seit Start, bei jungen Coins 1-min-Kerzen).
        -> (ok, Grund, Werte). Rug-Muster: Absturz-Kerze, toter Coin weit unter dem Hoch ohne Volumen."""
        pool = pair.get("pairAddress")
        hit = self.chart_cache.get(pool)
        if hit and now() - hit[0] < 600:
            st = hit[1]
        else:
            st = None
            try:
                for agg, unit in ((15, "minute"), (1, "minute")):
                    url = f"{self.gecko}/pools/{pool}/ohlcv/{unit}?aggregate={agg}&limit=1000&currency=usd"
                    for attempt in range(3):  # Rate-Limit (429): kurz warten und nochmal
                        try:
                            raw = gecko_json(url)
                            break
                        except urllib.error.HTTPError as e:
                            if e.code != 429 or attempt == 2:
                                raise
                            time.sleep(4 * (attempt + 1))
                    st = chart_stats(raw["data"]["attributes"]["ohlcv_list"])
                    if st and st["hours"] >= 3:
                        break  # genug Verlauf; sonst junge Coins mit 1-min-Kerzen genauer ansehen
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    return None, "Chart nicht abrufbar", {}
                st = None  # GeckoTerminal kennt den Pool (noch) nicht
            except Exception:
                return None, "Chart nicht abrufbar", {}
            self.chart_cache[pool] = (now(), st)
        if not st:
            return None, "Chart noch zu kurz", {}
        if c.get("max_candle_crash_pct") and st["worst_candle_pct"] <= -c["max_candle_crash_pct"]:
            when = time.strftime("%d.%m. %H:%M", time.localtime(st["worst_candle_ts"])) if st["worst_candle_ts"] else "?"
            return False, f"Rug im Chart: Kerze mit {st['worst_candle_pct']:.0f}% am {when}", st
        if c.get("max_below_ath_pct") and st["from_ath_pct"] <= -c["max_below_ath_pct"]:
            return False, f"toter Coin: {st['from_ath_pct']:.0f}% unter dem Hoch", st
        if c.get("dead_volume_any_pct") and st["vol_vs_max_pct"] < c["dead_volume_any_pct"] and st["hours"] >= 2:
            return False, f"toter Coin: Volumen nur {st['vol_vs_max_pct']:.1f}% vom Maximum", st
        if (c.get("dead_below_ath_pct") and st["from_ath_pct"] <= -c["dead_below_ath_pct"]
                and st["vol_vs_max_pct"] < c.get("dead_volume_pct", 2.5)):
            return False, f"toter Coin: {st['from_ath_pct']:.0f}% unter dem Hoch, Volumen nur {st['vol_vs_max_pct']:.1f}% vom Maximum", st
        return True, "Chart ok", st

    def comeback_candidate(self, token, c):
        """Coin wie $GTA6: aelter, grosse wachsende Community, echter Handel, sicherer Vertrag, steigt gerade stark.
        Nur Zahlen von Jupiter, ohne Abfrage. Der Chart wird danach in comeback_chart geprueft."""
        cb = c.get("comeback", {})
        j = self.organic.get(token) or {}
        if not cb.get("enabled") or not j.get("created"):
            return False
        age_d = (now() - j["created"]) / 86400
        return (cb.get("min_age_days", 2) <= age_d <= cb.get("max_age_days", 365)
                and j.get("holders", 0) >= cb.get("min_holders", 3000)
                and j.get("holder_change_24h", 0) >= cb.get("min_holder_change_pct", 10)
                and j.get("score", 0) >= cb.get("min_organic_score", 60)
                and j.get("price_change_24h", 0) >= cb.get("min_price_change_24h_pct", 20)
                and j.get("liq_change_24h", 0) > 0 and j.get("net_buyers_1h", 0) > 0 and j.get("safe_auth")
                and (j.get("top_holders") if j.get("top_holders") is not None else 100) <= cb.get("max_top10_pct", 25))

    def comeback_chart(self, pair, c):
        """Tageschart: Ausbruch nahe am Allzeithoch, Volumen springt an, hoehere Tiefs. -> (ok, Grund, Werte)"""
        cb = c.get("comeback", {})
        pool = pair.get("pairAddress")
        hit = self.chart_cache.get(("day", pool))
        if hit and now() - hit[0] < 1800:
            days = hit[1]
        else:
            try:
                days = sorted(gecko_json(f"{self.gecko}/pools/{pool}/ohlcv/day?limit=120&currency=usd")["data"]["attributes"]["ohlcv_list"])
            except Exception:
                return None, "Tageschart nicht abrufbar", {}
            self.chart_cache[("day", pool)] = (now(), days)
        if len(days) < 5:
            return False, "Comeback: zu wenig Tageskerzen", {}
        ath = max(fnum(d[2]) for d in days)
        price = fnum(pair.get("priceUsd")) or fnum(days[-1][4])
        prev = sum(fnum(d[5]) for d in days[-4:-1]) / 3
        jump = fnum((pair.get("volume") or {}).get("h24")) / prev if prev > 0 else 99
        lows = [fnum(d[3]) for d in days[-6:]]
        higher = sum(1 for x, y in zip(lows, lows[1:]) if y >= x * 0.97)
        st = {"from_ath_pct": round((price / ath - 1) * 100, 1) if ath else 0, "volume_jump": round(jump, 1), "higher_lows": higher, "days": len(days)}
        if st["from_ath_pct"] < -cb.get("max_below_ath_pct", 15):
            return False, f"Comeback: noch {st['from_ath_pct']:.0f}% unter dem Hoch (kein Ausbruch)", st
        if jump < cb.get("min_volume_jump", 1.5):
            return False, f"Comeback: Volumen nur {jump:.1f}x der Vortage", st
        if higher < cb.get("min_higher_lows", 3):
            return False, f"Comeback: kein Aufwaertstrend (nur {higher}/5 hoehere Tiefs)", st
        return True, "Comeback-Ausbruch", st

    @staticmethod
    def avg_trade_usd(pair):
        """Durchschnittliche Tradegroesse der letzten Stunde -> (Dollar pro Trade, Anzahl Trades)."""
        tx = (pair.get("txns") or {}).get("h1") or {}
        n = fnum(tx.get("buys")) + fnum(tx.get("sells"))
        return fnum((pair.get("volume") or {}).get("h1")) / max(n, 1), n

    def copycat_of(self, pair):
        """Gleicher Name/Ticker wie ein gemeldeter Rug (z. B. zweiter $USDF nach dem USDF-Rug) -> Ticker des Rugs."""
        norm = lambda x: re.sub(r"[^A-Z0-9]", "", (x or "").upper())
        sym, name = norm(pair["baseToken"].get("symbol")), norm(pair["baseToken"].get("name"))
        token = pair["baseToken"]["address"]
        for b in list(self.blacklist.values()):
            if b.get("token") == token:
                continue
            if sym and len(sym) >= 3 and norm(b.get("symbol")) == sym:
                return b.get("symbol")
            if name and len(name) >= 6 and norm(b.get("name")) == name:
                return b.get("symbol")
        return None

    @property
    def last_exit(self):
        """Token -> Zeit des letzten kompletten Verkaufs."""
        if getattr(self, "_last_exit_n", -1) != len(self.closed):
            self._last_exit = {}
            for t in self.closed:
                if not t.get("partial"):
                    self._last_exit[t["token"]] = max(self._last_exit.get(t["token"], 0), t.get("closed_at", 0))
            self._last_exit_n = len(self.closed)
        return self._last_exit

    # ================= Memecoin Desk (Scout -> Filter -> Analyst -> Executor -> Risk -> Logger) =================
    def desk_kill(self, pair, stage, reason, extra=None):
        """Logger: jeder Kill wird mit Stufe und Grund festgehalten."""
        rec = {"ts": now(), "symbol": clean_symbol(pair["baseToken"].get("symbol", "?")), "mint": pair["baseToken"]["address"],
               "stage": stage, "reason": reason}
        if extra:
            rec.update(extra)
        with self.lock:
            self.kill_log.append(rec)
            del self.kill_log[:-5000]
            self.stats["desk_" + stage.lower()] = self.stats.get("desk_" + stage.lower(), 0) + 1
            self.stats["desk_logged"] = self.stats.get("desk_logged", 0) + 1

    def analyze_desk(self, pair):
        d = self.cfg["desk"]
        token = pair["baseToken"]["address"]
        liq = fnum((pair.get("liquidity") or {}).get("usd"))
        mcap = fnum(pair.get("marketCap") or pair.get("fdv"))
        vol_h1 = fnum((pair.get("volume") or {}).get("h1"))
        tx1 = (pair.get("txns") or {}).get("h1") or {}
        buys, sells = fnum(tx1.get("buys")), fnum(tx1.get("sells"))
        ch = pair.get("priceChange") or {}
        ch_m5, ch_h1 = fnum(ch.get("m5")), fnum(ch.get("h1"))
        created = fnum(pair.get("pairCreatedAt")) / 1000
        age_min = (now() - created) / 60 if created else 0
        info = {"liq": liq, "mcap": mcap, "age_min": age_min, "ch_h1": ch_h1, "buys_h1": buys, "sells_h1": sells, "vol_h1": vol_h1}
        sym = (pair["baseToken"].get("symbol") or "").upper()

        # --- Scout: nur Paare 30 min - 72 h alt, MCap 300k - 20 Mio. $ (kein Kill, nur ausserhalb des Suchbereichs)
        if sym in QUOTE_SYMBOLS or fnum(pair.get("priceUsd")) <= 0:
            return False, "kein Memecoin", dict(info, recheck_min=1440)
        if age_min < d["min_age_minutes"]:
            return False, f"Scout: zu jung ({age_min:.0f} min)", dict(info, recheck_min=max(d["min_age_minutes"] - age_min, 1), young=True)
        if age_min > d["max_age_hours"] * 60:
            return False, "Scout: aelter als 72 h", dict(info, recheck_min=10080)
        if not (d["min_mcap_usd"] <= mcap <= d["max_mcap_usd"]):
            return False, f"Scout: MCap ${mcap:,.0f} ausserhalb", dict(info, recheck_min=15)
        with self.lock:
            self.stats["desk_scout"] = self.stats.get("desk_scout", 0) + 1
            self.range_ts.append(now())
            self.vol_window[token] = (now(), vol_h1)
            norm = re.sub(r"[^A-Z0-9]", "", sym)
            best = self.sym_best.get(norm)
            if norm and (not best or best[0] == token or mcap > best[1]):
                self.sym_best[norm] = (token, mcap)

        # --- Filter: harte Kills (fehlt ein Sicherheitswert -> "missing" = Kill)
        def kill(reason, recheck=60):
            self.desk_kill(pair, "Filter", reason)
            return False, "Filter: " + reason, dict(info, recheck_min=recheck)
        if liq < d["min_liquidity_usd"]:
            return kill(f"Liquiditaet ${liq:,.0f} < ${d['min_liquidity_usd']:,.0f}", 15)
        if vol_h1 < d["min_volume_h1_usd"]:
            return kill(f"Volumen 1h ${vol_h1:,.0f} < ${d['min_volume_h1_usd']:,.0f}", 15)
        if ch_h1 >= d["max_change_h1_pct"]:
            return kill(f"schon +{ch_h1:.0f}% in 1h", 30)
        best = self.sym_best.get(re.sub(r"[^A-Z0-9]", "", sym))
        if d.get("block_copycats", True) and best and best[0] != token and best[1] >= mcap * d.get("copycat_mcap_factor", 3):
            return kill(f"Nachahmer-Ticker (${sym} gibt es groesser: MCap ${best[1]:,.0f})", 1440)
        cc = self.copycat_of(pair)
        if cc:
            return kill(f"Nachahmer eines Rugs (${cc})", 1440)
        avg_t, n_t = self.avg_trade_usd(pair)
        if n_t >= d.get("fake_min_trades_h1", 300) and avg_t < d.get("fake_min_avg_trade_usd", 8):
            return kill(f"Fake-Volumen (${avg_t:.1f} pro Trade bei {n_t:.0f} Trades/1h)", 360)
        if self.chain == "solana" and token not in self.organic:
            self.fetch_organic([token])
        j = self.organic.get(token) or {}
        if j and j.get("score") is not None and j.get("score", 100) < d.get("min_organic_score", 15) and vol_h1 >= 100000:
            return kill(f"Fake-Volumen (Jupiter Organic Score {j['score']:.0f})", 360)
        rc = self.rugcheck(token) if self.chain == "solana" else None
        if rc is None:
            return False, "RugCheck nicht verfuegbar", dict(info, recheck_min=5)
        if rc.get("rugged"):
            return kill("RugCheck: gerugt", 10080)
        if rc.get("mintAuthority") or j.get("mint_off") is False:
            return kill("Mint nicht gesperrt", 1440)
        if rc.get("freezeAuthority") or j.get("freeze_off") is False:
            return kill("Freeze nicht gesperrt", 1440)
        lps = [m.get("lp") or {} for m in rc.get("markets") or []]
        total = sum(fnum(x.get("baseUSD")) + fnum(x.get("quoteUSD")) for x in lps)
        lp = fnum(rc.get("lpLockedPct")) if rc.get("lpLockedPct") is not None else (100 * sum(fnum(x.get("lpLockedUSD")) for x in lps) / total if total else None)
        if lp is None:
            return kill("LP-Status missing", 60)
        info["lp_locked"] = lp
        if lp < d["min_lp_locked_pct"]:
            return kill(f"LP nicht verbrannt/gesperrt ({lp:.0f}%)", 360)
        top10 = j.get("top_holders")
        if top10 is None and rc.get("topHolders"):  # RugCheck: Top-10 ohne Pool-Konten
            pools = {m.get("pubkey") for m in rc.get("markets") or []} | {(m.get("lp") or {}).get("lpMint") for m in rc.get("markets") or []}
            top10 = min(sum(fnum(h.get("pct")) for h in (rc.get("topHolders") or []) if h.get("owner") not in pools), 100)
        if top10 is None:
            return kill("Top-10-Anteil missing", 60)
        info["top10_pct"] = top10
        if top10 > d["max_top10_pct"]:
            return kill(f"Top-10 halten {top10:.0f}% (> {d['max_top10_pct']}%)", 120)
        supply = fnum((rc.get("token") or {}).get("supply"))
        dev_pct = j.get("dev_pct")
        if dev_pct is None and supply and rc.get("creatorBalance") is not None:
            dev_pct = 100 * fnum(rc.get("creatorBalance")) / supply
        if dev_pct is None:
            return kill("Dev-Anteil missing", 60)
        info["dev_pct"] = dev_pct
        if dev_pct > d["max_dev_pct"]:
            return kill(f"Dev haelt {dev_pct:.1f}% (> {d['max_dev_pct']}%)", 360)
        first = self.dev_seen.setdefault(token, dev_pct)
        if first - dev_pct >= d.get("dev_selling_pts", 0.5):
            return kill(f"Dev verkauft noch ({first:.1f}% -> {dev_pct:.1f}%)", 1440)
        dev = rc.get("creator") or j.get("dev")
        if dev:
            self.creators[token] = dev
        if dev and dev in self.blacklist:
            return kill(f"Dev hat schon gerugt (${self.blacklist[dev].get('symbol', '?')})", 10080)
        mints, migr = j.get("dev_mints"), j.get("dev_migrations")
        if mints and mints >= d["serial_dev_min_mints"] and (migr or 0) / mints < d["serial_dev_max_success"]:
            return kill(f"Serien-Dev: {mints:.0f} Coins gestartet, nur {migr or 0:.0f} geschafft", 10080)
        bundle = 100 * sum(fnum(n.get("currentHolding")) for n in rc.get("insiderNetworks") or []) / supply if supply else 0
        info["bundle_pct"] = bundle
        if bundle > d["max_bundle_pct"]:
            return kill(f"Bundle/Insider halten {bundle:.0f}% (> {d['max_bundle_pct']}%)", 360)

        # --- Judge: 4 geschlossene Fragen, je JA/NEIN mit Wahrscheinlichkeit -> Score 0-100
        judge = self.judge(token, j, pair)
        w = d["judge_weights"]
        score = 100 * sum(judge[k]["p"] * w[k] for k in judge) / sum(w.values())
        info["score"] = round(score)
        info["judge"] = judge
        info["score_parts"] = {k: round(v["p"] * 100) for k, v in judge.items()}
        with self.lock:
            self.stats["desk_scored"] = self.stats.get("desk_scored", 0) + 1
            self.desk_scores.append({"ts": now(), "symbol": clean_symbol(pair["baseToken"].get("symbol")), "mint": token, "score": round(score),
                                     "parts": info["score_parts"], "answers": {k: v["a"] for k, v in judge.items()}})
            del self.desk_scores[:-200]
        if score < d["min_score"]:
            self.desk_kill(pair, "Judge", f"Score {score:.0f} < {d['min_score']}", {"score": round(score), "parts": info["score_parts"]})
            return False, f"Judge: Score {score:.0f}/100 (< {d['min_score']})", dict(info, recheck_min=10)
        with self.lock:
            self.stats["desk_passed"] = self.stats.get("desk_passed", 0) + 1
        return True, f"Score {score:.0f}/100", info

    def judge(self, token, j, pair):
        """4 feste Fragen, nur aus Daten beantwortet. Fehlt ein Wert -> Antwort "missing" (zaehlt wie NEIN mit p 0)."""
        clamp = lambda x: max(0.0, min(1.0, x))
        out = {}
        nb, tr = j.get("net_buyers_1h_raw"), j.get("traders_1h_raw")
        # a) Wachsen die einzigartigen Kaeufer in der letzten Stunde? (mehr neue Kaeufer als Verkaeufer, im Verhaeltnis zu allen Tradern)
        out["buyers"] = None if nb is None or not tr else clamp(0.5 + nb / max(tr, 1) * 2.5)
        # b) Wachsen die Halter schneller als der Preis?
        hc, pc = j.get("holder_change_1h"), j.get("price_change_1h")
        if pc is None:
            pc = fnum((pair.get("priceChange") or {}).get("h1"), None)
        out["holders"] = None if hc is None or pc is None else clamp(0.5 + (hc - max(pc, 0) / 4) / 8)
        # c) Mehr Kauf- als Verkaufsvolumen? (Jupiter 1h-Volumen, sonst Anzahl Trades bei DexScreener, 5 min + 1 h)
        bv, sv = j.get("buy_vol_1h"), j.get("sell_vol_1h")
        if bv is not None and sv is not None and bv + sv > 0:
            r = bv / (bv + sv)
        else:
            t5 = (pair.get("txns") or {}).get("m5") or {}
            t1 = (pair.get("txns") or {}).get("h1") or {}
            b, s = fnum(t5.get("buys")) + fnum(t1.get("buys")), fnum(t5.get("sells")) + fnum(t1.get("sells"))
            r = b / (b + s) if b + s else None
        out["flow"] = None if r is None else clamp((r - 0.45) / 0.15)
        # d) Hat eine bewaehrte Wallet gekauft und haelt noch?
        mine = [t for t in self.ww_trades if t.get("mint") == token and now() - t.get("ts", 0) < 7200]
        bought = any(t.get("side") == "Kauf" for t in mine)
        sold = any(t.get("side") == "Verkauf" for t in mine)
        fb = (self.fomo_counts().get(token) or (0, 0))[0]
        out["smart"] = 0.95 if bought and not sold else 0.0 if sold else 0.7 if fb >= 3 else self.cfg["desk"].get("smart_unknown_p", 0.3)
        return {k: {"p": round(v or 0.0, 2), "a": "missing" if v is None else ("JA" if v >= 0.5 else "NEIN")} for k, v in out.items()}

    # ================= Markt-Regler, Tagesgrenze, Pausen =================
    def market_update(self):
        mc = self.cfg.get("market", {})
        ids = {"SOL": WSOL, "BTC": "cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij",
               "USDC": USDC_MINT, "USDT": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}
        data = {}
        try:
            for tok in http_json(f"{JUP}/search?query={','.join(ids.values())}") or []:
                for k, m in ids.items():
                    if tok.get("id") == m:
                        data[k] = {"price": fnum(tok.get("usdPrice")), "h1": fnum((tok.get("stats1h") or {}).get("priceChange"), None),
                                   "h24": fnum((tok.get("stats24h") or {}).get("priceChange"), None)}
        except Exception as e:
            self.log("system", f"Markt-Regler: Jupiter nicht erreichbar ({e})", "warn")
        clamp = lambda x: max(0.0, min(1.0, x))
        t = now()
        with self.lock:
            for m in [m for m, (ts, _) in self.vol_window.items() if t - ts > 3600]:
                del self.vol_window[m]
            vol_now = sum(v for _, v in self.vol_window.values())
            while self.range_ts and t - self.range_ts[0] > 6 * 3600:
                self.range_ts.popleft()
            in_range = len(self.range_ts)
            rugs = sum(1 for k in self.kill_log if t - k["ts"] <= 6 * 3600 and k.get("stage") == "Filter" and kill_is_rug(k.get("reason", "")))
            if not self.meme_samples or t - self.meme_samples[-1][0] >= 3600:
                if t - self.run_started >= 3000:  # erst nach ~50 min Laufzeit ist die Stunde voll
                    self.meme_samples.append([t, vol_now])
                    self.meme_samples = [x for x in self.meme_samples if t - x[0] <= 7 * 86400]
        parts, info = {}, {}
        sol, btc = data.get("SOL"), data.get("BTC")
        parts["sol"] = None if not sol or sol["h24"] is None else clamp(0.6 + sol["h24"] / 25)
        parts["btc"] = None if not btc or btc["h24"] is None else clamp(0.6 + btc["h24"] / 15)
        avg = [v for _, v in self.meme_samples[-168:]]
        if len(avg) >= 3 and sum(avg) > 0 and t - self.run_started >= 3000:
            ratio = vol_now / (sum(avg) / len(avg))
            parts["meme"] = clamp(ratio / 1.2)
            info["meme_ratio"] = round(ratio, 2)
        else:
            parts["meme"] = None
        share = rugs / in_range if in_range >= 30 else None
        parts["rugs"] = None if share is None else clamp(1 - (share - 0.5) / 0.4)
        info.update({"rug_share": None if share is None else round(share, 2), "in_range_6h": in_range, "meme_vol_1h": round(vol_now),
                     "sol": sol, "btc": btc, "usdc": data.get("USDC"), "usdt": data.get("USDT")})
        w = mc.get("weights", {"sol": 0.3, "btc": 0.2, "meme": 0.25, "rugs": 0.25})
        known = {k: v for k, v in parts.items() if v is not None}
        dial = sum(known[k] * w[k] for k in known) / sum(w[k] for k in known) if known else None
        # Alarm: SOL/BTC-Absturz in 1 h oder Stablecoin-Depeg
        alert = None
        if sol and sol["h1"] is not None and sol["h1"] <= -mc.get("sol_crash_1h_pct", 8):
            alert = f"SOL {sol['h1']:.1f}% in 1 h"
        elif btc and btc["h1"] is not None and btc["h1"] <= -mc.get("btc_crash_1h_pct", 5):
            alert = f"BTC {btc['h1']:.1f}% in 1 h"
        for k in ("USDC", "USDT"):
            x = data.get(k)
            if x and x["price"] and abs(x["price"] - 1) >= mc.get("depeg_pct", 2) / 100:
                alert = f"{k}-Depeg ({x['price']:.3f} $)"
        with self.lock:
            m = self.market
            if alert and not m.get("alert"):
                self.log("system", f"MARKT-ALARM: {alert} - nur noch scannen, keine Kaeufe", "warn")
            if alert:
                m["alert"], m["alert_since"], m["good_since"] = alert, t, 0
            if dial is not None and dial >= mc.get("min_dial", 0.5):
                m["good_since"] = m.get("good_since") or t
            else:
                m["good_since"] = 0
            if m.get("alert") and not alert and m["good_since"] and t - m["good_since"] >= mc.get("calm_minutes", 60) * 60:
                self.log("system", f"Markt wieder ruhig (Regler {dial:.2f} seit {mc.get('calm_minutes', 60)} min) - Kaeufe wieder erlaubt", "pass")
                m["alert"] = None
            m.update({"dial": None if dial is None else round(dial, 2), "parts": {k: (None if v is None else round(v, 2)) for k, v in parts.items()},
                      "ts": t, "info": info, "alert_now": alert})

    # ================= Hand-Trades: von Hand angelegte Spielgeld-Kaeufe (manual_trades.json) =================
    def manual_trades(self):
        try:
            with open(os.path.join(HERE, "manual_trades.json"), encoding="utf-8") as f:
                return json.load(f).get("trades", [])
        except (OSError, ValueError):
            return []

    def manual_loop(self):
        """Kurse der Hand-Trades verfolgen (jede Chain, ueber DexScreener). Zaehlt nicht zur Bot-Statistik."""
        while not self.stop_event.is_set():
            try:
                for tr in self.manual_trades():
                    if tr.get("closed_at"):
                        continue
                    d = http_json(f"{DEX}/tokens/v1/{tr['chain']}/{tr['token']}") or []
                    p = next((x for x in d if (x.get("pairAddress") or "").lower() == (tr.get("pair") or "").lower()), None)
                    p = p or max(d, key=lambda x: fnum((x.get("liquidity") or {}).get("usd")), default=None)
                    if not p or fnum(p.get("priceUsd")) <= 0:
                        continue
                    price = fnum(p["priceUsd"])
                    with self.lock:
                        live = self.manual_live.setdefault(tr["id"], {"peak": price, "hist": []})
                        live.update({"price": price, "mcap": fnum(p.get("marketCap") or p.get("fdv")), "liq": fnum((p.get("liquidity") or {}).get("usd")),
                                     "ts": now(), "peak": max(live.get("peak", price), price)})
                        h = live["hist"]
                        if not h or now() - h[-1][0] >= 60:
                            h.append([now(), price])
                            del h[:-1500]
            except Exception as e:
                self.log("system", f"Hand-Trades: {e}", "warn")
            self.stop_event.wait(20)

    def manual_view(self):
        out = []
        rate = self.cfg.get("usd_pro_eur", 1)
        exit_cost = (self.cfg["sniper"]["base_slippage_pct"] + self.cfg["sniper"]["fee_pct_per_side"]) / 100
        for tr in self.manual_trades():
            live = self.manual_live.get(tr["id"], {})
            cost = tr.get("cost_usd") or tr["eur"] * rate
            price = tr.get("exit_price") or live.get("price") or tr["entry_price"]
            value = tr["qty"] * price * (1 - exit_cost)
            out.append({"id": tr["id"], "symbol": tr["symbol"], "chain": tr["chain"], "token": tr["token"], "url": tr.get("url"),
                        "opened_at": tr["opened_at"], "closed_at": tr.get("closed_at"), "cost_usd": cost, "value_usd": value,
                        "pnl_usd": value - cost, "pnl_pct": (value / cost - 1) * 100, "entry_price": tr["entry_price"], "price": price,
                        "peak_pct": ((live.get("peak") or price) / tr["entry_price"] - 1) * 100, "mcap": live.get("mcap"),
                        "entry_mcap": tr.get("entry_mcap"), "liq": live.get("liq"), "price_ts": live.get("ts"), "note": tr.get("note"),
                        "hist": downsample(live.get("hist") or [], 200)})
        return out

    def market_loop(self):
        while not self.stop_event.is_set():
            try:
                self.market_update()
                self.check_day()
            except Exception as e:
                self.errors += 1
                self.log("system", f"Markt-Regler: {e}", "error")
            self.stop_event.wait(self.cfg.get("market", {}).get("interval_s", 120))

    def check_day(self):
        """Neuer Tag (UTC) -> Startwert fuer die Tagesverlust-Grenze merken."""
        d = time.strftime("%Y-%m-%d", time.gmtime())
        with self.lock:
            if self.day.get("date") != d:
                self.day = {"date": d, "start_equity": round(self.equity(), 2)}

    def entry_block(self):
        """Grund, warum gerade nicht gekauft wird (None = Kaufen erlaubt)."""
        mc, r = self.cfg.get("market", {}), self.cfg["risk"]
        self.check_day()
        m = self.market
        if m.get("alert"):
            return f"Markt-Alarm ({m['alert']}) - nur scannen"
        if m.get("dial") is not None and m["dial"] < mc.get("min_dial", 0.5):
            return f"Markt-Regler {m['dial']:.2f} < {mc.get('min_dial', 0.5)} - nur scannen"
        if m.get("dial") is None and mc.get("block_without_dial", True):
            return "Markt-Regler noch ohne Daten"
        lim = r.get("daily_loss_limit_pct")
        if lim and self.day.get("start_equity") and self.equity() <= self.day["start_equity"] * (1 - lim / 100):
            return f"Tagesverlust-Grenze -{lim}% erreicht - bis morgen keine Kaeufe"
        if now() < self.pause_until:
            return f"Pause nach {r.get('loss_streak_max', 3)} Verlusten in Folge (noch {(self.pause_until - now()) / 60:.0f} min)"
        return None

    def analyze(self, pair):
        if self.cfg.get("desk", {}).get("enabled"):
            return self.analyze_desk(pair)
        c = self.cfg["analyst"]
        liq = fnum((pair.get("liquidity") or {}).get("usd"))
        mcap = fnum(pair.get("marketCap") or pair.get("fdv"))
        vol_h1 = fnum((pair.get("volume") or {}).get("h1"))
        tx_h1 = (pair.get("txns") or {}).get("h1") or {}
        buys, sells = fnum(tx_h1.get("buys")), fnum(tx_h1.get("sells"))
        ch = pair.get("priceChange") or {}
        ch_m5, ch_h1 = fnum(ch.get("m5")), fnum(ch.get("h1"))
        created = fnum(pair.get("pairCreatedAt")) / 1000
        age_min = (now() - created) / 60 if created else 0
        price = fnum(pair.get("priceUsd"))

        young_wait = max(c["min_age_minutes"] - age_min, 1)
        # (ok, Grund, in wie vielen Minuten erneut pruefen)
        vol_h24 = fnum((pair.get("volume") or {}).get("h24"))
        token = pair["baseToken"]["address"]
        tx_h6 = (pair.get("txns") or {}).get("h6") or {}
        buys6, sells6 = fnum(tx_h6.get("buys")), fnum(tx_h6.get("sells"))
        depth = pool_depth(pair)
        lh = self.liq_hist.setdefault(token, [])
        lh.append((now(), depth))
        del lh[:-30]
        past = [l for ts, l in lh if now() - 5400 <= ts < now() - 600]
        shrink = (1 - depth / max(past)) * 100 if past and max(past) > 0 and depth > 0 else 0  # Liquiditaet abgezogen
        ch_h6, ch_h24 = fnum(ch.get("h6")), fnum(ch.get("h24"))
        avg_trade, tx_n = self.avg_trade_usd(pair)
        tx5 = (pair.get("txns") or {}).get("m5") or {}
        tx_m5 = fnum(tx5.get("buys")) + fnum(tx5.get("sells"))
        vol_m5 = fnum((pair.get("volume") or {}).get("m5"))
        avg_hour = vol_h24 / min(24, max(age_min / 60, 1))
        vol_trend = max(vol_h1, vol_m5 * 12) / avg_hour if avg_hour > 0 else 1  # laeuft gerade wieder an -> zaehlt auch
        copycat = self.copycat_of(pair) if c.get("block_copycats", True) else None
        comeback = self.comeback_candidate(token, c)
        max_age_min = c.get("comeback", {}).get("max_age_days", 365) * 1440 if comeback else c["max_age_hours"] * 60
        checks = [
            ((pair["baseToken"].get("symbol") or "").upper() not in QUOTE_SYMBOLS, "Basis-Token (kein Memecoin)", 10080),
            (self.token_losses.get(token, 0) < c.get("max_losses_per_token", 99), "zu viele Verluste mit diesem Coin", 10080),
            # nicht denselben Coin kurz nach dem Verkauf wieder kaufen (nach einem grossen Lauf = Kauf am Top)
            (not c.get("rebuy_block_hours") or now() - self.last_exit.get(token, 0) >= c["rebuy_block_hours"] * 3600,
             f"vor kurzem schon gehandelt (Wiederkauf erst nach {c.get('rebuy_block_hours')} h)", 60),
            (price > 0, "kein Preis", 10),
            (liq >= c["min_liquidity_usd"], f"Liquiditaet ${liq:,.0f} zu gering", 15),
            (liq <= c["max_liquidity_usd"], "zu gross/kein Memecoin", 1440),
            (age_min >= c["min_age_minutes"], f"zu jung ({age_min:.0f} min)", young_wait),
            (age_min <= max_age_min, "zu alt", 10080 if not c.get("comeback", {}).get("enabled") else 60),
            (vol_h1 >= c["min_volume_h1_usd"], f"Volumen 1h ${vol_h1:,.0f} zu gering", 10),
            (vol_h24 >= c.get("min_volume_h24_usd", 0), f"Volumen 24h unter ${c.get('min_volume_h24_usd', 0):,.0f}", 15),
            (c["min_mcap_usd"] <= mcap <= c["max_mcap_usd"], f"MCap ${mcap:,.0f} ausserhalb", 15),
            (buys >= c["min_buys_h1"], f"nur {buys:.0f} Kaeufe/1h", 10),
            (sells == 0 or buys / sells >= c["min_buy_sell_ratio_h1"], f"zu wenig Kaeufer ({buys / max(sells, 1):.1f} Kaeufer pro Verkaeufer)", 10),
            (not c.get("max_sell_buy_ratio_h1") or sells <= c["max_sell_buy_ratio_h1"] * max(buys, 1),
             f"starker Verkaufsdruck ({sells:.0f} Verkaeufe vs. {buys:.0f} Kaeufe/1h)", 15),
            (not c.get("max_sell_buy_ratio_h6") or sells6 <= c["max_sell_buy_ratio_h6"] * max(buys6, 1),
             f"Slow-Rug: langsamer Abverkauf ({sells6:.0f} Verkaeufe vs. {buys6:.0f} Kaeufe/6h)", 30),
            (not c.get("max_liq_shrink_pct_90m") or shrink < c["max_liq_shrink_pct_90m"],
             f"Rug: Liquiditaet abgezogen (Pool -{shrink:.0f}% in 90 min)", 30),
            (ch_h1 >= c["min_change_h1_pct"], f"faellt ({ch_h1:+.0f}% 1h)", 10),
            (ch_m5 <= c["max_change_m5_pct"], f"schon gepumpt ({ch_m5:+.0f}% 5m)", 10),
            (ch_h1 <= c["max_change_h1_pct"], f"schon gepumpt ({ch_h1:+.0f}% 1h)", 10),
            # schon gerugt: Kurs in 6 h / 24 h abgestuerzt, die letzte Stunde sieht nur nach Erholung aus (z. B. $USDF -99 %)
            (not c.get("max_crash_h6_pct") or ch_h6 > -c["max_crash_h6_pct"], f"schon gerugt ({ch_h6:+.0f}% 6h)", 1440),
            (not c.get("max_crash_h24_pct") or ch_h24 > -c["max_crash_h24_pct"], f"schon gerugt ({ch_h24:+.0f}% 24h)", 1440),
            # Fake-Handel: tausende Mini-Trades von Bots (USDF: 1-4 $ pro Trade, echte Coins 20-100 $)
            (not c.get("min_avg_trade_usd") or tx_n < 300 or avg_trade >= c["min_avg_trade_usd"],
             f"Fake-Handel (nur ${avg_trade:.1f} pro Trade bei {tx_n:.0f} Trades/1h)", 360),
            (not copycat, f"Nachahmer von gemeldetem Rug ${copycat}", 10080),
            # toter Coin: gerade handelt kaum jemand / Volumen stirbt (aktuelle Stunde < 30 % einer normalen Stunde)
            (tx_m5 >= c.get("min_trades_m5", 0), f"toter Coin: nur {tx_m5:.0f} Trades in 5 min", 15),
            (not c.get("min_volume_trend") or age_min < 120 or vol_trend >= c["min_volume_trend"],
             f"toter Coin: Volumen stirbt (aktuelle Stunde {vol_trend:.0%} einer normalen Stunde)", 30),
        ]
        for ok, reason, recheck in checks:
            if not ok:
                return False, reason, {"recheck_min": recheck, "young": reason.startswith("zu jung")}

        allowed = c.get("allowed_sources")
        if allowed and self.source.get(token) not in allowed:
            return False, "nicht aus erlaubter Quelle", {"recheck_min": 30}
        info = {"liq": liq, "mcap": mcap, "age_min": age_min, "ch_h1": ch_h1, "buys_h1": buys, "sells_h1": sells}
        org = self.organic.get(pair["baseToken"]["address"])
        if org:
            info["organic"] = org["score"]
        if c.get("min_organic_score", 0) > 0:
            if not org:
                return False, "kein Organic Score", dict(info, recheck_min=10)
            if org["score"] < c["min_organic_score"]:
                return False, f"Organic Score {org['score']:.0f} zu niedrig (Bot-Handel?)", dict(info, recheck_min=15)
        if org and org.get("top_holders") is not None:
            info["top10_pct"] = org["top_holders"]
        if c.get("use_dev_blacklist", True) and self.blacklist:
            dev = self.creator_of(token)
            if dev and dev in self.blacklist:
                b = self.blacklist[dev]
                return False, f"Dev-Wallet auf Blacklist (frueher Rug: ${b.get('symbol', '?')})", dict(info, recheck_min=10080)
        if self.chain != "solana" and c.get("use_goplus", True):
            ok, reason, extra = self.goplus_check(token, c)
            info.update(extra)
            if not ok:
                return False, reason, dict(info, recheck_min=extra.get("recheck_min", 360))
        if info.get("top10_pct") is not None and info["top10_pct"] > c.get("max_top10_pct", 100):
            return False, f"Top-10-Halter {info['top10_pct']:.0f}% (> {c.get('max_top10_pct')}%)", dict(info, recheck_min=60)
        if self.chain == "solana" and c["use_rugcheck"]:
            rc = self.rugcheck(pair["baseToken"]["address"])
            if rc is None:
                return False, "RugCheck nicht verfuegbar", dict(info, recheck_min=5)
            score = fnum(rc.get("score_normalised"), 100)
            dangers = [r.get("name") for r in rc.get("risks") or [] if r.get("level") == "danger"]
            if rc.get("rugged"):
                dangers.insert(0, "als gerugt markiert")
            if "lpLockedPct" in rc:
                lp = fnum(rc.get("lpLockedPct"))
            else:  # voller Bericht: gesperrter Anteil der Liquiditaet ueber alle Pools (wie RugCheck-Zusammenfassung)
                lps = [m.get("lp") or {} for m in rc.get("markets") or []]
                total = sum(fnum(x.get("baseUSD")) + fnum(x.get("quoteUSD")) for x in lps)
                lp = 100 * sum(fnum(x.get("lpLockedUSD")) for x in lps) / total if total else 0
            if rc.get("creator"):
                self.creators[token] = rc["creator"]
            supply = fnum((rc.get("token") or {}).get("supply"))
            insider = 100 * sum(fnum(n.get("currentHolding")) for n in rc.get("insiderNetworks") or []) / supply if supply else 0
            info["insider_pct"] = insider
            if c.get("max_insider_pct") and insider > c["max_insider_pct"]:
                return False, f"Insider-Netzwerke halten {insider:.0f}% (Soft-Rug-Gefahr)", dict(info, recheck_min=360)
            info.update({"rug_score": score, "lp_locked": lp})
            if c["reject_danger_risks"] and dangers:
                return False, f"RugCheck: {dangers[0]}", dict(info, recheck_min=360)
            if score > c["max_rugcheck_score_normalised"]:
                return False, f"RugCheck-Score {score:.0f} zu hoch", dict(info, recheck_min=360)
            if lp < (c.get("comeback", {}).get("min_lp_locked_pct", 0) if comeback else c["min_lp_locked_pct"]):
                return False, f"LP nur {lp:.0f}% gesperrt", dict(info, recheck_min=60)
        if comeback:
            ok, reason, st = self.comeback_chart(pair, c)
            info["comeback"] = st
            if ok is None:
                return False, reason, dict(info, recheck_min=5)
            if not ok:
                return False, reason, dict(info, recheck_min=30)
            self.source[token] = "Comeback"
        if c.get("chart_check", True):
            ok, reason, st = self.chart_check(pair, c)
            if st:
                info["chart"] = st
            if ok is None:
                # junge Coins hat GeckoTerminal oft noch nicht erfasst -> dann passen die Live-Rug-Waechter auf;
                # aeltere Coins ohne pruefbaren Chart werden nicht gekauft
                if not (reason == "Chart noch zu kurz" and age_min < c.get("chart_optional_below_min", 180)):
                    return False, reason, dict(info, recheck_min=2)
                info["chart_note"] = "Chart noch zu kurz"
            if not ok:
                return False, reason, dict(info, recheck_min=1440)
        return True, "alle Checks bestanden", info

    # ---------- Agent 3: Sniper ----------
    def slippage_pct(self, size_usd, liq_usd):
        # grobes Modell: Basis + Preiswirkung abhaengig von Ordergroesse/Liquiditaet
        return self.cfg["sniper"]["base_slippage_pct"] + 100 * size_usd / max(liq_usd, 1)

    def live_price(self, token):
        """Aktueller Preis von Jupiter (Solana), ohne Drossel - fuer den Kauf. 0 = unbekannt."""
        if self.chain != "solana" or not self.cfg.get("jupiter", {}).get("live_prices", True):
            return 0.0
        try:
            d = http_json("https://lite-api.jup.ag/price/v3?ids=" + token, timeout=5) or {}
            return fnum((d.get(token) or {}).get("usdPrice"))
        except Exception:
            return 0.0

    def buy(self, pair, info):
        s = self.cfg["sniper"]
        token = pair["baseToken"]["address"]
        sym = pair["baseToken"].get("symbol", "?")
        block = self.entry_block() if self.cfg.get("desk", {}).get("enabled") else None
        if block:
            self.log("sniper", f"${sym} bestanden, aber kein Kauf: {block}", "warn")
            with self.lock:
                self.stats["blocked"] = self.stats.get("blocked", 0) + 1
                self.next_check[token] = now() + 600
            return
        if any(t.get("mint") == token and t.get("side") == "Verkauf" and now() - t.get("ts", 0) < 86400 for t in self.ww_trades):
            self.log("sniper", f"${sym} uebersprungen - eine beobachtete Wallet hat ihn schon verkauft", "warn")
            return
        # DexScreener-Preise sind oft 30-60 s alt: zum aktuellen Preis kaufen und nicht in einen laufenden Absturz hinein
        dex_price = fnum(pair["priceUsd"])
        jp = self.live_price(token)
        if jp and dex_price and jp < dex_price * (1 - s.get("max_live_price_drop_pct", 10) / 100):
            self.log("sniper", f"${sym} uebersprungen - faellt gerade (aktuell {(jp / dex_price - 1) * 100:.0f}% unter DexScreener)", "warn")
            with self.lock:
                self.next_check[token] = now() + 300
            return
        with self.lock:
            if token in self.positions:
                return
            if s.get("max_open_positions") and len(self.positions) >= s["max_open_positions"]:
                self.log("sniper", f"${sym} uebersprungen - max. {s['max_open_positions']} Positionen offen", "warn")
                return
            if now() < self.cooldown.get(token, 0):
                return
            # Nachahmer-Coins: gleicher Name wie eine schon offene Position -> nicht kaufen
            if any(p["symbol"].lower() == sym.lower() for p in self.positions.values()):
                self.log("sniper", f"${sym} uebersprungen - Coin mit gleichem Namen schon offen (Nachahmer?)", "warn")
                return
            size = min(self.equity() * s["position_pct_of_equity"] / 100, s["max_position_usd"], self.cash - 1)
            dip = s.get("dip_buy", {})
            if dip.get("enabled") and dip.get("reserve_pct"):
                # einen Teil des Geldes fuer Dip-Kaeufe offener Positionen frei halten
                size = min(size, self.cash - 1 - self.equity() * dip["reserve_pct"] / 100)
            if size < s["min_position_usd"]:
                self.log("sniper", f"${sym} uebersprungen - zu wenig Cash", "warn")
                return
            liq = info["liq"]
            price = jp if jp and jp < dex_price * 1.3 else dex_price  # aktueller Preis, wenn plausibel
            fill = price * (1 + self.slippage_pct(size, liq) / 100)
            fee = size * s["fee_pct_per_side"] / 100 + s["network_fee_usd"]
            qty = (size - fee) / fill
            self.cash -= size
            self.positions[token] = {
                "token": token, "symbol": sym, "name": pair["baseToken"].get("name", sym),
                "pair": pair.get("pairAddress"), "url": pair.get("url"), "dex": pair.get("dexId"),
                "opened_at": now(), "cost_usd": size, "qty": qty, "entry_price": fill,
                "last_price": price, "peak_price": price, "entry_liq": liq, "last_liq": liq,
                "entry_mcap": info.get("mcap"), "last_mcap": info.get("mcap"), "hist": [[now(), price]],
                "entry_depth": pool_depth(pair) or None, "ladder2": True,
                "first_entry_price": fill, "first_cost": size, "adds": 0,
                "entry_hist": [[now(), fill]], "events": [[now(), "Kauf", fill]],
                "info": info, "source": self.source.get(token, "?"),
                "launchpad": launchpad_of(token, pair.get("dexId")),
            }
            self.cooldown[token] = now() + s["cooldown_minutes_per_token"] * 60
        self.log("sniper", f"KAUF ${sym} fuer ${size:,.2f} bei MCap ${info.get('mcap', 0):,.0f} (Liq ${liq:,.0f})", "buy")
        self.save_state()

    # ---------- Agent 4: Risk ----------
    def risk(self):
        with self.lock:
            tokens = list(self.positions)
        if tokens:
            with self.lock:
                pair_of = {t: self.positions[t].get("pair") for t in tokens if t in self.positions}
            by_pair = self.fetch_by_pair(pair_of.values())
            jup = self.jupiter_prices(tokens)  # Solana: Jupiter-Preis ist ca. 4x aktueller als DexScreener
            missing = [t for t, pa in pair_of.items() if pa not in by_pair]
            fallback = self.fetch_pairs(missing) if missing else {}  # nur fuer alte Positionen ohne Pool-Adresse
            r = self.cfg["risk"]
            rw = r.get("rugwatch", {})
            if rw.get("enabled") and self.chain == "solana":
                for token in tokens:
                    if now() < self.rugwatch_next.get(token, 0):
                        continue
                    self.rugwatch_next[token] = now() + rw.get("recheck_minutes", 10) * 60
                    self.rug_cache.pop(token, None)  # frischen Bericht holen
                    rc = self.rugcheck(token) or {}
                    supply = fnum((rc.get("token") or {}).get("supply"))
                    insider = 100 * sum(fnum(n.get("currentHolding")) for n in rc.get("insiderNetworks") or []) / supply if supply else 0
                    with self.lock:
                        pos = self.positions.get(token) or {}
                        before = pos.get("insider_pct")
                        if rc and supply:
                            pos["insider_pct"] = insider
                    dev_now = 100 * fnum(rc.get("creatorBalance")) / supply if supply and rc.get("creatorBalance") is not None else None
                    dev_in = ((self.positions.get(token) or {}).get("info") or {}).get("dev_pct")
                    if rc.get("rugged"):
                        self.rug_alarm[token] = "RugCheck meldet Rug"
                    elif dev_now is not None and dev_in is not None and dev_in - dev_now >= rw.get("dev_sell_pts", 0.3):
                        self.rug_alarm[token] = f"Dev verkauft ({dev_in:.1f}% -> {dev_now:.1f}%)"
                    elif insider > rw.get("max_insider_pct", 15):
                        self.rug_alarm[token] = f"Insider halten jetzt {insider:.0f}%"
                    elif before is not None and supply and before - insider >= rw.get("insider_dump_pts", 1.5):
                        self.rug_alarm[token] = f"Slow-Rug: Insider verkaufen ({before:.1f}% -> {insider:.1f}%)"
            if self.blacklist:  # Dev-Wallets offener Positionen kennen (ausserhalb des Locks, ggf. RugCheck/GoPlus)
                for token in tokens:
                    if token not in self.creators:
                        self.creator_of(token)
            for token in tokens:
                p = by_pair.get(pair_of.get(token)) or (fallback.get(token) if not pair_of.get(token) else None)
                with self.lock:
                    pos = self.positions.get(token)
                    if not pos:
                        continue
                    if p:
                        dex_price = fnum(p.get("priceUsd"), pos["last_price"])
                        jp = jup.get(token)
                        # Jupiter nur nutzen, wenn er zum Pool passt (gleicher Coin, kein Ausreisser > 30 %)
                        pos["last_price"] = jp if jp and 0.7 < jp / max(dex_price, 1e-30) < 1.3 else dex_price
                        pos["last_liq"] = fnum((p.get("liquidity") or {}).get("usd"), pos["last_liq"])
                        dep = pool_depth(p)
                        if dep > 0:
                            pos["last_depth"] = dep
                            pos.setdefault("entry_depth", dep)
                        pos["peak_price"] = max(pos["peak_price"], pos["last_price"])
                        pos["last_mcap"] = fnum(p.get("marketCap") or p.get("fdv"), pos.get("last_mcap") or 0)
                        m5 = (p.get("txns") or {}).get("m5") or {}
                        b5, s5 = fnum(m5.get("buys")), fnum(m5.get("sells"))
                        rw = r.get("rugwatch", {})
                        # Tiefe des Pools (sinkt nur, wenn Liquiditaet abgezogen wird, nicht durch Verkaeufe)
                        dh = pos.setdefault("depth_hist", [])
                        if pos.get("last_depth"):
                            dh.append([now(), pos["last_depth"]])
                            del dh[:-1500]
                        top10 = max((d for ts, d in dh if now() - ts <= 600), default=0)
                        top90 = max((d for ts, d in dh if now() - ts <= 5400), default=0)
                        cur_d = pos.get("last_depth") or 0
                        pulled10 = (1 - cur_d / top10) * 100 if top10 and cur_d else 0
                        pulled90 = (1 - cur_d / top90) * 100 if top90 and cur_d else 0
                        # Viele Verkaeufe allein sind kein Rug (z. B. grosser Trader + Copytrader steigen aus).
                        # Nur zusammen mit abgezogener Liquiditaet ist es ein Rug.
                        if (rw.get("enabled") and s5 >= rw.get("min_sells_m5", 30) and s5 >= rw.get("sell_buy_ratio_m5", 5) * max(b5, 1)
                                and pulled10 >= rw.get("panic_lp_pull_pct", 8)):
                            self.rug_alarm.setdefault(token, f"{s5:.0f} Verkaeufe vs. {b5:.0f} Kaeufe in 5 min und Liquiditaet abgezogen (-{pulled10:.0f}%)")
                        dv = r.get("dead_volume_exit", {})
                        if dv.get("enabled"):
                            n5 = b5 + s5
                            v1h = fnum((p.get("volume") or {}).get("h1"))
                            if n5 <= dv.get("max_trades_m5", 3) and v1h < dv.get("max_volume_h1_usd", 3000):
                                pos.setdefault("dead_since", now())
                                if now() - pos["dead_since"] >= dv.get("minutes", 10) * 60 and token not in self.chart_exit:
                                    self.chart_exit[token] = (f"Toter Coin: kein Volumen mehr ({n5:.0f} Trades in 5 min, "
                                                              f"${v1h:,.0f} in 1 h, seit {dv.get('minutes', 10)} min)")
                            else:
                                pos.pop("dead_since", None)
                        avg_t, n_t = self.avg_trade_usd(p)
                        min_avg = self.cfg["analyst"].get("min_avg_trade_usd")
                        if rw.get("enabled") and min_avg and n_t >= 300 and avg_t < min_avg:
                            self.rug_alarm.setdefault(token, f"Fake-Handel (${avg_t:.1f} pro Trade bei {n_t:.0f} Trades/1h)")
                        if rw.get("enabled") and self.cfg["analyst"].get("block_copycats", True):
                            cc = self.copycat_of(p)
                            if cc:
                                self.rug_alarm.setdefault(token, f"Nachahmer von gemeldetem Rug ${cc}")
                        pos.pop("liq_hist", None)
                        if rw.get("enabled") and pulled90 >= rw.get("liq_drain_pct_90m", 15):
                            self.rug_alarm.setdefault(token, f"Slow-Rug: Liquiditaet abgezogen (Pool -{pulled90:.0f}% in 90 min)")
                        h = pos.setdefault("hist", [])
                        crash = rw.get("crash_pct_2m")
                        if rw.get("enabled") and crash and h:
                            top = max((v for ts, v in h[-240:] if now() - ts <= 120), default=0)
                            if top and pos["last_price"] <= top * (1 - crash / 100):
                                self.rug_alarm.setdefault(token, f"Absturz {(pos['last_price'] / top - 1) * 100:.0f}% in 2 min (Rug-Kerze)")
                        h.append([now(), pos["last_price"]])
                        if len(h) > 3600:
                            old = h[:1800]
                            h[:1800] = old[::2]  # aeltere Haelfte ausduennen, juengste Stunde bleibt fein
                    pyr = self.cfg["sniper"].get("pyramid", {})
                    if p and pyr.get("enabled"):
                        self.pyramid_add(token, pos, pyr)
                    dip = self.cfg["sniper"].get("dip_buy", {})
                    if p and dip.get("enabled"):
                        self.dip_add(token, pos, dip, (p.get("txns") or {}).get("m5") or {})
                    run = r.get("runner", {})
                    if p and run.get("enabled") and run.get("dip_buy", True):
                        self.runner_dip_add(token, pos, run, (p.get("txns") or {}).get("m5") or {})
                    ptp = r.get("partial_tp", {})
                    if p and ptp.get("enabled"):
                        self.partial_take_profit(token, pos, ptp)
                    price, entry, peak = pos["last_price"], pos["entry_price"], pos["peak_price"]
                    pnl = (price / entry - 1) * 100
                    peak_pnl = (peak / entry - 1) * 100
                    held_min = (now() - pos["opened_at"]) / 60
                    if pos.get("entry_depth") and pos.get("last_depth"):
                        liq_drop = (1 - pos["last_depth"] / pos["entry_depth"]) * 100  # abgezogene Liquiditaet seit Kauf
                    else:
                        liq_drop = 0
                    # nach dem ersten Teilgewinn enger absichern, damit vom Gewinn mehr uebrig bleibt
                    trail_dist = r["trailing_distance_pct"]
                    if r.get("trailing_distance_after_tp_pct") and (pos.get("partial_level", 0) >= 1 or pos.get("partial_done")):
                        trail_dist = r["trailing_distance_after_tp_pct"]
                    run = r.get("runner", {})
                    is_runner = run.get("enabled") and pos.get("partial_level", 0) >= run.get("after_tp_level", 2)
                    if is_runner:
                        trail_dist = run.get("trailing_pct", 30)  # Laeufer (z. B. $DUBBLE): mehr Luft lassen, laufen lassen
                        if pos.get("runner_trims", 0) >= run.get("max_trims", 2):
                            trail_dist = run.get("moonbag_trailing_pct", 50)  # letzter Rest (Moonbag) mit viel Luft
                    reason = None
                    dev = self.creators.get(token)
                    if dev and dev in self.blacklist:
                        reason = "Dev-Wallet auf Blacklist (Rug gemeldet)"
                    elif token in self.rug_alarm:
                        reason = f"Soft-Rug-Alarm: {self.rug_alarm.pop(token)} (Rug-Gefahr)"
                    elif liq_drop >= r["exit_if_liquidity_drops_pct"]:
                        reason = f"Liquiditaet abgezogen: Pool -{liq_drop:.0f}% seit Kauf (Rug-Gefahr)"
                    elif r.get("breakeven", {}).get("activate_pct") and peak_pnl >= r["breakeven"]["activate_pct"] and pnl <= r["breakeven"].get("lock_pct", 2):
                        # war schon deutlich im Plus -> nicht mehr ins Minus laufen lassen
                        reason = f"Break-even-Stop (Hoch {peak_pnl:+.0f}%, jetzt {pnl:+.0f}%)"
                    elif pnl <= -(r.get("grace", {}).get("stop_loss_pct", r["stop_loss_pct"])
                                  if held_min < r.get("grace", {}).get("minutes", 0) else r["stop_loss_pct"]):
                        # Schonfrist: frische Kaeufe schwanken in den ersten Minuten stark -> weiterer Stop
                        reason = f"Stop-Loss {pnl:+.1f}%" + (" (Schonfrist)" if held_min < r.get("grace", {}).get("minutes", 0) else "")
                    elif r.get("take_profit_pct") and pnl >= r["take_profit_pct"]:
                        reason = f"Take-Profit {pnl:+.1f}%"
                    elif token in self.chart_exit:
                        reason = self.chart_exit.pop(token)
                    elif peak_pnl >= r["trailing_activate_pct"] and price <= peak * (1 - trail_dist / 100):
                        reason = f"Trailing-Stop (Hoch {peak_pnl:+.0f}%, jetzt {pnl:+.0f}%, Abstand {trail_dist:.0f}%)"
                        if is_runner and pos.get("runner_trims", 0) < run.get("max_trims", 2):
                            # Laeufer nicht komplett schliessen, nur weil der Chart gerade schwach aussieht: Teil verkaufen
                            k = pos.get("runner_trims", 0) + 1
                            part = run.get("trim_pct", 50) / 100
                            pnl_part = self.sell_part(token, pos, part, f"Laeufer-Teilverkauf {k}: {part * 100:.0f}% bei {pnl:+.0f}% (Hoch {peak_pnl:+.0f}%)", f"Teil {k}")
                            self.log("banker", f"LAEUFER ${pos['symbol']}: {part * 100:.0f}% verkauft bei {pnl:+.0f}% ({pnl_part:+,.2f} $), Rest laeuft weiter", "win")
                            pos["runner_trims"] = k
                            pos["peak_price"] = price  # Trailing ab hier neu messen
                            reason = None
                    elif r.get("max_hold_minutes") and held_min >= r["max_hold_minutes"] and (pnl < 0 or not r.get("hold_only_if_losing")):
                        reason = f"Haltezeit {held_min:.0f} min abgelaufen"
                    elif r.get("time_exit_minutes") and held_min >= r["time_exit_minutes"] and pnl < r.get("time_exit_min_gain_pct", 5) and p:
                        v1h = fnum((p.get("volume") or {}).get("h1"))
                        v_in = (pos.get("info") or {}).get("vol_h1") or 0
                        if v_in and v1h < v_in * r.get("time_exit_volume_ratio", 0.5):
                            reason = f"Zeit-Exit: {held_min:.0f} min ohne Fortschritt ({pnl:+.0f}%), Volumen 1h ${v1h:,.0f} statt ${v_in:,.0f}"
                if reason:
                    self.sell(token, reason)
        self.record_equity()

    def pyramid_add(self, token, pos, pyr):
        """Nachkauf: bei jedem +step_pct ueber dem ersten Kaufpreis add_pct_of_first % des ersten Einsatzes nachkaufen.
        Muss mit gehaltenem self.lock aufgerufen werden."""
        s = self.cfg["sniper"]
        first_price = pos.get("first_entry_price") or pos["entry_price"]
        first_cost = pos.get("first_cost") or pos["cost_usd"]
        adds = pos.get("adds", 0)
        if adds >= pyr.get("max_adds", 3):
            return
        gain = (pos["last_price"] / first_price - 1) * 100
        if gain < pyr.get("step_pct", 25) * (adds + 1):
            return
        size = min(first_cost * pyr.get("add_pct_of_first", 25) / 100, self.cash - 1)
        if size < s["min_position_usd"]:
            return
        fill = pos["last_price"] * (1 + self.slippage_pct(size, pos["last_liq"]) / 100)
        fee = size * s["fee_pct_per_side"] / 100 + s["network_fee_usd"]
        qty = (size - fee) / fill
        self.cash -= size
        pos["entry_price"] = (pos["qty"] * pos["entry_price"] + qty * fill) / (pos["qty"] + qty)  # Durchschnittspreis
        pos["qty"] += qty
        pos["cost_usd"] += size
        pos["adds"] = adds + 1
        pos["peak_price"] = max(pos["peak_price"], pos["last_price"])
        pos.setdefault("entry_hist", [[pos["opened_at"], first_price]]).append([now(), pos["entry_price"]])
        pos.setdefault("events", []).append([now(), f"Nachkauf {adds + 1}", fill])
        self.log("sniper", f"NACHKAUF ${pos['symbol']} fuer ${size:,.2f} bei +{gain:.0f}% (Nachkauf {adds + 1}, MCap ${pos.get('last_mcap') or 0:,.0f})", "buy")

    def dip_add(self, token, pos, dip, m5):
        """Dip-Kauf: faellt der Kurs in einer roten Kerze (-candle_drop_pct in window_s) unter den Entry, kauft der Bot
        add_pct_of_first % des ersten Einsatzes nach und senkt so seinen Durchschnitts-Entry. Nie bei Rug-Anzeichen,
        nie kurz vor dem Stop-Loss, nur wenn noch Kaeufer da sind. Muss mit gehaltenem self.lock aufgerufen werden."""
        s = self.cfg["sniper"]
        n = pos.get("dip_adds", 0)
        if n >= dip.get("max_adds", 2) or token in self.rug_alarm:
            return
        if now() - pos.get("last_dip_at", pos["opened_at"]) < dip.get("min_gap_s", 180):
            return
        price, entry = pos["last_price"], pos["entry_price"]
        below = (1 - price / entry) * 100
        if below < dip.get("min_below_entry_pct", 8) or below > dip.get("max_below_entry_pct", 18):
            return  # zu wenig gefallen oder schon zu nah am Stop-Loss
        recent = [v for ts, v in pos.get("hist", []) if now() - ts <= dip.get("window_s", 120)]
        drop = (1 - price / max(recent)) * 100 if recent else 0
        if drop < dip.get("candle_drop_pct", 6):
            return  # keine frische rote Kerze, sondern langsames Abrutschen
        b5, s5 = fnum(m5.get("buys")), fnum(m5.get("sells"))
        if b5 < dip.get("min_buy_sell_ratio_m5", 0.8) * s5:
            return  # Verkaeufer dominieren -> eher Abverkauf als Dip
        if pos.get("entry_depth") and pos.get("last_depth", 0) < pos["entry_depth"] * (1 - dip.get("max_liq_drop_pct", 10) / 100):
            return  # Liquiditaet wird abgezogen -> Rug-Gefahr
        size = min((pos.get("first_cost") or pos["cost_usd"]) * dip.get("add_pct_of_first", 25) / 100, self.cash - 1)
        if size < s["min_position_usd"]:
            return
        fill = price * (1 + self.slippage_pct(size, pos["last_liq"]) / 100)
        fee = size * s["fee_pct_per_side"] / 100 + s["network_fee_usd"]
        qty = (size - fee) / fill
        self.cash -= size
        old = entry
        pos["entry_price"] = (pos["qty"] * entry + qty * fill) / (pos["qty"] + qty)
        pos["qty"] += qty
        pos["cost_usd"] += size
        pos["dip_adds"] = n + 1
        pos["last_dip_at"] = now()
        pos.setdefault("entry_hist", [[pos["opened_at"], pos.get("first_entry_price") or old]]).append([now(), pos["entry_price"]])
        pos.setdefault("events", []).append([now(), f"Dip {n + 1}", fill])
        self.log("sniper", f"DIP-KAUF ${pos['symbol']} fuer ${size:,.2f} nach roter Kerze (-{drop:.0f}%), "
                           f"Entry {old:.8g} -> {pos['entry_price']:.8g}", "buy")

    def runner_dip_add(self, token, pos, run, m5):
        """Laeufer nach dem 2. Teilgewinn: bei einer roten Kerze nachkaufen und die Position vergroessern.
        Der Coin ist deutlich im Plus, der Entry steigt dadurch (gewollt). Nie bei Rug-Anzeichen, nur wenn noch Kaeufer
        da sind und die Liquiditaet nicht abgezogen wird. Muss mit gehaltenem self.lock aufgerufen werden."""
        s = self.cfg["sniper"]
        if pos.get("partial_level", 0) < run.get("after_tp_level", 2) or token in self.rug_alarm or token in self.chart_exit:
            return
        n = pos.get("runner_adds", 0)
        if n >= run.get("max_adds", 2) or now() - pos.get("last_runner_add", 0) < run.get("min_gap_s", 300):
            return
        price, entry = pos["last_price"], pos["entry_price"]
        if (price / entry - 1) * 100 < run.get("min_gain_pct", 30):
            return  # nur solange der Coin klar im Plus ist
        recent = [v for ts, v in pos.get("hist", []) if now() - ts <= run.get("window_s", 180)]
        drop = (1 - price / max(recent)) * 100 if recent else 0
        if not (run.get("min_red_pct", 8) <= drop <= run.get("max_red_pct", 20)):
            return  # keine rote Kerze - oder schon ein Absturz (dann nicht nachkaufen)
        b5, s5 = fnum(m5.get("buys")), fnum(m5.get("sells"))
        if b5 < run.get("min_buy_sell_ratio_m5", 0.8) * s5:
            return
        if pos.get("entry_depth") and pos.get("last_depth", 0) < pos["entry_depth"] * 0.9:
            return  # Liquiditaet wird abgezogen
        size = min((pos.get("first_cost") or pos["cost_usd"]) * run.get("add_pct_of_first", 25) / 100, self.cash - 1)
        if size < s["min_position_usd"]:
            return
        fill = price * (1 + self.slippage_pct(size, pos["last_liq"]) / 100)
        fee = size * s["fee_pct_per_side"] / 100 + s["network_fee_usd"]
        qty = (size - fee) / fill
        self.cash -= size
        old = entry
        pos["entry_price"] = (pos["qty"] * entry + qty * fill) / (pos["qty"] + qty)
        pos["qty"] += qty
        pos["cost_usd"] += size
        pos["runner_adds"] = n + 1
        pos["adds"] = pos.get("adds", 0) + 1
        pos["last_runner_add"] = now()
        pos.setdefault("entry_hist", [[pos["opened_at"], pos.get("first_entry_price") or old]]).append([now(), pos["entry_price"]])
        pos.setdefault("events", []).append([now(), f"Dip-Nachkauf {n + 1}", fill])
        self.log("sniper", f"LAEUFER-NACHKAUF ${pos['symbol']} fuer ${size:,.2f} in roter Kerze (-{drop:.0f}%, Coin {(price / old - 1) * 100:+.0f}%), "
                           f"Entry {old:.8g} -> {pos['entry_price']:.8g}", "buy")

    @staticmethod
    def partial_level(ptp, n):
        """Stufe n (0, 1, 2, ...) der Teilgewinn-Leiter -> (ab +x %, y % der Position verkaufen)."""
        levels = ptp.get("levels") or [[ptp.get("at_pct", 50), ptp.get("sell_pct", 25)]]
        if n < len(levels):
            return levels[n]
        every = ptp.get("then_every_pct")
        if not every:
            return None
        return [levels[-1][0] + every * (n - len(levels) + 1), ptp.get("then_sell_pct", levels[-1][1])]

    def partial_take_profit(self, token, pos, ptp):
        """Teilgewinne stufenweise, gemessen am Durchschnittspreis (z. B. +50 %: 25 %, +100 %: 20 %, dann alle +50 %: 20 %).
        Muss mit gehaltenem self.lock aufgerufen werden."""
        s = self.cfg["sniper"]
        n = pos.get("partial_level", 1 if pos.get("partial_done") else 0)
        step = self.partial_level(ptp, n)
        gain = (pos["last_price"] / pos["entry_price"] - 1) * 100
        if not step or gain < step[0]:
            return
        pos["partial_level"] = n + 1
        pos["partial_done"] = True
        pnl = self.sell_part(token, pos, step[1] / 100, f"Teilgewinn {n + 1}: {step[1]:.0f}% verkauft bei +{gain:.0f}%", f"TP {n + 1}")
        self.log("banker", f"TEILGEWINN {n + 1} ${pos['symbol']}: {step[1]:.0f}% verkauft bei +{gain:.0f}% ({pnl:+,.2f} $), Rest laeuft weiter", "win")

    def sell_part(self, token, pos, part, reason, label):
        """Einen Teil der Position verkaufen (part = 0..1). Muss mit gehaltenem self.lock aufgerufen werden. -> Gewinn in $"""
        s = self.cfg["sniper"]
        qty = pos["qty"] * part
        gross = qty * pos["last_price"]
        proceeds = gross * (1 - self.slippage_pct(gross, pos["last_liq"]) / 100)
        proceeds = max(proceeds - proceeds * s["fee_pct_per_side"] / 100 - s["network_fee_usd"], 0)
        cost = pos["cost_usd"] * part
        self.cash += proceeds
        pos["qty"] -= qty
        pos["cost_usd"] -= cost
        pos["realized_usd"] = pos.get("realized_usd", 0) + proceeds - cost
        pos.setdefault("events", []).append([now(), label, pos["last_price"]])
        pnl = proceeds - cost
        self.closed.append({
            "symbol": pos["symbol"], "token": token, "url": pos.get("url"), "partial": True, "pair": pos.get("pair"),
            "opened_at": pos["opened_at"], "closed_at": now(), "cost_usd": cost, "proceeds_usd": proceeds,
            "pnl_usd": pnl, "pnl_pct": pnl / cost * 100 if cost else 0, "reason": reason,
            "source": pos.get("source", "?"), "entry_mcap": pos.get("entry_mcap"), "exit_mcap": pos.get("last_mcap"),
            "peak_pct": (pos["peak_price"] / pos["entry_price"] - 1) * 100, "held_min": (now() - pos["opened_at"]) / 60,
        })
        return pnl

    # ---------- Agent 5: Banker ----------
    def sell(self, token, reason):
        s = self.cfg["sniper"]
        with self.lock:
            pos = self.positions.pop(token, None)
            if not pos:
                return
            gross = pos["qty"] * pos["last_price"]
            slip = self.slippage_pct(gross, pos["last_liq"])
            proceeds = gross * (1 - slip / 100)
            proceeds -= proceeds * s["fee_pct_per_side"] / 100 + s["network_fee_usd"]
            proceeds = max(proceeds, 0)
            self.cash += proceeds
            pnl = proceeds - pos["cost_usd"]
            trade = {
                "symbol": pos["symbol"], "token": token, "url": pos.get("url"),
                "opened_at": pos["opened_at"], "closed_at": now(),
                "cost_usd": pos["cost_usd"], "proceeds_usd": proceeds,
                "pnl_usd": pnl, "pnl_pct": pnl / pos["cost_usd"] * 100, "reason": reason,
                "source": pos.get("source", "?"), "entry": pos.get("info", {}),
                "launchpad": pos.get("launchpad", "?"), "dex": pos.get("dex"),
                "pair": pos.get("pair"), "entry_liq": pos.get("entry_liq"), "exit_liq": pos.get("last_liq"),
                "entry_mcap": pos.get("entry_mcap"), "exit_mcap": pos.get("last_mcap"), "adds": pos.get("adds", 0),
                "peak_pct": (pos["peak_price"] / pos["entry_price"] - 1) * 100,
                "held_min": (now() - pos["opened_at"]) / 60, "exit_price": pos["last_price"], "chain": self.chain,
                "score": (pos.get("info") or {}).get("score"), "slippage_pct": round(slip, 2),
                "stop_price": pos["entry_price"] * (1 - self.cfg["risk"]["stop_loss_pct"] / 100),
                "rug": "Rug-Gefahr" in reason or "Rug" in reason,
            }
            self.closed.append(trade)
        kind = "win" if pnl >= 0 else "loss"
        r = self.cfg["risk"]
        with self.lock:
            if pnl < 0:
                self.token_losses[token] = self.token_losses.get(token, 0) + 1
                self.loss_streak += 1
                if r.get("loss_streak_max") and self.loss_streak >= r["loss_streak_max"]:
                    self.pause_until = now() + r.get("loss_streak_pause_min", 60) * 60
                    self.loss_streak = 0
                    self.log("system", f"{r['loss_streak_max']} Verluste in Folge - {r.get('loss_streak_pause_min', 60)} min Kaufpause", "warn")
            else:
                self.loss_streak = 0
        self.log("banker", f"VERKAUF ${pos['symbol']} {trade['pnl_pct']:+.1f}% ({pnl:+,.2f} $) - {reason}", kind)
        rug_cfg = self.cfg.get("dev_blacklist", {})
        if "Rug-Gefahr" in reason or trade["pnl_pct"] <= -rug_cfg.get("rug_loss_pct", 40):
            dev = self.creator_of(token)
            if dev and dev_blacklist_add(self.blacklist_path, dev, {
                    "symbol": pos["symbol"], "name": pos.get("name"), "token": token, "chain": self.chain, "ts": now(),
                    "reason": reason, "pnl_pct": round(trade["pnl_pct"], 1)}):
                self.log("analyst", f"Dev-Wallet {dev[:6]}...{dev[-4:]} von ${pos['symbol']} gesperrt - kuenftig alle ihre Coins meiden", "warn")
        self.save_state()

    def record_equity(self):
        """Kontostand-Verlauf: fein (alle 10 s, letzte 2 h) und grob (alle 5 min, 60 Tage) fuer den 24h/7d/30d-Chart."""
        with self.lock:
            t, eq = now(), round(self.equity(), 2)
            if not self.equity_history or t - self.equity_history[-1][0] >= 10:
                self.equity_history.append([t, eq])
                while self.equity_history and t - self.equity_history[0][0] > 7200:
                    self.equity_history.pop(0)
            if not self.equity_long or t - self.equity_long[-1][0] >= 300:
                self.equity_long.append([t, eq])
                while self.equity_long and t - self.equity_long[0][0] > 60 * 86400:
                    self.equity_long.pop(0)

    def equity_ranges(self, n=300):
        """Kontostand je Zeitraum (1h, 24h, 7d, 30d, All), je hoechstens n Punkte, letzter Punkt = jetzt."""
        with self.lock:
            fine, coarse = list(self.equity_history), list(self.equity_long)
            cur = [now(), round(self.equity(), 2)]
        cut = fine[0][0] if fine else cur[0]
        merged = [x for x in coarse if x[0] < cut] + fine + [cur]
        out = {}
        for key, span in (("1h", 3600), ("24h", 86400), ("7d", 7 * 86400), ("30d", 30 * 86400), ("all", None)):
            pts = merged if span is None else [x for x in merged if x[0] >= cur[0] - span]
            out[key] = downsample(pts, n)
        return out

    # ---------- Schleifen ----------
    def scout_loop(self):
        while not self.stop_event.is_set():
            try:
                fresh = self.scout()
                pairs = self.fetch_pairs(fresh) if fresh else {}
                if pairs and self.chain == "solana" and self.cfg.get("jupiter", {}).get("enabled", True):
                    self.fetch_organic(list(pairs))
                for token in fresh:
                    if token not in pairs:
                        self.next_check[token] = now() + 600
                for token, pair in pairs.items():
                    if self.stop_event.is_set():
                        break
                    ok, reason, info = self.analyze(pair)
                    sym = clean_symbol(pair["baseToken"].get("symbol", "?"))
                    self.symbols[token] = sym
                    fb = self.fomo_counts().get(token)
                    if fb:
                        info["fomo_buyers"], info["fomo_sellers"] = fb
                    self.next_check[token] = now() + info.get("recheck_min", 5) * 60
                    if info.get("young"):
                        self.watch[token] = now() + 3600  # bis zu 1 h beobachten, bis er alt genug ist
                    elif ok or info.get("recheck_min", 0) >= 60:
                        self.watch.pop(token, None)
                    with self.lock:
                        self.stats["analyzed"] += 1
                        self.analyst_view = {"symbol": sym, "ok": ok, "reason": reason, "ts": now()}
                        if ok:
                            self.stats["passed"] += 1
                        else:
                            self.stats["rejected"] += 1
                            key = kill_label(reason)
                            self.stats["kills"][key] = self.stats["kills"].get(key, 0) + 1
                    if ok:
                        self.log("analyst", f"${sym} freigegeben - " + (f"Score {info['score']}/100, " if "score" in info else "")
                                 + f"Liq ${info['liq']:,.0f}, {info['ch_h1']:+.0f}% 1h", "pass")
                        self.buy(pair, info)
                    else:
                        self.log("analyst", f"${sym} abgelehnt: {reason}", "reject")
            except Exception as e:
                self.errors += 1
                self.log("system", f"Scout-Schleife: {e}", "error")
            stale = self.stale_sources()
            if stale and now() - self.health_warned >= 1800:
                self.health_warned = now()
                self.log("system", "Datenquelle liefert seit 30 min nichts: " + ", ".join(stale), "error")
            self.stop_event.wait(self.cfg["scout_interval_s"])

    def stale_sources(self):
        """Quellen, die frueher geliefert haben, aber seit 30 min nichts mehr (stiller Ausfall)."""
        lim = self.cfg.get("health", {}).get("stale_minutes", 30) * 60
        main = ("DS-Profil", "GT-Neu", "JUP-Neu", "PP-Neu")
        last_any = max(self.source_last.values(), default=0)
        out = [k for k in main if k in self.source_last and now() - self.source_last[k] > lim]
        if now() - self.run_started > lim and now() - last_any > lim:
            out.append("alle Quellen")
        return out

    # ---------- Live-Stream: jede neue Pump.fun/LetsBonk-Coin + Abschluesse (PumpPortal) ----------
    def stream_loop(self):
        c = self.cfg.get("pumpportal", {})
        if self.chain != "solana" or not c.get("enabled"):
            return
        backoff = 5
        while not self.stop_event.is_set():
            ws = None
            try:
                ws = MiniWebSocket("pumpportal.fun", "/api/data")
                ws.send_json({"method": "subscribeNewToken"})
                ws.send_json({"method": "subscribeMigration"})
                self.log("scout", "Live-Stream verbunden: alle neuen Pump.fun-Coins + Abschluesse")
                backoff = 5
                while not self.stop_event.is_set():
                    m = ws.recv_json()
                    mint = m.get("mint")
                    if not mint:
                        continue
                    t = now()
                    with self.lock:
                        if m.get("txType") == "migrate":
                            # Abschluss: Coin wechselt von der Bonding-Kurve an eine echte Boerse -> sofort pruefen
                            self.stats["stream_grad"] += 1
                            self.source[mint] = "PP-Abschluss"
                            self.watch[mint] = t + 7200
                            self.next_check[mint] = 0
                        else:
                            self.stats["stream_new"] += 1
                            if m.get("traderPublicKey"):
                                self.creators[mint] = m["traderPublicKey"]
                            self.source.setdefault(mint, "PP-Neu")
                            self.source_last["PP-Neu"] = t
                            self.watch.setdefault(mint, t + c.get("watch_minutes", 60) * 60)
                            self.next_check.setdefault(mint, t + c.get("first_check_minutes", 5) * 60)
                        if mint not in self.first_seen:
                            self.first_seen[mint] = t
                            self.run_seen += 1
                            self.stats["unique"] = len(self.first_seen)
            except Exception as e:
                if not self.stop_event.is_set():
                    self.log("scout", f"Live-Stream getrennt ({e}) - neuer Versuch in {backoff}s", "warn")
            finally:
                if ws:
                    ws.close()
            self.stop_event.wait(backoff)
            backoff = min(backoff * 2, 120)

    def wallet_watch_loop(self):
        """Wallets bestimmter Trader beobachten (z. B. Numerooo0 laut Kolscan) bis zu einem Enddatum.
        Jeder Kauf/Verkauf wird mitgeschrieben; gekaufte Coins prueft der Analyst sofort (gekauft wird nur, wenn alle
        Regeln passen)."""
        c = self.cfg.get("watch_wallets", {})
        if self.chain != "solana" or not c.get("enabled") or not c.get("wallets"):
            return
        url = c.get("rpc_url", self.cfg.get("fomo", {}).get("rpc_url", "https://api.mainnet-beta.solana.com"))
        try:
            until = datetime.fromisoformat(c.get("until", "2100-01-01T00:00:00Z").replace("Z", "+00:00")).timestamp()
        except ValueError:
            until = now() + 86400
        self.log("scout", f"beobachte {len(c['wallets'])} Trader-Wallets bis {time.strftime('%d.%m. %H:%M', time.localtime(until))}: "
                          + ", ".join(c["wallets"]))
        while not self.stop_event.is_set() and now() < until:
            for who, wallet in c["wallets"].items():
                if self.stop_event.is_set():
                    break
                try:
                    opts = {"limit": 20}
                    last = self.ww_last_sig.get(wallet)
                    if last:
                        opts["until"] = last
                    sigs = rpc_call(url, "getSignaturesForAddress", [wallet, opts]).get("result") or []
                    if not sigs:
                        continue
                    self.ww_last_sig[wallet] = sigs[0]["signature"]
                    if not last:
                        sigs = sigs[:5]  # beim Start nur die letzten Trades zeigen, nicht die ganze Historie
                    for sg in reversed([x for x in sigs if not x.get("err")]):
                        tx = (rpc_call(url, "getTransaction", [sg["signature"], {"encoding": "jsonParsed",
                              "maxSupportedTransactionVersion": 1, "commitment": "confirmed"}]) or {}).get("result")
                        if tx:
                            self.wallet_trade(who, wallet, tx, fresh=bool(last))
                        time.sleep(0.4)
                except urllib.error.HTTPError as e:
                    time.sleep(10 if e.code == 429 else 2)
                except Exception as e:
                    self.log("scout", f"Wallet {who}: {e}", "warn")
            self.stop_event.wait(c.get("poll_s", 20))

    def wallet_trade(self, who, wallet, tx, fresh=True):
        meta = tx.get("meta") or {}
        if meta.get("err"):
            return
        amt = lambda b: fnum((b.get("uiTokenAmount") or {}).get("uiAmount"))
        pre = {b["mint"]: amt(b) for b in meta.get("preTokenBalances") or [] if b.get("owner") == wallet}
        post = {b["mint"]: amt(b) for b in meta.get("postTokenBalances") or [] if b.get("owner") == wallet}
        acc = ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys", [])
        keys = [k.get("pubkey") if isinstance(k, dict) else k for k in acc]
        if wallet not in {k.get("pubkey") for k in acc if isinstance(k, dict) and k.get("signer")}:
            return  # nicht selbst gehandelt (z. B. jemand schickt ihm Coins)
        sol = 0.0
        if wallet in keys:
            i = keys.index(wallet)
            sol = (fnum((meta.get("postBalances") or [0] * (i + 1))[i]) - fnum((meta.get("preBalances") or [0] * (i + 1))[i])) / 1e9
        ts = tx.get("blockTime") or now()
        # bezahlt/erhalten in SOL, WSOL oder USDC (sonst nur Transfer/Airdrop, z. B. Spam-Coins -> ignorieren)
        quote = sol + sum((post.get(m, 0) - pre.get(m, 0)) * (1 if m == WSOL else 1 / 150) for m in (WSOL, USDC_MINT))
        for mint in set(pre) | set(post):
            if mint in IGNORE_TOKENS:
                continue
            d = post.get(mint, 0) - pre.get(mint, 0)
            if not d:
                continue
            if d > 0 and quote < -0.001:
                side = "Kauf"
            elif d < 0 and quote > 0.001:
                side = "Verkauf"
            else:
                continue
            sol = quote
            rec = {"ts": ts, "who": who, "wallet": wallet, "mint": mint, "side": side, "sol": round(abs(sol), 3),
                   "symbol": self.symbols.get(mint)}
            with self.lock:
                self.ww_trades.append(rec)
                del self.ww_trades[:-200]
                if side == "Kauf" and fresh and mint not in self.positions:
                    self.source[mint] = f"Wallet {who}"
                    self.next_check[mint] = 0
                    self.watch[mint] = max(self.watch.get(mint, 0), now() + 7200)
            if fresh:
                self.log("scout", f"WALLET {who}: {side} ${rec['symbol'] or mint[:6] + '…'} ({abs(sol):.2f} SOL)", "buy" if side == "Kauf" else "info")

    def after_exit_loop(self):
        """Lern-Log: verkaufte Coins noch 6 h weiter verfolgen (alle 5 min). So sieht man, ob ein Verkauf zu frueh war
        (Coin ist danach weit gestiegen) oder richtig (Coin ist danach weiter gefallen)."""
        while not self.stop_event.wait(300):
            try:
                with self.lock:
                    todo = [t for t in self.closed[-200:] if not t.get("partial") and t.get("pair") and t.get("exit_price")
                            and now() - t["closed_at"] <= 6 * 3600]
                if not todo:
                    continue
                pairs = self.fetch_by_pair({t["pair"] for t in todo})
                with self.lock:
                    for t in todo:
                        p = pairs.get(t["pair"])
                        price = fnum((p or {}).get("priceUsd"))
                        if price <= 0:
                            continue
                        ch = (price / t["exit_price"] - 1) * 100
                        a = t.setdefault("after", {"max_pct": ch, "min_pct": ch})
                        a["max_pct"], a["min_pct"], a["now_pct"] = max(a["max_pct"], ch), min(a["min_pct"], ch), ch
                        a["hours"] = round((now() - t["closed_at"]) / 3600, 1)
            except Exception as e:
                self.log("system", f"Lern-Log: {e}", "warn")

    def hot_loop(self):
        """Pump.fun "Heiss"/Top-Performer ohne API-Schluessel: alle neuen Pump.fun-Coins der letzten Stunden (Live-Stream)
        jede Minute bei DexScreener abfragen und nach Volumen/Kaeufen der letzten 5 min sortieren. Die heissesten Coins
        mit echter Liquiditaet prueft der Analyst sofort; Coins noch auf der Bonding-Kurve bleiben auf der Beobachtungsliste,
        bis sie an die Boerse wechseln."""
        c = self.cfg.get("pumpfun_hot", {})
        if self.chain != "solana" or not c.get("enabled"):
            return
        last_log = 0
        while not self.stop_event.wait(c.get("interval_s", 60)):
            try:
                t = now()
                with self.lock:
                    mints = [m for m, ts in self.first_seen.items()
                             if t - ts <= c.get("track_hours", 6) * 3600 and m not in self.pf_dead
                             and str(self.source.get(m, "")).startswith(("PP", "PF"))]
                mints = mints[-c.get("max_tokens", 1200):]
                pairs = self.fetch_pairs(mints) if mints else {}
                rows = []
                for m, p in pairs.items():
                    h1 = (p.get("txns") or {}).get("h1") or {}
                    if fnum(h1.get("buys")) + fnum(h1.get("sells")) < 5 and t - self.first_seen.get(m, t) > 1800:
                        self.pf_dead.add(m)  # seit einer Stunde kaum Handel -> nicht weiter verfolgen
                        continue
                    tx = (p.get("txns") or {}).get("m5") or {}
                    buys, vol5 = fnum(tx.get("buys")), fnum((p.get("volume") or {}).get("m5"))
                    if buys >= c.get("min_buys_m5", 15) and vol5 >= c.get("min_volume_m5_usd", 3000):
                        liq = fnum((p.get("liquidity") or {}).get("usd"))
                        rows.append((vol5, m, p["baseToken"].get("symbol", "?"), buys, fnum(p.get("marketCap") or p.get("fdv")), liq))
                for m in mints:
                    if m not in pairs and t - self.first_seen.get(m, t) > 1800:
                        self.pf_dead.add(m)  # DexScreener kennt ihn nach 30 min noch nicht -> tot
                rows.sort(reverse=True)
                top = rows[:c.get("top_n", 15)]
                min_liq = self.cfg["analyst"]["min_liquidity_usd"]
                with self.lock:
                    self.pf_hot = [m for v, m, sym, b, mc, liq in top if liq >= min_liq and m not in self.positions]
                    self.pf_hot_view = [[sym, m, round(v), int(b), round(mc), liq >= min_liq] for v, m, sym, b, mc, liq in top[:10]]
                    for v, m, *_ in top:  # heisse Coins laenger beobachten (auch wenn sie noch auf der Bonding-Kurve sind)
                        self.watch[m] = max(self.watch.get(m, 0), t + c.get("track_hours", 6) * 3600)
                    self.stats["pf_hot"] = len(top)
                if top and t - last_log >= 300:
                    last_log = t
                    self.log("scout", "Pump.fun heiss: " + ", ".join(f"${sym} ${v / 1e3:.0f}k/5min" for v, m, sym, *_ in top[:6])
                             + f" ({len(mints)} Coins verfolgt)")
            except Exception as e:
                self.log("scout", f"Pump.fun-Heiss-Liste: {e}", "warn")

    # ---------- Agent 6: FOMO ----------
    def fomo_counts(self):
        """mint -> (verschiedene Kaeufer, verschiedene Verkaeufer) im Zeitfenster."""
        window = self.cfg.get("fomo", {}).get("window_minutes", 10) * 60
        t = now()
        buy, sell = {}, {}
        with self.lock:
            while self.fomo_events and self.fomo_events[0][0] < t - window:
                self.fomo_events.popleft()
            for ts, mint, wallet, side in self.fomo_events:
                (buy if side > 0 else sell).setdefault(mint, set()).add(wallet)
        return {m: (len(buy.get(m, ())), len(sell.get(m, ()))) for m in set(buy) | set(sell)}

    def fomo_hot(self, n=50):
        rows = [(m, b, sl) for m, (b, sl) in self.fomo_counts().items() if b > 0]
        return sorted(rows, key=lambda r: (-r[1], r[2]))[:n]

    def parse_fomo_tx(self, tx):
        meta, msg = tx.get("meta") or {}, (tx.get("transaction") or {}).get("message") or {}
        if meta.get("err"):
            return []
        signers = {a["pubkey"] for a in msg.get("accountKeys", []) if a.get("signer")} - {FOMO_SIGNER}
        amt = lambda b: fnum((b.get("uiTokenAmount") or {}).get("uiAmount"))
        pre = {(b.get("owner"), b["mint"]): amt(b) for b in meta.get("preTokenBalances") or []}
        post = {(b.get("owner"), b["mint"]): amt(b) for b in meta.get("postTokenBalances") or []}
        out = []
        for owner, mint in set(pre) | set(post):
            if owner not in signers or mint in IGNORE_TOKENS:
                continue
            d = post.get((owner, mint), 0) - pre.get((owner, mint), 0)
            if d:
                out.append((mint, owner, 1 if d > 0 else -1))
        return out

    def fomo_poll(self):
        c = self.cfg["fomo"]
        url = c.get("rpc_url", "https://api.mainnet-beta.solana.com")
        opts = {"limit": 100}
        if self.fomo_last_sig:
            opts["until"] = self.fomo_last_sig
        sigs = rpc_call(url, "getSignaturesForAddress", [FOMO_SIGNER, opts]).get("result") or []
        if not sigs:
            return
        self.fomo_last_sig = sigs[0]["signature"]
        self.fomo_stats["tx_seen"] += len(sigs)
        # Oeffentliche RPCs sind limitiert -> nur die neuesten N Trades pro Runde im Detail lesen
        for sg in [x for x in sigs if not x.get("err")][: c.get("tx_per_cycle", 15)]:
            if self.stop_event.is_set():
                return
            try:
                r = rpc_call(url, "getTransaction", [sg["signature"], {
                    "encoding": "jsonParsed", "maxSupportedTransactionVersion": 1, "commitment": "confirmed"}])
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    time.sleep(3)
                    continue
                raise
            tx = r.get("result")
            if tx:
                self.fomo_stats["tx_parsed"] += 1
                ts = tx.get("blockTime") or now()
                for mint, wallet, side in self.parse_fomo_tx(tx):
                    with self.lock:
                        self.fomo_events.append((ts, mint, wallet, side))
                    self.fomo_stats["wallets"].add(wallet)
            time.sleep(c.get("rpc_delay_s", 0.4))

    def fomo_loop(self):
        c = self.cfg.get("fomo", {})
        if not c.get("enabled") or self.chain != "solana":  # FOMO handelt auf Solana
            return
        self.log("fomo", "beobachtet FOMO-Trades on-chain")
        last_report = 0
        while not self.stop_event.is_set():
            try:
                self.fomo_poll()
                hot = self.fomo_hot(3)
                if hot and now() - last_report > 120:
                    last_report = now()
                    txt = ", ".join(f"${self.symbols.get(m, m[:4] + '…')} {b} Kaeufer" for m, b, _ in hot)
                    self.log("fomo", f"FOMO-Nutzer kaufen gerade: {txt}")
            except urllib.error.HTTPError as e:
                if e.code == 413:
                    self.fomo_last_sig = None  # Abstand zur letzten Signatur zu gross -> neu ansetzen
                elif e.code != 429:
                    self.errors += 1
                    self.log("fomo", f"RPC-Fehler {e.code}", "error")
            except Exception as e:
                self.errors += 1
                self.log("fomo", f"Fehler: {e}", "error")
            self.stop_event.wait(c.get("poll_interval_s", 15))

    def risk_loop(self):
        while not self.stop_event.is_set():
            try:
                self.risk()
                if now() - getattr(self, "_last_save", 0) >= 10:
                    self._last_save = now()
                    self.save_state()
            except Exception as e:
                self.errors += 1
                self.log("system", f"Risk-Schleife: {e}", "error")
            self.stop_event.wait(self.cfg["risk_interval_s"])

    def start(self):
        for fn in (self.scout_loop, self.risk_loop, self.market_loop, self.manual_loop, self.fomo_loop, self.stream_loop, self.chart_watch_loop, self.hot_loop, self.after_exit_loop,
                   self.wallet_watch_loop):
            threading.Thread(target=fn, daemon=True).start()
        if os.environ.get("GITHUB_REPOSITORY") and os.environ.get("GITHUB_TOKEN") and self is self.family[0]:
            threading.Thread(target=self.github_reports_loop, daemon=True).start()

    def report_rug(self, token, who="dir"):
        """Coin von Hand als Rug melden: Dev-Wallet sperren und offene Position sofort verkaufen. -> Text fuer die Antwort."""
        token = (token or "").strip()
        if not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}|0x[0-9a-fA-F]{40}", token):
            return False, "Das ist keine gueltige Coin-Adresse."
        if self is self.family[0]:  # Coin einer anderen Chain -> deren Bot meldet ihn
            evm = token.startswith("0x")
            for other in self.family[1:]:
                if token in other.positions or (evm and self.chain == "solana" and other.chain != "solana"):
                    return other.report_rug(token, who)
        with self.lock:
            pos = self.positions.get(token)
            sym = pos["symbol"] if pos else token[:6]
            if pos:
                self.rug_alarm[token] = f"von {who} als Rug gemeldet"  # Risk verkauft beim naechsten Durchlauf (1 s)
        dev = self.creator_of(token)
        msg = f"${sym}: " + ("Position wird sofort verkauft. " if pos else "keine offene Position. ")
        if dev:
            added = dev_blacklist_add(self.blacklist_path, dev, {"symbol": sym, "name": (pos or {}).get("name"), "token": token, "chain": self.chain,
                                                                  "ts": now(), "reason": f"von {who} als Rug gemeldet"})
            msg += f"Dev-Wallet {dev[:6]}...{dev[-4:]} " + ("gesperrt." if added else "war schon gesperrt.")
        else:
            msg += "Dev-Wallet nicht gefunden (RugCheck kennt den Coin nicht)."
        self.log("analyst", f"RUG GEMELDET {msg}", "warn")
        return True, msg

    def github_reports_loop(self):
        """Auf GitHub (Actions): Rug-Meldungen von der Handy-Seite kommen als Issue "RUG <Adresse>".
        Nur Meldungen des Repo-Besitzers zaehlen; danach antwortet der Bot im Issue und schliesst es."""
        repo, tok = os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"]
        owner = repo.split("/")[0].lower()
        api = f"https://api.github.com/repos/{repo}/issues"

        def call(url, data=None, method="GET"):
            req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None, method=method,
                                         headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json",
                                                  "User-Agent": "grokbot-paper/1.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode() or "null")

        while not self.stop_event.is_set():
            try:
                for issue in call(api + "?state=open&per_page=30"):
                    title = (issue.get("title") or "").strip()
                    if issue.get("pull_request") or not title.upper().startswith("RUG"):
                        continue
                    if (issue.get("user") or {}).get("login", "").lower() != owner:
                        continue  # fremde Meldungen ignorieren (oeffentliches Repo)
                    m = re.search(r"[1-9A-HJ-NP-Za-km-z]{32,44}|0x[0-9a-fA-F]{40}", title + " " + (issue.get("body") or ""))
                    ok, msg = self.report_rug(m.group(0) if m else "", "dir (Handy)")
                    call(f"{api}/{issue['number']}/comments", {"body": ("✅ " if ok else "⚠️ ") + msg}, "POST")
                    call(f"{api}/{issue['number']}", {"state": "closed"}, "PATCH")
            except Exception as e:
                self.log("system", f"Rug-Meldungen von GitHub nicht lesbar: {e}", "error")
            self.stop_event.wait(30)

    # ---------- Berichte (Safe Desk) ----------
    @staticmethod
    def max_drawdown(points):
        peak, dd = 0, 0.0
        for _, v in points:
            peak = max(peak, v)
            if peak:
                dd = max(dd, (1 - v / peak) * 100)
        return dd

    @staticmethod
    def trade_stats(trades):
        wins = [t for t in trades if t["pnl_usd"] >= 0]
        losses = [t for t in trades if t["pnl_usd"] < 0]
        avg_w = sum(t["pnl_usd"] for t in wins) / len(wins) if wins else 0
        avg_l = sum(-t["pnl_usd"] for t in losses) / len(losses) if losses else 0
        best = max(trades, key=lambda t: t["pnl_usd"], default=None)
        worst = min(trades, key=lambda t: t["pnl_usd"], default=None)
        short = lambda t: t and {"symbol": t["symbol"], "pnl_usd": round(t["pnl_usd"], 2), "pnl_pct": round(t["pnl_pct"], 1), "reason": t.get("reason")}
        return {"trades": len(trades), "wins": len(wins), "losses": len(losses),
                "win_rate": round(100 * len(wins) / len(trades)) if trades else None,
                "avg_win": round(avg_w, 2), "avg_loss": round(avg_l, 2), "pnl": round(sum(t["pnl_usd"] for t in trades), 2),
                "best": short(best), "worst": short(worst)}

    @staticmethod
    def exit_kind(reason):
        r = reason or ""
        for key, label in (("Rug", "Rug-Exit"), ("Trailing", "Trailing-Stop"), ("Stop-Loss", "Stop-Loss"), ("Zeit-Exit", "Zeit-Exit"),
                           ("Liquiditaet", "Liquidität abgezogen"), ("Dev", "Dev verkauft")):
            if key in r:
                return label
        return "Sonstiges"

    @staticmethod
    def kill_rule(reason):
        r = reason or ""
        for key, label in (("Score", "Judge-Score < 70"), ("Liquiditaet", "Liquidität < $40k"), ("Volumen 1h", "Volumen 1h < $50k"),
                           ("in 1h", "schon +80% in 1h"), ("Nachahmer", "Nachahmer-Ticker"), ("Fake", "Fake-Volumen"),
                           ("gerugt", "schon gerugt"), ("Mint", "Mint nicht gesperrt"), ("Freeze", "Freeze nicht gesperrt"),
                           ("LP", "LP nicht gesperrt"), ("Top-10", "Top-10 > 25%"), ("Dev verkauft", "Dev verkauft noch"),
                           ("Dev-Anteil", "Dev-Anteil unbekannt"), ("Dev haelt", "Dev > 3%"), ("Dev hat", "Dev mit Rug-Vergangenheit"),
                           ("Serien", "Serien-Dev"), ("Bundle", "Bundle/Insider > 10%"), ("missing", "Daten fehlen")):
            if key in r:
                return label
        return r[:30]

    def reports(self):
        """Morgen-Bericht (letzte 24 h), Ehrlichkeits-Check (7 Tage) und Live-Bereitschaft."""
        t = now()
        full = [x for x in self.closed if not x.get("partial")]
        day = [x for x in full if t - x.get("closed_at", 0) <= 86400]
        week = [x for x in full if t - x.get("closed_at", 0) <= 7 * 86400]
        pts = [x for x in self.equity_long if t - x[0] <= 86400] + [x for x in self.equity_history if t - x[0] <= 86400]
        pts.sort()
        kills = {}
        for k in self.kill_log:
            if t - k["ts"] <= 86400:
                rule = self.kill_rule(k.get("reason"))
                kills[rule] = kills.get(rule, 0) + 1
        rec = self.trade_stats(day)
        last_rug = max((x.get("closed_at", 0) for x in full if x.get("rug") or "Rug" in (x.get("reason") or "")), default=None)
        by_exit = {}
        for x in day:
            k = self.exit_kind(x.get("reason"))
            by_exit[k] = by_exit.get(k, 0) + x["pnl_usd"]
        rec.update({
            "kills": sorted(kills.items(), key=lambda kv: -kv[1]), "kills_total": sum(kills.values()),
            "in_range": self.stats.get("desk_scout", 0), "blocked": self.stats.get("blocked", 0),
            "max_drawdown_pct": round(self.max_drawdown(pts), 1), "balance": round(self.equity(), 2),
            "days_without_rug": round((t - (last_rug or self.started_at)) / 86400, 1), "had_rug": last_rug is not None,
            "worked": max(by_exit.items(), key=lambda kv: kv[1])[0] if by_exit and max(by_exit.values()) > 0 else None,
            "change": min(by_exit.items(), key=lambda kv: kv[1])[0] if by_exit and min(by_exit.values()) < 0 else None,
        })
        buckets = []
        for lo, hi, label in ((70, 80, "70-79"), (80, 90, "80-89"), (90, 101, "90+")):
            b = [x for x in week if x.get("score") is not None and lo <= x["score"] < hi]
            w = [x for x in b if x["pnl_usd"] >= 0]
            buckets.append({"label": label, "n": len(b), "win_rate": round(100 * len(w) / len(b)) if b else None,
                            "avg_pct": round(sum(x["pnl_pct"] for x in b) / len(b), 1) if b else None})
        wk = self.trade_stats(week)
        all_pts = sorted(self.equity_long + self.equity_history)
        st = self.trade_stats(full)
        days = (t - self.started_at) / 86400
        g = self.cfg.get("go_live", {})
        checks = [
            (f"mind. {g.get('min_days', 7)} Tage Paper", days >= g.get("min_days", 7), f"{days:.1f} Tage"),
            (f"mind. {g.get('min_trades', 50)} Trades", st["trades"] >= g.get("min_trades", 50), f"{st['trades']} Trades"),
            ("Gewinn nach Gebühren + Slippage", st["pnl"] > 0, f"{st['pnl']:+.2f} $"),
            (f"max. Drawdown < {g.get('max_drawdown_pct', 20)}%", self.max_drawdown(all_pts) < g.get("max_drawdown_pct", 20), f"{self.max_drawdown(all_pts):.1f}%"),
            (f"Ø Gewinn > {g.get('win_loss_ratio', 1.5)}× Ø Verlust", st["avg_loss"] > 0 and st["avg_win"] > g.get("win_loss_ratio", 1.5) * st["avg_loss"],
             f"{st['avg_win']:.2f} $ vs. {st['avg_loss']:.2f} $"),
        ]
        return {"recap": rec,
                "honesty": {"buckets": buckets, "week": wk, "vs_nothing": wk["pnl"]},
                "go_live": {"ready": all(c[1] for c in checks), "checks": [{"name": n, "ok": ok, "value": v} for n, ok, v in checks]}}

    # ---------- Dashboard-Daten ----------
    def snapshot(self):
        """Dashboard-Daten; bei mehreren Chains alles zusammen (eine Kasse, alle Positionen, alle Trades)."""
        snap = self.own_snapshot()
        if self is not self.family[0] or len(self.family) == 1:
            return snap
        snap["chains"] = [b.chain for b in self.family]
        for other in self.family[1:]:
            o = other.own_snapshot()
            snap["positions"] = sorted(snap["positions"] + o["positions"], key=lambda x: -x["pnl_pct"])
            snap["closed"] = sorted(snap["closed"] + o["closed"], key=lambda t: -t.get("closed_at", 0))[:30]
            snap["activity"] = sorted(snap["activity"] + o["activity"], key=lambda a: -a["ts"])[:120]
            for k in ("trades", "wins", "losses", "partials", "realized_pnl", "errors", "scan_rate_min"):
                if isinstance(snap.get(k), (int, float)) and isinstance(o.get(k), (int, float)):
                    snap[k] += o[k]
            for k, v in (o.get("stats") or {}).items():
                if isinstance(v, (int, float)) and isinstance(snap["stats"].get(k), (int, float)):
                    snap["stats"][k] += v
                elif isinstance(v, dict) and isinstance(snap["stats"].get(k), dict):
                    for kk, vv in v.items():
                        if isinstance(vv, (int, float)):
                            snap["stats"][k][kk] = snap["stats"][k].get(kk, 0) + vv
        return snap

    def own_snapshot(self):
        with self.lock:
            eq = self.equity()
            start = self.cfg["start_balance_usd"]
            full = [t for t in self.closed if not t.get("partial")]
            wins = [t for t in full if t["pnl_usd"] >= 0]
            positions = []
            for p in self.positions.values():
                val = p["qty"] * p["last_price"]
                positions.append({
                    "symbol": p["symbol"], "url": p.get("url"), "cost_usd": p["cost_usd"], "value_usd": val,
                    "pnl_pct": (p["last_price"] / p["entry_price"] - 1) * 100,
                    "peak_pct": (p["peak_price"] / p["entry_price"] - 1) * 100,
                    "held_min": (now() - p["opened_at"]) / 60,
                    "entry_mcap": p.get("entry_mcap") or (p.get("info") or {}).get("mcap"),
                    "last_mcap": p.get("last_mcap") or (p.get("entry_mcap") or (p.get("info") or {}).get("mcap") or 0) * p["last_price"] / p["entry_price"],
                    "entry_price": p["entry_price"], "adds": p.get("adds", 0) + p.get("dip_adds", 0), "dip_adds": p.get("dip_adds", 0), "partial_done": p.get("partial_done", False),
                    "partial_level": p.get("partial_level", 1 if p.get("partial_done") else 0), "realized_usd": p.get("realized_usd", 0),
                    "hist": downsample(p.get("hist") or [], 900),
                    "entry_hist": p.get("entry_hist") or [[p["opened_at"], p.get("first_entry_price") or p["entry_price"]]],
                    "events": p.get("events") or [[p["opened_at"], "Kauf", p.get("first_entry_price") or p["entry_price"]]],
                    "token": p["token"], "pair": p.get("pair"), "chart": (p.get("info") or {}).get("chart"), "chain": self.chain,
                })
            hist = self.equity_history[-600:]
            ranges = self.equity_ranges()
            return {
                "mode": "PAPER", "now": now(), "started_at": self.started_at,
                "start_balance": start, "equity": eq, "cash": self.cash,
                "multiple": eq / start if start else 0, "goal": self.cfg["goal_multiple"],
                "trades": len(full), "wins": len(wins), "losses": len(full) - len(wins),
                "partials": len(self.closed) - len(full),
                "realized_pnl": sum(t["pnl_usd"] for t in self.closed),
                "stats": self.stats, "analyst_view": self.analyst_view, "errors": self.errors,
                "positions": sorted(positions, key=lambda x: -x["pnl_pct"]),
                "closed": self.closed[-30:][::-1],
                "activity": list(self.activity)[:120],
                "last_msg": self.last_msg,
                "blacklist": {"count": len(self.blacklist),
                              "recent": sorted(({"wallet": w, **v} for w, v in self.blacklist.items()),
                                               key=lambda x: -x.get("ts", 0))[:8]},
                "chain": self.chain,
                "scan_rate_min": self.run_seen / max((now() - self.run_started) / 60, 1),
                "fomo": {
                    "enabled": bool(self.cfg.get("fomo", {}).get("enabled")),
                    "tx_seen": self.fomo_stats["tx_seen"], "tx_parsed": self.fomo_stats["tx_parsed"],
                    "wallets": len(self.fomo_stats["wallets"]),
                    "hot": [{"mint": m, "symbol": self.symbols.get(m), "buyers": b, "sellers": sl} for m, b, sl in self.fomo_hot(8)],
                },
                "equity_history": hist, "equity_ranges": ranges, "pf_hot": self.pf_hot_view,
                "manual": self.manual_view(),
                "safe": dict(self.reports(), market=self.market, block=self.entry_block() if self.cfg.get("desk", {}).get("enabled") else None,
                             stale=self.stale_sources(), loss_streak=self.loss_streak, pause_until=self.pause_until, day=self.day),
                "desk": {"enabled": bool(self.cfg.get("desk", {}).get("enabled")), "kills": self.kill_log[-60:][::-1],
                         "scores": self.desk_scores[-25:][::-1], "min_score": self.cfg.get("desk", {}).get("min_score")},
                "wallets": {"watch": (self.cfg.get("watch_wallets") or {}).get("wallets", {}) if self.chain == "solana" else {},
                            "until": (self.cfg.get("watch_wallets") or {}).get("until"),
                            "trades": [dict(t, symbol=t.get("symbol") or self.symbols.get(t["mint"])) for t in self.ww_trades[-30:]][::-1]},
                "config": self.cfg,
            }


def export_snapshot(bot, path):
    """Dashboard-Daten als Datei (fuer die Handy-Seite auf GitHub Pages)."""
    snap = bot.snapshot()
    snap["static_export"] = True
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(snap, f, separators=(",", ":"), ensure_ascii=False)
    os.replace(tmp, path)


def chain_state_path(state_path, chain):
    return os.path.splitext(state_path)[0] + f"_{chain}.json"


def build_bot(cfg, state_path):
    """Hauptbot (cfg["chain"]) plus weitere Chains aus cfg["extra_chains"], alle mit einer gemeinsamen Kasse."""
    bot = Bot(cfg, state_path)
    for chain in cfg.get("extra_chains") or []:
        sub_cfg = json.loads(json.dumps(cfg))
        sub_cfg["chain"] = chain
        sub_cfg["extra_chains"] = []
        sub = Bot(sub_cfg, chain_state_path(state_path, chain), CHAIN_NAMES.get(chain, chain))
        bot.join_chain(sub)
    return bot


def start_all(bot):
    for b in bot.family:
        b.start()


def save_all(bot):
    for b in bot.family:
        b.save_state()


def run_headless(cfg, state_path, args):
    """Laeuft ohne Tastatur und Webserver eine feste Zeit, schreibt regelmaessig Stand und Dashboard-Daten."""
    bot = build_bot(cfg, state_path)
    start_all(bot)
    end = now() + args.minutes * 60
    print(f"  Grok Bot (PAPER) laeuft ohne Bildschirm fuer {args.minutes:.0f} Minuten", flush=True)
    try:
        while now() < end:
            time.sleep(min(args.export_every, max(end - now(), 0.1)))
            try:
                save_all(bot)
                if args.export:
                    export_snapshot(bot, args.export)
            except Exception as e:  # ein Fehler beim Speichern darf den Lauf nicht beenden
                bot.log("system", f"Speichern fehlgeschlagen: {e}", "error")
    except KeyboardInterrupt:
        pass
    finally:
        bot.stop_event.set()
        save_all(bot)
        if args.export:
            export_snapshot(bot, args.export)
        print("Lauf beendet, Stand gespeichert.", flush=True)


def setup_currency(cfg):
    """Startkapital in Euro erlauben; Anzeige in Euro mit aktuellem EZB-Kurs (Fallback: Wert aus config.json)."""
    rate = cfg.get("usd_pro_eur", 1.115)
    try:
        rate = fnum(http_json("https://api.frankfurter.dev/v1/latest?base=EUR&symbols=USD", timeout=8)["rates"]["USD"], rate)
    except Exception:
        pass
    cfg["usd_pro_eur"] = rate
    if cfg.get("start_kapital_eur"):
        cfg["start_balance_usd"] = round(cfg["start_kapital_eur"] * rate, 2)
    print(f"  Kurs: 1 EUR = {rate:.4f} USD", flush=True)


class Handler(BaseHTTPRequestHandler):
    bot = None

    def log_message(self, *a):
        pass

    def send(self, code, body, ctype):
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # Browser hat die Verbindung geschlossen (z. B. App-Wechsel auf dem iPhone)

    def do_GET(self):
        if self.path.startswith("/api/state"):
            self.send(200, json.dumps(self.bot.snapshot()).encode(), "application/json")
        elif self.path in ("/", "/index.html"):
            with open(os.path.join(STATIC_DIR, "index.html"), "rb") as f:
                self.send(200, f.read(), "text/html; charset=utf-8")
        else:
            self.send(404, b"not found", "text/plain")


    def do_POST(self):
        if self.path.startswith("/api/rug"):
            try:
                n = int(self.headers.get("Content-Length") or 0)
                token = json.loads(self.rfile.read(min(n, 4096)) or b"{}").get("token", "")
            except Exception:
                token = ""
            ok, msg = self.bot.report_rug(token)
            self.send(200 if ok else 400, json.dumps({"ok": ok, "msg": msg}).encode(), "application/json")
        else:
            self.send(404, b"not found", "text/plain")


def main():
    ap = argparse.ArgumentParser(description="Grok Bot - Paper-Trading fuer Solana-Memecoins")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--reset", action="store_true", help="state.json loeschen und neu starten")
    ap.add_argument("--config", default=CONFIG_PATH, help="andere Einstellungsdatei, z. B. config_abschluss.json")
    ap.add_argument("--rug", metavar="COIN_ADRESSE", help="Coin als Rug melden: Dev-Wallet sperren und Bot beenden")
    ap.add_argument("--headless", action="store_true", help="ohne Tastatur/Webserver laufen (z. B. GitHub Actions)")
    ap.add_argument("--minutes", type=float, default=55, help="mit --headless: so viele Minuten laufen, dann speichern und beenden")
    ap.add_argument("--export", metavar="DATEI", help="Dashboard-Daten regelmaessig als JSON schreiben (z. B. docs/data/state.json)")
    ap.add_argument("--export-every", type=float, default=20, help="Sekunden zwischen zwei Exporten")
    args = ap.parse_args()
    cfg_path = args.config if os.path.isabs(args.config) else os.path.join(HERE, args.config)
    state_path = STATE_PATH if cfg_path == CONFIG_PATH else os.path.splitext(cfg_path)[0] + "_state.json"

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # Windows-Konsole: exotische Coin-Namen nicht abstuerzen lassen
        except Exception:
            pass
    with open(cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)
    setup_currency(cfg)
    if args.reset:
        for path in [state_path] + [chain_state_path(state_path, c) for c in cfg.get("extra_chains") or []]:
            if os.path.exists(path):
                os.remove(path)

    if args.rug:
        ok, msg = build_bot(cfg, state_path).report_rug(args.rug)
        print(msg + (" Der Bot meidet kuenftig ihre Coins und verkauft offene Positionen." if ok else ""))
        return

    if args.headless:
        run_headless(cfg, state_path, args)
        return

    while True:
        bot = build_bot(cfg, state_path)
        start_all(bot)
        Handler.bot = bot
        srv = ThreadingHTTPServer((args.host, args.port), Handler)
        srv.handle_error = lambda request, client_address: None  # abgebrochene Verbindungen nicht ins Log
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        print(f"\n  Grok Bot (PAPER) laeuft -> http://{args.host}:{args.port}", flush=True)
        print("  Stoppen: q + Enter  ·  Neustart: j + Enter\n", flush=True)
        action = "stop"
        try:
            # Auf dem iPhone (a-Shell) kommt Strg+C oft nicht an, daher Stopp/Neustart per Eingabe
            while True:
                cmd = input().strip().lower()
                if cmd in ("q", "quit", "stop", "exit"):
                    break
                if cmd == "j":
                    print("\n  NEUSTART - Enter = Stand behalten, n + Enter = komplett neu mit Startkapital", flush=True)
                    action = "fresh" if input().strip().lower() == "n" else "restart"
                    break
        except EOFError:
            bot.stop_event.wait()  # ohne Tastatur (z. B. im Hintergrund) einfach weiterlaufen
        except KeyboardInterrupt:
            pass
        finally:
            bot.stop_event.set()
            srv.shutdown()
            srv.server_close()
            bot.save_state()
            print("Gestoppt, Stand gespeichert.", flush=True)
        if action == "stop":
            break
        if action == "fresh" and os.path.exists(state_path):
            os.remove(state_path)
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)  # geaenderte Einstellungen uebernehmen
        setup_currency(cfg)
        print("  Neustart " + ("mit frischem Startkapital" if action == "fresh" else "- Stand bleibt erhalten") + " ...", flush=True)
        time.sleep(1)


if __name__ == "__main__":
    main()
