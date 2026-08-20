# agent-immobilier — rental investment scout

Watches French property sites for buy-to-let opportunities and tells you which
ones are actually worth your time.

You set up saved-search email alerts on leboncoin, SeLoger, PAP, Bien'ici and
LogicImmo, pointed at a dedicated Gmail address. Every couple of hours this
pipeline reads the new alerts, extracts the listings, throws away the ones it
has already seen, checks each asking price against **what comparable properties
in that postal code actually sold for**, has Gemini score it against your
criteria, and pushes the good ones to Telegram, Notion and a web dashboard.

## Why email alerts instead of scraping

leboncoin and SeLoger sit behind DataDome. A scraper works for a week and then
spends its life fighting bot detection. Saved-search alerts are the same data,
delivered by the sites themselves, for free, with no terms-of-service problem.
The trade-off is that you only see what the alert contains — which is why the
pipeline also tries to fetch the full page, and says so when it can't.

## Why DVF

Asking prices tell you what sellers want. **DVF** (Demandes de Valeurs
Foncières) is the French government's public register of every recorded property
sale, so it tells you what buyers actually paid. Every listing is scored against
the median €/m² for its postal code, computed from the real transactions of the
last three years — so "cheap" means cheap against the market, not against the
other listings in your inbox.

For Paris, Lyon and Marseille the comparison is per *arrondissement*, not
city-wide: Lyon 3e and Lyon 9e are different markets and the median reflects
that.

Data comes from Etalab's `geo-dvf` export. No key, no quota, no cost.

## Pipeline

```
Gmail (IMAP)
   └─ parse  ── per-site parser, Gemini Flash as fallback when a template changes
       └─ dedupe ── SQLite; exact match on listing id, fuzzy match on size+price
           └─ DVF  ── median €/m² for the postal code, cached 90 days
               └─ enrich ── best-effort page fetch (often blocked; degrades cleanly)
                   └─ analyse ── Gemini Flash: rent, yield, red flags, score /100
                       └─ deliver ── Telegram push · Notion row · static dashboard
```

State lives in `data/listings.sqlite3`, committed back to the repo each run —
GitHub Actions has no disk that survives, and a single SQLite file is easy to
inspect locally with any browser.

## Setup

### 1. Dedicated Gmail

App-password IMAP rather than OAuth because this runs headless: one secret, no
token refresh, identical behaviour locally and in CI.

Use a **fresh account**, not your own. Three reasons: an app password is
full-mailbox access with no scopes, so you don't want it pointed at your real
mail; the pipeline reads *every* unread message in the inbox and hands anything
it doesn't recognise to Gemini Flash, so unrelated mail costs money; and the
alert filters below would fight with your existing ones.

**a. Create the account.** [accounts.google.com/signup](https://accounts.google.com/signup) —
anything memorable, e.g. `yourname.scout@gmail.com`. Google usually wants a phone
number; your own is fine, it can be reused across accounts.

**b. Turn on 2-Step Verification.** Google Account → **Security** → *How you sign
in to Google* → **2-Step Verification** → turn on. This is not optional: app
passwords do not exist on an account without it.

**c. Generate the app password.** Go to
[myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
(or Security → 2-Step Verification → *App passwords* at the bottom). Give it a
name like `rental-scout` and create it.

Google shows the result once, as 16 characters in four blocks — `abcd efgh ijkl mnop`.
**Strip the spaces** and store `abcdefghijklmnop` as `GMAIL_APP_PASSWORD`. There
is no way to view it again, only to delete it and make a new one. `GMAIL_ADDRESS`
is the full address, which doubles as the IMAP username.

If the app-passwords page refuses you: the account is on Advanced Protection, or
it's a Google Workspace account whose admin has app passwords disabled. Neither
is worth fighting — use a plain personal Gmail account.

**d. Check IMAP is on.** Gmail → ⚙ → **See all settings** →
**Forwarding and POP/IMAP**. If there's an *IMAP access* toggle, enable it and
save. Newer accounts have IMAP on permanently and show no toggle at all.

**e. Keep the alerts unread and in the inbox.** The pipeline reads `INBOX` for
`UNSEEN` messages and flags them `\Seen` once it has stored what it found. It
never deletes anything, so the mailbox stays auditable — but three settings will
make alerts invisible to it:

- a filter with **Skip the Inbox** (archiving) — the message is no longer in `INBOX`
- a filter with **Mark as read** — the message is no longer `UNSEEN`
- alerts landing in **Spam** — also not `INBOX`

The first two are self-inflicted; the third is common on the first alert from a
new sender. Fix it with one filter: search `from:(leboncoin.fr OR lbc.fr OR
seloger.com OR pap.fr OR bienici.com OR logic-immo.com)` → *Create filter* → tick
**Never send it to Spam**, and nothing else. Gmail's Promotions and Updates tabs are still the
inbox, so alerts sorted there are found normally.

Same reason: **don't read the alerts yourself** in the Gmail app before a run has
picked them up. Opening one marks it read and the pipeline will skip it. Triage in
Telegram, Notion or the dashboard instead — that's what they're for.

**f. Verify the connection.** Once `.env` exists (step 5):

```bash
pip install -r requirements.txt
export PYTHONPATH=src             # PowerShell: $env:PYTHONPATH="src"

python -m scout.gmail_client      # logs in, lists unread mail, marks nothing
```

It prints one line per unread message with the site each one was recognised as,
and leaves every message unread for the real run. `[unknown]` means no
deterministic parser claimed the sender — fine in itself, that's what the Flash
fallback is for, but it's also how you spot mail that has no business being in
this inbox.

Failure is almost always one of: spaces left in the app password, `GMAIL_ADDRESS`
missing the `@gmail.com`, or 2-Step Verification switched off again.

### 2. Saved-search alerts

On each site, run the search you care about and save it with email alerts
pointed at that address. Choose the most frequent option each site offers —
the pipeline dedupes, so more frequent alerts only ever help.

### 3. Telegram bot

Message [@BotFather](https://t.me/botfather) → `/newbot` → keep the token.
Send your new bot any message, then message [@userinfobot](https://t.me/userinfobot)
to get your numeric chat id.

### 4. Notion database

Create an integration at [notion.so/my-integrations](https://www.notion.so/my-integrations)
and keep the token. Create a database with exactly these properties:

| Property      | Type   |
| ------------- | ------ |
| `Name`        | Title  |
| `Listing Key` | Text   |
| `Profile`     | Text   |
| `Score`       | Number |
| `Price`       | Number |
| `Surface`     | Number |
| `Yield %`     | Number |
| `City`        | Text   |
| `URL`         | URL    |
| `Status`      | Select — options: New, Interested, Rejected, Contacted |
| `Analysis`    | Text   |

Share the database with your integration (••• → Connections). The database id is
the 32-character string in its URL.

Names are matched ignoring case and surrounding whitespace, so a column you
accidentally created as `Profile ` still works. A column that is genuinely
missing is skipped with a log line rather than failing the row — except `Name`
and `Listing Key`, without which a row can't be created or found again.

Under Notion API 2025-09-03 a database contains one or more *data sources*, and
the properties live on the data source. The pipeline resolves that from your
database id automatically, once per run; you still put the database id in
`.env`. This needs `notion-client` 3.x.

`Status` is yours: the pipeline sets it to `New` when it first creates a row and
never touches it again, so your triage survives re-runs.

`Listing Key` and `Profile` together identify a row: with several search profiles
active, one listing gets one row per profile that matched it, so you can mark it
Interested for one search and Rejected for another.

### 5. Secrets

Locally, copy `.env.example` to `.env` and fill it in. In GitHub, add the same
names under **Settings → Secrets and variables → Actions**.

`GEMINI_API_KEY` comes from [aistudio.google.com/apikey](https://aistudio.google.com/apikey) —
create a key, no billing account needed to start. `GOOGLE_API_KEY` is accepted as
an alias, since that's the other name the Gemini SDK looks for.

### 6. GitHub Pages

**Settings → Pages → Source: Deploy from a branch → `main` / `docs`.** The
dashboard is regenerated and committed on every run.

## Your criteria

Everything the pipeline judges against lives in [`criteria.yaml`](criteria.yaml):
budget, target postal codes, minimum surface, yield floor, DPE tolerance, red-flag
keywords, scoring weights, and which listings are worth a Telegram push.

`avg_rent_per_sqm_eur` per city is worth setting — DVF covers sale prices but not
rents, so without it the rent estimate is the model's judgement alone. Leave
`avg_price_per_sqm_eur` as `null` unless DVF has no data for an area.

### Several searches at once

The top level of `criteria.yaml` is the base. Add a `profiles:` block to hunt for
more than one kind of deal, each profile overriding only the keys where it
differs:

```yaml
profiles:
  - name: "studio-cashflow"
    label: "Studio · cashflow"
    budget:
      max_price_eur: 150000
    min_surface_m2: 18
    yield_thresholds:
      min_gross_yield_pct: 7.0
    cities: ["Lyon", "Villeurbanne"]

  - name: "family-t3"
    budget:
      max_price_eur: 380000
    min_surface_m2: 60
    min_rooms: 3
    scoring:
      score_threshold_notify: 75
    cities: ["Lyon"]
```

Anything a profile doesn't set is inherited from the base. Nested mappings merge
key by key, so `budget.max_price_eur` above leaves `budget.min_price_eur` alone;
lists replace wholesale, because a profile naming its own postal codes or
red-flag keywords means *those*, not "those as well". `cities:` selects entries
from `target_cities` by name, `label:` is the name shown in Telegram and on the
dashboard, and `enabled: false` parks a profile without deleting it.

`models`, `dvf` and `pipeline` are run-level and rejected inside a profile — one
run shares one model choice and one DVF cache.

Each new listing is matched against every profile and scored once per profile it
fits, so the same flat can be a 90 for the cashflow search and a 45 for the long
hold. Each profile has its own `score_threshold_notify`, and the Telegram message
names the profile that fired. With no `profiles:` block nothing changes: the base
is the single search, named `default`.

Two things worth knowing. **Profiles apply to listings seen from then on** — the
dedupe check is deliberately profile-blind, so adding a profile does not re-open
listings already in the database. And **overlapping profiles cost more**: a
listing matching two profiles is two analysis calls, though the DVF lookup and
page fetch are still paid for once.

## Running it

```bash
pip install -r requirements.txt
export PYTHONPATH=src          # PowerShell: $env:PYTHONPATH="src"

python -m scout.config         # check criteria.yaml parses and secrets are visible
python -m scout.models         # list callable Gemini models, flag bad ids
python -m scout --dry-run      # full run, but sends nothing and leaves mail unread
python -m scout                # the real thing
python -m scout --dashboard-only   # re-render the page from existing data

python -m scout --profile studio-cashflow   # one profile only; repeatable
python -m scout --deliver-pending           # send stored-but-undelivered verdicts
```

`--deliver-pending` exists because `--dry-run` stores verdicts without sending
them, and the listing is then *known* — so a later run will never look at it
again and those listings would never reach Telegram or Notion. This replays
them from the database, re-analysing nothing. It is idempotent: it only touches
rows that were never notified or never synced.

`python -m scout.config` prints every active profile with its thresholds — the
quickest way to check a `profiles:` block does what you meant.

`--dry-run` is the one to use first: it ingests, analyses and stores, but sends
no Telegram message, writes nothing to Notion, and leaves the emails unread so
you can run it again against the same messages. Note the asymmetry: the emails
stay unread, but the listings are stored and therefore *known*, so a later real
run skips them. Use `--deliver-pending` to send anything a dry run analysed.

```bash
python -m pytest               # 114 tests, no network, no API key needed
```

## Cost

The parsing fallback only runs when a site changes its email template; the
per-listing analysis runs on every new listing. Both models are set in
`criteria.yaml` under `models:` — currently `gemini-3.6-flash` for each — so
switching tier is a one-line change.

Model ids move faster than this README. `python -m scout.models` lists what your
key can actually call and stars the ones `criteria.yaml` names, exiting non-zero
if a configured model isn't callable.

The criteria go in the system instruction, which is byte-identical for every
listing a profile judges. Gemini 2.5 caches repeated prefixes implicitly, so that
repetition is discounted automatically — there's no breakpoint to place, but it's
why the criteria live in the instruction rather than in each listing's prompt.

`max_listings_per_run` in `criteria.yaml` caps the damage if an alert backlog
arrives at once; note it counts listings, and a listing matching two profiles
costs two analysis calls.

## When something breaks

**A site redesigns its alert email.** The deterministic parser stops matching and
Gemini Flash takes over automatically. You'll see `provenance = llm_fallback` in
the database — that's the signal to update the parser in
`src/scout/parsers/`, using a saved copy of the new email as a fixture.

**A listing shows "email only".** The site blocked the page fetch. Expected, and
the analysis says which details it therefore couldn't check. Not worth fixing —
bypassing DataDome is a fight not worth having for a personal tool.

**No DVF data for an area.** Very recent or very rural transactions may be
missing. The listing is still scored, the analysis flags the comparison as
unavailable, and you can set `avg_price_per_sqm_eur` for that city as a fallback.

**The run fails.** Any unhandled exception sends you a Telegram message and the
error is recorded in the `runs` table, so a broken pipeline doesn't fail
silently for days.

## Layout

```
criteria.yaml              what you're looking for, as one or more profiles
data/listings.sqlite3      state: listings, verdicts per profile, DVF cache, runs
docs/index.html            generated dashboard (GitHub Pages)
src/scout/
  config.py                .env + criteria.yaml -> typed objects, one per profile
  models.py                lists the Gemini models the key can call
  gmail_client.py          IMAP ingest
  parsers/                 per-site extraction + LLM fallback
  dedupe.py                SQLite store: listings (facts) + verdicts (per profile)
  dvf.py                   real sale prices per postal code
  enrich.py                best-effort page fetch
  analysis.py              Gemini scoring
  notify_telegram.py       push
  sync_notion.py           triage board
  dashboard.py             static page
  pipeline.py              the run
tests/                     offline: fixtures, dedup, DVF maths, rendering
```
