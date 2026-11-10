# Spider Colony: rules for Claude (apply every session in this folder)

Personal deal hunter for hotels, flights and rental cars. Created by the owner for family and friends.

## Scout schedule (owner's decision, permanent)
- The scheduled scout  runs **every 30 minutes** (`*/30 * * * *`), for **one week at a time**, then it stops and the app asks the owner whether he wants another week (`colony.py scout-status` / `scout-extend`). NOT every 15, 5 or 1 minute: it exists to save his usage and must never run forever.
- Never change that schedule, or turn the scout on, off or faster, unless the owner says so in that conversation. If asked to "pause" or "kill" it, set enabled=false; do not delete it.
- Car watches are checked about hourly (the car service allows 100 searches a day). Only restart a new week when the owner says so (the app's Continue button or his words).

## Always
- **Test as you build.** Any change to `colony.py`, `server.py` or `web/index.html` needs a test added or updated in `tests/`. Run `python3 -m unittest discover -s tests -v` before saying a change is done, and report failures plainly.
- **Prices must be all-in** (taxes and fees included, `--basis total`) to count toward a deal. Never alert on pre-tax or unknown prices.
- **Never book, hold or pay for anything.** Never enter payment, login or personal details. Booking tools of any connector are off limits.
- **Never commit secrets or personal data.** `.env`, `data/` and `*.db` hold Twilio keys, the user's phone and email, and price history. They stay in `.gitignore`. Check `git status` before any commit or push.
- **Be honest about limits.** Say when a source can't confirm something (room type, mileage, taxes) instead of implying it.
- Frontier, Spirit and Allegiant (ultra-low-cost, bag-fee airlines) ARE searched (the owner changed this Oct 23 so other people can use the app) but live in their own "Cheap airlines" section, never mixed into the normal best price. Their fares often leave out bags: log them `--basis total` only when a carry-on is confirmed included. Settings has a "Hide cheap airlines" switch (`hide_budget`). United Basic Economy is hidden unless the profile toggle is on (then the bag fee is added).
- Flexible dates is a checkbox on the hotel and flight forms (`flex: true`, about 3 days either side). Not on cars (quota).
- The car connector allows 100 searches/day: check car watches about hourly.

## Honesty rules learned the hard way
- **Never show corporate-negotiated rates** (`is_corporate_rate: true` from the car service) as if the user can get them. They are not public prices.
- Before telling the user a price "beats" their target, make sure it is a price a regular customer can actually book. When a company's own site is available, spot-check it.
- Only claim a "Book it now" link pre-fills the trip if it was actually opened and checked. Verified: Avis (airport-code pickups) and Hertz (ATL only, location ID ATLT11); Kiwi flight links open the exact itinerary. NOT verified: Budget (the Avis-style link gave an empty page), Thrifty and the rest: they go to the home page. Add a Hertz airport ID only after checking it on hertz.com.
- Kiwi flight prices are all-in (Kiwi's checkout confirms taxes and fees are included) but some itineraries use a self-transfer (separate tickets); say so when relevant.

## UI rule (owner's decision)
- On search results: if a result's all-in price is **at or under the user's target** (per-day targets are multiplied by the trip's days), show **Book it now** and NO "Watch this" button. If it is **over** the target (or no target was set), show **★ Watch this**. The server decides this (`within_target`), and it is covered by tests.
- Book it now only opens the booking page; the user pays on the vendor's site. When a source gives no exact link, link to the vendor's home page and say so.

## Layout
`colony.py` core and CLI, `server.py` + `web/index.html` the app, `SCOUT.md` the scheduled scout's instructions, `tests/` the test suite.
