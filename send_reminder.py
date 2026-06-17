#!/usr/bin/env python3
"""Daily garbage-pickup SMS reminder for a Perfect Waste address.

Fetches the Perfect Waste collection calendar (open, unauthenticated), checks
whether anything *we care about* is collected tomorrow, and if so sends one SMS
via GatewayAPI to each recipient. We care about pap, storskrald, farligt affald
and haveaffald; we ignore Mad/Rest, glas, papir, plast, metal, kartoner and the
4-kammer container. Filter fails OPEN: an unknown fraction name -> send anyway.

Message = a cozy opening line, then a skimmable date + bin block:

    <cozy line>

    I morgen, torsdag d. 18. juni, henter de:
    HAVEAFFALD

GSM-7 only: aeoeaa render fine, but emoji / em-dash / ellipsis / curly quotes
become "?" - use plain "-", "..." and straight quotes. See MAINTENANCE.md.

Config via env vars (no secrets or addresses in this file): GATEWAYAPI_TOKEN
(required), RECIPIENTS (required, comma-separated MSISDNs), ADDRESS_ID
(required, Perfect Waste address id), MUNICIPALITY (required, Perfect Waste
municipality id), SENDER (default "Skrald"), DRY_RUN ("1" prints instead of
sending). Exit 0 on success (incl. nothing-to-send); non-zero only on a real
network/API error.

This file is the source of truth: the cloud routine clones this repo each run
and executes it directly (see MAINTENANCE.md).
"""

import json
import os
import random
import sys
import urllib.request
import urllib.error
from base64 import b64encode
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

PW_URL = "https://europe-west3-perfect-waste.cloudfunctions.net/getAddressCollections"
GATEWAYAPI_URL = "https://gatewayapi.com/rest/mtsms"
TZ = ZoneInfo("Europe/Copenhagen")

DA_WEEKDAYS = [
    "mandag", "tirsdag", "onsdag", "torsdag",
    "fredag", "lørdag", "søndag",
]
DA_MONTHS = [
    "", "januar", "februar", "marts", "april", "maj", "juni",
    "juli", "august", "september", "oktober", "november", "december",
]

# Fractions we deliberately IGNORE (normalized: lowercased, stripped). Anything
# NOT in this set counts as relevant, so unknown/new names trigger a send.
IGNORED_FRACTIONS = {
    "mad/rest", "mad-/rest", "mad og rest", "madaffald", "mad", "rest",
    "restaffald", "glas", "papir", "plast", "metal", "glas/metal",
    "metal/glas", "glas og metal", "plast/metal", "mad-drikkekartoner",
    "mad- og drikkekartoner", "drikkekartoner", "papir/glas",
    "4-kammer", "4 kammer", "4-kammerbeholder", "firekammer",
}


def _env(name, default=None, required=False):
    val = os.environ.get(name, default)
    if required and not val:
        sys.exit(f"ERROR: missing required environment variable {name}")
    return val


def fetch_collections(address_id, municipality):
    """Return the list of upcoming collections from Perfect Waste."""
    body = json.dumps(
        {"data": {"addressID": str(address_id), "municipality": int(municipality)}}
    ).encode()
    req = urllib.request.Request(
        PW_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.load(resp)
    return payload.get("result", []) or []


def parse_pickup_date(raw):
    """Parse '2026-06-16T00:00:00.000Z' into a Copenhagen-local date.

    The API returns midnight-UTC timestamps that stand for a calendar date, so
    we take the date part directly rather than shifting timezones."""
    return date.fromisoformat(raw[:10])


def collections_for(target, collections):
    """All entries whose pickup date equals `target` (a date)."""
    return [c for c in collections if parse_pickup_date(c["date"]) == target]


def relevant_fractions(entries):
    """Fraction names for `entries` that we actually care about (fail open)."""
    names = []
    for entry in entries:
        for fr in entry.get("fractions", []):
            name = (fr.get("fractionName") or "").strip()
            if not name:
                continue
            if name.lower() in IGNORED_FRACTIONS:
                continue
            if name not in names:
                names.append(name)
    return names


def format_date(target):
    weekday = DA_WEEKDAYS[target.weekday()]
    return f"{weekday} d. {target.day}. {DA_MONTHS[target.month]}"


# A single GSM-7 SMS holds 160 characters. GatewayAPI does NOT split a longer
# message into parts - it silently TRUNCATES it, which would cut the bin type
# off the end. The bin type is the one thing that must never be lost, so we only
# ever pick an opening line that keeps the whole message within one segment.
#
# GatewayAPI used to prepend "THIS IS A TEST SMS: " (20 chars) until the account
# had credit / the sender was sorted out. That prefix is now GONE, so we no longer
# hold any room back: GATEWAY_PREFIX_RESERVE is 0 and the full 160 chars are usable.
# (If the prefix ever returns, set this back to 20 - see MAINTENANCE.md.)
SMS_SEGMENT_LIMIT = 160
GATEWAY_PREFIX_RESERVE = 0


def topic_for(fraction_name):
    """Map a raw Perfect Waste fraction name to one of our opening-line topics,
    or None if we have no bin-specific bucket for it. Keyword match on the
    lowercased name so small naming variations still land."""
    name = (fraction_name or "").lower()
    if "have" in name:
        return "haveaffald"
    if "storskrald" in name or "stor" in name:
        return "storskrald"
    if "farlig" in name:
        return "farligt"
    if "pap" in name:  # "papir" is ignored upstream, so it never reaches here
        return "pap"
    return None


def build_message(target, fractions, intro=None):
    """A cozy line, then a plain, skimmable date + bin-type block.

    The opening line must be RELEVANT to what is collected: when `intro` is None
    we pick at random from the "general" (bin-agnostic) lines plus any lines
    tagged for the bin(s) actually being collected - never, say, a pap fact on a
    haveaffald day. We also only consider lines short enough to keep the whole
    message in one SMS segment (so the bin type is never truncated).

    GSM-7 safe: no emoji, no em-dash. `intro` lets callers (e.g. the delivery
    test) pin a specific line, in which case the caller owns the length."""
    fraction_text = ", ".join(fractions).upper() if fractions else "AFFALD"
    tail = f"\n\nI morgen, {format_date(target)}, henter de:\n{fraction_text}"
    if intro is None:
        wanted = {"general"}
        for fr in fractions:
            topic = topic_for(fr)
            if topic:
                wanted.add(topic)
        pool = [text for topic, text in GARBAGE_LINES if topic in wanted]
        budget = SMS_SEGMENT_LIMIT - GATEWAY_PREFIX_RESERVE - len(tail)
        eligible = [text for text in pool if len(text) <= budget]
        intro = random.choice(eligible or [min(pool, key=len)])
    return f"{intro}{tail}"


def send_sms(token, sender, recipients, message):
    body = json.dumps(
        {
            "sender": sender,
            "message": message,
            "recipients": [{"msisdn": int(m)} for m in recipients],
        }
    ).encode()
    auth = b64encode(f"{token}:".encode()).decode()
    req = urllib.request.Request(
        GATEWAYAPI_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Basic {auth}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def main():
    token = _env("GATEWAYAPI_TOKEN", required=True)
    recipients_raw = _env("RECIPIENTS", required=True)
    recipients = [r.strip() for r in recipients_raw.split(",") if r.strip()]
    sender = _env("SENDER", "Skrald")
    address_id = _env("ADDRESS_ID", required=True)
    municipality = _env("MUNICIPALITY", required=True)
    dry_run = str(_env("DRY_RUN", "")).lower() in ("1", "true", "yes")

    today = datetime.now(TZ).date()
    tomorrow = today + timedelta(days=1)

    collections = fetch_collections(address_id, municipality)
    entries = collections_for(tomorrow, collections)
    fractions = relevant_fractions(entries)

    if not fractions:
        all_names = [
            fr.get("fractionName")
            for e in entries for fr in e.get("fractions", [])
        ]
        print(
            f"Nothing relevant tomorrow ({tomorrow}). "
            f"All fractions: {all_names or 'none'}. Sending nothing."
        )
        return

    message = build_message(tomorrow, fractions)
    print(f"Relevant pickup tomorrow ({tomorrow}). Message:\n{message}")

    if dry_run:
        print(f"DRY_RUN: would send to {recipients}")
        return

    result = send_sms(token, sender, recipients, message)
    print(f"Sent. GatewayAPI response: {result}")


# Cozy, family-friendly opening lines as (topic, text) pairs - verified Danish
# affald facts and named TV-character references, NOT jokes. build_message picks one at random
# from the "general" lines plus the lines tagged for the bin(s) collected.
# Topics: "general" (any bin) or a bin: "haveaffald", "pap", "storskrald",
# "farligt". Full curation rules live in MAINTENANCE.md; a larger pool of vetted
# lines to draw from lives in opening_line_archive.py. Quality over quantity -
# a weak line is worse than a missing one. GSM-7 safe only (no emoji/em-dash).
GARBAGE_LINES = [
    # --- general (fits any bin) ---
    ("general", "Lille fakta: hver dansker smider omkring 750 kg affald ud om året."),
    ("general", "Dagrenovationen i Danmark er faldet 37% siden 2011, fordi vi sorterer mere."),
    # --- haveaffald ---
    ("haveaffald", "Vidste du det? Vi sorterer i snit 118 kg haveaffald pr. person om året."),
    ("haveaffald", "Det tager typisk 1-2 år for en bunke haveaffald at blive til muld."),
    ("haveaffald", "Sam Gamgee var gartner - han ville elske vores haveaffald."),
    ("haveaffald", "Groot er selv et træ - han ville føle sig hjemme i haveaffaldet."),
    # --- pap (fun facts only - no character ties land for cardboard) ---
    ("pap", "Lille detalje: pap og papir fylder omkring 34 kg pr. dansker om året."),
    # --- storskrald ---
    ("storskrald", "Carrie elskede en oprydning i skabet - storskrald er samme følelse."),
    ("storskrald", "Ross' berygtede sofa fra Friends ville være endt som storskrald."),
    # --- farligt affald ---
    ("farligt", "Snapes eliksirer skulle behandles varsomt - lige som vores kemi."),
]

# Flat list of just the line texts, for length / GSM-7 checks.
ALL_LINES = [text for _topic, text in GARBAGE_LINES]


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP error: {e.code} {e.read().decode(errors='replace')}")
    except urllib.error.URLError as e:
        sys.exit(f"Network error: {e}")
