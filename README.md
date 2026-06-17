# garbage-reminder

A tiny daily reminder that texts us the evening before a garbage pickup.

It reads the collection calendar for our address from Perfect Waste's open
Cloud Function, checks whether anything is collected **tomorrow**, and if so
sends an SMS via [GatewayAPI](https://gatewayapi.com) to each recipient. When
several fractions are collected the same day, they are all listed together in one
SMS (e.g. `HAVEAFFALD, STORSKRALD`).

Runs as a scheduled [Claude Code cloud routine](https://claude.ai/code/routines)
once a day — no laptop or server required.

> **Maintaining or redeploying this?** See **[MAINTENANCE.md](MAINTENANCE.md)** —
> it has the locked-in decisions (tone, GSM-7 encoding, message format), the
> opening-line curation rules, and the exact regenerate-blob + redeploy steps.

## How it works

```
Perfect Waste Cloud Function  ──▶  send_reminder.py  ──▶  GatewayAPI  ──▶  SMS
   (open, no auth)                  (checks "tomorrow")     (SMS gateway)
```

The Perfect Waste endpoint is unauthenticated:

```bash
curl -s -X POST \
  https://europe-west3-perfect-waste.cloudfunctions.net/getAddressCollections \
  -H 'Content-Type: application/json' \
  -d '{"data":{"addressID":"<your-address-id>","municipality":<your-municipality>}}'
```

- `municipality` → your kommune's Perfect Waste id
- `addressID` → your address's id

(Find both by inspecting the network calls the Perfect Waste app/site makes for
your address.) It returns ~3 months of upcoming pickups, each with a date and
the waste `fractions` collected that day.

## Configuration

All config is via environment variables — **no secrets are committed**:

| Variable           | Required | Default   | Notes                                            |
| ------------------ | -------- | --------- | ------------------------------------------------ |
| `GATEWAYAPI_TOKEN` | yes      | —         | GatewayAPI REST token (basic-auth username)      |
| `RECIPIENTS`       | yes      | —         | Comma-separated MSISDNs, e.g. `4512345678,45...` |
| `ADDRESS_ID`       | yes      | —         | Perfect Waste address id                         |
| `MUNICIPALITY`     | yes      | —         | Perfect Waste municipality id                    |
| `SENDER`           | no       | `Skrald`  | SMS sender name, max 11 chars                    |
| `DRY_RUN`          | no       | —         | `1` to print the SMS instead of sending          |

## Run locally

The required vars are kept in a **gitignored `.env`** (never committed). Load it
and run:

```bash
set -a; source .env; set +a
DRY_RUN=1 python3 send_reminder.py        # dry run: prints the SMS, sends nothing
```

Or pass them inline instead of using `.env`:

```bash
GATEWAYAPI_TOKEN=x RECIPIENTS=4512345678 \
ADDRESS_ID=12345 MUNICIPALITY=101 DRY_RUN=1 python3 send_reminder.py
```

Real send:

```bash
GATEWAYAPI_TOKEN=your_token \
RECIPIENTS=4512345678,4587654321 \
ADDRESS_ID=12345 MUNICIPALITY=101 \
python3 send_reminder.py
```

## Schedule

A Claude Code cloud routine runs `python3 send_reminder.py` every day at
**10:00 Europe/Copenhagen** (08:00 UTC). The token, recipient numbers, and
address ids are all provided by the routine's config at runtime, so they never
touch this repo.
