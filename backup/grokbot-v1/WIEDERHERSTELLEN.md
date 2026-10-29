# Sicherung: Grok Bot v1 (Stand 29.10.2026, vor dem "Memecoin Desk")

Diese Kopie enthaelt den kompletten Bot so, wie er vor dem Umbau lief:
bot.py, config.json, config_abschluss.json, Dashboard (static/), Skripte, Workflow, Spielstand (state.json) und Dev-Blacklist.

## Zurueck zur alten Version
Im Repo-Hauptordner:

```bash
cp backup/grokbot-v1/bot.py backup/grokbot-v1/config.json backup/grokbot-v1/config_abschluss.json .
cp -r backup/grokbot-v1/static backup/grokbot-v1/scripts .
cp backup/grokbot-v1/.github/workflows/bot-run.yml.txt .github/workflows/bot-run.yml
cp backup/grokbot-v1/state.json backup/grokbot-v1/dev_blacklist.json .   # alter Spielstand (optional)
python3 scripts/build_docs.py
git add -A && git commit -m "Zurueck zu Grok Bot v1" && git push
```

Oder einfach Claude sagen: "Stell den alten Grok Bot wieder her."
