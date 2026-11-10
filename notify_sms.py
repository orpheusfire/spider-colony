#!/usr/bin/env python3
"""Send an SMS via Twilio. Usage: notify_sms.py "message"

Reads TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM, SMS_TO from
spider-colony/.env (KEY=value lines). Exits 0 with a skip note if unconfigured.
"""
import base64
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

env_file = Path(__file__).resolve().parent / ".env"
env = {}
if env_file.exists():
    for line in env_file.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()

needed = ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM", "SMS_TO"]
if not all(env.get(k) for k in needed):
    print(json.dumps({"sms": "skipped", "reason": "Twilio not configured in .env"}))
    sys.exit(0)

sid = env["TWILIO_ACCOUNT_SID"]
req = urllib.request.Request(
    f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
    data=urllib.parse.urlencode({"From": env["TWILIO_FROM"], "To": env["SMS_TO"], "Body": sys.argv[1][:1500]}).encode(),
    headers={"Authorization": "Basic " + base64.b64encode(f"{sid}:{env['TWILIO_AUTH_TOKEN']}".encode()).decode()},
)
with urllib.request.urlopen(req, timeout=20) as r:
    print(json.dumps({"sms": "sent", "status": r.status}))
