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
from telegram import Update, InputMediaPhoto
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

I'll search real venues and send each one with photos. 🎯

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


def get_place_photos(place_id: str, max_photos: int = 3) -> list[str]:
    """Get photo URLs for a place from Google Places."""
    if not GOOGLE_PLACES_API_KEY or not place_id:
        return []

    photo_urls = []
    try:
        det_url = "https://maps.googleapis.com/maps/api/place/details/json"
        det_params = {
            "place_id": place_id,
            "fields": "photos",
            "key": GOOGLE_PLACES_API_KEY,
        }
        resp = requests.get(det_url, params=det_params, timeout=10)
        photos = resp.json().get("result", {}).get("photos", [])

        for photo in photos[:max_photos]:
            ref = photo.get("photo_reference")
            if ref:
                url = (
                    f"https://maps.googleapis.com/maps/api/place/photo"
                    f"?maxwidth=800&photo_reference={ref}&key={GOOGLE_PLACES_API_KEY}"
                )
                photo_urls.append(url)
    except Exception as e:
        logger.error(f"Photo fetch error: {e}")

    return photo_urls


def search_google_places(query: str, location: str) -> list[dict]:
    """Search Google Places API for venues."""
    if not GOOGLE_PLACES_API_KEY:
        return []

    results = []
    try:
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

            if place_id:
                det_url = "https://maps.googleapis.com/maps/api/place/details/json"
                det_params = {
                    "place_id": place_id,
                    "fields": "name,formatted_address,website,formatted_phone_number,rating,user_ratings_total,types,price_level",
                    "key": GOOGLE_PLACES_API_KEY,
                }
                det_resp = requests.get(det_url, params=det_params, timeout=10)
                details = det_resp.json().get("result", {})

            # Price level: 0=free, 1=$, 2=$$, 3=$$$, 4=$$$$
            price_level = details.get("price_level")
            price_str = ""
            if price_level is not None:
                price_map = {0: "Free", 1: "Budget ($)", 2: "Moderate ($$)", 3: "Upscale ($$$)", 4: "Luxury ($$$$)"}
                price_str = price_map.get(price_level, "")

            results.append({
                "name": place.get("name", ""),
                "place_id": place_id or "",
                "address": details.get("formatted_address") or place.get("formatted_address", ""),
                "rating": place.get("rating", ""),
                "rating_count": details.get("user_ratings_total", ""),
                "website": details.get("website", ""),
                "phone": details.get("formatted_phone_number", ""),
                "types": ", ".join(place.get("types", [])[:3]),
                "price_level": price_str,
            })

    except Exception as e:
        logger.error(f"Google Places error: {e}")

    return results


def parse_request(user_request: str) -> dict:
    """Parse user request with Claude."""
    response = anthropic_client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=400,
        messages=[{
            "role": "user",
            "content": f"""Parse this event venue request. Return ONLY valid JSON, no markdown:

Request: "{user_request}"

{{
  "location": "city name",
  "capacity": "number or range",
  "budget": "budget info or unknown",
  "vibe": "atmosphere/theme keywords",
  "search_query": "2-4 word venue search query like: rooftop event space"
}}"""
        }]
    )

    try:
        text = response.content[0].text.strip()
        text = re.sub(r"```(?:json)?\s*", "", text).strip().rstrip("`")
        return json.loads(text)
    except Exception as e:
        logger.error(f"Parse error: {e}")
        return {
            "location": "unknown",
            "capacity": "unknown",
            "budget": "unknown",
            "vibe": "event space",
            "search_query": "event venue"
        }


def format_venue_card(venue: dict, analysis: dict) -> str:
    """Format a single venue card."""
    name = venue.get("name", "Unknown Venue")
    address = venue.get("address", "")
    rating = venue.get("rating", "")
    rating_count = venue.get("rating_count", "")
    website = venue.get("website", "")
    price_level = venue.get("price_level", "")

    why = analysis.get("why", "")
    capacity_est = analysis.get("capacity_estimate", "")
    vibe_match = analysis.get("vibe_match", "")
    est_budget = analysis.get("estimated_budget", "")

    lines = [f"🏛 *{name}*"]

    if address:
        lines.append(f"📍 _{address}_")

    if rating:
        stars = f"⭐ {rating}/5"
        if rating_count:
            stars += f" _({rating_count} reviews)_"
        lines.append(stars)

    if capacity_est:
        lines.append(f"👥 Capacity: {capacity_est}")

    if price_level or est_budget:
        budget_line = "💰 "
        if price_level:
            budget_line += price_level
        if est_budget:
            budget_line += f" — est. {est_budget}"
        lines.append(budget_line)

    if vibe_match:
        lines.append(f"✨ {vibe_match}")

    if why:
        lines.append(f"💡 {why}")

    if website:
        lines.append(f"🔗 {website}")

    return "\n".join(lines)


async def send_venue(update: Update, venue: dict, analysis: dict) -> None:
    """Send a single venue card with photos."""
    card_text = format_venue_card(venue, analysis)

    # Try to get photos
    photos = []
    if venue.get("place_id"):
        photos = get_place_photos(venue["place_id"], max_photos=3)

    if photos:
        try:
            if len(photos) == 1:
                await update.message.reply_photo(
                    photo=photos[0],
                    caption=card_text,
                    parse_mode=ParseMode.MARKDOWN,
                )
            else:
                # Send as media group
                media = []
                for i, url in enumerate(photos):
                    if i == 0:
                        media.append(InputMediaPhoto(media=url, caption=card_text, parse_mode=ParseMode.MARKDOWN))
                    else:
                        media.append(InputMediaPhoto(media=url))
                await update.message.reply_media_group(media=media)
            return
        except Exception as e:
            logger.warning(f"Photo send failed: {e}, sending text only")

    # Fallback: text only
    await update.message.reply_text(card_text, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_message = update.message.text
    user_name = update.effective_user.first_name or "there"

    thinking_msg = await update.message.reply_text(
        f"🐾 On it, {user_name}! Sniffing out venues..."
    )

    try:
        # Parse request
        parsed = parse_request(user_message)
        location = parsed.get("location", "unknown")
        search_query = parsed.get("search_query", "event venue")

        # Search Google Places
        places = search_google_places(search_query, location)

        if not places:
            await thinking_msg.edit_text(
                "🐾 Couldn't find venues via Google Places. Try being more specific about the city!"
            )
            return

        # Ask Claude to analyze and rank venues
        places_context = "\n\n".join([
            f"Venue {i+1}: {p['name']} | {p['address']} | Rating: {p.get('rating','')} | Types: {p['types']}"
            for i, p in enumerate(places)
        ])

        analysis_response = anthropic_client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1500,
            messages=[{
                "role": "user",
                "content": f"""You are Pup Event Scout. Analyze these venues for the user's request.

User request: "{user_message}"
Location: {location}, Capacity needed: {parsed.get('capacity')}, Budget: {parsed.get('budget')}, Vibe: {parsed.get('vibe')}

Venues found:
{places_context}

Return ONLY valid JSON array with analysis for top 3-4 venues (by index, 0-based):
[
  {{
    "index": 0,
    "why": "one sentence why it fits",
    "capacity_estimate": "e.g. 30-100 people",
    "vibe_match": "e.g. Rooftop / upscale / great views",
    "estimated_budget": "e.g. $3,000-6,000 for full buyout"
  }}
]

Be specific with budget estimates based on venue type and location. Only include venues that genuinely fit the request."""
            }]
        )

        try:
            analysis_text = analysis_response.content[0].text.strip()
            analysis_text = re.sub(r"```(?:json)?\s*", "", analysis_text).strip().rstrip("`")
            analyses = json.loads(analysis_text)
        except Exception as e:
            logger.error(f"Analysis parse error: {e}")
            analyses = [{"index": i, "why": "", "capacity_estimate": "", "vibe_match": "", "estimated_budget": ""} for i in range(min(3, len(places)))]

        # Delete thinking message
        await thinking_msg.delete()

        # Send intro
        await update.message.reply_text(
            f"🐾 Found *{len(analyses)} venues* in *{location}* for you:",
            parse_mode=ParseMode.MARKDOWN
        )

        # Send each venue as separate message
        for analysis in analyses:
            idx = analysis.get("index", 0)
            if idx < len(places):
                await send_venue(update, places[idx], analysis)

    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        await thinking_msg.edit_text(
            "🐾 Hit a snag! Try again with city, capacity and vibe details."
        )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(WELCOME_MESSAGE, parse_mode=ParseMode.MARKDOWN)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_MESSAGE, parse_mode=ParseMode.MARKDOWN)


async def post_init(app) -> None:
    from telegram import BotCommand
    await app.bot.set_my_commands([
        BotCommand("start", "Welcome message"),
        BotCommand("help", "How to use Pup Event Scout"),
    ])


def main() -> None:
    if not TELEGRAM_TOKEN:
        raise ValueError("TELEGRAM_TOKEN not set!")
    if not ANTHROPIC_API_KEY:
        raise ValueError("ANTHROPIC_API_KEY not set!")

    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("🐾 Pup Event Scout bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
