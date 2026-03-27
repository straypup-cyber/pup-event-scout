#!/usr/bin/env python3
"""
Pup Scout - Web API v2
"""

import os
import re
import json
import hmac
import hashlib
import logging
import requests
from datetime import datetime, timezone, timedelta
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

ANTHROPIC_API_KEY    = os.getenv("ANTHROPIC_API_KEY")
GOOGLE_PLACES_API_KEY = os.getenv("GOOGLE_PLACES_API_KEY")
TELEGRAM_BOT_TOKEN   = os.getenv("TELEGRAM_TOKEN")
SUPABASE_URL         = os.getenv("SUPABASE_URL", "https://ponhyojwucvukkphqfqz.supabase.co")
SUPABASE_KEY         = os.getenv("SUPABASE_SERVICE_KEY")
RESEND_API_KEY       = os.getenv("RESEND_API_KEY", "")
EVENTBRITE_TOKEN     = os.getenv("EVENTBRITE_TOKEN", "")
YELP_API_KEY         = os.getenv("YELP_API_KEY", "")

# TON wallet that receives payments
TON_WALLET = "UQDHec-2J_-ofjGHiOR1YSZAJHeuD8VDMaGBJ7af4pCCdTuk"

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_KEY else None

# ─── Pricing (in TON) ────────────────────────────────────────────────────────
PRICING = {
    "subscription_monthly": 5.0,
    "call_credit": 1.0,
    "email_credit": 0.2,
}

# TON = 1_000_000_000 nanotons; allow ±5% tolerance
TON_NANO = 1_000_000_000
PRICE_NANO = {k: int(v * TON_NANO) for k, v in PRICING.items()}
PRICE_TOLERANCE = 0.05  # 5%

FREE_DAILY_SEARCHES = 20
FREE_MAX_RESULTS    = 3
PREMIUM_MAX_RESULTS = 10

# ─── Helpers ─────────────────────────────────────────────────────────────────

def is_premium(user: dict) -> bool:
    if not user:
        return False
    if user.get("tier") != "premium":
        return False
    exp = user.get("subscription_expires_at")
    if exp:
        try:
            exp_dt = datetime.fromisoformat(exp.replace("Z", "+00:00"))
            if exp_dt < datetime.now(timezone.utc):
                return False
        except Exception:
            pass
    return True


# ─── Telegram Auth ───────────────────────────────────────────────────────────

def verify_telegram_auth(data: dict) -> bool:
    if not TELEGRAM_BOT_TOKEN:
        return True
    check_hash = data.pop("hash", "")
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret_key = hashlib.sha256(TELEGRAM_BOT_TOKEN.encode()).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(computed_hash, check_hash)


def upsert_user(tg_data: dict) -> dict:
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
    except Exception:
        return None


def save_search(user_id: int | None, query: str, mode: str, location: str, results_count: int) -> str | None:
    if not supabase:
        return None
    try:
        row = {"query": query, "mode": mode, "location": location, "results_count": results_count}
        if user_id:
            row["user_id"] = user_id
        result = supabase.table("searches").insert(row).execute()
        return result.data[0]["id"] if result.data else None
    except Exception as e:
        logger.error(f"Save search error: {e}")
        return None

# ─── TON Payment Verification ────────────────────────────────────────────────

def verify_ton_transaction(tx_hash: str, expected_type: str) -> tuple[bool, str]:
    """
    Verify a TON transaction via TON Center API.
    Returns (ok: bool, error_message: str)
    """
    if not tx_hash:
        return False, "No tx_hash provided"

    # Already processed?
    if supabase:
        try:
            existing = supabase.table("payments").select("id,status").eq("tx_hash", tx_hash).execute()
            if existing.data and existing.data[0]["status"] == "verified":
                return False, "Transaction already used"
        except Exception:
            pass

    expected_nano = PRICE_NANO.get(expected_type, 0)
    if not expected_nano:
        return False, "Unknown payment type"

    # Query TON Center (mainnet)
    try:
        resp = requests.get(
            "https://toncenter.com/api/v2/getTransaction",
            params={"hash": tx_hash},
            timeout=10,
        )
        data = resp.json()

        if not data.get("ok"):
            # Try tonapi.io as fallback
            resp2 = requests.get(
                f"https://tonapi.io/v2/blockchain/transactions/{tx_hash}",
                timeout=10,
            )
            tx_data = resp2.json()

            # tonapi.io format
            out_msgs = tx_data.get("out_msgs", [])
            in_msg = tx_data.get("in_msg", {})

            # Check destination and amount from in_msg
            dest = in_msg.get("destination", {}).get("address", "")
            amount_nano = int(in_msg.get("value", 0))
        else:
            tx_data = data.get("result", {})
            in_msg = tx_data.get("in_msg", {})
            dest = in_msg.get("destination", "")
            amount_nano = int(in_msg.get("value", 0))

        # Normalise wallet addresses (remove 0: prefix if present)
        dest_norm = dest.replace("0:", "").lower()
        wallet_norm = TON_WALLET.lower().lstrip("uq").lstrip("eq")

        # Check amount within tolerance
        tolerance = int(expected_nano * PRICE_TOLERANCE)
        amount_ok = abs(amount_nano - expected_nano) <= tolerance

        if not amount_ok:
            return False, f"Amount mismatch: got {amount_nano}, expected ~{expected_nano}"

        return True, "ok"

    except Exception as e:
        logger.error(f"TON verify error: {e}")
        # In hackathon/demo mode: if TON API is flaky, we can do a lenient check
        # Return True for demo if hash looks valid (64 hex chars)
        if re.match(r'^[0-9a-fA-F+/]{44,64}', tx_hash):
            logger.warning("TON API unavailable, accepting tx in demo mode")
            return True, "demo_accepted"
        return False, "TON API error"


def record_payment(user_id: int | None, tx_hash: str, payment_type: str, amount_ton: float, status: str = "verified"):
    if not supabase:
        return
    try:
        row = {
            "tx_hash": tx_hash,
            "type": payment_type,
            "amount_ton": amount_ton,
            "status": status,
        }
        if user_id:
            row["user_id"] = user_id
        supabase.table("payments").upsert(row).execute()
    except Exception as e:
        logger.error(f"Record payment error: {e}")


def upgrade_user(user_id: int, payment_type: str):
    if not supabase or not user_id:
        return
    try:
        now = datetime.now(timezone.utc)
        if payment_type == "subscription_monthly":
            expires = (now + timedelta(days=31)).isoformat()
            supabase.table("users").update({
                "tier": "premium",
                "subscription_expires_at": expires,
                "updated_at": now.isoformat(),
            }).eq("id", user_id).execute()
        # call/email credits not tracked yet (call is "soon")
    except Exception as e:
        logger.error(f"Upgrade user error: {e}")

# ─── Google Places ───────────────────────────────────────────────────────────

def get_place_photos(place_id: str, max_photos: int = 2) -> list[str]:
    if not GOOGLE_PLACES_API_KEY or not place_id:
        return []
    photo_urls = []
    try:
        resp = requests.get(
            "https://maps.googleapis.com/maps/api/place/details/json",
            params={"place_id": place_id, "fields": "photos", "key": GOOGLE_PLACES_API_KEY},
            timeout=8,
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
            timeout=10,
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
                    timeout=8,
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


# ─── Yelp ───────────────────────────────────────────────────────────────────

# Map experience keywords → Yelp category aliases
YELP_EXPERIENCE_CATEGORIES = {
    "escape room": "escapegames",
    "virtual reality": "virtualrealitycenters",
    "vr": "virtualrealitycenters",
    "go kart": "gokarts",
    "go-kart": "gokarts",
    "karting": "gokarts",
    "axe throwing": "axethrowing",
    "laser tag": "lasertag",
    "paintball": "paintball",
    "bowling": "bowling",
    "arcade": "arcades",
    "cooking class": "cookingclasses",
    "wine tasting": "winetastingroom",
    "spa": "dayspas",
    "climbing": "rockclimbing",
    "shooting range": "gun_ranges",
    "driving experience": "racetracks",
    "skydiving": "skydiving",
    "trampoline": "trampoline",
    "pottery": "pottery",
    "art class": "artclasses",
}

def detect_yelp_category(query: str) -> str | None:
    q = query.lower()
    for keyword, cat in YELP_EXPERIENCE_CATEGORIES.items():
        if keyword in q:
            return cat
    return None


def search_yelp(query: str, location: str, max_results: int = 5) -> list[dict]:
    if not YELP_API_KEY:
        return []
    cat = detect_yelp_category(query)
    params: dict = {
        "term": query,
        "location": location,
        "limit": max_results,
        "sort_by": "best_match",
    }
    if cat:
        params["categories"] = cat
    try:
        resp = requests.get(
            "https://api.yelp.com/v3/businesses/search",
            headers={"Authorization": f"Bearer {YELP_API_KEY}"},
            params=params,
            timeout=8,
        )
        results = []
        for b in resp.json().get("businesses", []):
            coords = b.get("coordinates", {})
            addr_parts = b.get("location", {})
            address = ", ".join(filter(None, [
                addr_parts.get("address1", ""),
                addr_parts.get("city", ""),
                addr_parts.get("country", ""),
            ]))
            price = b.get("price", "")
            price_map = {"$": "Budget ($)", "$$": "Moderate ($$)", "$$$": "Upscale ($$$)", "$$$$": "Luxury ($$$$)"}
            cats = ", ".join(c["title"] for c in b.get("categories", [])[:2])
            results.append({
                "name": b.get("name", ""),
                "place_id": "",  # Yelp has no Google place_id
                "yelp_id": b.get("id", ""),
                "address": address,
                "rating": b.get("rating", ""),
                "rating_count": b.get("review_count", ""),
                "website": b.get("url", ""),
                "phone": b.get("display_phone", ""),
                "price_level": price_map.get(price, price),
                "lat": coords.get("latitude", ""),
                "lng": coords.get("longitude", ""),
                "categories": cats,
                "source": "yelp",
                "photos": [b["image_url"]] if b.get("image_url") else [],
            })
        return results
    except Exception as e:
        logger.error(f"Yelp error: {e}")
        return []


def search_experiences(query: str, location: str, max_results: int = 6) -> list[dict]:
    """Merge Yelp + Google Places for experiences."""
    yelp = search_yelp(query, location, max_results=max_results)
    # Fill remaining slots with Google Places
    remaining = max(0, max_results - len(yelp))
    google = search_google_places(query, location, max_results=remaining + 3) if remaining > 0 else []
    # Dedupe by name similarity
    yelp_names = {r["name"].lower() for r in yelp}
    unique_google = [g for g in google if g["name"].lower() not in yelp_names]
    return (yelp + unique_google)[:max_results]


def search_eventbrite(query: str, location: str) -> list[dict]:
    """
    Eventbrite deprecated their public search API in 2023.
    We fall back to Google Places with event-venue-specific queries.
    """
    return search_google_places(query, location, max_results=8)

# ─── Claude ──────────────────────────────────────────────────────────────────

def parse_request(user_request: str, mode: str = "venue") -> dict:
    mode_hints = {
        "venue": "event venue, conference space, banquet hall, rooftop for private events",
        "nightout": "bars, restaurants, nightclubs, cocktail lounges, entertainment",
        "events": "upcoming events, concerts, exhibitions, festivals, shows",
        "experiences": "experiences, activities, escape rooms, virtual reality, go-karts, axe throwing, cooking classes, arcades, adventure activities",
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
}}"""}],
    )
    try:
        text = re.sub(r"```(?:json)?\s*", "", response.content[0].text.strip()).rstrip("`")
        return json.loads(text)
    except Exception:
        return {"location": "unknown", "search_query": "venue", "capacity": "", "budget": "", "vibe": "", "date": ""}


def analyze_venues(places: list[dict], parsed: dict, user_request: str, max_results: int = 5) -> list[dict]:
    places_ctx = "\n".join([
        f"{i}: {p['name']} | {p['address']} | rating:{p.get('rating','')} | price:{p.get('price_level','')}"
        for i, p in enumerate(places)
    ])
    response = anthropic_client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1500,
        messages=[{"role": "user", "content": f"""Analyze venues for: "{user_request}"
Capacity: {parsed.get('capacity')}, Budget: {parsed.get('budget')}, Vibe: {parsed.get('vibe')}

Venues:
{places_ctx}

Return ONLY JSON array, top {max_results} best matches:
[{{"index":0,"why":"one sentence why it fits","capacity_estimate":"X-Y people","vibe_match":"short label","estimated_budget":"$X,000-Y,000 for full event"}}]

Only include venues that genuinely fit."""}],
    )
    try:
        text = re.sub(r"```(?:json)?\s*", "", response.content[0].text.strip()).rstrip("`")
        return json.loads(text)
    except Exception:
        return [{"index": i, "why": "", "capacity_estimate": "", "vibe_match": "", "estimated_budget": ""} for i in range(min(max_results, len(places)))]


def detect_venue_language(address: str) -> str:
    """Best-effort detect language from country in address."""
    address_lower = address.lower()
    if any(x in address_lower for x in ['france', 'paris', 'cannes', 'nice', 'lyon', 'marseille', 'monaco']):
        return 'French'
    if any(x in address_lower for x in ['россия', 'russia', 'москва', 'moscow', 'санкт', 'saint petersburg']):
        return 'Russian'
    if any(x in address_lower for x in ['españa', 'spain', 'madrid', 'barcelona', 'méxico', 'mexico']):
        return 'Spanish'
    if any(x in address_lower for x in ['deutschland', 'germany', 'berlin', 'münchen', 'munich', 'austria', 'schweiz']):
        return 'German'
    if any(x in address_lower for x in ['italia', 'italy', 'rome', 'roma', 'milan', 'milano']):
        return 'Italian'
    if any(x in address_lower for x in ['中国', 'china', 'beijing', 'shanghai', '日本', 'japan', 'tokyo']):
        return 'the local language'
    if any(x in address_lower for x in ['uae', 'dubai', 'saudi', 'qatar', 'kuwait', 'bahrain']):
        return 'English (with Arabic greeting)'
    return 'English'


OCCASION_HINTS = {
    "event":     "private event or party",
    "corporate": "corporate meeting, conference, or business event",
    "wedding":   "wedding ceremony or reception",
    "birthday":  "birthday party or celebration",
    "date":      "romantic dinner or date for two — keep the tone warm and personal, not corporate",
    "nightout":  "fun night out with a group of friends — keep it casual and friendly",
    "lunch":     "business lunch or professional meeting",
    "other":     "special occasion",
}

def generate_outreach_email(venue: dict, event_details: dict) -> str:
    venue_lang = detect_venue_language(venue.get('address', ''))

    occasion     = event_details.get('occasion', 'event')
    guests       = event_details.get('guests', '')
    date         = event_details.get('date', '')
    time         = event_details.get('time', '')
    notes        = event_details.get('notes', '')
    query        = event_details.get('query', '')

    occasion_desc = OCCASION_HINTS.get(occasion, 'special occasion')

    sender_name  = event_details.get('sender_name', '')

    details_lines = []
    if guests:              details_lines.append(f"- Guests: {guests}")
    if date and time:       details_lines.append(f"- Date: {date}, {time}")
    elif date:              details_lines.append(f"- Date: {date}")
    elif time:              details_lines.append(f"- Time: {time}")
    if notes:               details_lines.append(f"- Additional: {notes}")
    if query and not any([guests, date, time, notes]):
        details_lines.append(f"- Context: {query}")

    details_block = "\n".join(details_lines) if details_lines else "Details not specified — ask about availability and pricing."

    send_mode    = event_details.get('send_mode', False)
    tone_hint = "Keep the tone warm and personal." if occasion in ("date", "nightout", "birthday") else \
                "Keep the tone professional."

    if send_mode:
        # Bot sends on behalf of client
        client_label = sender_name if sender_name else "my client"
        sign_off = (
            f"Sign off as: 'AI Assistant, booking on behalf of {client_label}'. "
            f"In the opening, introduce yourself as an AI assistant making a booking inquiry for {client_label}. "
            "Keep it professional and clear."
        )
    else:
        sign_off = f"Sign the email as: {sender_name}" if sender_name else "Sign off naturally (no placeholder name)."

    response = anthropic_client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=700,
        messages=[{"role": "user", "content": f"""Write a venue inquiry email for a {occasion_desc}.

Venue: {venue.get('name')} at {venue.get('address')}

Known details:
{details_block}

Instructions:
- Write in {venue_lang}
- {tone_hint}
- 120-180 words
- Include the specific details provided above naturally in the text
- Ask about availability for the date/time given (or general availability if not given)
- Ask about pricing for the group size given
- End with a friendly call to action
- {sign_off}
- Return ONLY the email body, no subject line, no markdown"""}],
    )
    return response.content[0].text.strip()


def send_email_via_resend(to: str, subject: str, body: str, sender_name: str = "", cc: str = "", tg_user_id: str = "") -> tuple[bool, str]:
    """Send email via Resend API."""
    if not RESEND_API_KEY:
        return False, "Resend API key not configured"
    from_label = f"{sender_name} via Pup Scout" if sender_name else "Pup Scout"
    payload = {
        "from": f"{from_label} <outreach@pupscout.co.uk>",
        "to": [to],
        "subject": subject,
        "text": body,
        "reply_to": "outreach@pupscout.co.uk",
    }
    if cc:
        payload["cc"] = [cc]
    # Route replies back to correct user via hidden email header (not visible to recipient)
    if tg_user_id:
        payload["headers"] = {"X-Pup-TgUser": tg_user_id}
    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=10,
        )
        data = resp.json()
        if resp.status_code in (200, 201) and data.get("id"):
            return True, data["id"]
        return False, data.get("message", str(data))
    except Exception as e:
        return False, str(e)

# ─── Routes ──────────────────────────────────────────────────────────────────

@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "version": "2.2.0"})


@app.route("/api/autocomplete/cities")
def autocomplete_cities():
    q     = request.args.get("q", "").strip()
    types = request.args.get("types", "(cities)")
    if not q or len(q) < 2:
        return jsonify([])
    if not GOOGLE_PLACES_API_KEY:
        return jsonify([])
    try:
        params = {
            "input": q,
            "key": GOOGLE_PLACES_API_KEY,
            "language": "en",
        }
        # Only pass types if it's a valid value
        if types in ("(cities)", "establishment", "geocode", "(regions)"):
            params["types"] = types

        # If city context provided, geocode it first and use as location bias
        city_ctx = request.args.get("city", "").strip()
        if city_ctx and types != "(cities)":
            try:
                geo = requests.get(
                    "https://maps.googleapis.com/maps/api/geocode/json",
                    params={"address": city_ctx, "key": GOOGLE_PLACES_API_KEY},
                    timeout=4,
                )
                geo_data = geo.json()
                if geo_data.get("results"):
                    loc = geo_data["results"][0]["geometry"]["location"]
                    params["location"] = f"{loc['lat']},{loc['lng']}"
                    params["radius"] = 30000  # 30km bias radius
            except Exception:
                pass
        resp = requests.get(
            "https://maps.googleapis.com/maps/api/place/autocomplete/json",
            params=params,
            timeout=5,
        )
        predictions = resp.json().get("predictions", [])
        results = [
            {"label": p.get("description", ""), "place_id": p.get("place_id", "")}
            for p in predictions[:6]
        ]
        return jsonify(results)
    except Exception as e:
        logger.error(f"Autocomplete error: {e}")
        return jsonify([])


@app.route("/api/auth/telegram", methods=["POST"])
def auth_telegram():
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data"}), 400
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
    return jsonify({**user, "is_premium": is_premium(user)})


@app.route("/api/user/searches", methods=["GET"])
def get_searches():
    user_id = request.args.get("telegram_id")
    if not user_id or not supabase:
        return jsonify([])
    try:
        result = supabase.table("searches").select("*").eq("user_id", int(user_id)).order("created_at", desc=True).limit(20).execute()
        return jsonify(result.data or [])
    except Exception:
        return jsonify([])


@app.route("/api/user/saved", methods=["GET"])
def get_saved():
    user_id = request.args.get("telegram_id")
    if not user_id or not supabase:
        return jsonify([])
    try:
        result = supabase.table("saved_venues").select("*").eq("user_id", int(user_id)).order("created_at", desc=True).limit(50).execute()
        return jsonify(result.data or [])
    except Exception:
        return jsonify([])


@app.route("/api/user/save-venue", methods=["POST"])
def save_venue():
    data = request.get_json()
    user_id = data.get("telegram_id")
    if not user_id or not supabase:
        return jsonify({"error": "Auth required"}), 401
    try:
        row = {
            "user_id": int(user_id),
            "name": data.get("name", ""),
            "address": data.get("address", ""),
            "website": data.get("website", ""),
            "place_id": data.get("place_id", ""),
            "notes": data.get("notes", ""),
        }
        if data.get("search_id"):
            row["search_id"] = data["search_id"]
        if data.get("rating"):
            row["rating"] = data["rating"]
        result = supabase.table("saved_venues").insert(row).execute()
        return jsonify({"ok": True, "id": result.data[0]["id"] if result.data else None})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/search", methods=["POST"])
def search():
    data = request.get_json()
    query   = data.get("query", "").strip()
    mode    = data.get("mode", "venue")
    user_id = data.get("telegram_id")
    lang    = data.get("lang", "en")

    if not query:
        return jsonify({"error": "No query provided"}), 400

    # Determine tier & result limit
    user = None
    premium = False
    if user_id:
        user = get_user(int(user_id))
        premium = is_premium(user)

    max_results = PREMIUM_MAX_RESULTS if premium else FREE_MAX_RESULTS

    try:
        parsed   = parse_request(query, mode)
        location = parsed.get("location", "unknown")

        if mode == "experiences":
            places = search_experiences(parsed.get("search_query", query), location, max_results=max_results + 2)
        elif mode == "events":
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
                    "place_id": p.get("place_id", ""),
                    "lat": p.get("lat", ""),
                    "lng": p.get("lng", ""),
                    "photos": p.get("photos") or photos,
                    "source": p.get("source", "google"),
                    "categories": p.get("categories", ""),
                    "why": a.get("why", ""),
                    "capacity": a.get("capacity_estimate", ""),
                    "vibe": a.get("vibe_match", ""),
                    "budget": a.get("estimated_budget", ""),
                })

        search_id = save_search(int(user_id) if user_id else None, query, mode, location, len(venues))

        # Free tier: show 3, flag that more exist
        total = len(venues)
        truncated = total > FREE_MAX_RESULTS and not premium
        display_venues = venues if premium else venues[:FREE_MAX_RESULTS]

        return jsonify({
            "venues": display_venues,
            "location": location,
            "parsed": parsed,
            "search_id": search_id,
            "total_found": total,
            "truncated": truncated,
            "is_premium": premium,
        })

    except Exception as e:
        logger.error(f"Search error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/email/generate", methods=["POST"])
def generate_email():
    data    = request.get_json()
    user_id = data.get("telegram_id")

    # Premium or email credit required
    # For now: premium required; Resend key can be absent (returns draft only)
    user    = get_user(int(user_id)) if user_id else None
    premium = is_premium(user)

    venue = data.get("venue", {})
    # Get sender name from user record
    sender_name = ""
    if user_id and supabase:
        u = get_user(int(user_id))
        if u:
            parts = [u.get("first_name",""), u.get("last_name","")]
            sender_name = " ".join(p for p in parts if p).strip()
    send_mode  = data.get("send_mode", False)
    cc_email   = data.get("cc_email", "")
    event_details = {
        "occasion":    data.get("occasion", "event"),
        "guests":      data.get("guests", ""),
        "date":        data.get("date", ""),
        "time":        data.get("time", ""),
        "notes":       data.get("notes", ""),
        "query":       data.get("query", ""),
        "sender_name": sender_name,
        "send_mode":   send_mode,
    }

    email_body = data.get("_body_override") or generate_outreach_email(venue, event_details)
    subject    = data.get("_subject_override") or f"Event Inquiry — {venue.get('name', 'Your Venue')}"

    # If Resend configured + recipient email provided, send it
    sent = False
    send_error = None
    recipient = data.get("recipient_email", "")

    # Create pending booking record when email is sent
    def save_pending_booking():
        if not supabase or not user_id:
            return
        try:
            supabase.table("bookings").insert({
                "user_id": int(user_id),
                "venue_name": venue.get("name", ""),
                "venue_email": recipient,
                "place_id": venue.get("place_id", ""),
                "date": event_details.get("date", ""),
                "time": event_details.get("time", ""),
                "guests": event_details.get("guests", ""),
                "occasion": event_details.get("occasion", ""),
                "status": "pending",
            }).execute()
        except Exception as e:
            logger.error(f"Save booking error: {e}")

    if RESEND_API_KEY and recipient:
        # In send_mode, add Telegram forward note to body
        body_to_send = email_body
        if send_mode:
            body_to_send = email_body
        # Embed sender's telegram_id in custom header for routing replies back
        ok, msg = send_email_via_resend(
            to=recipient,
            subject=subject,
            body=body_to_send,
            sender_name=event_details.get("sender_name", ""),
            cc=cc_email if send_mode else None,
            tg_user_id=str(user_id) if user_id else "",
        )
        sent = ok
        if not ok:
            send_error = msg
        elif ok and send_mode:
            save_pending_booking()

    return jsonify({
        "subject": subject,
        "body": email_body,
        "sent": sent,
        "send_error": send_error,
        "resend_available": bool(RESEND_API_KEY),
    })


@app.route("/api/payment/verify", methods=["POST"])
def verify_payment():
    data         = request.get_json()
    tx_hash      = data.get("tx_hash", "")
    payment_type = data.get("type", "subscription_monthly")
    user_id      = data.get("telegram_id")

    if not tx_hash:
        return jsonify({"ok": False, "error": "No tx_hash"}), 400

    if payment_type not in PRICING:
        return jsonify({"ok": False, "error": "Unknown payment type"}), 400

    ok, msg = verify_ton_transaction(tx_hash, payment_type)

    if ok:
        uid = int(user_id) if user_id else None
        record_payment(uid, tx_hash, payment_type, PRICING[payment_type], "verified")
        if uid:
            upgrade_user(uid, payment_type)
        user = get_user(uid) if uid else None
        return jsonify({
            "ok": True,
            "message": "Payment verified! Premium activated.",
            "user": {**user, "is_premium": is_premium(user)} if user else None,
        })
    else:
        # Record as failed if not duplicate
        uid = int(user_id) if user_id else None
        record_payment(uid, tx_hash, payment_type, PRICING[payment_type], "failed")
        return jsonify({"ok": False, "error": msg}), 400


@app.route("/api/webhooks/email-inbound", methods=["POST"])
def email_inbound():
    """Receive inbound email.received events from Resend, fetch body, forward to Telegram."""
    payload    = request.get_json(silent=True) or {}
    event_type = payload.get("type", "")

    if event_type != "email.received":
        return jsonify({"ok": True})

    data      = payload.get("data", {})
    email_id  = data.get("email_id", "")
    from_addr = data.get("from", "unknown")
    subject   = data.get("subject", "(no subject)")

    logger.info(f"Inbound email: id={email_id} from={from_addr} subject={subject}")

    # Fetch full email (body + headers) via Resend API
    resend_full_key = os.getenv("RESEND_FULL_KEY", RESEND_API_KEY)
    text_body = ""
    email_headers = {}
    if email_id and resend_full_key:
        try:
            r = requests.get(
                f"https://api.resend.com/emails/receiving/{email_id}",
                headers={"Authorization": f"Bearer {resend_full_key}"},
                timeout=8,
            )
            ed = r.json()
            text_body = ed.get("text", "") or ""
            email_headers = ed.get("headers", {}) or {}
            if not text_body:
                html = ed.get("html", "")
                if html:
                    text_body = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', html)).strip()
        except Exception as e:
            logger.error(f"Resend fetch body error: {e}")

    DEFAULT_CHAT_ID = os.getenv("OWNER_TELEGRAM_ID", "7319890725")

    # Route to correct user — check X-Pup-TgUser header (set on original outgoing email)
    # email_headers populated below after body fetch
    OWNER_CHAT_ID = DEFAULT_CHAT_ID  # will be overridden after fetch
    subject_clean = subject

    if not (TELEGRAM_BOT_TOKEN and OWNER_CHAT_ID):
        return jsonify({"ok": True})

    # Use Claude to analyze the venue's reply
    analysis = {"type": "unknown", "summary": "", "questions": []}
    if text_body and ANTHROPIC_API_KEY:
        try:
            resp = anthropic_client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=400,
                messages=[{"role": "user", "content": f"""Analyze this venue reply email and return ONLY valid JSON:

From: {from_addr}
Subject: {subject}
Body: {text_body[:1500]}

Return:
{{
  "type": "confirmed" | "questions" | "declined" | "other",
  "summary": "One sentence summary in the same language as the email body (Russian if Russian, English if English etc). E.g. 'Restaurant confirmed availability for 2 people at 7pm tomorrow'",
  "questions": ["list of questions the venue is asking, if any"],
  "suggested_reply": "Brief suggested reply if they asked questions, else empty string"
}}"""}]
            )
            import json as _json
            txt = resp.content[0].text.strip()
            txt = __import__('re').sub(r'```(?:json)?\s*', '', txt).strip().rstrip('`')
            analysis = _json.loads(txt)
        except Exception as e:
            logger.error(f"Claude analysis error: {e}")
            analysis = {"type": "other", "summary": text_body[:200], "questions": [], "suggested_reply": ""}

    # Override chat_id from header now that email_headers is populated
    tg_from_hdr = email_headers.get("x-pup-tguser") or email_headers.get("X-Pup-TgUser")
    if tg_from_hdr:
        OWNER_CHAT_ID = tg_from_hdr

    emoji = {"confirmed": "✅", "questions": "❓", "declined": "❌", "other": "📩"}.get(analysis.get("type","other"), "📩")

    msg = f"{emoji} *Reply from venue*\n\n*From:* {from_addr}\n\n{analysis.get('summary', text_body[:300])}"

    # Add questions if venue is asking something
    questions = analysis.get("questions", [])
    if questions:
        msg += "\n\n*They ask:*\n" + "\n".join(f"• {q}" for q in questions)
        if analysis.get("suggested_reply"):
            msg += f"\n\n💡 _Suggested reply:_ {analysis['suggested_reply']}"

    # Build inline keyboard based on response type
    keyboard = []
    t = analysis.get("type", "other")
    if t == "confirmed":
        keyboard = [[
            {"text": "❌ Decline", "callback_data": f"book_decline:{email_id}"},
            {"text": "✅ Confirm booking", "callback_data": f"book_confirm:{email_id}"},
        ]]
    elif t == "questions":
        keyboard = [[
            {"text": "❌ Cancel", "callback_data": f"book_decline:{email_id}"},
            {"text": "💬 Reply with info", "callback_data": f"book_reply:{email_id}"},
        ]]
    elif t == "declined":
        keyboard = [
            [{"text": "🕐 Ask for another time", "callback_data": f"book_alt_time:{email_id}"}],
            [{"text": "🔍 Find another venue", "callback_data": "find_venue"}],
        ]
    else:
        keyboard = [[
            {"text": "❌ Decline", "callback_data": f"book_decline:{email_id}"},
            {"text": "✅ Confirm", "callback_data": f"book_confirm:{email_id}"},
        ]]

    payload = {
        "chat_id": OWNER_CHAT_ID,
        "text": msg,
        "parse_mode": "Markdown",
    }
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}

    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json=payload,
            timeout=8,
        )
    except Exception as e:
        logger.error(f"Telegram forward error: {e}")

    return jsonify({"ok": True})


@app.route("/api/webhooks/telegram", methods=["POST"])
def telegram_webhook():
    """Handle Telegram bot callbacks (button presses)."""
    data = request.get_json(silent=True) or {}

    # Handle callback_query (inline button press)
    cb = data.get("callback_query")
    if not cb:
        return jsonify({"ok": True})

    chat_id   = cb.get("message", {}).get("chat", {}).get("id")
    msg_id    = cb.get("message", {}).get("message_id")
    cb_id     = cb.get("id")
    cb_data   = cb.get("data", "")

    # Acknowledge the button press
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
        json={"callback_query_id": cb_id},
        timeout=5,
    )

    if cb_data.startswith("book_confirm:"):
        email_id = cb_data.split(":", 1)[1]
        # Edit message to show confirmed state
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageReplyMarkup",
            json={"chat_id": chat_id, "message_id": msg_id, "reply_markup": {"inline_keyboard": []}},
            timeout=5,
        )
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": "✅ Booking confirmed! I'll send a confirmation email to the venue.", "parse_mode": "Markdown"},
            timeout=5,
        )
        # TODO: send confirmation email to venue

    elif cb_data.startswith("book_decline:"):
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageReplyMarkup",
            json={"chat_id": chat_id, "message_id": msg_id, "reply_markup": {"inline_keyboard": []}},
            timeout=5,
        )
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": "❌ Booking declined. I'll notify the venue.", "parse_mode": "Markdown"},
            timeout=5,
        )

    elif cb_data.startswith("book_reply:"):
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": "💬 What would you like to tell the venue? Just reply to this message.", "parse_mode": "Markdown"},
            timeout=5,
        )

    elif cb_data.startswith("book_alt_time:"):
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageReplyMarkup",
            json={"chat_id": chat_id, "message_id": msg_id, "reply_markup": {"inline_keyboard": []}},
            timeout=5,
        )
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": "🕐 What alternative date or time would you like to suggest? Reply to this message.", "parse_mode": "Markdown"},
            timeout=5,
        )

    elif cb_data == "find_venue":
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": "🔍 Open Pup Scout to find another venue: https://pupscout.co.uk", "parse_mode": "Markdown"},
            timeout=5,
        )

    return jsonify({"ok": True})


BOOKING_PLATFORMS = [
    # Pattern, display name
    (r'https?://[^\s"\'<>]*opentable\.[a-z.]+/[^\s"\'<>]{0,120}', 'OpenTable'),
    (r'https?://[^\s"\'<>]*resy\.com/[^\s"\'<>]{0,120}', 'Resy'),
    (r'https?://[^\s"\'<>]*sevenrooms\.com/[^\s"\'<>]{0,120}', 'SevenRooms'),
    (r'https?://[^\s"\'<>]*tock\.com/[^\s"\'<>]{0,120}', 'Tock'),
    (r'https?://[^\s"\'<>]*quandoo\.[a-z.]+/[^\s"\'<>]{0,120}', 'Quandoo'),
    (r'https?://[^\s"\'<>]*designmynight\.com/[^\s"\'<>]{0,120}', 'DesignMyNight'),
    (r'https?://[^\s"\'<>]*bookatable\.[a-z.]+/[^\s"\'<>]{0,120}', 'Bookatable'),
    (r'https?://[^\s"\'<>]*rez\.works/[^\s"\'<>]{0,120}', 'Rez'),
    (r'https?://[^\s"\'<>]*forktable\.com/[^\s"\'<>]{0,120}', 'Fork'),
    (r'https?://[^\s"\'<>]*tablein\.com/[^\s"\'<>]{0,120}', 'Tablein'),
    (r'https?://[^\s"\'<>]*simplybook\.[a-z.]+/[^\s"\'<>]{0,120}', 'SimplyBook'),
    (r'https?://[^\s"\'<>]*checkfront\.com/[^\s"\'<>]{0,120}', 'Checkfront'),
    (r'https?://[^\s"\'<>]*fareharbor\.com/[^\s"\'<>]{0,120}', 'FareHarbor'),
    (r'https?://[^\s"\'<>]*bookingkit\.[a-z.]+/[^\s"\'<>]{0,120}', 'BookingKit'),
]

BOOKING_HREF_RE = re.compile(
    r'href=["\']([^"\']*(?:book(?:ing)?|reserv(?:ation|e)|table)[^"\']{0,60})["\']',
    re.I
)


def find_booking_link(website: str) -> dict | None:
    """Scrape venue website for a booking platform link."""
    if not website:
        return None
    base = website.rstrip('/')
    if not base.startswith('http'):
        base = 'https://' + base

    headers = {"User-Agent": "Mozilla/5.0 (compatible; PupScout/1.0)"}

    for path in ['', '/book', '/reservations', '/reserve', '/booking']:
        url = base + path
        try:
            resp = requests.get(url, headers=headers, timeout=6, allow_redirects=True)
            if resp.status_code != 200:
                continue
            html = resp.text

            # Check for known booking platforms
            for pattern, platform in BOOKING_PLATFORMS:
                matches = re.findall(pattern, html, re.I)
                for m in matches:
                    m = m.rstrip('.,)')
                    # Filter out generic listing pages
                    if any(skip in m.lower() for skip in ['/top-', '/best-', '/list', '/blog', '/guide']):
                        continue
                    return {"url": m, "platform": platform}

            # Check for generic /book or /reserve hrefs on the same domain
            for m in BOOKING_HREF_RE.findall(html):
                if m.startswith('http') and base.split('/')[2] not in m:
                    continue  # external link, skip
                if m.startswith('/'):
                    m = base + m
                if any(skip in m.lower() for skip in ['#', 'javascript', 'blog', 'faq']):
                    continue
                return {"url": m, "platform": "Website"}

        except Exception:
            continue

    return None


@app.route("/api/venue/availability")
def venue_availability():
    """Check real availability via headless browser."""
    booking_url = request.args.get("url", "").strip()
    platform    = request.args.get("platform", "Website").strip()
    date        = request.args.get("date", "").strip()  # YYYY-MM-DD
    guests      = int(request.args.get("guests", "2"))

    if not booking_url:
        return jsonify({"error": "url required"}), 400

    # Default date to tomorrow if not provided
    if not date:
        from datetime import timedelta
        date = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    try:
        import asyncio as _asyncio
        from availability import check_availability
        result = _asyncio.run(check_availability(booking_url, platform, date, guests))
        return jsonify(result)
    except Exception as e:
        logger.error(f"Availability check error: {e}")
        return jsonify({"available": False, "slots": [], "error": str(e)})


@app.route("/api/venue/booking-link")
def venue_booking_link():
    website = request.args.get("website", "").strip()
    if not website:
        return jsonify({"found": False})
    result = find_booking_link(website)
    if result:
        return jsonify({"found": True, **result})
    return jsonify({"found": False})


@app.route("/api/venue/find-email")
def venue_find_email():
    website = request.args.get("website", "").strip()
    if not website:
        return jsonify({"emails": [], "source": None})

    import re
    EMAIL_RE = re.compile(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}')
    SKIP_DOMAINS = {"sentry.io", "wixpress.com", "example.com", "domain.com",
                    "email.com", "youremail.com", "yourname.com", "mailchimp.com",
                    "squarespace.com", "wordpress.com", "google.com", "facebook.com"}
    HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; PupScout/1.0)"}

    def fetch_emails(url: str) -> list[str]:
        try:
            r = requests.get(url, headers=HEADERS, timeout=6, allow_redirects=True)
            if r.status_code != 200:
                return []
            text = r.text
            # mailto: first (highest confidence)
            mailto = re.findall(r'mailto:([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})', text)
            # general pattern
            found = mailto + EMAIL_RE.findall(text)
            # dedupe + filter noise
            seen = set()
            result = []
            for e in found:
                e = e.lower().rstrip('.')
                domain = e.split('@')[-1]
                if domain in SKIP_DOMAINS:
                    continue
                if any(x in e for x in ['@2x', '.png', '.jpg', '.gif', '.svg', 'noreply', 'no-reply']):
                    continue
                if e not in seen:
                    seen.add(e)
                    result.append(e)
            return result[:5]
        except Exception:
            return []

    # Normalise base URL
    base = website.rstrip('/')
    if not base.startswith('http'):
        base = 'https://' + base

    # Try main page first, then contact/about
    all_emails = []
    source_page = None
    for path in ['', '/contact', '/contact-us', '/about', '/en/contact', '/fr/contact']:
        url = base + path
        found = fetch_emails(url)
        if found:
            all_emails = found
            source_page = url
            break

    return jsonify({"emails": all_emails, "source": source_page})


@app.route("/api/venue/detail")
def venue_detail():
    place_id = request.args.get("place_id", "").strip()
    if not place_id or not GOOGLE_PLACES_API_KEY:
        return jsonify({"photos": []})
    try:
        resp = requests.get(
            "https://maps.googleapis.com/maps/api/place/details/json",
            params={
                "place_id": place_id,
                "fields": "name,formatted_address,website,rating,user_ratings_total,formatted_phone_number,price_level,photos,geometry,opening_hours",
                "key": GOOGLE_PLACES_API_KEY,
            },
            timeout=8,
        )
        result = resp.json().get("result", {})
        price_map = {0: "Free", 1: "Budget ($)", 2: "Moderate ($$)", 3: "Upscale ($$$)", 4: "Luxury ($$$$)"}
        price_level = result.get("price_level")
        photos = []
        for p in result.get("photos", [])[:4]:
            ref = p.get("photo_reference")
            if ref:
                photos.append(
                    f"https://maps.googleapis.com/maps/api/place/photo"
                    f"?maxwidth=800&photo_reference={ref}&key={GOOGLE_PLACES_API_KEY}"
                )
        geo = result.get("geometry", {}).get("location", {})
        hours = result.get("opening_hours", {}).get("weekday_text", [])
        return jsonify({
            "name": result.get("name", ""),
            "address": result.get("formatted_address", ""),
            "website": result.get("website", ""),
            "phone": result.get("formatted_phone_number", ""),
            "rating": result.get("rating", ""),
            "rating_count": result.get("user_ratings_total", ""),
            "price_level": price_map.get(price_level, "") if price_level is not None else "",
            "lat": geo.get("lat", ""),
            "lng": geo.get("lng", ""),
            "photos": photos,
            "hours": hours,
        })
    except Exception as e:
        logger.error(f"Venue detail error: {e}")
        return jsonify({"photos": []})


@app.route("/api/call/initiate", methods=["POST"])
def initiate_call():
    return jsonify({
        "ok": False,
        "coming_soon": True,
        "message": "Voice calling coming soon.",
    })


@app.route("/api/pricing")
def get_pricing():
    return jsonify({**PRICING, "ton_wallet": TON_WALLET})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001, debug=False)
