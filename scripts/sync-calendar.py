#!/usr/bin/env python3
"""
sync-calendar.py — read Google Calendar events and write availability.json.

Status model (see CALENDAR.md):
  - An event marked reserved  => "reserved"
      Reserved is detected by EITHER:
        (a) the modern event LABEL named "Zauzeto" (Bosnian: taken/reserved),
            read via the labelProperties of the calendar, OR
        (b) the legacy red colorId "11" (Tomato) as a fallback.
  - Every other event          => "available"
  - A day with no event        => not a trip (not shown)

NOTE on access: the modern Google Calendar stores colors as "labels". A service
account at "reader" (See all event details) level CANNOT see label colors or
the legacy colorId — they come back empty. The calendar must be shared with the
service account at "Make changes to events" (writer) level for colors to appear.

Configuration (env vars):
  GOOGLE_SERVICE_ACCOUNT_JSON  JSON key contents (GitHub Actions secret), OR
  GOOGLE_KEY_FILE              path to the JSON key file (Option B / local)
  GOOGLE_CALENDAR_ID           the calendar id to read
  OUTPUT_PATH                  output file (default: availability.json)

Writes the file only when the *trips* actually changed (generatedAt is not
part of the change check), so no-change runs produce no commit and no
Cloudflare build.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    SAR = ZoneInfo("Europe/Sarajevo")
except Exception:  # fallback if zoneinfo data is missing
    SAR = timezone(timedelta(hours=1))  # CET

try:
    from google.oauth2 import service_account
    from google.auth.transport.requests import AuthorizedSession
except ImportError:
    sys.exit("ERROR: google-auth not installed. Run: pip install google-auth requests")

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
RESERVED_COLOR_ID = "11"            # legacy red (Tomato) — fallback signal
RESERVED_LABEL_KEYWORD = "zauzet"   # matches "Zauzeto" (Bosnian: reserved/taken)
MONTHS_AHEAD = 6


def load_credentials():
    """Build service-account credentials from the JSON env var or a key file."""
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if raw and raw.strip():
        return service_account.Credentials.from_service_account_info(
            json.loads(raw), scopes=SCOPES)
    keyfile = os.environ.get("GOOGLE_KEY_FILE")
    if keyfile:
        return service_account.Credentials.from_service_account_file(
            keyfile, scopes=SCOPES)
    sys.exit("ERROR: set GOOGLE_SERVICE_ACCOUNT_JSON (or GOOGLE_KEY_FILE)")


def event_date(ev):
    """Return the event's local date as 'YYYY-MM-DD', or None."""
    start = ev.get("start") or {}
    if "date" in start:                      # all-day event — already a date
        return start["date"]
    if "dateTime" in start:                  # timed event — convert to local tz
        dt = datetime.fromisoformat(start["dateTime"].replace("Z", "+00:00"))
        return dt.astimezone(SAR).date().isoformat()
    return None


def fetch_label_map(session, cal_id):
    """Return {eventLabelId -> label object} from the calendar's labelProperties.

    Requires eventLabelVersion=1 (passed as a query param). Labels hold the
    human 'name' (e.g. "Zauzeto") and backgroundColor set by the calendar owner.
    """
    url = f"https://www.googleapis.com/calendar/v3/calendars/{cal_id}"
    r = session.get(url, params={"eventLabelVersion": "1"}, timeout=30)
    if r.status_code != 200:
        sys.exit(f"ERROR: Calendars.get {r.status_code}: {r.text[:400]}")
    meta = r.json()
    labels = (meta.get("labelProperties") or {}).get("eventLabels") or []
    return {lb["id"]: lb for lb in labels if "id" in lb}


def fetch_events(session, cal_id, time_min, time_max):
    url = f"https://www.googleapis.com/calendar/v3/calendars/{cal_id}/events"
    params = {
        "timeMin": time_min,
        "timeMax": time_max,
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": 250,
        "eventLabelVersion": "1",   # expose eventLabelId on each event
    }
    items = []
    while True:
        r = session.get(url, params=params, timeout=30)
        if r.status_code != 200:
            sys.exit(f"ERROR: Calendar API {r.status_code}: {r.text[:400]}")
        data = r.json()
        items.extend(data.get("items", []))
        token = data.get("nextPageToken")
        if not token:
            break
        params["pageToken"] = token
    return items


def is_reserved(ev, label_map):
    """True if the event is marked reserved (Zauzeto label, or legacy red)."""
    # 1) Modern label system: the event's label name contains the reserved keyword.
    label_id = ev.get("eventLabelId")
    if label_id and label_id in label_map:
        name = (label_map[label_id].get("name") or "").lower()
        if RESERVED_LABEL_KEYWORD in name:
            return True
    # 2) Fallback: legacy red colorId (Tomato = 11).
    if str(ev.get("colorId")) == RESERVED_COLOR_ID:
        return True
    return False


def _log(trips):
    avail = sum(1 for t in trips if t["status"] == "available")
    res = sum(1 for t in trips if t["status"] == "reserved")
    print(f"   {len(trips)} trips ({avail} available, {res} reserved).")


def main():
    cal_id = os.environ.get("GOOGLE_CALENDAR_ID") or sys.exit("ERROR: set GOOGLE_CALENDAR_ID")
    creds = load_credentials()
    session = AuthorizedSession(creds)

    now = datetime.now(SAR)
    time_min = now.isoformat()
    time_max = (now + timedelta(days=30 * MONTHS_AHEAD)).isoformat()

    label_map = fetch_label_map(session, cal_id)
    items = fetch_events(session, cal_id, time_min, time_max)

    by_date = {}
    for ev in items:
        if ev.get("status") == "cancelled":     # skip cancelled recurring instances
            continue
        d = event_date(ev)
        if not d:
            continue
        status = "reserved" if is_reserved(ev, label_map) else "available"
        trip = {"date": d, "status": status}
        title = (ev.get("summary") or "").strip()
        if title:
            trip["title"] = title
        # if a date has several events, "reserved" wins over "available"
        cur = by_date.get(d)
        if cur is None or (cur["status"] == "available" and status == "reserved"):
            by_date[d] = trip

    trips = sorted(by_date.values(), key=lambda t: t["date"])
    out = {
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "trips": trips,
    }

    out_path = os.environ.get("OUTPUT_PATH", "availability.json")
    new_text = json.dumps(out, ensure_ascii=False, indent=2) + "\n"

    # Decide by trips only: generatedAt must NOT force a rewrite, otherwise
    # every run commits -> ~96 commits/day -> Cloudflare build limit blown.
    trips_text = json.dumps(trips, ensure_ascii=False, indent=2)
    try:
        with open(out_path, "r", encoding="utf-8") as f:
            old_trips = json.load(f).get("trips", [])
        if json.dumps(old_trips, ensure_ascii=False, indent=2) == trips_text:
            print(f"OK: no changes — {out_path} untouched.", end=" ")
            _log(trips)
            return
    except (FileNotFoundError, ValueError):
        pass

    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(new_text)
    os.replace(tmp, out_path)
    print(f"OK: wrote {out_path}.", end=" ")
    _log(trips)


if __name__ == "__main__":
    main()
