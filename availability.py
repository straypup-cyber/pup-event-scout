"""
Venue availability scraper using Playwright headless browser.
Supports: OpenTable, Resy, SevenRooms, generic booking pages.
"""
import asyncio
import re
import logging
from datetime import datetime
from urllib.parse import urlparse, urlencode

logger = logging.getLogger(__name__)


async def _get_opentable_slots(page, url: str, date: str, guests: int) -> list[str]:
    """Extract time slots from OpenTable page."""
    # Navigate to specific date/guests if we can parse the restaurant slug
    # OpenTable URL format: opentable.co.uk/r/restaurant-name or /restaurant/id
    try:
        # Inject date + covers into URL params
        base = url.split('?')[0]
        params = f"?covers={guests}&dateTime={date}T19:00"
        await page.goto(base + params, wait_until="networkidle", timeout=15000)
    except Exception:
        await page.goto(url, wait_until="domcontentloaded", timeout=12000)

    await page.wait_for_timeout(2000)

    # Look for time slot buttons
    slots = []
    try:
        # OpenTable time buttons
        buttons = await page.query_selector_all('[data-test="time-button"], .ot-time-button, [class*="timeslot"], [class*="time-slot"]')
        for btn in buttons[:12]:
            text = (await btn.inner_text()).strip()
            if re.match(r'\d{1,2}:\d{2}', text) or re.match(r'\d{1,2}(am|pm)', text, re.I):
                slots.append(text)
    except Exception:
        pass

    if not slots:
        # Fallback: look for any text matching time patterns
        content = await page.content()
        slots = re.findall(r'\b\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)\b', content)[:8]

    return list(dict.fromkeys(slots))  # dedupe preserving order


async def _get_resy_slots(page, url: str, date: str, guests: int) -> list[str]:
    """Extract time slots from Resy."""
    try:
        base = url.split('?')[0]
        params = f"?date={date}&party_size={guests}"
        await page.goto(base + params, wait_until="domcontentloaded", timeout=15000)
    except Exception:
        await page.goto(url, wait_until="domcontentloaded", timeout=12000)

    # Wait for slots to render
    try:
        await page.wait_for_selector('[class*="ReservationButton"], [class*="timeslot"], [class*="time-slot"]', timeout=8000)
    except Exception:
        await page.wait_for_timeout(4000)

    slots = []
    try:
        buttons = await page.query_selector_all('[class*="ReservationButton"], [class*="timeslot"], [class*="time-slot"], button[class*="time"]')
        for btn in buttons[:12]:
            text = (await btn.inner_text()).strip()
            if re.search(r'\d{1,2}:\d{2}|\d{1,2}(am|pm)', text, re.I):
                slots.append(text.split('\n')[0].strip())
    except Exception:
        pass

    if not slots:
        content = await page.content()
        slots = re.findall(r'\b\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)\b', content)[:8]

    return list(dict.fromkeys(slots))


async def _get_sevenrooms_slots(page, url: str, date: str, guests: int) -> list[str]:
    """Extract time slots from SevenRooms."""
    try:
        await page.goto(url, wait_until="networkidle", timeout=15000)
    except Exception:
        await page.goto(url, wait_until="domcontentloaded", timeout=12000)

    await page.wait_for_timeout(3000)

    slots = []
    try:
        buttons = await page.query_selector_all('[class*="time-slot"], [class*="timeslot"], [data-qa*="time"]')
        for btn in buttons[:12]:
            text = (await btn.inner_text()).strip()
            if re.search(r'\d{1,2}:\d{2}|\d{1,2}(am|pm)', text, re.I):
                slots.append(text.split('\n')[0].strip())
    except Exception:
        pass

    if not slots:
        content = await page.content()
        slots = re.findall(r'\b\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)\b', content)[:8]

    return list(dict.fromkeys(slots))


async def _get_generic_slots(page, url: str, date: str, guests: int) -> list[str]:
    """Try to extract any time slots from a generic booking page."""
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=12000)
    except Exception:
        return []

    await page.wait_for_timeout(2000)
    content = await page.content()
    slots = re.findall(r'\b\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)\b', content)
    return list(dict.fromkeys(slots))[:8]


async def check_availability(booking_url: str, platform: str, date: str, guests: int) -> dict:
    """
    Check venue availability via headless browser.
    Returns: {"available": bool, "slots": [...], "platform": str, "url": str}
    """
    from playwright.async_api import async_playwright

    result = {"available": False, "slots": [], "platform": platform, "url": booking_url}

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage",
                  "--disable-blink-features=AutomationControlled"]
        )
        try:
            ctx = await browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                locale="en-GB",
            )
            # Hide webdriver fingerprint
            await ctx.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            """)
            page = await ctx.new_page()

            platform_lower = platform.lower()
            if "opentable" in platform_lower:
                slots = await _get_opentable_slots(page, booking_url, date, guests)
            elif "resy" in platform_lower:
                slots = await _get_resy_slots(page, booking_url, date, guests)
            elif "sevenrooms" in platform_lower:
                slots = await _get_sevenrooms_slots(page, booking_url, date, guests)
            else:
                slots = await _get_generic_slots(page, booking_url, date, guests)

            result["slots"] = slots
            result["available"] = len(slots) > 0

        except Exception as e:
            logger.error(f"Playwright error: {e}")
            result["error"] = str(e)
        finally:
            await browser.close()

    return result
