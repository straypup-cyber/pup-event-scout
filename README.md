# 🐾 Pup Event Scout

> AI-powered venue finder for event planners. Built for the OpenClaw Lobster Cave Cannes Hackathon.

## What It Does

Tell the bot what you need in plain English. It finds real venues, ranks them, and sends each one with photos and a budget estimate.

**Example:** _Find a rooftop venue in Cannes for 50 people, $5k budget, cocktail vibe_

→ Bot returns 3-4 venues, each as a separate message with:
- 📍 Address
- ⭐ Google rating
- 👥 Capacity estimate
- 💰 Expected budget range
- ✨ Vibe match
- 📸 Photos from Google Places

## Try It

🤖 Telegram: [@pupeventscout_bot](https://t.me/pupeventscout_bot)
🌐 Website: http://139.59.169.236

## Tech Stack

| Component | Technology |
|-----------|------------|
| Bot framework | python-telegram-bot v20+ |
| AI | Anthropic Claude Sonnet |
| Venue data | Google Places API |
| Hosting | DigitalOcean + systemd + nginx |
| Runtime | OpenClaw |

## Setup

```bash
git clone https://github.com/straypup-cyber/pup-event-scout
cd pup-event-scout
pip3 install -r requirements.txt
cp .env.example .env
# Edit .env with your keys
python3 bot.py
```

## Environment Variables

```
TELEGRAM_TOKEN=your_bot_token
ANTHROPIC_API_KEY=your_anthropic_key
GOOGLE_PLACES_API_KEY=your_google_places_key
```

## Hackathon

Built for [OpenClaw Lobster Cave Cannes](https://identityhub.app/contests/openclaw-eth-cc) — Event Scout track.
Demo day: March 30, 2026 in Cannes.

---
Made with 🐾 by Stray Pup
