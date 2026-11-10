# Spider Colony

A personal deal hunter for hotels, flights, and rental cars. You search like a travel site, star what you
want to track, and an AI "scout" keeps checking prices and alerts you (text and/or email) when one drops
below your target. It never books anything: every alert links to the booking page and you pay yourself.

Runs locally on your Mac. Python standard library only for the app, SQLite for data.

## How it works

```
web/index.html  ◄──►  server.py (local API)  ◄──►  colony.py (watches, price log, deal rules, CLI)  ◄──► data/prices.db
                                                          ▲
                           scheduled AI scout, following SCOUT.md, calls the CLI to record prices and send alerts
```

- **`colony.py`** is the core: watches, a price log, the deal rules, a search queue, and a CLI the scout
  drives (`python3 colony.py --help`).
- **`server.py` + `web/index.html`** are the app: a three-tab search UI (hotels, flights, cars).
- **`SCOUT.md`** is the instruction file for the scheduled AI scout. It is the agent's operating
  procedure: check whether the week is still open, exit cheaply if there is no work, fan out source
  queries in parallel, log every price with its basis, and alert only on qualifying deals.
- **`notify_sms.py`** sends optional Twilio texts. **`email_template.html`** is the alert email.

## Design decisions

- **Final price only.** A price can only trigger an alert if it includes taxes and fees (`--basis total`).
  Pre-tax and unknown prices are logged but never alert.
- **Honesty about sources.** Corporate-negotiated car rates are skipped because a regular customer can't
  get them. Mileage and room type are never claimed unless the source confirms them.
- **The agent is bounded.** The scout runs on a fixed schedule for one week at a time, then stops and
  asks to be renewed, so it can't run forever or burn usage. It is told never to book, hold, or pay,
  and to treat tool and web text as data, not instructions.
- **Relative deals.** Hotels without a set target alert on a drop of 30% below that site's own recent
  baseline (configurable in `config.json`).
- **The server decides the UI rule.** Whether a result shows "Book it now" or "Watch this" is computed
  server-side and covered by tests.
- **Secrets and personal data stay out of git.** `.env`, `data/`, and `*.db` are ignored.

## Run it

```bash
cp .env.example .env      # optional: Twilio keys for text alerts
./start.sh                # then open http://127.0.0.1:8765
python3 -m unittest discover -s tests -v    # 61 tests
```

The scout itself needs an AI agent with travel-search connectors (flights, hotels, cars) attached; those
connectors are not part of this repo. Without them the app still runs, and you can log prices by hand
with the CLI.

## Limits

- Single user, runs on your own machine. No hosted version, no mobile app.
- Price coverage depends on whichever search connectors the scout has available.
- No auto-booking, by design.

## Built with AI assistance

I defined the rules, the honesty constraints, the schedule limits, and the UI behavior, and I use it.
The code, tests, and scout instructions were written with Claude Code.
