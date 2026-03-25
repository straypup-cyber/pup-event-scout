#!/usr/bin/env python3
"""
Pup Scout - Web API
"""

import os
import re
import json
import hmac
import hashlib
import logging
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv
import anthropic
from flask import Flask, request, jsonify
from flask_cors import CORS
from supabase import create_client, Client

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app, origins="*")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
GOOGLE_PLACES_API_KEY = os.getenv("GOOGLE_PLACES_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL", "https://ponhyojwucvukkphqfqz.supabase.co")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")  # TODO: add when available
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")    # TODO: add when available
TWILIO_PHONE = os.getenv("TWILIO_PHONE", "")              # TODO: add when available
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")  # TODO: add when available
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")          # TODO: add when available
EVENTBRITE_TOKEN = os.getenv("EVENTBRITE_TOKEN", "")      # TODO: add when available

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_KEY else None

# ─── Pricing ────────────────────────────────────────────────────────────────
PRICING = {
    "subscription_monthly": 5.0,   # TON per month
    "call_credit": 1.0,             # TON per call
    "email_credit": 0.2,            # TON per email outreach
}

FREE_DAILY_SEARCHES = 10
FREE_MAX_RESULTS = 3
PREMIUM_MAX_RESULTS = 10

# ─── Telegram Auth ───────────────────────────────────────────────────────────

def verify_telegram_auth(data: dict) -> bool:
    """Verify Telegram Login Widget data integrity."""
    if not TELEGRAM_BOT_TOKEN:
        return True  # Skip verification if no token (dev mode)
    check_hash = data.pop("hash", "")
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret_key = hashlib.sha256(TELEGRAM_BOT_TOKEN.encode()).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(computed_hash, check_hash)


def upsert_user(tg_data: dict) -> dict:
    """Create or update user in Supabase."""
    if not supabase:
        return tg_data
    try:
        user = {
            "id": int(tg_data.get("id", 0)),
            "username": tg_data.get("username", ""),
            "first_name": tg_data.get("first_name", ""),
            "last_name": tg_data.get("last_name", ""),
            "photo_url": tg_data.get("photo_url", ""),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        result = supabase.table("users").upsert(user).execute()
        return result.data[0] if result.data else user
    except Exception as e:
        logger.error(f"Upsert user error: {e}")
        return tg_data


def get_user(user_id: int) -> dict | None:
    if not supabase:
        return None
    try:
        result = supabase.table("users").select("*").eq("id", user_id).single().execute()
        return result.data
    except:
        return None


def save_search(user_id: int | None, query: str, mode: str, location: str, results_count: int) -> str | None:
    if not supabase or not user_id:
        return None
    try:
        result = supabase.table("searches").insert({
            "user_id": user_id,
            "query": query,
            "mode": mode,
            "location": location,
            "results_count": results_count,
        }).execute()
        return result.data[0]["id"] if result.data else None
    except Exception as e:
        logger.error(f"Save search error: {e}")
        return None

# ─── Google Places ───────────────────────────────────────────────────────────

def get_place_photos(place_id: str, max_photos: int = 2) -> list[str]:
    if not GOOGLE_PLACES_API_KEY or not place_id:
        return []
    photo_urls = []
    try:
        resp = requests.get(
            "https://maps.googleapis.com/maps/api/place/details/json",
            params={"place_id": place_id, "fields": "photos", "key": GOOGLE_PLACES_API_KEY},
            timeout=8
        )
        photos = resp.json().get("result", {}).get("photos", [])
        for photo in photos[:max_photos]:
            ref = photo.get("photo_reference")
            if ref:
                photo_urls.append(
                    f"https://maps.googleapis.com/maps/api/place/photo"
                    f"?maxwidth=600&photo_reference={ref}&key={GOOGLE_PLACES_API_KEY}"
                )
    except Exception as e:
        logger.error(f"Photo error: {e}")
    return photo_urls


def search_google_places(query: str, location: str, max_results: int = 5) -> list[dict]:
    if not GOOGLE_PLACES_API_KEY:
        return []
    results = []
    try:
        resp = requests.get(
            "https://maps.googleapis.com/maps/api/place/textsearch/json",
            params={"query": f"{query} in {location}", "key": GOOGLE_PLACES_API_KEY},
            timeout=10
        )
        data = resp.json()
        for place in data.get("results", [])[:max_results]:
            place_id = place.get("place_id")
            details = {}
            if place_id:
                det = requests.get(
                    "https://maps.googleapis.com/maps/api/place/details/json",
                    params={
                        "place_id": place_id,
                        "fields": "name,formatted_address,website,rating,user_ratings_total,price_level,formatted_phone_number,geometry",
                        "key": GOOGLE_PLACES_API_KEY,
                    },
                    timeout=8
                )
                details = det.json().get("result", {})

            price_map = {0: "Free", 1: "Budget ($)", 2: "Moderate ($$)", 3: "Upscale ($$$)", 4: "Luxury ($$$$)"}
            price_level = details.get("price_level")
            geo = details.get("geometry", {}).get("location", {})

            results.append({
                "name": place.get("name", ""),
                "place_id": place_id or "",
                "address": details.get("formatted_address") or place.get("formatted_address", ""),
                "rating": place.get("rating", ""),
                "rating_count": details.get("user_ratings_total", ""),
                "website": details.get("website", ""),
                "phone": details.get("formatted_phone_number", ""),
                "price_level": price_map.get(price_level, "") if price_level is not None else "",
                "lat": geo.get("lat", ""),
                "lng": geo.get("lng", ""),
            })
    except Exception as e:
        logger.error(f"Google Places error: {e}")
    return results


def search_eventbrite(query: str, location: str) -> list[dict]:
    """Search Eventbrite for events. TODO: full implementation when token available."""
    if not EVENTBRITE_TOKEN:
        return []
    try:
        resp = requests.get(
            "https://www.eventbriteapi.com/v3/events/search/",
            headers={"Authorization": f"Bearer {EVENTBRITE_TOKEN}"},
            params={
                "q": query,
                "location.address": location,
                "expand": "venue",
                "sort_by": "date",
            },
            timeout=10
        )
        events = []
        for e in resp.json().get("events", [])[:5]:
            venue = e.get("venue", {})
            events.append({
                "name": e.get("name", {}).get("text", ""),
                "address": venue.get("address", {}).get("localized_address_display", ""),
                "website": e.get("url", ""),
                "date": e.get("start", {}).get("local", ""),
                "price": "Free" if e.get("is_free") else "Paid",
                "place_id": "",
                "rating": "",
                "rating_count": "",
                "phone": "",
                "price_level": "",
                "lat": venue.get("latitude", ""),
                "lng": venue.get("longitude", ""),
            })
        return events
    except Exception as e:
        logger.error(f"Eventbrite error: {e}")
        return []

# ─── Claude parsing ──────────────────────────────────────────────────────────

def parse_request(user_request: str, mode: str = "venue") -> dict:
    mode_hints = {
        "venue": "event venue, conference space, banquet hall, rooftop for private events",
        "nightout": "bars, restaurants, nightclubs, cocktail lounges, entertainment",
        "events": "upcoming events, concerts, exhibitions, festivals, shows",
    }
    response = anthropic_client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=400,
        messages=[{"role": "user", "content": f"""Parse this {mode} search request. Return ONLY valid JSON, no markdown:

Request: "{user_request}"
Mode: {mode} ({mode_hints.get(mode, '')})

{{
  "location": "city name",
  "capacity": "number or range or empty",
  "budget": "budget info or empty",
  "vibe": "atmosphere/theme keywords",
  "date": "date or weekend or empty",
  "search_query": "2-5 word search query for Google Places"
}}"""}]
    )
    try:
        text = re.sub(r"```(?:json)?\s*", "", response.content[0].text.strip()).rstrip("`")
        return json.loads(text)
    except:
        return {"location": "unknown", "search_query": "venue", "capacity": "", "budget": "", "vibe": "", "date": ""}


def analyze_venues(places: list[dict], parsed: dict, user_request: str, max_results: int = 5) -> list[dict]:
    places_ctx = "\n".join([f"{i}: {p['name']} | {p['address']} | rating:{p.get('rating','')} | price:{p.get('price_level','')}" for i, p in enumerate(places)])
    response = anthropic_client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1500,
        messages=[{"role": "user", "content": f"""Analyze venues for: "{user_request}"
Capacity: {parsed.get('capacity')}, Budget: {parsed.get('budget')}, Vibe: {parsed.get('vibe')}

Venues:
{places_ctx}

Return ONLY JSON array, top {max_results} best matches:
[{{"index":0,"why":"one sentence why it fits","capacity_estimate":"X-Y people","vibe_match":"short label","estimated_budget":"$X,000-Y,000 for full event"}}]

Only include venues that genuinely fit."""}]
    )
    try:
        text = re.sub(r"```(?:json)?\s*", "", response.content[0].text.strip()).rstrip("`")
        return json.loads(text)
    except:
        return [{"index": i, "why": "", "capacity_estimate": "", "vibe_match": "", "estimated_budget": ""} for i in range(min(max_results, len(places)))]


def generate_outreach_email(venue: dict, event_details: dict) -> str:
    """Generate personalized outreach email for a venue."""
    response = anthropic_client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=600,
        messages=[{"role": "user", "content": f"""Write a professional venue inquiry email.

Venue: {venue.get('name')} at {venue.get('address')}
Event details: {json.dumps(event_details)}

Write a concise, professional email (150-200 words) from an event planner. Include:
- Brief event description
- Date flexibility (if provided)
- Number of guests
- Budget range
- Specific questions about availability and pricing

Return ONLY the email body, no subject line, no markdown."""}]
    )
    return response.content[0].text.strip()

# ─── Routes ──────────────────────────────────────────────────────────────────

@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "version": "2.0.0"})


@app.route("/api/auth/telegram", methods=["POST"])
def auth_telegram():
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data"}), 400

    # Verify hash (skip in dev if no token)
    data_copy = dict(data)
    if TELEGRAM_BOT_TOKEN and not verify_telegram_auth(data_copy):
        return jsonify({"error": "Invalid auth data"}), 401

    user = upsert_user(data)
    return jsonify({"ok": True, "user": user})


@app.route("/api/user/me", methods=["GET"])
def get_me():
    user_id = request.args.get("telegram_id")
    if not user_id:
        return jsonify({"error": "No user id"}), 400
    user = get_user(int(user_id))
    if not user:
        return jsonify({"error": "User not found"}), 404
    return jsonify(user)


@app.route("/api/user/searches", methods=["GET"])
def get_searches():
    user_id = request.args.get("telegram_id")
    if not user_id or not supabase:
        return jsonify([])
    try:
        result = supabase.table("searches")\
            .select("*")\
            .eq("user_id", int(user_id))\
            .order("created_at", desc=True)\
            .limit(20)\
            .execute()
        return jsonify(result.data or [])
    except Exception as e:
        return jsonify([])


@app.route("/api/user/saved", methods=["GET"])
def get_saved():
    user_id = request.args.get("telegram_id")
    if not user_id or not supabase:
        return jsonify([])
    try:
        result = supabase.table("saved_venues")\
            .select("*")\
            .eq("user_id", int(user_id))\
            .order("created_at", desc=True)\
            .limit(50)\
            .execute()
        return jsonify(result.data or [])
    except Exception as e:
        return jsonify([])


@app.route("/api/user/save-venue", methods=["POST"])
def save_venue():
    data = request.get_json()
    user_id = data.get("telegram_id")
    if not user_id or not supabase:
        return jsonify({"error": "Auth required"}), 401
    try:
        result = supabase.table("saved_venues").insert({
            "user_id": int(user_id),
            "search_id": data.get("search_id"),
            "name": data.get("name", ""),
            "address": data.get("address", ""),
            "website": data.get("website", ""),
            "rating": data.get("rating"),
            "place_id": data.get("place_id", ""),
            "notes": data.get("notes", ""),
        }).execute()
        return jsonify({"ok": True, "id": result.data[0]["id"] if result.data else None})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/search", methods=["POST"])
def search():
    data = request.get_json()
    query = data.get("query", "").strip()
    mode = data.get("mode", "venue")
    user_id = data.get("telegram_id")
    lang = data.get("lang", "en")

    if not query:
        return jsonify({"error": "No query provided"}), 400

    # Check user tier for result limits
    max_results = FREE_MAX_RESULTS
    if user_id:
        user = get_user(int(user_id))
        if user and user.get("tier") == "premium":
            max_results = PREMIUM_MAX_RESULTS

    try:
        parsed = parse_request(query, mode)
        location = parsed.get("location", "unknown")

        # Get venues based on mode
        if mode == "events" and EVENTBRITE_TOKEN:
            places = search_eventbrite(parsed.get("vibe", query), location)
        else:
            places = search_google_places(parsed.get("search_query", "venue"), location, max_results=8)

        if not places:
            return jsonify({"venues": [], "location": location, "parsed": parsed})

        analyses = analyze_venues(places, parsed, query, max_results=max_results)

        venues = []
        for a in analyses:
            idx = a.get("index", 0)
            if idx < len(places):
                p = places[idx]
                photos = get_place_photos(p["place_id"], max_photos=2) if p.get("place_id") else []
                venues.append({
                    "name": p["name"],
                    "address": p["address"],
                    "rating": p["rating"],
                    "rating_count": p["rating_count"],
                    "website": p["website"],
                    "phone": p["phone"],
                    "price_level": p["price_level"],
                    "place_id": p["place_id"],
                    "lat": p.get("lat", ""),
                    "lng": p.get("lng", ""),
                    "photos": photos,
                    "why": a.get("why", ""),
                    "capacity": a.get("capacity_estimate", ""),
                    "vibe": a.get("vibe_match", ""),
                    "budget": a.get("estimated_budget", ""),
                })

        # Save search to DB
        search_id = save_search(int(user_id) if user_id else None, query, mode, location, len(venues))

        # Truncate to free tier limit if not premium
        is_truncated = len(venues) > FREE_MAX_RESULTS and max_results == FREE_MAX_RESULTS
        display_venues = venues[:FREE_MAX_RESULTS] if max_results == FREE_MAX_RESULTS else venues

        return jsonify({
            "venues": display_venues,
            "location": location,
            "parsed": parsed,
            "search_id": search_id,
            "total_found": len(venues),
            "truncated": is_truncated,
        })

    except Exception as e:
        logger.error(f"Search error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/email/generate", methods=["POST"])
def generate_email():
    """Generate outreach email for a venue (premium feature)."""
    data = request.get_json()
    user_id = data.get("telegram_id")

    # TODO: verify premium tier or deduct email credit
    # For now: allow all, mark as TODO
    # STUB: check payment

    venue = data.get("venue", {})
    event_details = {
        "query": data.get("query", ""),
        "date": data.get("date", "flexible"),
        "guests": data.get("guests", "TBD"),
        "budget": data.get("budget", "TBD"),
    }

    email_body = generate_outreach_email(venue, event_details)
    subject = f"Event Inquiry — {venue.get('name', 'Your Venue')}"

    return jsonify({"subject": subject, "body": email_body})


@app.route("/api/call/initiate", methods=["POST"])
def initiate_call():
    """Initiate a Twilio call to a venue (premium feature)."""
    # TODO: implement when Twilio credentials are provided
    # STUB: returns placeholder response
    data = request.get_json()
    return jsonify({
        "ok": False,
        "stub": True,
        "message": "Voice calling coming soon. Twilio credentials pending.",
        "venue": data.get("venue_name", ""),
        "phone": data.get("venue_phone", ""),
    })


@app.route("/api/payment/verify", methods=["POST"])
def verify_payment():
    """Verify TON payment and upgrade user tier."""
    # TODO: implement TON transaction verification
    # STUB: returns placeholder
    data = request.get_json()
    tx_hash = data.get("tx_hash", "")
    payment_type = data.get("type", "subscription_monthly")
    user_id = data.get("telegram_id")

    return jsonify({
        "ok": False,
        "stub": True,
        "message": "TON payment verification coming soon.",
        "tx_hash": tx_hash,
        "type": payment_type,
    })


@app.route("/api/pricing")
def get_pricing():
    return jsonify(PRICING)



@app.route("/api/maps-embed")
def maps_embed():
    q = request.args.get("q", "")
    if not q or not GOOGLE_PLACES_API_KEY:
        return jsonify({"url": ""}), 400
    url = f"https://www.google.com/maps/embed/v1/search?key={GOOGLE_PLACES_API_KEY}&q={requests.utils.quote(q)}"
    return jsonify({"url": url})

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001, debug=False)
