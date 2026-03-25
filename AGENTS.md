# AGENTS.md - Pup Event Scout Workspace

## Project
Pup Event Scout is an AI-powered venue finder bot built for the OpenClaw Lobster Cave Cannes hackathon (Event Scout track).

## What This Is
A Telegram bot + website that helps event planners find perfect venues by:
1. Accepting natural language requests ("venue for 50 people in Cannes, rooftop vibe, $5k budget")
2. Using Claude Sonnet to parse the request and analyze results
3. Searching Google Places API for real, rated venues with photos
4. Sending each venue as a separate message with photos, budget estimate, and vibe match

## Files
- `bot.py` — Main Telegram bot (python-telegram-bot + Anthropic + Google Places)
- `index.html` — Landing page / web demo
- `requirements.txt` — Python dependencies
- `.env` — Credentials (NOT in git)
- `start.sh` — Quick start script
- `SOUL.md` — Agent persona
- `README.md` — Project documentation

## Running the Bot
```bash
cp .env.example .env  # fill in credentials
pip3 install -r requirements.txt
python3 bot.py
```

## Architecture
- **Bot framework:** python-telegram-bot v20+
- **AI:** Anthropic Claude Sonnet (claude-sonnet-4-5)
- **Venues:** Google Places API (text search + details + photos)
- **Deployment:** DigitalOcean droplet (London) + systemd autostart + nginx

## Flow
1. User sends natural language request
2. Claude parses: location, capacity, budget, vibe, search query
3. Google Places text search → top 5 venues with details
4. Claude analyzes and ranks venues, adds budget estimates
5. Each venue sent as separate message with up to 3 photos (media group)
