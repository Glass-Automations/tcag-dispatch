# TCAG Dispatch Board — data feed

Keeps the Twin Cities Auto Glass dispatch board supplied with current Omega EDI
data. The board itself is published as a page on glassautomations.com; this repo
only produces the JSON it reads.

## What is published here

`publish` branch — one file, one commit, force-pushed on every run:

- **windowed** to roughly today −2 / +14 days
- **scrubbed** of `customer`, `phone` and `address`
- note text redacted for phone numbers, street addresses and ZIPs

A guard in the workflow re-opens the generated file and **fails the build** if any
of those fields survive. If someone changes the scrubbing and gets it wrong, the
job stops rather than publishing customer home addresses.

## What is NOT published here

`tech_scheduler_data_tcag.json` — the working cache — carries ~4,700 customer
addresses and ~1,600 names and phone numbers. It lives in the GitHub Actions
cache between runs and is in `.gitignore`. **Never commit it.** This repo is
public.

## Schedule

| | When | Cost |
|---|---|---|
| Light refresh | every 30 min, 06:00–18:30 US Central, Mon–Sat | ~26 Omega calls, ~20s |
| Full id-walk | 03:15 US Central daily | ~1,980 calls, ~11 min |

Cron is UTC and does not follow DST, so the window shifts an hour in winter.

## Why the sync looks the way it does

Omega's `GET /Appointments` returns a rolling window of about today−15 → today+1
and ignores `page`. Appointments dated beyond that exist and are readable
individually by id, but unreachable by any list query — hence the id walk.

The **reconcile pass** re-reads cached appointments in the forward window on every
run. Without it the cache merges but never corrects: measured on real data, 33 of
40 upcoming appointments were stale after five days, 29 of them still showing as
unassigned after they had been dispatched to named techs.

Rate limiting is self-throttled to 170/min against Omega's 200/min. Going over
does not merely slow down — it 429s the key for the rest of the minute and every
in-flight call fails.

## Setup

- `secrets.OMEGA_API_KEY` — Omega EDI API key
- `vars.DATA_FILE` — published filename (deliberately unguessable)
