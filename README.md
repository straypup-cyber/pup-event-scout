# 🐾 Pup Event Scout

> AI-powered venue finder for events. Built for the OpenClaw Lobster Cave Cannes Hackathon.

[![Telegram](https://img.shields.io/badge/Telegram-@pupeventscout__bot-blue?logo=telegram)](https://t.me/pupeventscout_bot)
[![Powered by Anthropic](https://img.shields.io/badge/AI-Anthropic%20Claude-orange)](https://anthropic.com)
[![Built with OpenClaw](https://img.shields.io/badge/Platform-OpenClaw-purple)](https://openclaw.ai)

## What It Does

Pup Event Scout helps event planners find perfect venues with a simple natural language request:

> "Find a venue in Cannes for 50 people, $5k budget, rooftop vibe"

The bot will:
1. 🧠 Use Claude AI to parse your request (location, capacity, budget, vibe)
2. 🔍 Search DuckDuckGo for real venues matching your criteria
3. 🎯 Return a curated shortlist of 3-5 venues with full details

## Hackathon

**Event:** OpenClaw Lobster Cave Cannes Hackathon  
**Track:** Event Scout  
**Team:** Stray Pup AI

## Features

- 🤖 Natural language understanding via Anthropic Claude Haiku
- 🔍 Real venue search via DuckDuckGo (no API key needed)
- 📱 Telegram bot interface
- 🌐 Web landing page
- ⚡ Fast, concise responses

## Try It

**Telegram Bot:** [@pupeventscout_bot](https://t.me/pupeventscout_bot)

Example queries:
- "Venue in Cannes for 50 people, rooftop, $5k budget"
- "Conference space in Nice, 100 people, professional"
- "Beachfront event space Monaco, luxury, 80 guests"

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Bot Framework | python-telegram-bot v20+ |
| AI/LLM | Anthropic Claude Haiku (claude-haiku-4-5) |
| Search | DuckDuckGo HTML scraping |
| Platform | OpenClaw |
| Deployment | DigitalOcean |

## Setup

### Prerequisites
- Python 3.10+
- Telegram Bot Token
- Anthropic API Key

### Install & Run

```bash
git clone https://github.com/straypup-cyber/pup-event-scout
cd pup-event-scout

# Configure credentials
cp .env.example .env
# Edit .env with your TELEGRAM_TOKEN and ANTHROPIC_API_KEY

# Start the bot
./start.sh
```

### Manual Start

```bash
pip3 install -r requirements.txt
python3 bot.py
```

## Project Structure

```
pup-event-scout/
├── bot.py              # Main Telegram bot
├── index.html          # Landing page
├── requirements.txt    # Python dependencies
├── start.sh           # Quick start script
├── .env.example       # Environment template
├── .env               # Your credentials (not in git!)
├── AGENTS.md          # Agent workspace instructions
├── SOUL.md            # Bot persona
└── README.md          # This file
```

## How It Works

1. User sends a message like "venue in Cannes for 50 people"
2. Claude parses the request and generates targeted search queries
3. DuckDuckGo is scraped for venue results
4. Claude analyzes the results and formats a helpful shortlist
5. User gets 3-5 venues with name, address, capacity, vibe match, and links

## License

MIT - Built for hackathon purposes
