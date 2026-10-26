#!/usr/bin/env python3
"""Garmin Health/Activity API -> lokale Datendatei fuer das Trainingscockpit.

Ablauf:
  1. Einmalig:  python sync.py auth          (OAuth 1.0a, Token wird lokal gespeichert)
  2. Laufend:   python sync.py pull --days 90 (holt Aktivitaeten, Tageswerte, Schlaf)
  3. index.html im Browser oeffnen.

Konfiguration ueber .env (siehe .env.example). Secrets und Daten bleiben lokal
und sind per .gitignore vom Repository ausgeschlossen.
"""

import argparse
import http.server
import json
import os
import sys
import threading
import time
import urllib.parse
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import requests
    from requests_oauthlib import OAuth1Session
except ImportError:
    sys.exit("Fehlende Pakete. Bitte installieren: pip install -r requirements.txt")

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"
TOKEN_FILE = HERE / ".garmin-token.json"
CACHE_FILE = DATA_DIR / "cache.json"
OUT_FILE = DATA_DIR / "garmin-data.js"

DAY = 86400


def load_env():
    env_file = HERE / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def cfg(name, default=None, required=False):
    value = os.environ.get(name, default)
    if required and not value:
        sys.exit(f"{name} fehlt. Bitte in garmin/.env eintragen (Vorlage: .env.example).")
    return value


def urls():
    return {
        "request_token": cfg("GARMIN_REQUEST_TOKEN_URL", "https://connectapi.garmin.com/oauth-service/oauth/request_token"),
        "authorize": cfg("GARMIN_AUTHORIZE_URL", "https://connect.garmin.com/oauthConfirm"),
        "access_token": cfg("GARMIN_ACCESS_TOKEN_URL", "https://connectapi.garmin.com/oauth-service/oauth/access_token"),
        "api_base": cfg("GARMIN_API_BASE", "https://apis.garmin.com/wellness-api/rest").rstrip("/"),
    }


# ---------------------------------------------------------------- auth

class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    result = {}

    def do_GET(self):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _CallbackHandler.result = {k: v[0] for k, v in query.items()}
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write("<p>Garmin-Verbindung bestaetigt. Du kannst dieses Fenster schliessen.</p>".encode("utf-8"))

    def log_message(self, *args):
        pass


def cmd_auth(_args):
    key = cfg("GARMIN_CONSUMER_KEY", required=True)
    secret = cfg("GARMIN_CONSUMER_SECRET", required=True)
    port = int(cfg("GARMIN_CALLBACK_PORT", "8765"))
    callback = f"http://localhost:{port}/callback"
    u = urls()

    oauth = OAuth1Session(key, client_secret=secret, callback_uri=callback)
    request_token = oauth.fetch_request_token(u["request_token"])
    auth_url = oauth.authorization_url(u["authorize"])

    server = http.server.HTTPServer(("localhost", port), _CallbackHandler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    print("Browser oeffnet die Garmin-Freigabe. Falls nicht, diese Adresse aufrufen:\n" + auth_url)
    webbrowser.open(auth_url)
    thread.join(timeout=300)
    server.server_close()

    verifier = _CallbackHandler.result.get("oauth_verifier")
    if not verifier:
        pasted = input("Keine Rueckmeldung erhalten. Bitte die komplette Adresse aus dem Browser nach der Freigabe einfuegen:\n> ")
        verifier = urllib.parse.parse_qs(urllib.parse.urlparse(pasted.strip()).query).get("oauth_verifier", [None])[0]
    if not verifier:
        sys.exit("Kein oauth_verifier gefunden. Bitte 'python sync.py auth' erneut starten.")

    oauth = OAuth1Session(
        key,
        client_secret=secret,
        resource_owner_key=request_token["oauth_token"],
        resource_owner_secret=request_token["oauth_token_secret"],
        verifier=verifier,
    )
    access = oauth.fetch_access_token(u["access_token"])
    TOKEN_FILE.write_text(json.dumps({
        "oauth_token": access["oauth_token"],
        "oauth_token_secret": access["oauth_token_secret"],
        "created": datetime.now(timezone.utc).isoformat(),
    }, indent=2), encoding="utf-8")
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except OSError:
        pass
    print(f"Token gespeichert in {TOKEN_FILE.name}. Weiter mit: python sync.py pull --days 90")


def session():
    key = cfg("GARMIN_CONSUMER_KEY", required=True)
    secret = cfg("GARMIN_CONSUMER_SECRET", required=True)
    if not TOKEN_FILE.exists():
        sys.exit("Noch kein Token vorhanden. Zuerst ausfuehren: python sync.py auth")
    token = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    return OAuth1Session(
        key,
        client_secret=secret,
        resource_owner_key=token["oauth_token"],
        resource_owner_secret=token["oauth_token_secret"],
    )


# ---------------------------------------------------------------- pull

ENDPOINTS = {
    "activities": "activities",
    "dailies": "dailies",
    "sleeps": "sleeps",
}


def fetch_window(sess, base, endpoint, start, end):
    params = {"uploadStartTimeInSeconds": start, "uploadEndTimeInSeconds": end}
    for attempt in range(4):
        resp = sess.get(f"{base}/{endpoint}", params=params, timeout=30)
        if resp.status_code == 429:
            time.sleep(2 ** (attempt + 1))
            continue
        if resp.status_code == 401:
            sys.exit("401 Unauthorized: Token ungueltig oder Zugriff entzogen. 'python sync.py auth' erneut ausfuehren.")
        if resp.status_code >= 400:
            hint = ""
            if "InvalidPullToken" in resp.text:
                hint = ("\nHinweis: Garmin verlangt fuer produktive Keys oft Ping/Push statt freiem Pull. "
                        "Mit Evaluation-Key testen oder Ping-Endpunkt im Developer Portal pruefen.")
            raise RuntimeError(f"{endpoint}: HTTP {resp.status_code} {resp.text[:300]}{hint}")
        return resp.json() if resp.text.strip() else []
    raise RuntimeError(f"{endpoint}: zu viele Anfragen (429), spaeter erneut versuchen.")


def local_iso(seconds, offset):
    return datetime.fromtimestamp(seconds + (offset or 0), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def norm_activity(a):
    return {
        "id": str(a.get("summaryId") or a.get("activityId")),
        "type": a.get("activityType", "OTHER"),
        "name": a.get("activityName") or "",
        "start": local_iso(a.get("startTimeInSeconds", 0), a.get("startTimeOffsetInSeconds")),
        "dur": a.get("durationInSeconds") or 0,
        "dist": a.get("distanceInMeters") or 0,
        "kcal": a.get("activeKilocalories") or 0,
        "avgHr": a.get("averageHeartRateInBeatsPerMinute"),
        "maxHr": a.get("maxHeartRateInBeatsPerMinute"),
        "elev": a.get("totalElevationGainInMeters") or 0,
    }


def norm_daily(d):
    return {
        "date": d.get("calendarDate"),
        "steps": d.get("steps"),
        "restingHr": d.get("restingHeartRateInBeatsPerMinute"),
        "stress": d.get("averageStressLevel"),
    }


def norm_sleep(s):
    return {
        "date": s.get("calendarDate"),
        "dur": s.get("durationInSeconds") or 0,
        "deep": s.get("deepSleepDurationInSeconds") or 0,
        "light": s.get("lightSleepDurationInSeconds") or 0,
        "rem": s.get("remSleepInSeconds") or 0,
        "awake": s.get("awakeDurationInSeconds") or 0,
    }


def load_cache():
    if CACHE_FILE.exists():
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    return {"activities": {}, "dailies": {}, "sleeps": {}, "syncedUntil": None}


def cmd_pull(args):
    sess = session()
    base = urls()["api_base"]
    DATA_DIR.mkdir(exist_ok=True)
    cache = load_cache()

    now = int(time.time())
    start = now - args.days * DAY
    if cache.get("syncedUntil") and not args.full:
        # Inkrementell: einen Tag Ueberlappung, damit nachtraeglich hochgeladene Summaries nicht fehlen
        start = max(start, int(cache["syncedUntil"]) - DAY)

    windows = list(range(start, now, DAY))
    print(f"Hole {len(windows)} Tagesfenster ab {datetime.fromtimestamp(start):%d.%m.%Y} ...")
    for i, t in enumerate(windows, 1):
        end = min(t + DAY, now)
        for key, endpoint in ENDPOINTS.items():
            for item in fetch_window(sess, base, endpoint, t, end):
                if key == "activities":
                    rec = norm_activity(item)
                    cache["activities"][rec["id"]] = rec
                elif key == "dailies":
                    rec = norm_daily(item)
                    if rec["date"]:
                        cache["dailies"][rec["date"]] = rec
                else:
                    rec = norm_sleep(item)
                    if rec["date"]:
                        cache["sleeps"][rec["date"]] = rec
            time.sleep(0.15)
        print(f"\r  {i}/{len(windows)}", end="", flush=True)
    print()

    cache["syncedUntil"] = now
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    write_output(cache)


def write_output(cache):
    payload = {
        "source": "Garmin Health API",
        "generated": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "activities": sorted(cache["activities"].values(), key=lambda a: a["start"]),
        "dailies": sorted(cache["dailies"].values(), key=lambda d: d["date"]),
        "sleeps": sorted(cache["sleeps"].values(), key=lambda s: s["date"]),
    }
    # Als JS-Datei, damit index.html auch per Doppelklick (file://) ohne Webserver laedt
    OUT_FILE.write_text("window.GARMIN_DATA = " + json.dumps(payload, ensure_ascii=False) + ";\n", encoding="utf-8")
    print(f"{len(payload['activities'])} Aktivitaeten, {len(payload['dailies'])} Tage, "
          f"{len(payload['sleeps'])} Naechte -> {OUT_FILE.relative_to(HERE)}")


def cmd_export(_args):
    write_output(load_cache())


def main():
    load_env()
    parser = argparse.ArgumentParser(description="Garmin Health API -> Trainingscockpit")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("auth", help="Garmin-Konto einmalig verbinden (OAuth)").set_defaults(func=cmd_auth)
    pull = sub.add_parser("pull", help="Daten holen und Dashboard-Datei schreiben")
    pull.add_argument("--days", type=int, default=90, help="Zeitraum rueckwirkend in Tagen (Standard 90)")
    pull.add_argument("--full", action="store_true", help="Zeitraum komplett neu holen statt inkrementell")
    pull.set_defaults(func=cmd_pull)
    sub.add_parser("export", help="Dashboard-Datei aus dem lokalen Cache neu schreiben").set_defaults(func=cmd_export)
    args = parser.parse_args()
    try:
        args.func(args)
    except (RuntimeError, requests.RequestException) as exc:
        sys.exit(f"Fehler: {exc}")


if __name__ == "__main__":
    main()
