#!/usr/bin/env python3
"""Daily garbage-pickup SMS reminder for a Perfect Waste address.

Fetches the Perfect Waste collection calendar (open, unauthenticated), checks
whether anything *we care about* is collected tomorrow, and if so sends one SMS
via GatewayAPI to each recipient. We care about pap, storskrald and haveaffald;
we ignore farligt affald (no container at this address - never collected here),
plus Mad/Rest, glas, papir, plast, metal, kartoner and the 4-kammer container.
Filter fails OPEN for unknown names (send anyway), but farligt is always dropped.

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
# NOT in this set counts as relevant, so unknown/new names trigger a send. Farligt
# affald is dropped separately (by keyword) in relevant_fractions, not listed here,
# since its exact Perfect Waste name is unknown - see that function.
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
    """Fraction names for `entries` that we actually care about.

    Fails OPEN (an unknown name is kept, so we send rather than miss a pickup),
    with one hard exception: farligt affald is always dropped - there is no
    farligt container at this address and it is never collected here, so it must
    never trigger a send nor appear in the bin block."""
    names = []
    for entry in entries:
        for fr in entry.get("fractions", []):
            name = (fr.get("fractionName") or "").strip()
            if not name:
                continue
            if name.lower() in IGNORED_FRACTIONS:
                continue
            if topic_for(name) == "farligt":
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
    we pick ONE of the collected fractions at random and draw the line from the
    "general" (bin-agnostic) lines plus that bin's lines - never, say, a pap fact
    on a haveaffald-only day. On a day with several fractions we simply spotlight
    one of them at random; the block below still lists them all. We only consider
    lines short enough to keep the whole message in one SMS segment, and if none
    fit we OMIT the opening line rather than let GatewayAPI truncate the bin block
    off the end (the bin type must never be lost).

    GSM-7 safe: no emoji, no em-dash. `intro` lets callers (e.g. the delivery
    test) pin a specific line, in which case the caller owns the length."""
    fraction_text = ", ".join(fractions).upper() if fractions else "AFFALD"
    tail = f"\n\nI morgen, {format_date(target)}, henter de:\n{fraction_text}"
    if intro is None:
        wanted = {"general"}
        chosen = random.choice(fractions) if fractions else None
        chosen_topic = topic_for(chosen) if chosen else None
        if chosen_topic:
            wanted.add(chosen_topic)
        pool = [text for topic, text in GARBAGE_LINES if topic in wanted]
        budget = SMS_SEGMENT_LIMIT - GATEWAY_PREFIX_RESERVE - len(tail)
        eligible = [text for text in pool if len(text) <= budget]
        if not eligible:
            return tail.lstrip("\n")
        intro = random.choice(eligible)
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


# Family-friendly opening lines as (topic, text) pairs. Each line is EITHER a
# verified Danish affald FUN FACT or a NAMED TV/film-character reference with a
# real tie to the bin - never a joke or nag. build_message picks ONE of the
# collected fractions at random and draws the line from the "general"
# (bin-agnostic) lines plus that bin's lines, so the line always fits what is
# collected; on a multi-fraction day it just spotlights one bin at random.
# Topics: "general" (any bin) or a bin: "haveaffald", "pap", "storskrald".
# (Farligt affald is never collected at this address, so there is no farligt
# topic.) This list is the SINGLE SOURCE OF TRUTH for the lines (the old
# opening_line_archive.py reference pool was removed). Full curation rules live
# in MAINTENANCE.md. GSM-7 safe only (no emoji/em-dash); facts must be verified.
GARBAGE_LINES = [
    # --- general (fits any bin): fun facts ---
    ("general", "Lille fakta: hver dansker smider omkring 750 kg affald ud om året."),
    ("general", "Dagrenovationen i Danmark er faldet 37% siden 2011, fordi vi sorterer mere."),
    ("general", "Vidste du det? Glas kan smeltes om og genbruges igen og igen uden at miste kvalitet."),
    ("general", "Lille fakta: en aluminiumsdåse, der genbruges, sparer omkring 95% af energien til en ny."),
    ("general", "Lille fakta: omkring 90% af de pantede flasker og dåser kommer retur i Danmark."),
    ("general", "Lille fakta: at sortere madaffald fra bliver til både biogas og gødning til markerne."),
    # --- haveaffald: fun facts + named characters ---
    ("haveaffald", "Vidste du det? Vi sorterer i snit 118 kg haveaffald pr. person om året."),
    ("haveaffald", "Det tager typisk et til to år for en bunke haveaffald at blive til muld."),
    ("haveaffald", "Sam Gamgee var gartner og ville elske vores haveaffald."),
    ("haveaffald", "Groot er selv et træ og ville føle sig hjemme i haveaffaldet."),
    ("haveaffald", "Carmy ved, at de bedste krydderurter starter i havens jord."),
    ("haveaffald", "Neville Longbottom var bedst til urtologi og ville elske havearbejdet."),
    ("haveaffald", "Pomona Sprout passede drivhusene og ville nikke til en god bunke haveaffald."),
    ("haveaffald", "Vidste du det? Græs er bedst i et tyndt lag i kompostbunken, så det ikke mugner."),
    ("haveaffald", "Lille fakta: en god kompost har brug for både grønt, brunt og lidt tålmodighed."),
    ("haveaffald", "Vidste du det? Visne blade bliver til den fineste bladkompost på et år eller to."),
    ("haveaffald", "Vidste du det? En kompostbunke kan blive over 60 grader varm, mens den arbejder."),
    ("haveaffald", "Vidste du det? Græsafklip er omkring 80% vand og kan komposteres på stedet."),
    # --- pap: fun facts only (no character tie lands for cardboard) ---
    ("pap", "Lille detalje: pap og papir fylder omkring 34 kg pr. dansker om året."),
    ("pap", "Papfibre kan genbruges mange gange, før de bliver for korte."),
    ("pap", "Æggebakker af pap er allerede genbrugt mindst en gang."),
    ("pap", "Lille fakta: pap skal være tørt og rent, ellers kan fibrene ikke genbruges."),
    ("pap", "Lille fakta: når fibrene endelig bliver for korte, kan pappet brændes til energi."),
    ("pap", "Lille fakta: at genbruge pap sparer både træer og en hel del vand og energi."),
    ("pap", "Vidste du det? Toiletruller af pap er som regel allerede genbrugt mindst en gang."),
    ("pap", "Lille fakta: flad pap fylder mindre, så der er plads til meget mere i samme spand."),
    ("pap", "Vidste du det? Pap kan typisk genbruges fem til syv gange, før fibrene bliver for korte."),
    # --- storskrald: named characters + fun fact ---
    ("storskrald", "Carrie elskede en oprydning i skabet, præcis som en storskraldsdag."),
    ("storskrald", "Ross' berygtede sofa fra Friends ville være endt som storskrald."),
    ("storskrald", "Tony Stark skrottede gamle dragter for at gøre plads, ligesom en storskraldsdag."),
    ("storskrald", "Ted Mosby gemte på alt, så en storskraldsdag ville gøre ham godt."),
    ("storskrald", "Richie rev hele restaurantkøkkenet ud, en oprydning helt i storskraldens ånd."),
    ("storskrald", "Marshall Eriksson elskede sin gamle stol, og sådan en ender til sidst som storskrald."),
    ("storskrald", "Monica Geller ville nyde en storskraldsdag og endelig få det gamle rod ud."),
    ("storskrald", "Lille fakta: gamle møbler i storskrald bliver tit til nye materialer af træ og metal."),
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
