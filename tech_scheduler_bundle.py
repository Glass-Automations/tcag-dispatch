#!/usr/bin/env python3
"""
Build a single self-contained dispatch board for hosting on a GHL funnel page.

GHL serves a page but has nowhere to put a JSON file the page can fetch at a
relative path, so the cache and the config are inlined into the HTML. The output
is one file: paste it into the funnel page's custom-code block exactly the way the
quoting tools are published.

  python3 tech_scheduler_bundle.py --profile tcag                 # scrubbed (default)
  python3 tech_scheduler_bundle.py --profile tcag --keep-pii      # full detail
  python3 tech_scheduler_bundle.py --profile tcag --back 7 --ahead 28

WHY SCRUBBING IS THE DEFAULT: a GHL funnel page is public. Anyone with the URL
reads whatever is in the file. Customer name, home address and phone are not
needed to dispatch - the dispatcher needs who, when, where (city), what vehicle.
Full detail stays one click away in Omega, where staff are already signed in.
"""
import argparse, datetime, json, pathlib, re, sys

HERE = pathlib.Path(__file__).parent
PII_FIELDS = ('customer', 'phone', 'address')

# Free-text notes carry PII that no field-level strip would catch:
# "HOME ADDRESS IN STILLWATER/PLEASE CALL WITH ETA 651-398-2238".
PHONE_RE = re.compile(r'\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b')
STREET_RE = re.compile(
    r'\b\d{2,6}\s+[NSEW]?\.?\s*[A-Za-z0-9\'.-]+(?:\s+[A-Za-z0-9\'.-]+)*?\s+'
    r'(?:ST|STREET|AVE|AVENUE|RD|ROAD|BLVD|BOULEVARD|DR|DRIVE|LN|LANE|WAY|CT|COURT|'
    r'PL|PLACE|TER|TERRACE|CIR|CIRCLE|PKWY|PARKWAY|HWY|HIGHWAY|TRL|TRAIL)\b\.?',
    re.I)
ZIP_RE = re.compile(r'\b\d{5}(?:-\d{4})?\b')


def scrub_note(note):
    if not note:
        return note
    n = PHONE_RE.sub('[phone]', note)
    n = STREET_RE.sub('[address]', n)
    n = ZIP_RE.sub('[zip]', n)
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--profile', default='', help='account label, e.g. tcag')
    ap.add_argument('--back', type=int, default=7, help='days of history to include')
    ap.add_argument('--ahead', type=int, default=28, help='days of lookahead to include')
    ap.add_argument('--keep-pii', action='store_true',
                    help='keep customer name, phone, address and raw notes (NOT for a public page)')
    ap.add_argument('--remote', default='',
                    help='URL of the live cache JSON. Baked into the page: the inlined '
                         'copy paints instantly, then fresh data is fetched over it and '
                         're-polled every 5 minutes. This is what lets the published page '
                         'stay current without ever being re-pasted.')
    ap.add_argument('--json-only', action='store_true',
                    help='emit just the scrubbed, windowed cache JSON instead of an HTML '
                         'page. This is what the scheduled job publishes for the live '
                         'board to fetch; the full cache never leaves the private side.')
    ap.add_argument('--out', default='')
    args = ap.parse_args()

    sfx = f'_{args.profile}' if args.profile else ''
    page = HERE / 'tech_scheduler_v2.html'
    cache = HERE / f'tech_scheduler_data{sfx}.json'
    config = HERE / f'tech_config{sfx}.json'
    # --json-only never reads the page, so don't demand it. The data feed runs in
    # CI where only the sync and this script are checked out.
    required = (cache,) if args.json_only else (page, cache)
    for f in required:
        if not f.exists():
            sys.exit(f'missing {f.name}')

    html = page.read_text() if not args.json_only else ''
    data = json.loads(cache.read_text())
    cfg = json.loads(config.read_text()) if config.exists() else {}

    today = datetime.date.today()
    lo = (today - datetime.timedelta(days=args.back)).isoformat()
    hi = (today + datetime.timedelta(days=args.ahead)).isoformat()

    kept, redacted = {}, 0
    for k, a in data['appointments'].items():
        d = a.get('date') or ''
        if not (lo <= d <= hi):
            continue
        if args.keep_pii:
            kept[k] = a
            continue
        b = {kk: vv for kk, vv in a.items() if kk not in PII_FIELDS}
        # Where this job sits in Omega's billing lifecycle (WO / IN / QO / VO).
        # Only the status code travels - never amounts or balances, which are
        # financial data and this page is public.
        js = (data.get('invoices') or {}).get(str(a.get('invoice_id') or ''))
        if js:
            b['job_status'] = js
        if b.get('note'):
            s = scrub_note(b['note'])
            if s != b['note']:
                redacted += 1
            b['note'] = s
        kept[k] = b
    data['appointments'] = kept
    data.pop('invoices', None)      # the per-appointment code above is all the board needs
    data['bundled_at'] = datetime.datetime.now().isoformat(timespec='seconds')
    data['pii'] = 'full' if args.keep_pii else 'scrubbed'
    data['window'] = [lo, hi]

    if args.json_only:
        out = pathlib.Path(args.out) if args.out else HERE / f'tech_scheduler_public{sfx}.json'
        out.write_text(json.dumps(data, separators=(',', ':')))
        print(f'window       : {lo} .. {hi}')
        print(f'appointments : {len(kept)}')
        print(f'PII          : {"KEPT - do not publish" if args.keep_pii else f"scrubbed ({redacted} notes redacted)"}')
        print(f'wrote        : {out.name}  ({out.stat().st_size/1024:.0f} KB)')
        return

    # </script> inside JSON would close the tag early; escaping the slash is the
    # standard fix and stays valid JSON.
    def embed(obj):
        return json.dumps(obj, separators=(',', ':')).replace('</', '<\\/')

    remote = f'window.__TS_REMOTE__={json.dumps(args.remote)};\n' if args.remote else ''
    inject = (f'<script>window.__TS_DATA__={embed(data)};\n'
              f'window.__TS_CFG__={embed(cfg)};\n{remote}</script>\n<script>')
    if '\n<script>\n"use strict";' not in html:
        sys.exit('could not find the board script tag to inject before')
    out_html = html.replace('\n<script>\n"use strict";',
                            '\n' + inject + '\n"use strict";', 1)

    out = pathlib.Path(args.out) if args.out else HERE / f'tech_scheduler_bundle{sfx}.html'
    out.write_text(out_html)

    kb = len(out_html) / 1024
    print(f'profile      : {args.profile or "(default)"}')
    print(f'window       : {lo} .. {hi}')
    print(f'appointments : {len(kept)} of {len(json.loads(cache.read_text())["appointments"])}')
    print(f'PII          : {"KEPT - do not host publicly" if args.keep_pii else f"scrubbed ({redacted} notes redacted)"}')
    print(f'remote       : {args.remote or "(none - inlined data only, needs re-pasting to refresh)"}')
    print(f'wrote        : {out.name}  ({kb:.0f} KB)')


if __name__ == '__main__':
    main()
