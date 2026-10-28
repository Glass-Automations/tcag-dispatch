#!/usr/bin/env python3
"""
Pull Omega appointments into a local cache for the Tech Scheduler dashboard.

Omega's GET /Appointments list only returns a rolling window of roughly
[today-15, today+1] and ignores the `page` param, so appointments booked for
later dates are unreachable that way even though they exist. They ARE readable
individually via GET /Appointments/{id}. This syncs by walking the id space.

  python3 tech_scheduler_sync.py --full      # first run: deep scan
  python3 tech_scheduler_sync.py             # incremental
"""
import argparse, datetime, json, os, pathlib, sys, threading, time, urllib.parse, urllib.request
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor

BASE = "https://app.omegaedi.com/api/2.0"
HERE = pathlib.Path(__file__).parent
# One Omega account per profile. Default profile keeps the original filenames so
# existing deployments are untouched; named profiles get their own cache/config.
CACHE = HERE / "tech_scheduler_data.json"
CONFIG = HERE / "tech_config.json"

def set_profile(name):
    global CACHE, CONFIG
    if name:
        CACHE = HERE / f"tech_scheduler_data_{name}.json"
        CONFIG = HERE / f"tech_config_{name}.json"
WORKERS = 4

# Omega returns appointment windows as unix timestamps, which are absolute. The
# wall-clock time a dispatcher expects depends on the SHOP's timezone - not the
# timezone of whatever machine happens to run this sync.
#
# Getting this wrong is silent and total. datetime.fromtimestamp() with no tzinfo
# uses the local zone, so the same data rendered 07:00 on a Mountain-time laptop
# and 13:00 on a UTC CI runner, and neither was right: checked against the times
# dispatchers hand-write into their own notes, 2,943 of 3,014 showed the true time
# was one hour later than the laptop said. TCAG is Central.
#
# Set it per profile in tech_config_<profile>.json as "timezone".
TZ = None          # set in run(); None means fall back to local with a warning

# Omega enforces 200 req/min and returns 429 once you cross it. Going wider than
# this does not just slow down - it locks the key out for the rest of the minute,
# and every in-flight call fails. Stay under with headroom.
RATE_PER_MIN = 170
CAP_DAYS = 21            # days of forward capacity to pull from /Capacities
_lock = threading.Lock()
_slots = []


def throttle():
    """Block until issuing one more request keeps us under RATE_PER_MIN."""
    while True:
        with _lock:
            now = time.time()
            _slots[:] = [t for t in _slots if now - t < 60]
            if len(_slots) < RATE_PER_MIN:
                _slots.append(now)
                return
            wait = 60 - (now - _slots[0]) + 0.05
        time.sleep(max(wait, 0.05))

# Omega glass SKU prefixes -> the shorthand TCAG uses on the paper schedule
SKU_WORK = {
    "FW": "WS", "DW": "WS",           # windshield
    "FB": "BG", "DB": "BG",           # back glass
    "FD": "DG", "DD": "DG",           # door glass
    "FQ": "QG", "DQ": "QG",           # quarter glass
    "FV": "VG", "DV": "VG",           # vent glass
}


def api(path, params=None, key=None, timeout=30, retries=4):
    """One GET, rate-limited, retrying with backoff on 429."""
    qs = ("?" + urllib.parse.urlencode(params)) if params else ""
    url = f"{BASE}{path}{qs}"
    for attempt in range(retries):
        throttle()
        req = urllib.request.Request(url, headers={"api_key": key})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                back = 5 * (attempt + 1)
                print(f"  429 rate limited, backing off {back}s", file=sys.stderr)
                time.sleep(back)
                continue
            if e.code == 404:
                return None
            raise
    raise RuntimeError(f"gave up after {retries} attempts (429): {path}")


def try_api(path, params=None, key=None):
    """Like api() but never raises. Prints the reason - a silent [] here would
    read as 'nothing scheduled' rather than 'the call failed'."""
    try:
        return api(path, params, key)
    except Exception as e:
        print(f"  ! {path} failed: {e}", file=sys.stderr)
        return None


def norm_ts(v):
    """requested_window_* is unix seconds on most rows but milliseconds on some."""
    if not v:
        return None
    v = int(v)
    while v > 4_000_000_000:
        v //= 1000
    return v


def work_code(appt):
    sku = (appt.get("sku") or "").strip().upper()
    if sku.startswith("WSREPAIR") or sku.startswith("RPR"):
        return "WR"
    code = SKU_WORK.get(sku[:2])
    if code:
        return code
    if "RECAL" in sku:
        return "RECAL"
    return sku[:10] or "—"


def city_of(appt):
    txt = appt.get("service_address_text") or ""
    parts = [p.strip() for p in txt.split(",")]
    return parts[-2].upper() if len(parts) >= 2 else (parts[0].upper() if parts else "")


def shape(appt):
    """Flatten an Omega appointment into what the board renders."""
    s, e = norm_ts(appt.get("requested_window_start")), norm_ts(appt.get("requested_window_end"))
    if not s:
        return None
    sd = datetime.datetime.fromtimestamp(s, TZ)
    ed = datetime.datetime.fromtimestamp(e, TZ) if e else None
    veh = " ".join(x for x in (appt.get("vehicle_year"), appt.get("vehicle_make"),
                               appt.get("vehicle_model")) if x).strip()
    return {
        "id": str(appt.get("id")),
        "date": sd.strftime("%Y-%m-%d"),
        "start": sd.strftime("%H:%M"),
        "end": ed.strftime("%H:%M") if ed else None,
        "window": f"{sd.hour}-{ed.hour}" if ed else f"{sd.hour}",
        "status": appt.get("status"),
        "tech": appt.get("user_id") or "",
        "location_id": str(appt.get("location_id") or ""),
        "type": appt.get("type") or "",
        "invoice_id": str(appt.get("invoice_id") or ""),
        "customer": " ".join(x.strip() for x in (appt.get("customer_fname") or "",
                                                 appt.get("customer_surname") or "") if x.strip()),
        "phone": appt.get("customer_phone") or "",
        "city": city_of(appt),
        "address": appt.get("service_address_text") or "",
        "vehicle": veh,
        "sku": appt.get("sku") or "",
        "work": work_code(appt),
        # Capacity is measured in points (hours), not job count: a chip repair is
        # 0.5 and a windshield R&I is 1. Omega computes the per-job total on the
        # LIST rows - the detail endpoint returns it as null, hence the fallback
        # to the /SaleItems catalogue by sku.
        "points": (float(appt["points"]) if str(appt.get("points") or "").strip()
                   not in ("", "None") else None),
        "ignore_capacity": str(appt.get("ignore_capacity") or "0") == "1",
        "note": (appt.get("note") or "").strip(),
        # Skills is a list of {id,name,icon} on list rows but plain strings on some detail rows
        "skills": [(s.get("name") if isinstance(s, dict) else str(s)).strip()
                   for s in (appt.get("Skills") or []) if s],
        "parts_ordered": appt.get("glass_parts_ordered"),
        "all_received": appt.get("all_received"),
    }


def sync_invoices(cache, key, back_days, ahead_days, backfill_cap=80):
    """Record each invoice's position in Omega's billing lifecycle.

    Omega tracks the JOB separately from the BILL:
        LE Lead -> QO Quote -> WO Work Order -> IN Invoice -> CL Archived / VO Void
    An appointment being CLOSED only means the work happened. The invoice can
    still be sitting at WO, unbilled - which is exactly what the dispatcher
    wants to see, so this cannot be inferred from appointment status.

    /Invoices filters on the invoice's OWN date, not the appointment's, so a job
    scheduled next week may hang off an invoice raised a month ago. A sweep alone
    therefore never reaches every invoice on the board: measured, today-17d
    covered 368 of 398 and today-45d still missed 7. So sweep the recent range
    (cheap, catches WO -> IN transitions) and individually fetch whatever is
    still unknown, capped so one run cannot blow past the rate limit."""
    today = datetime.date.today()
    d1 = (today - datetime.timedelta(days=back_days)).isoformat()
    d2 = (today + datetime.timedelta(days=ahead_days)).isoformat()
    inv = dict(cache.get("invoices") or {})

    swept = 0
    for page in range(1, 41):
        rows = try_api("/Invoices", {"date1": d1, "date2": d2, "page": page}, key=key) or []
        for r in rows:
            iid = str(r.get("id") or "")
            if iid:
                inv[iid] = r.get("job_status") or ""
                swept += 1
        if len(rows) < 100:
            break

    # Anything an appointment points at that the sweep did not reach.
    need = {str(a.get("invoice_id")) for a in cache["appointments"].values()
            if a.get("invoice_id")}
    missing = sorted(need - set(inv), reverse=True)[:backfill_cap]
    filled = 0
    for iid in missing:
        d = try_api(f"/Invoices/{urllib.parse.quote(iid)}", key=key)
        if isinstance(d, dict) and d.get("id"):
            inv[str(d["id"])] = d.get("job_status") or ""
            filled += 1
        else:
            inv[iid] = ""          # remember we looked, so we stop re-asking

    cache["invoices"] = inv
    known = len(need & set(inv))
    print(f"invoices: swept {swept} {d1}..{d2}, backfilled {filled}, "
          f"{known}/{len(need)} on the board have a billing status")
    return inv


def active_locations(cache):
    """Locations that have ever carried an appointment, newest-biased.

    A light run polls these instead of every location on the account. For TCAG
    that is roughly 9 of 23 - the others are will-calls and dropboxes that have
    never booked anything, so spending two calls each on them every 15 minutes
    buys nothing."""
    live = {str(a.get("location_id")) for a in (cache.get("appointments") or {}).values()
            if a.get("location_id")}
    live |= {str(k) for k, v in (cache.get("capacity") or {}).items()
             if any((d or {}).get("capacity") for d in (v or {}).values())}
    ordered = [l for l in (cache.get("locations") or {}) if l in live]
    return ordered or list(cache.get("locations") or {})


def load_cache():
    if CACHE.exists():
        return json.loads(CACHE.read_text())
    return {"appointments": {}, "techs": {}, "locations": {}, "max_id": 0, "synced_at": None}


def fetch_ids_detailed(ids, key):
    """Fetch by id, separating "Omega says this is gone" from "the call failed".

    The distinction is the whole point: a 404 means the appointment was deleted
    and should leave the cache, while a timeout or a 429 means we simply do not
    know. Treating the second as the first would silently drop real scheduled
    work off the board, which is the worst failure this tool can have."""
    found, gone, errs = {}, set(), 0

    def one(aid):
        try:
            d = api(f"/Appointments/{aid}", key=key)
            # api() returns None only on a 404; anything else raises.
            return aid, ("ok", d) if d else ("gone", None)
        except Exception:
            return aid, ("error", None)

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for aid, (status, d) in ex.map(one, ids):
            if status == "ok" and d and d.get("id"):
                found[str(aid)] = d
            elif status == "gone":
                gone.add(str(aid))
            else:
                errs += 1
    return found, gone, errs


def fetch_ids(ids, key):
    """Fetch many appointments by id concurrently. Returns {id: raw_appt}."""
    out = {}

    def one(aid):
        try:
            return aid, api(f"/Appointments/{aid}", key=key)
        except Exception:
            return aid, None

    total = len(ids)
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for i, (aid, d) in enumerate(ex.map(one, ids), 1):
            if d and d.get("id"):
                out[str(aid)] = d
            if i % 250 == 0:
                print(f"  {i}/{total} ids probed, {len(out)} found", file=sys.stderr)
    return out


def seed_config(cache):
    """Create tech_config.json on first run, and add rows for techs who appear
    later. Dispatch detail (start location, capabilities, territory, days off)
    has no home in Omega, so it lives here and is never overwritten by a sync -
    existing rows are left exactly as the dispatcher edited them.
    """
    cfg = json.loads(CONFIG.read_text()) if CONFIG.exists() else {"techs": {}}
    cfg.setdefault("techs", {})

    scheduled = {a["tech"] for a in cache["appointments"].values() if a.get("tech")}
    added = 0
    for emp, t in cache["techs"].items():
        if emp in cfg["techs"]:
            continue
        if int(t.get("active") or 0) != 1 and emp not in scheduled:
            continue  # skip the long tail of inactive staff
        cfg["techs"][emp] = {
            "name": t.get("name") or emp,
            "start": "",          # "Starts at PGW Fridley" - not in Omega
            # Left blank on purpose: the board renders Technician Skills live from
            # the Omega user record. This is only an override for kit Omega has no
            # skill for, e.g. "Big Van", "Autel tool".
            "caps": "",
            "territory": "",      # "West Metro: Minnetonka, Wayzata" - not in Omega
            "off": [],            # ISO dates the tech is out
            "order": 100,
            "hide": False,
        }
        added += 1

    cfg["updated"] = datetime.datetime.now().isoformat(timespec="seconds")
    CONFIG.write_text(json.dumps(cfg, indent=1))
    print(f"tech_config.json: {len(cfg['techs'])} techs ({added} new)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", default=os.environ.get("OMEGA_API_KEY"))
    ap.add_argument("--full", action="store_true", help="deep scan the id space")
    ap.add_argument("--light", action="store_true",
                    help="intraday refresh: list window + capacity for locations that "
                         "actually book work. Skips the id walk, the per-tech detail "
                         "calls and /SaleItems, which change rarely. ~18 calls, a few "
                         "seconds - cheap enough to run several times an hour.")
    ap.add_argument("--depth", type=int, default=3000, help="ids to walk back on a full scan")
    ap.add_argument("--no-invoices", dest="invoices", action="store_false",
                    help="skip the invoice billing-status pass")
    ap.add_argument("--probe", type=int, default=80,
                    help="on a light run, how many ids past the known maximum to "
                         "check for newly created appointments")
    ap.add_argument("--tz", default=os.environ.get("OMEGA_TZ"),
                    help="IANA timezone for the shop, e.g. America/Chicago. "
                         "Defaults to \"timezone\" in the profile config.")
    ap.add_argument("--reconcile-days", type=int, default=0,
                    help="re-read every cached appointment dated within N days ahead, "
                         "so reschedules, cancellations and tech changes propagate. "
                         "0 = use the default for the run type (7 light, 60 otherwise).")
    ap.add_argument("--no-reconcile", action="store_true",
                    help="skip the forward-window re-read (faster, but cancelled and "
                         "reassigned jobs will keep their stale values)")
    ap.add_argument("--profile", default=os.environ.get("OMEGA_PROFILE", ""),
                    help="account label, e.g. tcag - keeps each client's cache separate")
    args = ap.parse_args()
    if not args.key:
        sys.exit("need --key or OMEGA_API_KEY")
    set_profile(args.profile)
    if args.profile:
        print(f"profile: {args.profile}  ->  {CACHE.name}, {CONFIG.name}")

    lock = HERE / f"tech_scheduler_sync{('_'+args.profile) if args.profile else ''}.lock"
    if lock.exists():
        age = time.time() - lock.stat().st_mtime
        if age < 3600:
            sys.exit(f"another sync has been running for {age:.0f}s ({lock.name}) - skipping. "
                     f"Delete the lock if that run died.")
        lock.unlink()          # older than an hour: assume it died
    lock.write_text(str(os.getpid()))
    try:
        run(args, lock)
    finally:
        if lock.exists():
            lock.unlink()


def run(args, lock):
    global TZ
    # The shop's timezone, from the profile config, so every machine that runs
    # this sync produces identical wall-clock times.
    tzname = args.tz
    if not tzname and CONFIG.exists():
        try:
            tzname = (json.loads(CONFIG.read_text()) or {}).get("timezone")
        except Exception:
            tzname = None
    if tzname:
        TZ = ZoneInfo(tzname)
        print(f"timezone: {tzname}")
    else:
        TZ = None
        print("timezone: NOT SET - falling back to this machine's local zone. "
              "Times will differ between machines. Set \"timezone\" in the profile "
              "config (e.g. \"America/Chicago\").", file=sys.stderr)

    cache = load_cache()

    locations = try_api("/Locations", key=args.key) or []
    cache["locations"] = {str(l["id"]): l.get("name") for l in locations}

    prev_techs = dict(cache.get("techs") or {})
    users = try_api("/Users", key=args.key) or []
    # `active` comes back as an integer 1/0, not the string "1" - comparing
    # against a string silently drops every active tech.
    cache["techs"] = {
        u["employee_number"]: {
            "name": f"{(u.get('fname') or '').strip()} {(u.get('lname') or '').strip()}".strip(),
            "active": int(u.get("active") or 0),
            "role_id": str(u.get("role_id") or ""),
            "location_id": str(u.get("location_id") or ""),
            # Omega does carry per-tech day capacity and skills, even though it has
            # no field for start location or territory.
            "capacity": int(u.get("capacity") or 0),
            "skills": [s.get("name", "").strip() for s in (u.get("Skills") or [])
                       if isinstance(s, dict) and s.get("name")],
        }
        for u in users if u.get("employee_number")
    }

    # Availability lives only on the User DETAIL record - the /Users list omits
    # works_mon..works_sun, time_off_start/end and Timeoffs. One call per field
    # tech (capacity > 0); office staff are skipped.
    DOW = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    detail_n = timeoff_n = 0
    for emp, t in ({} if args.light else cache["techs"]).items():
        if t.get("capacity", 0) <= 0 or t.get("active") != 1:
            continue
        d = try_api(f"/Users/{urllib.parse.quote(emp)}", key=args.key)
        if not isinstance(d, dict):
            continue
        detail_n += 1
        t["works"] = [x for x in DOW if str(d.get(f"works_{x}")) == "1"]
        t["time_off_start"] = d.get("time_off_start")
        t["time_off_end"] = d.get("time_off_end")
        t["timeoffs"] = d.get("Timeoffs") or []
        if t["time_off_start"] or t["timeoffs"]:
            timeoff_n += 1
    if args.light:
        # Carry the previous run's availability forward; works_* and Timeoffs change
        # on the order of weeks, not minutes.
        for emp, t in cache["techs"].items():
            prev = prev_techs.get(emp) or {}
            for k in ("works", "time_off_start", "time_off_end", "timeoffs"):
                if k in prev:
                    t[k] = prev[k]
        print("availability: carried forward (light run)")
    else:
        print(f"availability: {detail_n} tech detail records, {timeoff_n} with time off recorded")

    # Per-day booked/capacity straight from Omega. This is also the only
    # independent check on whether our appointment cache is complete.
    cap = dict(cache.get("capacity") or {}) if args.light else {}
    today = datetime.date.today()
    d1, d2 = today.isoformat(), (today + datetime.timedelta(days=CAP_DAYS)).isoformat()
    # Of TCAG's 23 locations only ~9 ever carry appointments; the rest are
    # will-calls, dropboxes and winter shops with zero capacity. On a light run
    # poll only the ones the cache has actually seen work at.
    poll = active_locations(cache) if args.light else list(cache["locations"])
    for lid in poll:
        rows = try_api("/Appointments/Capacities",
                       {"date1": d1, "date2": d2, "location_id": lid}, key=args.key) or []
        for r in rows:
            cap.setdefault(lid, {})[r.get("date")] = {
                "capacity": int(r.get("capacity") or 0),
                "booked": int(r.get("appointments") or 0),
            }
    cache["capacity"] = cap

    # sku -> points, so appointments reached by id (where Omega returns a null
    # points) can still be costed against a tech's capacity.
    sale = [] if args.light else (try_api("/SaleItems", key=args.key) or [])
    if args.light:
        print(f"sale items: carried forward ({len(cache.get('sale_points') or {})} skus)")
    cache["sale_points"] = cache.get("sale_points", {}) if args.light else {
        str(s["sku"]).strip(): float(s["points"])
        for s in sale
        if s.get("sku") and str(s.get("points") or "0") not in ("0", "0.0", "")
    }
    if not args.light:
        print(f"sale items: {len(cache['sale_points'])} skus carry points")
    print(f"capacity: {sum(len(v) for v in cap.values())} location-days {d1}..{d2}")

    # 1. cheap pass: the list window gives us recent + today + tomorrow in ~6 calls
    seen = {}
    for lid in poll:
        rows = try_api("/Appointments", {"location_id": lid}, key=args.key) or []
        for a in rows:
            seen[str(a["id"])] = a
    print(f"list window: {len(seen)} appointments")
    if not seen and not cache["appointments"]:
        sys.exit("list window came back empty and the cache is cold - check the "
                 "api_key and whether you are rate limited (429), then retry")

    known_max = max([int(i) for i in seen] + [cache.get("max_id") or 0])

    todo = []
    if args.light:
        # Omega's /Appointments list only reaches about today+1, so a job booked
        # today for ANY later date never appears in it. Without this probe a new
        # booking stayed invisible until the nightly full scan - verified: five
        # OPEN appointments existed in Omega that a light run would not find.
        #
        # Ids are assigned sequentially, so scanning a short way past the highest
        # id we know about catches every new booking whatever date it is for.
        lo = (cache.get("max_id") or known_max) + 1
        todo = [i for i in range(lo, lo + args.probe) if str(i) not in seen]
        print(f"light run: probing ids {lo}..{lo + args.probe - 1} for new bookings")

    # 2. id walk: the only way to reach appointments dated beyond tomorrow
    elif args.full:
        lo = max(1, known_max - args.depth)
        todo = [i for i in range(lo, known_max + 1) if str(i) not in seen]
        print(f"full scan: walking ids {lo}..{known_max} ({len(todo)} to fetch)")
    else:
        lo = cache.get("max_id") or (known_max - 400)
        todo = list(range(lo + 1, known_max + 60))
        print(f"incremental: walking ids {lo+1}..{known_max+59} ({len(todo)} to fetch)")

    if todo:
        t0 = time.time()
        fetched = fetch_ids(todo, args.key)
        print(f"  -> {len(fetched)} existed ({time.time()-t0:.0f}s)")
        seen.update(fetched)
        # If the probe filled its whole range, bookings are arriving faster than
        # the window can see. Say so rather than silently missing the overflow.
        if args.light and fetched and len(fetched) >= args.probe:
            print(f"  ! probe window full ({args.probe}) - raise --probe", file=sys.stderr)

    # merge into cache, keeping anything we've seen before
    for aid, raw in seen.items():
        row = shape(raw)
        if row:
            cache["appointments"][aid] = row

    # ---- reconcile the forward window -------------------------------------
    # The id walk only reaches ids ABOVE the previous max, and the list window
    # stops at roughly tomorrow, so an appointment dated further out keeps
    # whatever we cached the first time we saw it - forever.
    #
    # This is not theoretical. Comparing a three-week-old TCAG cache against a
    # fresh pull: 214 appointments had changed status (11 of them CANCELled) and
    # 45 had moved to a different tech. The board was showing cancelled jobs as
    # live work and jobs on the wrong technician.
    #
    # The fix is cheap because the forward window is small - a few dozen ids.
    if not args.no_reconcile:
        days = args.reconcile_days or (7 if args.light else 60)
        t_iso = datetime.date.today().isoformat()
        horizon = (datetime.date.today() + datetime.timedelta(days=days)).isoformat()
        recon = [aid for aid, a in cache["appointments"].items()
                 if t_iso <= (a.get("date") or "") <= horizon and aid not in seen]
        if recon:
            t0 = time.time()
            found, gone, errs = fetch_ids_detailed(recon, args.key)
            changed = 0
            for aid, raw in found.items():
                row = shape(raw)
                if row and row != cache["appointments"].get(aid):
                    cache["appointments"][aid] = row
                    changed += 1
            for aid in gone:
                cache["appointments"].pop(aid, None)
            print(f"reconcile: re-read {len(recon)} ids within {days}d "
                  f"({changed} changed, {len(gone)} deleted, {errs} unreadable) "
                  f"({time.time()-t0:.0f}s)")
        else:
            print("reconcile: nothing cached in the forward window yet")

    # Billing status, so the board can show what has been invoiced. Light runs
    # sweep a short window (most WO -> IN transitions are recent); full runs go
    # wider and backfill harder.
    if args.invoices:
        sync_invoices(cache, args.key,
                      back_days=21 if args.light else 60,
                      ahead_days=21,
                      backfill_cap=40 if args.light else 300)

    cache["max_id"] = max([int(i) for i in seen] + [known_max])
    # UTC with an explicit Z. This field is written by two different publishers -
    # this machine and a GitHub runner - and read by browsers in a third zone.
    # A naive local timestamp made the board's "synced N min ago" wrong by hours
    # depending on who published last, which is worse than showing nothing.
    cache["synced_at"] = (datetime.datetime.now(datetime.timezone.utc)
                          .strftime("%Y-%m-%dT%H:%M:%SZ"))
    CACHE.write_text(json.dumps(cache, indent=1))

    seed_config(cache)

    dates = sorted({a["date"] for a in cache["appointments"].values()})
    print(f"\ncached {len(cache['appointments'])} appointments, {dates[0]}..{dates[-1]}")
    print(f"wrote {CACHE}")


if __name__ == "__main__":
    main()
