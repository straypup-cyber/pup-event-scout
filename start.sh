#!/bin/bash
# Pup Event Scout - Start Script

set -e

echo "🐾 Pup Event Scout - Starting up..."

# Check for .env file
if [ ! -f ".env" ]; then
    echo "❌ .env file not found! Copy .env.example and fill in your credentials."
    exit 1
fi

# Install dependencies
echo "📦 Installing dependencies..."
pip3 install -r requirements.txt --quiet

# Kill any existing bot process
if pgrep -f "python3 bot.py" > /dev/null; then
    echo "⚠️  Stopping existing bot process..."
    pkill -f "python3 bot.py" || true
    sleep 2
fi

# Start bot with nohup
echo "🚀 Starting bot..."
nohup python3 bot.py > bot.log 2>&1 &
BOT_PID=$!

echo "✅ Pup Event Scout started! PID: $BOT_PID"
echo "📋 Logs: tail -f bot.log"
echo "🛑 Stop: kill $BOT_PID (or pkill -f 'python3 bot.py')"

# Save PID
echo $BOT_PID > bot.pid
