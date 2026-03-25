# AGENTS.md - Pup Event Scout Workspace

## Project
Pup Event Scout is an AI-powered venue finder bot built for the OpenClaw Lobster Cave Cannes hackathon (Event Scout track).

## What This Is
A Telegram bot + website that helps event planners find perfect venues by:
1. Accepting natural language requests ("venue for 50 people in Cannes, rooftop vibe, $5k budget")
2. Using Claude AI to intelligently parse the request
3. Searching DuckDuckGo for real venues
4. Returning a curated shortlist with all relevant details

## Files
- `bot.py` - Main Telegram bot (Python, python-telegram-bot + Anthropic)
- `index.html` - Landing page / web demo
- `requirements.txt` - Python dependencies
- `.env` - Credentials (NOT in git)
- `start.sh` - Quick start script
- `SOUL.md` - Agent persona
- `README.md` - Project documentation

## Running the Bot
```bash
cp .env.example .env  # fill in credentials
./start.sh
```

## Architecture
- **Bot framework:** python-telegram-bot v20+
- **AI:** Anthropic Claude Haiku (claude-haiku-4-5)
- **Search:** DuckDuckGo HTML scraping (no API key needed)
- **Deployment:** Runs on DigitalOcean droplet

## Development Notes
- Bot parses user requests with Claude to extract location/capacity/budget/vibe
- Generates 2-3 targeted search queries
- Feeds search results back to Claude for smart venue recommendation
- Returns formatted Telegram markdown with venue shortlist
