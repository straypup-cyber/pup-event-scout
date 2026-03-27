#!/usr/bin/env python3
"""
Pup Scout - Telegram Bot
Personal booking assistant: email comms, saved venues, subscription, bookings calendar
"""

import os
import logging
import requests
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)
from telegram.constants import ParseMode

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
API_BASE         = "https://pupscout.co.uk/api"
SUPABASE_URL     = os.getenv("SUPABASE_URL", "https://ponhyojwucvukkphqfqz.supabase.co")
SUPABASE_KEY     = os.getenv("SUPABASE_SERVICE_KEY", "")

try:
    from supabase import create_client as _sb_create
    supabase = _sb_create(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_KEY else None
except Exception:
    supabase = None

# ─── Helpers ─────────────────────────────────────────────────────────────────

import asyncio

async def _send_confirmation_email(email_id: str, tg_user) -> tuple[bool, str]:
    """Fetch original email context and send a thank-you reply via API."""
    import aiohttp
    resend_key = os.getenv("RESEND_FULL_KEY") or os.getenv("RESEND_API_KEY", "")
    if not resend_key:
        return False, "No Resend key"

    sender_name = ""
    if tg_user:
        parts = [tg_user.first_name or "", tg_user.last_name or ""]
        sender_name = " ".join(p for p in parts if p).strip()

    try:
        # Fetch original inbound email to get from address, subject, language context
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://api.resend.com/emails/receiving/{email_id}",
                headers={"Authorization": f"Bearer {resend_key}"},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as r:
                email_data = await r.json()

        orig_from    = email_data.get("from", "")
        orig_subject = email_data.get("subject", "")
        orig_text    = (email_data.get("text", "") or "")[:500]

        # Extract reply-to address
        reply_headers = email_data.get("headers", {})
        reply_to = reply_headers.get("reply-to") or reply_headers.get("Reply-To") or orig_from

        # Detect language from original text + subject
        lang_hint = ""
        if any(c in orig_text + orig_subject for c in "àáâãäåæçèéêëìíîïðñòóôõöùúûüý"):
            lang_hint = "French"
        elif any(c in orig_text + orig_subject for c in "äöüßÄÖÜ"):
            lang_hint = "German"
        elif any(c in orig_text + orig_subject for c in "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"):
            lang_hint = "Russian"
        elif any(c in orig_text + orig_subject for c in "áéíóúüñ¡¿"):
            lang_hint = "Spanish"
        else:
            lang_hint = "English"

        client_label = sender_name if sender_name else "our client"
        sign = f"AI Assistant, booking on behalf of {client_label}" if sender_name else "AI Assistant, Pup Scout"

        # Generate thank-you via Anthropic
        import anthropic as _anthropic
        anthropic_key = os.getenv("ANTHROPIC_API_KEY", "")
        if not anthropic_key:
            return False, "No Anthropic key"

        ac = _anthropic.Anthropic(api_key=anthropic_key)
        resp = ac.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=300,
            messages=[{"role": "user", "content": f"""Write a brief, warm thank-you email confirming a booking.

Context: The venue just confirmed our reservation request.
Original venue email subject: {orig_subject}
Language to use: {lang_hint}
Sign off as: {sign}

Write 2-3 sentences max. Confirm the booking, express gratitude, say we look forward to the visit.
Return ONLY the email body, no subject line."""}]
        )
        body = resp.content[0].text.strip()
        subject = f"Re: {orig_subject}" if orig_subject else "Booking confirmed — thank you!"

        # Send via Resend
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {resend_key}", "Content-Type": "application/json"},
                json={
                    "from": f"{sign} <outreach@pupscout.co.uk>",
                    "to": [reply_to],
                    "subject": subject,
                    "text": body,
                },
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                result = await r.json()
                if r.status in (200, 201) and result.get("id"):
                    return True, ""
                return False, result.get("message", "Send failed")

    except Exception as e:
        logger.error(f"Confirmation email error: {e}")
        return False, str(e)


async def _build_calendar_invite(email_id: str, tg_user) -> str | None:
    """Build an .ics calendar file from the booking email context."""
    resend_key = os.getenv("RESEND_FULL_KEY") or os.getenv("RESEND_API_KEY", "")
    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not resend_key or not anthropic_key:
        return None

    try:
        import aiohttp, json as _json, re as _re, uuid as _uuid
        from datetime import datetime, timezone, timedelta

        # Fetch original email
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://api.resend.com/emails/receiving/{email_id}",
                headers={"Authorization": f"Bearer {resend_key}"},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as r:
                email_data = await r.json()

        orig_from    = email_data.get("from", "")
        orig_subject = email_data.get("subject", "")
        orig_text    = (email_data.get("text", "") or "")[:1000]

        # Also check Supabase for booking details
        booking_details = {}
        if supabase and tg_user:
            try:
                rows = sb_get("bookings", f"user_id=eq.{tg_user.id}&status=eq.pending&order=created_at.desc&limit=1")
                if rows:
                    booking_details = rows[0]
            except Exception:
                pass

        # Extract venue name from email sender or booking
        venue_name = booking_details.get("venue_name", "") or orig_from.split('<')[0].strip() or "Venue"
        date_str   = booking_details.get("date", "")
        time_str   = booking_details.get("time", "")
        guests_str = booking_details.get("guests", "")
        occasion   = booking_details.get("occasion", "")
        venue_email = booking_details.get("venue_email", "")

        # Use Claude to extract structured info from email
        import anthropic as _anthropic
        ac = _anthropic.Anthropic(api_key=anthropic_key)
        resp = ac.messages.create(
            model="claude-haiku-4-5",
            max_tokens=300,
            messages=[{"role": "user", "content": f"""Extract booking details from this venue confirmation email. Return ONLY valid JSON:

Email from: {orig_from}
Subject: {orig_subject}
Body: {orig_text}

Known details: date={date_str}, time={time_str}, guests={guests_str}

{{
  "venue_name": "name of venue",
  "date": "YYYY-MM-DD or best guess",
  "time": "HH:MM in 24h or best guess",
  "address": "full address if mentioned",
  "booking_ref": "booking/reservation reference number if mentioned, else empty",
  "duration_hours": 2,
  "notes": "any special notes or instructions from the venue"
}}

If date/time unknown use today + 1 day at 19:00."""}]
        )
        txt = _re.sub(r'```(?:json)?\s*', '', resp.content[0].text.strip()).rstrip('`')
        info = _json.loads(txt)

        # Build datetime
        date_val = info.get("date", date_str) or (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        time_val = info.get("time", time_str) or "19:00"
        try:
            dt_start = datetime.strptime(f"{date_val} {time_val[:5]}", "%Y-%m-%d %H:%M")
        except Exception:
            dt_start = datetime.now() + timedelta(days=1, hours=19)
        dt_end = dt_start + timedelta(hours=info.get("duration_hours", 2))

        # Format for ICS (UTC — note: ideally convert from local but we don't have TZ)
        fmt = "%Y%m%dT%H%M%S"
        dtstart = dt_start.strftime(fmt)
        dtend   = dt_end.strftime(fmt)
        dtstamp = datetime.now(timezone.utc).strftime(fmt) + "Z"
        uid = str(_uuid.uuid4())

        vname = info.get("venue_name", venue_name)
        address = info.get("address", "")
        booking_ref = info.get("booking_ref", "")

        # Try to get full address + timezone from Google Places
        tz_id = "Europe/London"  # fallback
        if not address and vname:
            try:
                gkey = os.getenv("GOOGLE_MAPS_API_KEY", "")
                if gkey:
                    # Build specific query using venue_email domain as hint for city
                    venue_domain = (venue_email or "").split("@")[-1] if venue_email else ""
                    search_q = vname
                    # Add city hint from booking details if available
                    if booking_details.get("venue_email", ""):
                        pass  # use venue name as-is, rely on place_id if available
                    # Use findplace with location bias if we have place_id
                    place_id = booking_details.get("place_id", "")
                    if place_id:
                        async with aiohttp.ClientSession() as gs:
                            async with gs.get(
                                "https://maps.googleapis.com/maps/api/place/details/json",
                                params={"place_id": place_id, "fields": "formatted_address,geometry", "key": gkey},
                                timeout=aiohttp.ClientTimeout(total=5),
                            ) as gr:
                                gdata = await gr.json()
                                result = gdata.get("result", {})
                                address = result.get("formatted_address", "")
                                loc = result.get("geometry", {}).get("location", {})
                    else:
                        # Search with venue email domain as city hint
                        async with aiohttp.ClientSession() as gs:
                            async with gs.get(
                                "https://maps.googleapis.com/maps/api/place/findplacefromtext/json",
                                params={"input": search_q, "inputtype": "textquery",
                                        "fields": "formatted_address,geometry,place_id", "key": gkey},
                                timeout=aiohttp.ClientTimeout(total=5),
                            ) as gr:
                                gdata = await gr.json()
                                candidates = gdata.get("candidates", [])
                                if candidates:
                                    address = candidates[0].get("formatted_address", "")
                                    loc = candidates[0].get("geometry", {}).get("location", {})

                    # Get timezone from coordinates
                    if loc and loc.get("lat"):
                        import time as _time
                        async with aiohttp.ClientSession() as gs:
                            async with gs.get(
                                "https://maps.googleapis.com/maps/api/timezone/json",
                                params={"location": f"{loc['lat']},{loc['lng']}",
                                        "timestamp": int(_time.time()), "key": gkey},
                                timeout=aiohttp.ClientTimeout(total=5),
                            ) as gr:
                                tzdata = await gr.json()
                                tz_id = tzdata.get("timeZoneId", "Europe/London")
            except Exception as e:
                logger.warning(f"Places lookup error: {e}")

        notes_parts = []
        if guests_str:   notes_parts.append(f"Guests: {guests_str}")
        if booking_ref:  notes_parts.append(f"Booking ref: {booking_ref}")
        if venue_email:  notes_parts.append(f"Contact: {venue_email}")
        extra = info.get("notes", "")
        if extra:        notes_parts.append(extra)
        description = "\\n".join(notes_parts) or "Booking via Pup Scout"

        title = f"Dinner @ {vname}"
        if occasion and occasion not in ("event", "other", "dinner", ""):
            title = f"{occasion.title()} @ {vname}"

        ics = "\r\n".join([
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//Pup Scout//Booking//EN",
            "CALSCALE:GREGORIAN",
            "METHOD:PUBLISH",
            "BEGIN:VEVENT",
            f"UID:{uid}@pupscout.co.uk",
            f"DTSTAMP:{dtstamp}",
            f"DTSTART;TZID={tz_id}:{dtstart}",
            f"DTEND;TZID={tz_id}:{dtend}",
            f"SUMMARY:{title}",
            f"DESCRIPTION:{description}",
            f"LOCATION:{address}",
            "STATUS:CONFIRMED",
            "BEGIN:VALARM",
            "TRIGGER:-PT60M",
            "ACTION:DISPLAY",
            "DESCRIPTION:Booking reminder",
            "END:VALARM",
            "END:VEVENT",
            "END:VCALENDAR",
        ])
        return ics

    except Exception as e:
        logger.error(f"Calendar invite error: {e}")
        return None


def _update_booking_by_email_id(email_id: str, status: str):
    """Update booking status by fetching venue email from Resend then matching in DB."""
    if not supabase:
        return
    try:
        import requests as _req
        resend_key = os.getenv("RESEND_FULL_KEY") or os.getenv("RESEND_API_KEY", "")
        if not resend_key:
            return

        # Get the inbound email to find which venue sent it
        r = _req.get(
            f"https://api.resend.com/emails/receiving/{email_id}",
            headers={"Authorization": f"Bearer {resend_key}"},
            timeout=8,
        )
        email_data = r.json()
        from_addr = email_data.get("from", "")

        # Extract email from "Name <email>" format
        import re as _re
        match = _re.search(r'<([^>]+)>', from_addr)
        venue_email = match.group(1).lower() if match else from_addr.lower().strip()

        # Find matching pending booking by venue_email
        rows = supabase.table("bookings")\
            .select("id")\
            .ilike("venue_email", venue_email)\
            .eq("status", "pending")\
            .order("created_at", desc=True)\
            .limit(1)\
            .execute()

        if rows.data:
            booking_id = rows.data[0]["id"]
            supabase.table("bookings").update({
                "status": status,
                "updated_at": __import__("datetime").datetime.utcnow().isoformat(),
            }).eq("id", booking_id).execute()
            logger.info(f"Booking {booking_id} → {status} (venue: {venue_email})")
        else:
            # Fallback: update most recent pending booking for this user
            logger.warning(f"No booking found for venue_email={venue_email}, checking all pending")

    except Exception as e:
        logger.error(f"Update booking error: {e}")


def sb_get(table: str, filters_str: str = "") -> list:
    try:
        resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/{table}?{filters_str}&order=created_at.desc&limit=20",
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
            },
            timeout=8,
        )
        return resp.json() if isinstance(resp.json(), list) else []
    except Exception as e:
        logger.error(f"Supabase error: {e}")
        return []


def get_user_by_tg(tg_id: int) -> dict | None:
    rows = sb_get("users", f"id=eq.{tg_id}")
    return rows[0] if rows else None


def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📍 Saved Venues", callback_data="menu_saved"),
         InlineKeyboardButton("📅 Bookings", callback_data="menu_bookings")],
        [InlineKeyboardButton("📩 Recent Emails", callback_data="menu_emails"),
         InlineKeyboardButton("⚡ Subscription", callback_data="menu_subscription")],
        [InlineKeyboardButton("🌐 Open Pup Scout", url="https://pupscout.co.uk")],
    ])


# ─── Commands ─────────────────────────────────────────────────────────────────

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    welcome = (
        f"🐾 *Hey {user.first_name}!*\n\n"
        f"I'm your personal booking assistant.\n\n"
        f"I'll handle:\n"
        f"• 📩 Email communication with venues\n"
        f"• 📍 Your saved places\n"
        f"• 📅 Booking reminders\n"
        f"• ⚡ Your subscription\n\n"
        f"Use the menu below or open the web app to search for venues."
    )
    await update.message.reply_text(welcome, parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu_keyboard())


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    help_text = (
        "🐾 *Pup Scout Bot*\n\n"
        "*Commands:*\n"
        "/start — Main menu\n"
        "/saved — Your saved venues\n"
        "/bookings — Confirmed bookings\n"
        "/subscription — Manage subscription\n\n"
        "*Web app:* https://pupscout.co.uk\n\n"
        "Venue replies are forwarded here automatically 📩"
    )
    await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu_keyboard())


def _esc(text: str) -> str:
    """Escape special chars for Telegram MarkdownV2."""
    for ch in r'_*[]()~`>#+=|{}.!-':
        text = text.replace(ch, f'\\{ch}')
    return text


async def saved_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg_id = update.effective_user.id
    venues = sb_get("saved_venues", f"user_id=eq.{tg_id}")
    if not venues:
        await update.message.reply_text(
            "📍 No saved venues yet.\n\nSearch on pupscout.co.uk and save your favourites.",
        )
        return
    lines = ["📍 Your saved venues:\n"]
    for i, v in enumerate(venues[:10], 1):
        name    = v.get("name", "Unknown")
        addr    = v.get("address", "")
        website = v.get("website", "")
        line = f"{i}. {name}"
        if addr:    line += f"\n    {addr}"
        if website: line += f"\n    🔗 {website}"
        lines.append(line)
    await update.message.reply_text(
        "\n\n".join(lines),
        disable_web_page_preview=True,
    )


def _is_past(date_str: str) -> bool:
    """Check if a booking date is in the past."""
    if not date_str:
        return False
    from datetime import datetime, timezone, date as dt_date
    try:
        # Try ISO date
        d = datetime.strptime(date_str.strip()[:10], "%Y-%m-%d").date()
        return d < dt_date.today()
    except Exception:
        return False


def _format_booking(b: dict) -> str:
    parts = [f"• {b.get('venue_name', '?')}"]
    detail = []
    if b.get("date"): detail.append(f"📅 {b['date']}")
    if b.get("time"): detail.append(b["time"])
    if b.get("guests"): detail.append(f"👥 {b['guests']}")
    if detail: parts.append("  " + "  ".join(detail))
    return "\n".join(parts)


async def bookings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg_id = update.effective_user.id
    msg = update.message or update.callback_query.message

    try:
        all_bookings = sb_get("bookings", f"user_id=eq.{tg_id}")
    except Exception:
        all_bookings = []

    if not all_bookings:
        await msg.reply_text(
            "📅 No bookings yet.\n\nWhen you email a venue and they respond, I'll track it here.",
        )
        return

    confirmed_upcoming = [b for b in all_bookings if b.get("status") == "confirmed" and not _is_past(b.get("date",""))]
    pending            = [b for b in all_bookings if b.get("status") == "pending"]
    lines = []

    if confirmed_upcoming:
        lines.append("✅ Confirmed — upcoming\n")
        lines.extend(_format_booking(b) for b in confirmed_upcoming[:5])

    if pending:
        if lines: lines.append("")
        lines.append("⏳ Awaiting reply\n")
        lines.extend(_format_booking(b) for b in pending[:5])

    if not lines:
        lines.append("📅 No upcoming bookings.\n\nUse /history to see past bookings.")

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("📖 Past bookings", callback_data="menu_history"),
    ]])
    await msg.reply_text("\n".join(lines), reply_markup=keyboard)


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg_id = update.effective_user.id
    msg = update.message or (update.callback_query.message if update.callback_query else None)

    try:
        all_bookings = sb_get("bookings", f"user_id=eq.{tg_id}")
    except Exception:
        all_bookings = []

    past_confirmed = [b for b in all_bookings if b.get("status") == "confirmed" and _is_past(b.get("date",""))]
    past_declined  = [b for b in all_bookings if b.get("status") == "declined"]

    if not past_confirmed and not past_declined:
        await msg.reply_text("📖 No past bookings yet.")
        return

    lines = []
    if past_confirmed:
        lines.append("🎉 Past — completed\n")
        lines.extend(_format_booking(b) for b in past_confirmed[:10])

    if past_declined:
        if lines: lines.append("")
        lines.append("❌ Past — declined\n")
        lines.extend(_format_booking(b) for b in past_declined[:10])

    await msg.reply_text("\n".join(lines))


async def subscription_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg_id = update.effective_user.id
    user = get_user_by_tg(tg_id)

    if not user:
        await update.message.reply_text(
            "⚡ *Sign in first*\n\nVisit [pupscout.co.uk](https://pupscout.co.uk) and sign in with Telegram to manage your subscription.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    tier = user.get("tier", "free")
    expires = user.get("subscription_expires_at", "")

    if tier == "premium" and expires:
        from datetime import datetime, timezone
        try:
            exp_dt = datetime.fromisoformat(expires.replace("Z", "+00:00"))
            days_left = (exp_dt - datetime.now(timezone.utc)).days
            status_line = f"⚡ *Premium* — {days_left} days remaining"
        except Exception:
            status_line = "⚡ *Premium*"
    else:
        status_line = "🆓 *Free plan* — 3 results per search"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 Upgrade on web", url="https://pupscout.co.uk")],
    ])

    await update.message.reply_text(
        f"⚡ *Subscription*\n\n{status_line}\n\n"
        f"*Premium includes:*\n"
        f"• Up to 10 results per search\n"
        f"• Full email outreach history\n"
        f"• Priority venue matching\n\n"
        f"Upgrade for 5 TON/month on the web app.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keyboard,
    )


# ─── Callback handler ─────────────────────────────────────────────────────────

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data

    # ── Booking action callbacks (sent by email inbound handler) ──
    if data.startswith("book_confirm:"):
        email_id = data.split(":", 1)[1]
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("✅ Booking confirmed! Sending thank-you to the venue…")
        _update_booking_by_email_id(email_id, "confirmed")
        # Send confirmation email back to venue
        ok, err = await _send_confirmation_email(email_id, update.effective_user)
        if ok:
            await query.message.reply_text("📧 Thank-you email sent to the venue.")
        else:
            await query.message.reply_text(f"✅ Confirmed! (Email send failed: {err})")

        # Send calendar invite
        ics = await _build_calendar_invite(email_id, update.effective_user)
        if ics:
            from io import BytesIO
            await query.message.reply_document(
                document=BytesIO(ics.encode()),
                filename="booking.ics",
                caption="📅 Add to your calendar",
            )

    elif data.startswith("book_decline:"):
        email_id = data.split(":", 1)[1]
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("❌ Booking declined.")
        _update_booking_by_email_id(email_id, "declined")

    elif data.startswith("book_reply:"):
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("💬 What would you like to tell the venue? Just send me a message.")

    elif data.startswith("book_alt_time:"):
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("🕐 What alternative date or time would you like to suggest?")

    elif data == "find_venue":
        await query.message.reply_text(
            "🔍 Open Pup Scout to find another venue:\nhttps://pupscout.co.uk",
            disable_web_page_preview=True,
        )

    # ── Menu callbacks ──
    elif data == "menu_saved":
        tg_id = update.effective_user.id
        venues = sb_get("saved_venues", f"user_id=eq.{tg_id}")
        if not venues:
            text = "📍 No saved venues yet.\n\nSearch on pupscout.co.uk and save your favourites."
        else:
            lines = ["📍 Your saved venues:\n"]
            for i, v in enumerate(venues[:10], 1):
                line = f"{i}. {v.get('name','?')}"
                if v.get("address"): line += f"\n    {v['address']}"
                if v.get("website"): line += f"\n    🔗 {v['website']}"
                lines.append(line)
            text = "\n\n".join(lines)
        await query.message.reply_text(text, disable_web_page_preview=True)

    elif data == "menu_bookings":
        await bookings_command(update, context)

    elif data == "menu_history":
        await history_command(update, context)

    elif data == "menu_emails":
        await query.message.reply_text(
            "📩 *Recent venue emails*\n\n"
            "Replies from venues are forwarded here automatically.\n"
            "Your full email history is on [pupscout.co.uk](https://pupscout.co.uk).",
            parse_mode=ParseMode.MARKDOWN,
        )

    elif data == "menu_subscription":
        await subscription_command(update, context)


# ─── Unknown messages ─────────────────────────────────────────────────────────

async def unknown_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🐾 Use the menu or open [pupscout.co.uk](https://pupscout.co.uk) to search for venues.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_keyboard(),
    )


# ─── Setup ────────────────────────────────────────────────────────────────────

async def post_init(app) -> None:
    from telegram import BotCommand
    await app.bot.set_my_commands([
        BotCommand("start",        "Main menu"),
        BotCommand("saved",        "Your saved venues"),
        BotCommand("bookings",     "Upcoming bookings"),
        BotCommand("history",      "Past bookings"),
        BotCommand("subscription", "Manage subscription"),
        BotCommand("help",         "Help"),
    ])


def main() -> None:
    if not TELEGRAM_TOKEN:
        raise ValueError("TELEGRAM_TOKEN not set!")

    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start",        start_command))
    app.add_handler(CommandHandler("help",         help_command))
    app.add_handler(CommandHandler("saved",        saved_command))
    app.add_handler(CommandHandler("bookings",     bookings_command))
    app.add_handler(CommandHandler("history",      history_command))
    app.add_handler(CommandHandler("subscription", subscription_command))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_message))

    logger.info("🐾 Pup Scout bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
