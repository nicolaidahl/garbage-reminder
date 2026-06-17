# Maintenance guide

How to **safely change and redeploy** the garbage-pickup SMS reminder. This is
the source of truth for the project's context and the locked-in decisions. Read
this before touching anything.

For the high-level "what it is", see [README.md](README.md). This document is
specifically about **operating the thing that runs in the cloud**.

---

## What is actually running

There is **no server**. The reminder is a scheduled
[Claude Code cloud routine](https://claude.ai/code/routines) that runs on
Anthropic's cloud once a day. Nothing runs on a laptop.

```
cron (daily)  ─▶  cloud agent  ─▶  git clone this repo  ─▶  runs send_reminder.py
                                                                   │
                Perfect Waste API ◀──────────────────────────────┤
                (open, no auth)    "anything relevant tomorrow?"   │
                                                                   ▼
                                              if yes: 1 SMS via GatewayAPI
```

### Key facts

| Thing                | Value                                                            |
| -------------------- | ---------------------------------------------------------------- |
| Routine id           | `trig_01Pivn7Fj498TTH4LssdC8ux`                                  |
| Routine URL          | https://claude.ai/code/routines/trig_01Pivn7Fj498TTH4LssdC8ux    |
| Environment id       | `env_019NvuDYwsvT7LmjjJRac4Xw` (Default, anthropic_cloud)        |
| Event uuid           | `f30d4c44-c795-41f6-8534-364e6c3b720e` (changes when the prompt is re-saved) |
| Cron                 | `0 8 * * *` UTC = **10:00 Europe/Copenhagen** (09:00 in winter)  |
| Recipients           | in the routine config only (`RECIPIENTS` env) - never in this repo |
| Sender               | `Skrald`                                                         |
| GatewayAPI token     | **lives only in the routine config + GatewayAPI dashboard** — never in this repo |
| Perfect Waste address| in the routine config only (`ADDRESS_ID` / `MUNICIPALITY` env) - never in this repo |

### How the script reaches the cloud

This repo is **public**, so each run the cloud agent clones it and runs the
script straight from source - no embedded copy, no blob, nothing for an LLM to
reproduce. The routine prompt runs, verbatim:

```
rm -rf /tmp/garbage-reminder
git clone --depth 1 https://github.com/nicolaidahl/garbage-reminder.git /tmp/garbage-reminder
GATEWAYAPI_TOKEN=... RECIPIENTS=... ADDRESS_ID=... MUNICIPALITY=... SENDER='Skrald' python3 /tmp/garbage-reminder/send_reminder.py
```

github.com is on the sandbox's default network allowlist and a public repo needs
no auth, so the clone just works; the repo is fetched **fresh every run**, so a
push to `main` is live on the next daily run. The token, recipient numbers, and
address ids are all supplied by the routine config at runtime (see the deploy
section) - this repo holds none of them.

**To change anything: edit here, commit, push.** That is the entire deploy -
there is no separate cloud copy to keep in sync.

> **History note (why it isn't a blob anymore).** Earlier versions shipped
> `send_reminder.py` to the cloud as a gzip+base64 blob inside the routine prompt,
> because the repo was private and couldn't be cloned. That was abandoned in June
> 2026: an LLM had to reproduce ~7 KB of opaque base64 verbatim into the deploy
> call (and the cloud agent again at runtime), and it silently flipped bytes
> (`datetime` → `datetiCE`), breaking `gunzip` and the whole routine for days.
> Worse, when `gunzip` failed the cloud agent would *improvise* a workaround and
> run a corrupted file. Cloning a public repo removes every point where an LLM
> regenerates the script. If the repo ever goes private again, the robust path is
> the **Claude GitHub App** (a bare OAuth connection only exposes *public* repos),
> never the blob.

---

## Locked decisions (do NOT relitigate)

These were settled with the user. Changing them means re-opening a closed
conversation — don't, unless explicitly asked.

1. **Timing is correct as-is.** The SMS arrives the **day before** pickup at
   **10:00**, naming *tomorrow's* pickup. No change.

2. **Tone: cozy, warm, family-friendly. NO jokes.** An earlier comedy direction
   (Danish sketch-comedy, film/meme references) was explicitly killed. It must
   not come back.

3. **Content of the opening line** is one of:
   - an **interesting, verified statistic** about Danish affald, or
   - a **cozy, concrete observation**.

   (An earlier "legendary quotes / proverbs" category was cut — see the curation
   rules below. Lean toward **facts over quotes**.)

4. **Two-part, skimmable format** (implemented in `build_message`):

   ```
   <cozy opening line>

   I morgen, <ugedag d. D. maaned>, henter de:
   <BIN TYPE IN CAPS>
   ```

   The bin type MUST always be explicit and easy to skim. This was the user's
   single biggest complaint about earlier versions ("I don't get which type of
   garbage").

5. **Encoding: GSM-7 only — critical.** GatewayAPI sends in the GSM-7 alphabet
   and **silently replaces anything outside it with `?`**. Confirmed empirically
   (the user got `?`-garbage from an emoji version).
   - **Allowed:** the real Danish letters **æ ø å** render fine and must be used.
   - **NOT allowed** (each becomes `?`): emoji, em-dash (`—`), middle dot,
     ellipsis glyph (`…`), curly quotes. Use a plain `-`, three periods `...`,
     and straight quotes.

6. **Fraction filter, fail-open.** Send only when *tomorrow* includes **pap,
   storskrald, farligt affald, or haveaffald**. Ignore Mad/Rest, glas, papir,
   plast, metal, mad-/drikkekartoner, and the 4-kammer combo. Any **unknown**
   fraction name → send anyway ("if in doubt, send"). See `IGNORED_FRACTIONS`.

---

## The opening lines (`GARBAGE_LINES`)

The opening lines live in `send_reminder.py` as `GARBAGE_LINES`, a list of
`(topic, text)` pairs. (It was previously, misleadingly, called `FUNNY` — the
lines are cozy/factual, not jokes — and was later a flat list before topics were
added.) `ALL_LINES` is a flat list of just the texts, for checks.

**Topic relevance.** Each line is tagged either `"general"` (fits any bin) or one
of the four bins: `"haveaffald"`, `"pap"`, `"storskrald"`, `"farligt"`.
`build_message` picks at random from the **general lines plus the lines tagged
for the bin(s) actually being collected** — so the opening line is always
relevant (no pap fact on a haveaffald day). `topic_for(fraction_name)` maps a raw
Perfect Waste fraction name to a topic by keyword. Bins with few or no specific
lines simply fall back to the general pool.

### Curation rules (agreed with the user)

When adding, editing, or pruning lines, apply these — they were derived directly
from the user's feedback in the review session:

1. **Every line must be EITHER a verified fun fact OR a named-character
   reference — nothing else.** This is the hard gate the user set (June 2026):
   the pool was consolidated down to exactly these two kinds of line. Drop
   generic cozy observations, sorting tips/advice, aphorisms, proverbs, and
   "as they say…" quote lines entirely. A line that is neither a fun fact nor a
   named-character reference does not belong, however pleasant it reads.
2. **No tacked-on cutesy kicker.** A clean fact or observation, then stop. Do
   NOT append an editorialising second sentence after a period
   (killed examples: `". Haven knokler."`, `". Som en ren tavle."`,
   `". Fint at taenke paa."`). The user finds these irritating.
3. **Cut anything whose comparison does not land** or needs explaining
   (killed examples: "vejer som en malkeko" — nobody knows what a cow weighs;
   "storskrald er som et loppemarked" — storskrald just gets hauled away).
4. **Warm pop-culture references are welcome — but only with a STRONG, concrete
   thread to the bin, and the character must be NAMED.** Two hard rules the user
   set in the June 2026 review:
   - **Real thread, not a clever-but-loose link.** A whole batch was rejected as
     "horrible — no real thread between the characters and the garbage type"
     (e.g. Money Heist → pap, Suits paperwork → pap, Mandalorian "travels light"
     → storskrald, Frodo leaving the Shire → storskrald). If the connection
     needs a paragraph to justify, cut it. **Cardboard (pap) has no good
     character tie — pap lines are ALL fun facts, zero references.**
   - **A reference must name a CHARACTER — not a prop, place, or catchphrase.**
     Object/phrase references that name no person were cut in the June 2026
     consolidation (Jerntronen/Iron Throne is a chair; "Winter is coming",
     "with great power…" and Wildfire are catchphrases/things, not characters).
     If you can't point to the named person, it's not a character reference.
   - **Name the character; never "X i [Show]".** Write "Sam Gamgee var
     gartner…", not "Carmy i The Bear…" or "Neville i Harry Potter…". Lead with
     the name and let the reference itself make it clear who they are.

   The named-character references currently in the pool (each ties tightly to
   its bin): Carrie's closet cleanout → storskrald; Ross' sofa → storskrald;
   Tony Stark scrapping old suits → storskrald; Ted Mosby the keeper-of-
   everything → storskrald; Sam Gamgee the gardener → haveaffald; Groot is a
   tree → haveaffald; Carmy's fresh herbs → haveaffald; Neville Longbottom the
   Herbology master → haveaffald; Snape's potions → chemicals/farligt; Tony
   Stark's reactor → batteries/farligt. The watched-together show list is fixed
   (HIMYM, The Bear, Sex and the City, Friends, Game of Thrones, all Marvel
   films, Lord of the Rings, Harry Potter, Suits, The Pitt, House of the Dragon,
   The Mandalorian, Beef, Money Heist). Proverbs/aphorisms/catchphrases are out —
   a warm reference is a NAMED character, not a quote line. Because a line is
   tagged to a bin it only ever shows on that bin, so a bin-specific reference
   can never land on a mismatch.
5. **The opening line must NEVER nag about putting the bins out.** No raised
   finger / "husk at stille skraldet ud". The reminder ("husk") info lives
   **only** in the part-2 `I morgen … henter de: <TYPE>` block.
6. **Verified facts only.** Statistics must be accurate. Sources used so far:
   Danmarks Statistik / Miljoestyrelsen 2022–2023 figures; STIHL / Bolius /
   Havenyt for composting facts.
7. **GSM-7 safe** (see locked decision 5) **and short enough for one SMS**
   (see the next section). Run the GSM-7 check below after any edit.

**Line count (currently 18 lines: general 2, haveaffald 6, pap 3, storskrald 4,
farligt 3).** This used to be a hard operational constraint - the script shipped
as a base64 blob an LLM had to reproduce verbatim, so a bigger script meant a
likelier corrupt deploy. **That constraint is GONE** now that the cloud clones
the repo instead: the script can be any size. Keeping it focused is now purely an
editorial choice - quality beats count, and a weak line is worse than a missing
one.

**`GARBAGE_LINES` in `send_reminder.py` is now the single source of truth for the
lines.** A ~236-line reference pool (`opening_line_archive.py`) once held extra
candidates, but in the June 2026 consolidation everything that was not a fun fact
or a named-character reference was dropped and the archive file was deleted - so
there is no separate pool to promote from anymore. To add lines, write new ones
straight into `GARBAGE_LINES` and apply curation rule 1 (fun fact OR named
character, nothing else) plus all the rules above.

### SMS length / one-segment guarantee

A single GSM-7 SMS holds **160 characters**, and GatewayAPI **truncates** longer
messages rather than splitting them — which would cut the **bin type** (the most
important part) off the end. To make that impossible, `build_message` only picks
an opening line that keeps the whole message within one segment:

- `SMS_SEGMENT_LIMIT = 160` — one GSM-7 segment.
- `GATEWAY_PREFIX_RESERVE = 0` — the `THIS IS A TEST SMS: ` prefix GatewayAPI used
  to inject is **gone** (account sorted out in June 2026), so no room is held back
  and the full 160 chars are usable. If the prefix ever returns, set this to 20.

With the reserve at 0, the worst-case usable budget for an opening line is
`160 - len(tail)`, where the longest tail (e.g. `torsdag d. 24. september` +
`FARLIGT AFFALD`) is ~63 chars — so ~97 chars per line. All current lines fit.

If you add a longer line, it just won't be picked on the longest days — keep new
lines under ~97 characters if you want them in heavy rotation on every date.

---

## How to change a line and deploy

**The deploy IS git.** Edit, verify locally, commit, push - the next daily run
(or a manual run) clones the new `main` and uses it. No blob, no routine edit,
no paste, no sha256 dance.

### 1. Edit the script

Edit `GARBAGE_LINES` (or any logic) in `send_reminder.py` — it is the single
source of truth for the lines. Any new line must be a fun fact or a named-
character reference (curation rule 1); there is no separate archive to draw from.

### 2. Preview the rendered message

```bash
GATEWAYAPI_TOKEN=x RECIPIENTS=4512345678 ADDRESS_ID=12345 MUNICIPALITY=101 DRY_RUN=1 python3 send_reminder.py
```

`ADDRESS_ID`/`MUNICIPALITY` are required and live only in the routine config
(never in this repo), so pass your real values locally - the `12345`/`101` above
are placeholders. On a no-pickup day it prints `Nothing relevant tomorrow …` and
sends nothing - that is success, not an error. To preview a specific line, import
`build_message(date, [fraction], intro="your line")`.

### 3. Check GSM-7 safety

```bash
python3 - <<'PY'
import send_reminder
GSM7 = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ ÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
    "^{}\\[~]|€"  # GSM-7 extension chars
)
bad = [(l, sorted(set(c for c in l if c not in GSM7))) for l in send_reminder.ALL_LINES]
bad = [b for b in bad if b[1]]
print("OK — all GSM-7 safe" if not bad else f"NOT GSM-7 safe:\n" + "\n".join(map(str, bad)))
PY
```

### 4. Commit and push - that's the deploy

```bash
git add -A
git commit -m "..."
git push
```

The cloud clones `main` fresh on every run, so the next scheduled 10:00 fire uses
your change. Nothing else to do.

### 5. (Optional) verify in the cloud without sending an SMS

The API does **not** return cloud stdout - you read the result on the routine
page. To check a change end-to-end without sending, temporarily insert `DRY_RUN=1`
into the run command in the routine prompt (`RemoteTrigger action:update`), do one
`action:run`, then read the routine page (it prints the rendered message +
`DRY_RUN: would send …`). Remove `DRY_RUN=1` afterwards. **Never** fire a non-DRY
manual run on a pickup day - it sends a real SMS to both recipients.

### Routine config (where the secrets/PII live)

The repo carries none of these; they live only in the routine prompt's run
command (`RemoteTrigger action:get` to see it): `GATEWAYAPI_TOKEN` (or
`GARBAGE_SMS_TOKEN`), `RECIPIENTS`, `ADDRESS_ID`, `MUNICIPALITY`, `SENDER`. Change
a recipient or the address by editing that command, not the repo.

---

## Known issues

- **The routine fails silently (no SMS, but the run shows "Completed").** A run
  that errors still shows green in the routine list - the only way to know it
  actually sent is to read the run's output on the routine page (or just notice
  the SMS arrived). After any change, use the optional cloud DRY_RUN check
  (deploy step 5). Historically this bit us hard: a corrupt deploy made the
  routine fail every morning while the list looked "Completed."

- **`git clone` fails in the cloud run.** Means either the repo is private again
  (make it public, or wire the Claude GitHub App) or the environment's network
  access was set to "None" (github.com must be reachable - the Default "Trusted"
  allowlist already includes it). Check the run output on the routine page.

- **(Historical) gunzip / `crc error`.** The old blob-in-prompt deploy failed
  this way when an LLM flipped a base64 byte; it broke the routine for days in
  June 2026. Gone with the move to repo-clone - see the history note up top.

- **(Resolved June 2026) GatewayAPI `THIS IS A TEST SMS:` prefix.** GatewayAPI
  used to prepend `THIS IS A TEST SMS: ` (20 chars) to every message until the
  account had credit / the sender was sorted out. The user added credit and the
  prefix is **gone**, so `GATEWAY_PREFIX_RESERVE` was set to `0` (full 160 chars
  usable). If `?`-garbage or the prefix ever reappears, set it back to `20`.

- **DST.** The cron is a fixed `08:00 UTC` = 10:00 Copenhagen in summer but
  **09:00 in winter**. Accepted. A year-round 10:00 local would need a seasonal
  cron change.

---

## Security notes

- The **GatewayAPI token** lives in the routine config and the GatewayAPI
  dashboard — **never commit it to this repo.** When referencing it in docs or
  scripts, use a placeholder.
- Blast radius is capped by keeping the GatewayAPI balance low (manual top-up,
  ~100 SMS) and **auto-recharge OFF**.
- The real risk if the token leaks is **abuse / sender-reputation** (someone
  sending spam/phishing as "Skrald"), not the small balance. If it is ever
  exposed, **rotate it** in the GatewayAPI dashboard and update the routine
  config's run command with the new value (see "Routine config" above).
