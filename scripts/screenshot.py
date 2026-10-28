#!/usr/bin/env python3
"""Screenshot der Handy-Seite nach jedem Lauf: docs/screenshots/JJJJ-MM-TT_HHMM.png (UTC), behaelt die letzten 72."""
import functools
import http.server
import os
import sys
import threading
import time
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(ROOT, "docs")
OUT = os.path.join(DOCS, "screenshots")
KEEP = 72


def main():
    os.makedirs(OUT, exist_ok=True)
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

    handler = functools.partial(Quiet, directory=DOCS)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    name = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M") + ".png"
    with sync_playwright() as p:
        # auf GitHub ist Chrome schon installiert -> kein Browser-Download noetig
        try:
            browser = p.chromium.launch(channel="chrome")
        except Exception:
            browser = p.chromium.launch(executable_path=os.environ.get("CHROME_PATH"))
        page = browser.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=2)
        page.goto(f"http://127.0.0.1:{srv.server_address[1]}/", wait_until="networkidle")
        time.sleep(4)  # Charts zeichnen lassen
        height = min(page.evaluate("document.documentElement.scrollHeight"), 2600)
        page.screenshot(path=os.path.join(OUT, name), full_page=True,
                        clip={"x": 0, "y": 0, "width": 390, "height": height})
        browser.close()
    srv.shutdown()
    for old in sorted(f for f in os.listdir(OUT) if f.endswith(".png"))[:-KEEP]:
        os.remove(os.path.join(OUT, old))
    print("Screenshot gespeichert:", name)


if __name__ == "__main__":
    sys.exit(main())
