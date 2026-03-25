#!/usr/bin/env python3
"""
Pup Event Scout - Telegram Bot
AI-powered venue finder for events
"""

import os
import re
import logging
import requests
from bs4 import BeautifulSoup
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

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

WELCOME_MESSAGE = """🐾 *Woof! Welcome to Pup Event Scout!*

I'm your AI-powered venue finder. Tell me what kind of event you're planning and I'll sniff out the perfect spots for you!

Just describe your needs in plain English, like:
• _"Find a venue in Cannes for 50 people, $5k budget, rooftop vibe"_
• _"Beachfront conference space in Nice, 100 attendees, elegant"_
• _"Intimate cocktail bar in Paris for 30 people, creative atmosphere"_

I'll search for real venues and give you a curated shortlist with all the details you need. 🎯

Type /help for more info, or just tell me what you need!"""

HELP_MESSAGE = """🐾 *Pup Event Scout - Help*

*How to use:*
Just send me a message describing your event needs! Include:
• 📍 *City/Location* - Where do you need the venue?
• 👥 *Capacity* - How many people?
• 💰 *Budget* - What's your budget range?
• 🎨 *Vibe/Theme* - What atmosphere are you going for?

*Example queries:*
• "Find a venue in Cannes for 50 people, $5k budget, rooftop vibe"
• "Conference center in London for 200 people, professional setting"
• "Beachfront event space in Monaco, 80 guests, luxury feel"
• "Creative warehouse venue in Berlin for 150 people, nightlife vibe"

*What you'll get:*
A shortlist of 3-5 real venues with:
✅ Name & address
✅ Capacity estimate
✅ Vibe match score
✅ Why it's perfect for you
🔗 Website link

*Powered by:* OpenClaw + Anthropic Claude AI"""


def search_venues(query: str) -> list[dict]:
    """Search DuckDuckGo for venue information."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }

    results = []
    try:
        url = f"https://html.duckduckgo.com/html/?q={requests.utils.quote(query)}"
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        result_divs = soup.find_all("div", class_="result__body")

        for div in result_divs[:8]:
            title_el = div.find("a", class_="result__a")
            snippet_el = div.find("a", class_="result__snippet")
            url_el = div.find("a", class_="result__url")

            if title_el:
                result = {
                    "title": title_el.get_text(strip=True),
                    "snippet": snippet_el.get_text(strip=True) if snippet_el else "",
                    "url": url_el.get_text(strip=True) if url_el else "",
                }
                results.append(result)

    except Exception as e:
        logger.error(f"Search error: {e}")

    return results


def parse_and_find_venues(user_request: str) -> str:
    """Use Claude to parse request and find venues."""

    # First, use Claude to create optimized search queries
    parse_response = anthropic_client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=500,
        messages=[
            {
                "role": "user",
                "content": f"""You are a venue search assistant. Parse this event request and generate 2-3 specific search queries to find real venues.

User request: "{user_request}"

Return ONLY a JSON object with this structure (no markdown, no explanation):
{{
  "location": "city/location extracted",
  "capacity": "number of people",
  "budget": "budget info",
  "vibe": "atmosphere/theme",
  "search_queries": ["query1", "query2", "query3"]
}}

Make search queries specific, like: "rooftop venue hire Cannes 50 people event space" """,
            }
        ],
    )

    try:
        # Extract JSON from response
        parse_text = parse_response.content[0].text.strip()
        # Clean up any markdown code blocks
        parse_text = re.sub(r"```(?:json)?\s*", "", parse_text).strip().rstrip("`")
        import json
        parsed = json.loads(parse_text)
    except Exception as e:
        logger.error(f"Parse error: {e}, text: {parse_response.content[0].text}")
        parsed = {
            "location": "unknown",
            "capacity": "unknown",
            "budget": "unknown",
            "vibe": "unknown",
            "search_queries": [f"event venue {user_request}"],
        }

    # Collect search results
    all_results = []
    for query in parsed.get("search_queries", [])[:2]:
        results = search_venues(query)
        all_results.extend(results)

    # Deduplicate by title
    seen = set()
    unique_results = []
    for r in all_results:
        if r["title"] not in seen:
            seen.add(r["title"])
            unique_results.append(r)

    # Format results for Claude
    search_context = "\n\n".join(
        [
            f"Result {i+1}:\nTitle: {r['title']}\nURL: {r['url']}\nSnippet: {r['snippet']}"
            for i, r in enumerate(unique_results[:8])
        ]
    )

    if not search_context:
        search_context = "No specific search results found. Use your knowledge to suggest well-known venues."

    # Now use Claude to analyze and present venues
    final_response = anthropic_client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=1500,
        messages=[
            {
                "role": "user",
                "content": f"""You are Pup Event Scout 🐾 - a friendly and efficient AI venue finder.

User's event request: "{user_request}"

Parsed details:
- Location: {parsed.get('location', 'unknown')}
- Capacity: {parsed.get('capacity', 'unknown')}
- Budget: {parsed.get('budget', 'unknown')}
- Vibe: {parsed.get('vibe', 'unknown')}

Search results found:
{search_context}

Based on the request and search results, provide a shortlist of 3-5 venue recommendations.
Format your response in Telegram markdown (use *bold* and _italic_).

For each venue, include:
🏛 *Venue Name*
📍 Address (if known)
👥 Capacity: X-Y people
✨ Vibe: describe the atmosphere
💡 Why it's perfect: 1-2 sentences
🔗 Website: URL if available

If search results contain real venues, prioritize those. If not, suggest well-known venues in the requested location that match the criteria.

Start with a brief 1-line intro, then list the venues, end with a helpful tip.
Keep it concise but informative. Be friendly and enthusiastic!""",
            }
        ],
    )

    return final_response.content[0].text


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command."""
    await update.message.reply_text(WELCOME_MESSAGE, parse_mode=ParseMode.MARKDOWN)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help command."""
    await update.message.reply_text(HELP_MESSAGE, parse_mode=ParseMode.MARKDOWN)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle regular messages - find venues."""
    user_message = update.message.text
    user_name = update.effective_user.first_name or "there"

    # Send thinking message
    thinking_msg = await update.message.reply_text(
        f"🐾 On it, {user_name}! Sniffing out the best venues for you...",
        parse_mode=ParseMode.MARKDOWN,
    )

    try:
        # Get venue recommendations
        response = parse_and_find_venues(user_message)

        # Delete thinking message and send real response
        await thinking_msg.delete()
        await update.message.reply_text(response, parse_mode=ParseMode.MARKDOWN)

    except Exception as e:
        logger.error(f"Error handling message: {e}")
        await thinking_msg.edit_text(
            "🐾 Woof! I hit a snag while searching. Please try again with more details about your location and event needs!"
        )


def main() -> None:
    """Start the bot."""
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
