#!/usr/bin/env python3
"""
Pup Event Scout - Telegram Bot
AI-powered venue finder for events
"""

import os
import re
import json
import logging
import requests
from dotenv import load_dotenv
import anthropic
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ParseMode

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
GOOGLE_PLACES_API_KEY = os.getenv("GOOGLE_PLACES_API_KEY")

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

WELCOME_MESSAGE = """🐾 *Woof! Welcome to Pup Event Scout!*

I'm your AI-powered venue finder. Tell me what kind of event you're planning and I'll sniff out the perfect spots!

Just describe your needs in plain English:
• _"Find a venue in Cannes for 50 people, $5k budget, rooftop vibe"_
• _"Beachfront conference space in Nice, 100 attendees, elegant"_
• _"Intimate cocktail bar in Paris for 30 people, creative atmosphere"_

I'll search real venues and give you a curated shortlist. 🎯

Type /help for more info, or just tell me what you need!"""

HELP_MESSAGE = """🐾 *Pup Event Scout - Help*

*How to use:*
Send me a message describing your event needs:
• 📍 *City/Location* - Where?
• 👥 *Capacity* - How many people?
• 💰 *Budget* - Budget range?
• 🎨 *Vibe/Theme* - What atmosphere?

*Examples:*
• "Venue in Cannes for 50 people, $5k, rooftop"
• "Conference center in London for 200 people, professional"
• "Luxury beachfront event space in Monaco, 80 guests"

*Powered by:* OpenClaw + Anthropic Claude + Google Places"""


def search_google_places(query: str, location: str) -> list[dict]:
    """Search Google Places API for venues."""
    if not GOOGLE_PLACES_API_KEY:
        return []

    results = []
    try:
        # Text search
        url = "https://maps.googleapis.com/maps/api/place/textsearch/json"
        params = {
            "query": f"{query} in {location}",
            "key": GOOGLE_PLACES_API_KEY,
            "type": "establishment",
        }
        resp = requests.get(url, params=params, timeout=10)
        data = resp.json()

        for place in data.get("results", [])[:5]:
            place_id = place.get("place_id")
            details = {}

            # Get place details
            if place_id:
                det_url = "https://maps.googleapis.com/maps/api/place/details/json"
                det_params = {
                    "place_id": place_id,
                    "fields": "name,formatted_address,website,formatted_phone_number,rating,user_ratings_total,types",
                    "key": GOOGLE_PLACES_API_KEY,
                }
                det_resp = requests.get(det_url, params=det_params, timeout=10)
                details = det_resp.json().get("result", {})

            results.append({
                "name": place.get("name", ""),
                "address": details.get("formatted_address") or place.get("formatted_address", ""),
                "rating": place.get("rating", "N/A"),
                "website": details.get("website", ""),
                "types": ", ".join(place.get("types", [])[:3]),
            })

    except Exception as e:
        logger.error(f"Google Places error: {e}")

    return results


def parse_and_find_venues(user_request: str) -> str:
    """Use Claude to parse request, search Google Places, and return recommendations."""

    # Step 1: Parse the request with Claude
    parse_response = anthropic_client.messages.create(
        model="claude-haiku-3-5",
        max_tokens=400,
        messages=[{
            "role": "user",
            "content": f"""Parse this event venue request and extract key info. Return ONLY valid JSON, no markdown:

Request: "{user_request}"

{{
  "location": "city name",
  "capacity": "number or range",
  "budget": "budget info or unknown",
  "vibe": "atmosphere/theme keywords",
  "search_query": "short venue search query like: rooftop event space"
}}"""
        }]
    )

    try:
        parse_text = parse_response.content[0].text.strip()
        parse_text = re.sub(r"```(?:json)?\s*", "", parse_text).strip().rstrip("`")
        parsed = json.loads(parse_text)
    except Exception as e:
        logger.error(f"Parse error: {e}")
        parsed = {
            "location": "unknown",
            "capacity": "unknown",
            "budget": "unknown",
            "vibe": "event space",
            "search_query": "event venue"
        }

    location = parsed.get("location", "unknown")
    search_query = parsed.get("search_query", "event venue hall")

    # Step 2: Search Google Places
    places = search_google_places(search_query, location)

    # Format places data for Claude
    if places:
        places_context = "\n\n".join([
            f"Venue {i+1}:\n"
            f"Name: {p['name']}\n"
            f"Address: {p['address']}\n"
            f"Rating: {p['rating']}/5\n"
            f"Website: {p['website'] or 'not listed'}\n"
            f"Types: {p['types']}"
            for i, p in enumerate(places)
        ])
    else:
        places_context = "No Google Places results. Use your knowledge of venues in this location."

    # Step 3: Claude formats the final response
    final_response = anthropic_client.messages.create(
        model="claude-haiku-3-5",
        max_tokens=1500,
        messages=[{
            "role": "user",
            "content": f"""You are Pup Event Scout 🐾 - a friendly AI venue finder for event managers.

User request: "{user_request}"

Parsed: location={parsed.get('location')}, capacity={parsed.get('capacity')}, budget={parsed.get('budget')}, vibe={parsed.get('vibe')}

Google Places results:
{places_context}

Write a Telegram-formatted shortlist of 3-5 venues. Use *bold* and _italic_.

For each venue:
🏛 *Venue Name*
📍 _Address_
⭐ Rating (if available)
👥 Capacity estimate
✨ Vibe match
💡 Why it fits: 1 sentence
🔗 Website (if available)

Start with a 1-line intro, list venues, end with one helpful tip.
Be concise, friendly, and useful. If Google results are good, use them. Otherwise suggest real known venues."""
        }]
    )

    return final_response.content[0].text


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(WELCOME_MESSAGE, parse_mode=ParseMode.MARKDOWN)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_MESSAGE, parse_mode=ParseMode.MARKDOWN)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_message = update.message.text
    user_name = update.effective_user.first_name or "there"

    thinking_msg = await update.message.reply_text(
        f"🐾 On it, {user_name}! Sniffing out venues...",
    )

    try:
        response = parse_and_find_venues(user_message)
        await thinking_msg.delete()
        await update.message.reply_text(response, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Error: {e}")
        await thinking_msg.edit_text(
            "🐾 Hit a snag! Try again with location, capacity and vibe details."
        )


def main() -> None:
    if not TELEGRAM_TOKEN:
        raise ValueError("TELEGRAM_TOKEN not set!")
    if not ANTHROPIC_API_KEY:
        raise ValueError("ANTHROPIC_API_KEY not set!")

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("🐾 Pup Event Scout bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
