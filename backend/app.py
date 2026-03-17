from pathlib import Path
from datetime import datetime, timezone
import os
import json

import requests
from flask import Flask, jsonify, send_from_directory, request
from flask_cors import CORS
from apscheduler.schedulers.background import BackgroundScheduler

from backend.fetch_epic import main as fetch_once


# =========================================================
# PATHS
# =========================================================

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
IMAGES_DIR = DATA_DIR / "images"


# =========================================================
# APP
# =========================================================

app = Flask(__name__)
CORS(app)


# =========================================================
# HELPERS
# =========================================================

def cleanup_images(keep: int = 5):
    """Keep only the newest N images in data/images/."""
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)

    imgs = sorted(
        IMAGES_DIR.glob("*.png"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    for old in imgs[keep:]:
        try:
            old.unlink()
        except Exception as e:
            print("Cleanup error:", old, e)


def warm_start():
    """
    Fetch once and cleanup cache.
    Safe to call multiple times; used both locally and on first cloud request.
    """
    try:
        fetch_once()
        cleanup_images(keep=5)
        print("Warm fetch & cleanup complete.")
    except Exception as e:
        print("Warm fetch failed:", e)


def _read_latest_meta():
    """Read cached metadata.json (returns dict or None)."""
    meta_path = DATA_DIR / "metadata.json"

    if not meta_path.exists():
        return None

    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception as e:
        print("Metadata read/parse failed:", e)
        return None


def _abs_url(path: str) -> str:
    """
    Build an absolute URL to this backend based on the current request.
    Works locally + on deploy (Render, etc).
    """
    return f"{request.host_url.rstrip('/')}{path}"


def _utc_iso_from_unix(ts: int) -> str:
    """Convert UNIX timestamp to UTC ISO string."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


# =========================================================
# ROUTES
# =========================================================

@app.get("/health")
def health():
    """Simple health endpoint for frontend status checks."""
    return jsonify(
        {
            "ok": True,
            "status": "online",
            "service": "spaceport-telemetry-backend",
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        }
    ), 200


@app.get("/api/latest")
def latest():
    meta = DATA_DIR / "metadata.json"

    if not meta.exists():
        return jsonify({"error": "No data cached yet"}), 404

    return app.response_class(
        meta.read_text(encoding="utf-8"),
        mimetype="application/json",
    )


@app.get("/epic")
def epic():
    """
    Frontend-compatible EPIC endpoint.
    Returns: {"images": ["<absolute_url>", ...]}
    Uses cached local image first; NASA direct URL as fallback.
    """
    d = _read_latest_meta()

    if not d:
        return jsonify({"images": []}), 200

    image_local = d.get("image_local")  # e.g. /images/<file>.png
    if image_local:
        return jsonify({"images": [_abs_url(image_local)]}), 200

    image_url = d.get("image_url")  # NASA direct URL
    return jsonify({"images": [image_url] if image_url else []}), 200


@app.get("/apod")
def apod():
    """
    Frontend-compatible APOD endpoint.
    Never throws (prevents UI hangs when NASA rate-limits DEMO_KEY).
    Returns:
      { ok: bool, url, title, media_type, date, error? }
    """
    key = os.getenv("NASA_API_KEY", "DEMO_KEY")

    try:
        r = requests.get(
            "https://api.nasa.gov/planetary/apod",
            params={"api_key": key},
            timeout=15,
        )

        if r.status_code != 200:
            return jsonify(
                {
                    "ok": False,
                    "url": None,
                    "title": None,
                    "media_type": None,
                    "date": None,
                    "error": f"NASA APOD error {r.status_code}: {r.text[:200]}",
                }
            ), 200

        d = r.json()

        return jsonify(
            {
                "ok": True,
                "url": d.get("url"),
                "title": d.get("title"),
                "media_type": d.get("media_type"),
                "date": d.get("date"),
            }
        ), 200

    except Exception as e:
        return jsonify(
            {
                "ok": False,
                "url": None,
                "title": None,
                "media_type": None,
                "date": None,
                "error": str(e),
            }
        ), 200


# =========================================================
# SUN / MOON
# =========================================================

@app.get("/sunmoon")
def sunmoon():
    """
    Sun: sunrise-sunset.org API (no key required)
    Moon: calculated locally (no external dependency)

    Query params:
      lat, lon

    Returns:
      Telemetry fields + structured sun/moon objects
    """

    # -----------------------------------------------------
    # Coordinates
    # -----------------------------------------------------

    try:
        lat = float(request.args.get("lat", "28.5729"))
        lon = float(request.args.get("lon", "-80.6490"))
    except Exception:
        return jsonify({"ok": False, "error": "Invalid lat/lon"}), 400

    now = datetime.now(timezone.utc)
    ymd = now.strftime("%Y-%m-%d")

    # -----------------------------------------------------
    # SUN DATA
    # -----------------------------------------------------

    try:
        sun_r = requests.get(
            "https://api.sunrise-sunset.org/json",
            params={"lat": lat, "lng": lon, "formatted": 0},
            timeout=15,
        )

        sun_r.raise_for_status()
        sun_json = sun_r.json()

        if sun_json.get("status") != "OK":
            return jsonify({"ok": False, "error": "Sun API returned non-OK"}), 502

        s = sun_json.get("results", {})

    except Exception as e:
        return jsonify({"ok": False, "error": f"Sun fetch failed: {e}"}), 502


    # -----------------------------------------------------
    # MOON PHASE (local calculation)
    # -----------------------------------------------------

    def julian_day(dt: datetime) -> float:
        y = dt.year
        m = dt.month
        d = dt.day + (dt.hour + dt.minute/60 + dt.second/3600) / 24

        if m <= 2:
            y -= 1
            m += 12

        a = y // 100
        b = 2 - a + (a // 4)

        jd = int(365.25*(y+4716)) + int(30.6001*(m+1)) + d + b - 1524.5
        return jd


    jd = julian_day(now)

    # reference new moon
    synodic = 29.53058867
    days_since = jd - 2451550.1
    phase_days = days_since % synodic
    phase_fraction = phase_days / synodic


    def phase_name(frac: float) -> str:

        if frac < 0.0625 or frac >= 0.9375:
            return "New Moon"

        if frac < 0.1875:
            return "Waxing Crescent"

        if frac < 0.3125:
            return "First Quarter"

        if frac < 0.4375:
            return "Waxing Gibbous"

        if frac < 0.5625:
            return "Full Moon"

        if frac < 0.6875:
            return "Waning Gibbous"

        if frac < 0.8125:
            return "Last Quarter"

        return "Waning Crescent"


    illumination = round((1 - abs(2 * phase_fraction - 1)) * 100, 1)

    moon = {
        "phase": phase_name(phase_fraction),
        "illumination": illumination,
        "age": round(phase_days, 2),
        "distance": 384400,  # average km
        "date": ymd,
    }


    # -----------------------------------------------------
    # RESPONSE
    # -----------------------------------------------------

    return jsonify(
        {
            "ok": True,
            "lat": lat,
            "lon": lon,
            "date": ymd,

            # --- flat telemetry fields (for UI dashboard) ---
            "day_length": s.get("day_length"),
            "civil_twilight_begin": s.get("civil_twilight_begin"),
            "civil_twilight_end": s.get("civil_twilight_end"),

            "moon_phase": moon["phase"],
            "moon_illumination": moon["illumination"],
            "moon_age": moon["age"],
            "moon_distance": moon["distance"],

            # --- structured data (useful for future features) ---
            "sun": {
                "sunrise": s.get("sunrise"),
                "sunset": s.get("sunset"),
                "solar_noon": s.get("solar_noon"),
                "day_length": s.get("day_length"),
                "civil_twilight_begin": s.get("civil_twilight_begin"),
                "civil_twilight_end": s.get("civil_twilight_end"),
            },

            "moon": moon,
        }
    ), 200

@app.get("/iss-pass")
def iss_pass():

    try:
        lat = float(request.args.get("lat", "28.5729"))
        lon = float(request.args.get("lon", "-80.6490"))
    except Exception:
        return jsonify({"ok": False, "error": "Invalid lat/lon"}), 400

    url = "https://api.wheretheiss.at/v1/satellites/25544"

    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()

        data = r.json()

        now = datetime.now(timezone.utc)

        risetime = int(now.timestamp()) + 600
        duration = 600
        countdown_seconds = 600

        return jsonify(
            {
                "ok": True,
                "lat": lat,
                "lon": lon,
                "risetime": risetime,
                "risetime_utc": _utc_iso_from_unix(risetime),
                "duration": duration,
                "countdown_seconds": countdown_seconds,
                "iss_position": {
                    "latitude": data.get("latitude"),
                    "longitude": data.get("longitude"),
                    "altitude_km": data.get("altitude"),
                    "velocity_kmh": data.get("velocity"),
                },
            }
        ), 200

    except Exception as e:
        return jsonify({"ok": False, "error": f"ISS fetch failed: {e}"}), 502

    except Exception as e:
        return jsonify({"ok": False, "error": f"ISS pass fetch failed: {e}"}), 502


@app.get("/images/<path:name>")
def images(name: str):
    return send_from_directory(IMAGES_DIR, name)


@app.get("/")
def root():
    return jsonify(
        {
            "ok": True,
            "endpoints": [
                "/",
                "/health",
                "/api/latest",
                "/epic",
                "/apod",
                "/sunmoon",
                "/iss-pass",
                "/images/<file>",
                "/api/refresh",
            ],
        }
    ), 200


@app.route("/api/refresh", methods=["GET", "POST"])
def api_refresh():
    """Manual refresh endpoint (local + deploy)."""
    try:
        warm_start()
        return jsonify({"ok": True, "refreshed": True}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# =========================================================
# ONE-TIME INIT
# =========================================================

_initialized = False


@app.before_request
def ensure_initialized():
    """
    Flask 3.x removed before_first_request; this guards a one-time warm_start
    when the first real request hits the service (works on Render + locally).
    """
    global _initialized

    if not _initialized:
        if not (DATA_DIR / "metadata.json").exists():
            warm_start()
        _initialized = True


# =========================================================
# LOCAL DEV BOOT
# =========================================================

if __name__ == "__main__":
    warm_start()

    scheduler = BackgroundScheduler(daemon=True)

    def scheduled_job():
        warm_start()
        print("Scheduled fetch & cleanup @", datetime.now(timezone.utc).isoformat())

    scheduler.add_job(
        scheduled_job,
        "interval",
        hours=3,
        next_run_time=datetime.now(timezone.utc),
    )
    
scheduler.start()

import os

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False)
