#!/usr/bin/env python3
"""Baut die Handy-Seite fuer GitHub Pages aus static/index.html (liest docs/data/state.json statt /api/state)."""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
html = open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8").read()
repo = os.environ.get("GITHUB_REPOSITORY", "cvhfmjr499-svg/Grok-bot")  # Rug-Meldungen landen dort als Issue
inject = (f'<script>window.GROKBOT_API = "data/state.json"; window.GROKBOT_REFRESH_MS = 15000; window.GROKBOT_REPO = "{repo}";</script>\n'
          '<meta name="robots" content="noindex">\n')
html = html.replace("<!-- TradingView Lightweight Charts", inject + "<!-- TradingView Lightweight Charts", 1)
os.makedirs(os.path.join(ROOT, "docs", "data"), exist_ok=True)
open(os.path.join(ROOT, "docs", "index.html"), "w", encoding="utf-8").write(html)
open(os.path.join(ROOT, "docs", ".nojekyll"), "w").close()
print("docs/index.html gebaut")
