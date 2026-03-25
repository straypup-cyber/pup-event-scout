#!/usr/bin/env python3
"""
Pup Event Scout - Web API
Serves /api/search for the website
"""

import os
import re
import json
import logging
import requests
from dotenv import load_dotenv
import anthropic
from flask import Flask, request, jsonify
from flask_cors import CORS

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
GOOGLE_PLACES_API_KEY = os.getenv("GOOGLE_PLACES_API_KEY")

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


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


def search_google_places(query: str, location: str) -> list[dict]:
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
        for place in data.get("results", [])[:5]:
            place_id = place.get("place_id")
            details = {}
            if place_id:
                det = requests.get(
                    "https://maps.googleapis.com/maps/api/place/details/json",
                    params={
                        "place_id": place_id,
                        "fields": "name,formatted_address,website,rating,user_ratings_total,price_level",
                        "key": GOOGLE_PLACES_API_KEY,
                    },
                    timeout=8
                )
                details = det.json().get("result", {})

            price_level = details.get("price_level")
            price_map = {0: "Free", 1: "Budget ($)", 2: "Moderate ($$)", 3: "Upscale ($$$)", 4: "Luxury ($$$$)"}

            results.append({
                "name": place.get("name", ""),
                "place_id": place_id or "",
                "address": details.get("formatted_address") or place.get("formatted_address", ""),
                "rating": place.get("rating", ""),
                "rating_count": details.get("user_ratings_total", ""),
                "website": details.get("website", ""),
                "price_level": price_map.get(price_level, "") if price_level is not None else "",
            })
    except Exception as e:
        logger.error(f"Google Places error: {e}")
    return results


@app.route("/api/search", methods=["POST"])
def search():
    data = request.get_json()
    query = data.get("query", "").strip()
    if not query:
        return jsonify({"error": "No query provided"}), 400

    try:
        # Parse request
        parse_resp = anthropic_client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=300,
            messages=[{"role": "user", "content": f"""Parse this venue request. Return ONLY valid JSON:

"{query}"

{{"location": "city", "capacity": "N people", "budget": "budget", "vibe": "keywords", "search_query": "2-4 word query"}}"""}]
        )
        try:
            text = re.sub(r"```(?:json)?\s*", "", parse_resp.content[0].text.strip()).rstrip("`")
            parsed = json.loads(text)
        except:
            parsed = {"location": "unknown", "search_query": "event venue", "capacity": "", "budget": "", "vibe": ""}

        location = parsed.get("location", "unknown")
        places = search_google_places(parsed.get("search_query", "event venue"), location)

        if not places:
            return jsonify({"venues": [], "error": "No venues found"}), 200

        # Analyze with Claude
        places_ctx = "\n".join([f"{i}: {p['name']} | {p['address']} | {p.get('rating','')}" for i, p in enumerate(places)])
        analysis_resp = anthropic_client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1000,
            messages=[{"role": "user", "content": f"""Analyze venues for: "{query}"
Capacity: {parsed.get('capacity')}, Budget: {parsed.get('budget')}, Vibe: {parsed.get('vibe')}

Venues:
{places_ctx}

Return ONLY JSON array, top 3-4 venues:
[{{"index":0,"why":"one sentence","capacity_estimate":"X-Y people","vibe_match":"short label","estimated_budget":"$X,000-Y,000"}}]"""}]
        )

        try:
            atxt = re.sub(r"```(?:json)?\s*", "", analysis_resp.content[0].text.strip()).rstrip("`")
            analyses = json.loads(atxt)
        except:
            analyses = [{"index": i, "why": "", "capacity_estimate": "", "vibe_match": "", "estimated_budget": ""} for i in range(min(3, len(places)))]

        venues = []
        for a in analyses:
            idx = a.get("index", 0)
            if idx < len(places):
                p = places[idx]
                photos = get_place_photos(p["place_id"], max_photos=2)
                venues.append({
                    "name": p["name"],
                    "address": p["address"],
                    "rating": p["rating"],
                    "rating_count": p["rating_count"],
                    "website": p["website"],
                    "price_level": p["price_level"],
                    "photos": photos,
                    "why": a.get("why", ""),
                    "capacity": a.get("capacity_estimate", ""),
                    "vibe": a.get("vibe_match", ""),
                    "budget": a.get("estimated_budget", ""),
                })

        return jsonify({"venues": venues, "location": location})

    except Exception as e:
        logger.error(f"Search error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001, debug=False)
