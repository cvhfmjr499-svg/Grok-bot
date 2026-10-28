#!/usr/bin/env python3
"""
Grok Bot - Paper-Trading-Bot fuer Solana-Memecoins.

5 Agenten in einer Pipeline:
  Scout   -> findet neue Tokens (DexScreener: neueste Profile + Boosts)
  Analyst -> filtert nach Liquiditaet, Alter, Volumen, Kaeuferdruck + RugCheck
  Sniper  -> kauft (mit Spielgeld) inkl. simulierter Slippage und Gebuehren
  Risk    -> ueberwacht offene Positionen: Stop-Loss, Trailing-Stop, Take-Profit
  Banker  -> verkauft, verbucht Gewinn/Verlust

Es wird KEIN echtes Geld bewegt und keine Wallet benoetigt.
Start:  python3 bot.py            -> Dashboard auf http://localhost:8080
"""
import argparse
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
GOPLUS_CHAIN = {"bsc": "56", "base": "8453", "robinhood": "4663"}
QUOTE_SYMBOLS = {"WETH", "ETH", "WBNB", "BNB", "USDC", "USDT", "DAI", "USD1", "CBBTC", "WBTC", "SOL", "WSOL", "USDE"}
JUP = "https://lite-api.jup.ag/tokens/v2"
# Jupiter-Listen: (Pfad, Abfrage alle x Sekunden, Quellen-Name)
JUP_LISTS = [("/toptrending/1h?limit=100", 120, "JUP-Trend"), ("/toporganicscore/1h?limit=100", 120, "JUP-Organic"),
             ("/toptraded/1h?limit=100", 180, "JUP-Traded"), ("/recent?limit=100", 90, "JUP-Neu")]
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


def fnum(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


class Bot:
    def __init__(self, cfg, state_path=STATE_PATH, name=""):
        self.cfg = cfg
        self.state_path = state_path
        self.name = name
        self.lock = threading.RLock()
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
        self.equity_history = []  # [ts, equity]
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
            self.closed = s["closed"]
            self.equity_history = s.get("equity_history", [])[-2000:]
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
                "equity_history": self.equity_history[-2000:], "cooldown": self.cooldown,
                "token_losses": self.token_losses,
                "started_at": self.started_at, "start_balance_usd": self.cfg["start_balance_usd"],
            }
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(s, f)
        os.replace(tmp, self.state_path)

    # ---------- Hilfen ----------
    def log(self, agent, msg, kind="info"):
        entry = {"ts": now(), "agent": agent, "msg": msg, "kind": kind}
        with self.lock:
            self.activity.appendleft(entry)
            self.last_msg[agent] = entry
        print(f"[{time.strftime('%H:%M:%S')}] {self.name + ' ' if self.name else ''}{agent:<8} {msg}", flush=True)

    def equity(self):
        with self.lock:
            return self.cash + sum(p["qty"] * p["last_price"] for p in self.positions.values())

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
            data = http_json(self.gecko + path).get("data", [])
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
        self.organic[tok["id"]] = {"ts": now(), "score": fnum(tok.get("organicScore")),
                                   "label": tok.get("organicScoreLabel"),
                                   "top_holders": fnum((tok.get("audit") or {}).get("topHoldersPercentage"), None)}

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

    def analyze(self, pair):
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
        lh = self.liq_hist.setdefault(token, [])
        lh.append((now(), liq))
        del lh[:-30]
        past = [l for ts, l in lh if now() - 5400 <= ts < now() - 600]
        shrink = (1 - liq / max(past)) * 100 if past and max(past) > 0 else 0
        checks = [
            ((pair["baseToken"].get("symbol") or "").upper() not in QUOTE_SYMBOLS, "Basis-Token (kein Memecoin)", 10080),
            (self.token_losses.get(token, 0) < c.get("max_losses_per_token", 99), "zu viele Verluste mit diesem Coin", 10080),
            (price > 0, "kein Preis", 10),
            (liq >= c["min_liquidity_usd"], f"Liquiditaet ${liq:,.0f} zu gering", 15),
            (liq <= c["max_liquidity_usd"], "zu gross/kein Memecoin", 1440),
            (age_min >= c["min_age_minutes"], f"zu jung ({age_min:.0f} min)", young_wait),
            (age_min <= c["max_age_hours"] * 60, "zu alt", 10080),
            (vol_h1 >= c["min_volume_h1_usd"], f"Volumen 1h ${vol_h1:,.0f} zu gering", 10),
            (vol_h24 >= c.get("min_volume_h24_usd", 0), f"Volumen 24h unter ${c.get('min_volume_h24_usd', 0):,.0f}", 15),
            (c["min_mcap_usd"] <= mcap <= c["max_mcap_usd"], f"MCap ${mcap:,.0f} ausserhalb", 15),
            (buys >= c["min_buys_h1"], f"nur {buys:.0f} Kaeufe/1h", 10),
            (sells == 0 or buys / sells >= c["min_buy_sell_ratio_h1"], "mehr Verkaeufer als Kaeufer", 10),
            (not c.get("max_sell_buy_ratio_h1") or sells <= c["max_sell_buy_ratio_h1"] * max(buys, 1),
             f"starker Verkaufsdruck ({sells:.0f} Verkaeufe vs. {buys:.0f} Kaeufe/1h)", 15),
            (not c.get("max_sell_buy_ratio_h6") or sells6 <= c["max_sell_buy_ratio_h6"] * max(buys6, 1),
             f"Slow-Rug: langsamer Abverkauf ({sells6:.0f} Verkaeufe vs. {buys6:.0f} Kaeufe/6h)", 30),
            (not c.get("max_liq_shrink_pct_90m") or shrink < c["max_liq_shrink_pct_90m"],
             f"Slow-Rug: Liquiditaet schrumpft (-{shrink:.0f}% in 90 min)", 30),
            (ch_h1 >= c["min_change_h1_pct"], f"faellt ({ch_h1:+.0f}% 1h)", 10),
            (ch_m5 <= c["max_change_m5_pct"], f"schon gepumpt ({ch_m5:+.0f}% 5m)", 10),
            (ch_h1 <= c["max_change_h1_pct"], f"schon gepumpt ({ch_h1:+.0f}% 1h)", 10),
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
            if lp < c["min_lp_locked_pct"]:
                return False, f"LP nur {lp:.0f}% gesperrt", dict(info, recheck_min=60)
        return True, "alle Checks bestanden", info

    # ---------- Agent 3: Sniper ----------
    def slippage_pct(self, size_usd, liq_usd):
        # grobes Modell: Basis + Preiswirkung abhaengig von Ordergroesse/Liquiditaet
        return self.cfg["sniper"]["base_slippage_pct"] + 100 * size_usd / max(liq_usd, 1)

    def buy(self, pair, info):
        s = self.cfg["sniper"]
        token = pair["baseToken"]["address"]
        sym = pair["baseToken"].get("symbol", "?")
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
            price = fnum(pair["priceUsd"])
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
                    if rc.get("rugged"):
                        self.rug_alarm[token] = "RugCheck meldet Rug"
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
                        pos["last_price"] = fnum(p.get("priceUsd"), pos["last_price"])
                        pos["last_liq"] = fnum((p.get("liquidity") or {}).get("usd"), pos["last_liq"])
                        pos["peak_price"] = max(pos["peak_price"], pos["last_price"])
                        pos["last_mcap"] = fnum(p.get("marketCap") or p.get("fdv"), pos.get("last_mcap") or 0)
                        m5 = (p.get("txns") or {}).get("m5") or {}
                        b5, s5 = fnum(m5.get("buys")), fnum(m5.get("sells"))
                        rw = r.get("rugwatch", {})
                        if rw.get("enabled") and s5 >= rw.get("min_sells_m5", 30) and s5 >= rw.get("sell_buy_ratio_m5", 5) * max(b5, 1):
                            self.rug_alarm.setdefault(token, f"{s5:.0f} Verkaeufe vs. {b5:.0f} Kaeufe in 5 min")
                        h6 = (p.get("txns") or {}).get("h6") or {}
                        b6, s6 = fnum(h6.get("buys")), fnum(h6.get("sells"))
                        losing = pos["last_price"] < pos["entry_price"]
                        if rw.get("enabled") and losing and rw.get("sell_buy_ratio_h6") and s6 >= 100 and s6 >= rw["sell_buy_ratio_h6"] * max(b6, 1):
                            self.rug_alarm.setdefault(token, f"Slow-Rug: {s6:.0f} Verkaeufe vs. {b6:.0f} Kaeufe in 6 h")
                        lq = pos.setdefault("liq_hist", [])
                        lq.append([now(), pos["last_liq"]])
                        del lq[:-1500]
                        recent = [l for ts, l in lq if now() - ts <= 5400]
                        if rw.get("enabled") and losing and recent and pos["last_liq"] < max(recent) * (1 - rw.get("liq_drain_pct_90m", 15) / 100):
                            self.rug_alarm.setdefault(token, f"Slow-Rug: Liquiditaet fliesst ab (-{(1 - pos['last_liq'] / max(recent)) * 100:.0f}% in 90 min)")
                        h = pos.setdefault("hist", [])
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
                    ptp = r.get("partial_tp", {})
                    if p and ptp.get("enabled"):
                        self.partial_take_profit(token, pos, ptp)
                    price, entry, peak = pos["last_price"], pos["entry_price"], pos["peak_price"]
                    pnl = (price / entry - 1) * 100
                    peak_pnl = (peak / entry - 1) * 100
                    held_min = (now() - pos["opened_at"]) / 60
                    liq_drop = (1 - pos["last_liq"] / max(pos["entry_liq"], 1)) * 100
                    reason = None
                    dev = self.creators.get(token)
                    if dev and dev in self.blacklist:
                        reason = "Dev-Wallet auf Blacklist (Rug gemeldet)"
                    elif token in self.rug_alarm:
                        reason = f"Soft-Rug-Alarm: {self.rug_alarm.pop(token)} (Rug-Gefahr)"
                    elif liq_drop >= r["exit_if_liquidity_drops_pct"]:
                        reason = f"Liquiditaet -{liq_drop:.0f}% (Rug-Gefahr)"
                    elif pnl <= -r["stop_loss_pct"]:
                        reason = f"Stop-Loss {pnl:+.1f}%"
                    elif r.get("take_profit_pct") and pnl >= r["take_profit_pct"]:
                        reason = f"Take-Profit {pnl:+.1f}%"
                    elif peak_pnl >= r["trailing_activate_pct"] and price <= peak * (1 - r["trailing_distance_pct"] / 100):
                        reason = f"Trailing-Stop (Hoch {peak_pnl:+.0f}%, jetzt {pnl:+.0f}%)"
                    elif held_min >= r["max_hold_minutes"] and (pnl < 0 or not r.get("hold_only_if_losing")):
                        reason = f"Haltezeit {held_min:.0f} min abgelaufen"
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
        if pos["last_liq"] < pos["entry_liq"] * (1 - dip.get("max_liq_drop_pct", 10) / 100):
            return  # Liquiditaet schrumpft -> Rug-Gefahr
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
        part = step[1] / 100
        qty = pos["qty"] * part
        gross = qty * pos["last_price"]
        proceeds = gross * (1 - self.slippage_pct(gross, pos["last_liq"]) / 100)
        proceeds = max(proceeds - proceeds * s["fee_pct_per_side"] / 100 - s["network_fee_usd"], 0)
        cost = pos["cost_usd"] * part
        self.cash += proceeds
        pos["qty"] -= qty
        pos["cost_usd"] -= cost
        pos["partial_level"] = n + 1
        pos["partial_done"] = True
        pos["realized_usd"] = pos.get("realized_usd", 0) + proceeds - cost
        pos.setdefault("events", []).append([now(), f"TP {n + 1}", pos["last_price"]])
        pnl = proceeds - cost
        self.closed.append({
            "symbol": pos["symbol"], "token": token, "url": pos.get("url"), "partial": True,
            "opened_at": pos["opened_at"], "closed_at": now(), "cost_usd": cost, "proceeds_usd": proceeds,
            "pnl_usd": pnl, "pnl_pct": pnl / cost * 100 if cost else 0,
            "reason": f"Teilgewinn {n + 1}: {step[1]:.0f}% verkauft bei +{gain:.0f}%",
            "source": pos.get("source", "?"), "entry_mcap": pos.get("entry_mcap"), "exit_mcap": pos.get("last_mcap"),
            "peak_pct": (pos["peak_price"] / pos["entry_price"] - 1) * 100, "held_min": (now() - pos["opened_at"]) / 60,
        })
        self.log("banker", f"TEILGEWINN {n + 1} ${pos['symbol']}: {step[1]:.0f}% verkauft bei +{gain:.0f}% ({pnl:+,.2f} $), Rest laeuft weiter", "win")

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
                "held_min": (now() - pos["opened_at"]) / 60,
            }
            self.closed.append(trade)
        kind = "win" if pnl >= 0 else "loss"
        if pnl < 0:
            with self.lock:
                self.token_losses[token] = self.token_losses.get(token, 0) + 1
        self.log("banker", f"VERKAUF ${pos['symbol']} {trade['pnl_pct']:+.1f}% ({pnl:+,.2f} $) - {reason}", kind)
        rug_cfg = self.cfg.get("dev_blacklist", {})
        if "Rug-Gefahr" in reason or trade["pnl_pct"] <= -rug_cfg.get("rug_loss_pct", 40):
            dev = self.creator_of(token)
            if dev and dev_blacklist_add(self.blacklist_path, dev, {
                    "symbol": pos["symbol"], "token": token, "chain": self.chain, "ts": now(),
                    "reason": reason, "pnl_pct": round(trade["pnl_pct"], 1)}):
                self.log("analyst", f"Dev-Wallet {dev[:6]}...{dev[-4:]} von ${pos['symbol']} gesperrt - kuenftig alle ihre Coins meiden", "warn")
        self.save_state()

    def record_equity(self):
        with self.lock:
            self.equity_history.append([now(), round(self.equity(), 2)])
            if len(self.equity_history) > 4000:
                self.equity_history = self.equity_history[::2]

    # ---------- Schleifen ----------
    def scout_loop(self):
        while not self.stop_event.is_set():
            try:
                fresh = self.scout()
                pairs = self.fetch_pairs(fresh) if fresh else {}
                if pairs and self.cfg.get("jupiter", {}).get("enabled", True):
                    self.fetch_organic(list(pairs))
                for token in fresh:
                    if token not in pairs:
                        self.next_check[token] = now() + 600
                for token, pair in pairs.items():
                    if self.stop_event.is_set():
                        break
                    ok, reason, info = self.analyze(pair)
                    sym = pair["baseToken"].get("symbol", "?")
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
                        self.log("analyst", f"${sym} freigegeben - Liq ${info['liq']:,.0f}, {info['ch_h1']:+.0f}% 1h", "pass")
                        self.buy(pair, info)
                    else:
                        self.log("analyst", f"${sym} abgelehnt: {reason}", "reject")
            except Exception as e:
                self.errors += 1
                self.log("system", f"Scout-Schleife: {e}", "error")
            self.stop_event.wait(self.cfg["scout_interval_s"])

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
        if not c.get("enabled"):
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
        for fn in (self.scout_loop, self.risk_loop, self.fomo_loop, self.stream_loop):
            threading.Thread(target=fn, daemon=True).start()
        if os.environ.get("GITHUB_REPOSITORY") and os.environ.get("GITHUB_TOKEN"):
            threading.Thread(target=self.github_reports_loop, daemon=True).start()

    def report_rug(self, token, who="dir"):
        """Coin von Hand als Rug melden: Dev-Wallet sperren und offene Position sofort verkaufen. -> Text fuer die Antwort."""
        token = (token or "").strip()
        if not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}|0x[0-9a-fA-F]{40}", token):
            return False, "Das ist keine gueltige Coin-Adresse."
        with self.lock:
            pos = self.positions.get(token)
            sym = pos["symbol"] if pos else token[:6]
            if pos:
                self.rug_alarm[token] = f"von {who} als Rug gemeldet"  # Risk verkauft beim naechsten Durchlauf (1 s)
        dev = self.creator_of(token)
        msg = f"${sym}: " + ("Position wird sofort verkauft. " if pos else "keine offene Position. ")
        if dev:
            added = dev_blacklist_add(self.blacklist_path, dev, {"symbol": sym, "token": token, "chain": self.chain,
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

    # ---------- Dashboard-Daten ----------
    def snapshot(self):
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
                    "token": p["token"], "pair": p.get("pair"),
                })
            hist = self.equity_history[-600:]
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
                "equity_history": hist,
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


def run_headless(cfg, state_path, args):
    """Laeuft ohne Tastatur und Webserver eine feste Zeit, schreibt regelmaessig Stand und Dashboard-Daten."""
    bot = Bot(cfg, state_path)
    bot.start()
    end = now() + args.minutes * 60
    print(f"  Grok Bot (PAPER) laeuft ohne Bildschirm fuer {args.minutes:.0f} Minuten", flush=True)
    try:
        while now() < end:
            time.sleep(min(args.export_every, max(end - now(), 0.1)))
            bot.save_state()
            if args.export:
                export_snapshot(bot, args.export)
    except KeyboardInterrupt:
        pass
    finally:
        bot.stop_event.set()
        bot.save_state()
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
    if args.reset and os.path.exists(state_path):
        os.remove(state_path)

    if args.rug:
        ok, msg = Bot(cfg, state_path).report_rug(args.rug)
        print(msg + (" Der Bot meidet kuenftig ihre Coins und verkauft offene Positionen." if ok else ""))
        return

    if args.headless:
        run_headless(cfg, state_path, args)
        return

    while True:
        bot = Bot(cfg, state_path)
        bot.start()
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
