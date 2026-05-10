# parsers/browser_pool.py
# Singleton stealth browser: launch once, reuse everywhere.
#
# Uses Patchright (https://github.com/Kaliiiiiiiiii-Vinyzu/patchright) — a fork of
# Playwright that removes CDP leaks (Runtime.Enable, Console API, Origin frame) so
# modern bot detectors (Cloudflare, DataDome, Akamai Bot Manager) cannot fingerprint
# the automation. Drop-in API compatible with playwright.async_api.
#
# Used for: Blocket (JS-rendered), AS24 fallback (when HTTP path blocked).

import asyncio
import random
from typing import Optional
from loguru import logger

# Patchright preferred; fall back to vanilla Playwright if not installed.
try:
    from patchright.async_api import async_playwright, Playwright, Browser, BrowserContext  # type: ignore
    _USING_PATCHRIGHT = True
except ImportError:
    from playwright.async_api import async_playwright, Playwright, Browser, BrowserContext
    _USING_PATCHRIGHT = False
    logger.warning("[browser] patchright not installed — using vanilla Playwright (higher bot-detection risk)")

# Rotate UAs per context so repeated scrapes don't all look identical.
_UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
]

# Viewport pool — realistic desktop resolutions (1080p / 1440p / macbook).
_VIEWPORT_POOL = [
    {"width": 1920, "height": 1080},
    {"width": 1440, "height": 900},
    {"width": 1536, "height": 864},
    {"width": 1280, "height": 800},
]


class BrowserPool:
    _pw: Optional[Playwright] = None
    _browser: Optional[Browser] = None
    _lock = asyncio.Lock()

    @classmethod
    async def _ensure(cls) -> Browser:
        if cls._browser and cls._browser.is_connected():
            return cls._browser
        async with cls._lock:
            if cls._browser and cls._browser.is_connected():
                return cls._browser
            logger.info(f"[browser] launching {'patchright' if _USING_PATCHRIGHT else 'playwright'} Chromium")
            cls._pw = await asyncio.wait_for(async_playwright().start(), timeout=30)
            # Patchright + channel='chrome' → uses real Chrome (not bundled Chromium),
            # which bypasses "HeadlessChrome" UA giveaway. Falls back to Chromium if
            # Chrome not installed.
            launch_kwargs = {
                "headless": True,
                "args": [
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--disable-infobars",
                    "--no-first-run",
                    "--disable-default-apps",
                    "--disable-extensions",
                ],
            }
            try:
                cls._browser = await asyncio.wait_for(
                    cls._pw.chromium.launch(channel="chrome", **launch_kwargs),
                    timeout=30,
                )
                logger.info("[browser] launched with real Chrome channel")
            except Exception as e:
                logger.info(f"[browser] real Chrome unavailable ({e.__class__.__name__}), using bundled Chromium")
                cls._browser = await asyncio.wait_for(
                    cls._pw.chromium.launch(**launch_kwargs),
                    timeout=30,
                )
            return cls._browser

    @classmethod
    async def acquire(cls, locale: str = "en-GB") -> BrowserContext:
        """Create a fresh context with randomized UA + viewport per invocation."""
        browser = await cls._ensure()
        ua = random.choice(_UA_POOL)
        viewport = random.choice(_VIEWPORT_POOL)
        ctx = await browser.new_context(
            user_agent=ua,
            locale=locale,
            viewport=viewport,
            # Realistic screen — avoid obvious "1280x800" bot giveaway.
            screen=viewport,
            # Timezone alignment with locale (en-GB→London, sv-SE→Stockholm, de-DE→Berlin)
            timezone_id={"en-GB": "Europe/London", "sv-SE": "Europe/Stockholm", "de-DE": "Europe/Berlin"}.get(locale, "Europe/Berlin"),
            # Accept languages header matching locale
            extra_http_headers={
                "Accept-Language": {"en-GB": "en-GB,en;q=0.9", "sv-SE": "sv-SE,sv;q=0.9,en;q=0.8", "de-DE": "de-DE,de;q=0.9,en;q=0.8"}.get(locale, "en-GB,en;q=0.9"),
            },
            color_scheme="light",
            # Ignore HTTPS errors in dev (Playwright default false, fine for prod too).
        )
        return ctx

    @classmethod
    async def prewarm(cls) -> None:
        """Pre-launch the browser at startup so first user request doesn't pay 5-10s cold start."""
        try:
            await cls._ensure()
            logger.info("[browser] pre-warmed OK")
        except Exception as e:
            logger.warning(f"[browser] pre-warm failed: {e}")

    @classmethod
    def is_active(cls) -> bool:
        return cls._browser is not None and cls._browser.is_connected()

    @classmethod
    async def shutdown(cls):
        if cls._browser:
            try:
                await cls._browser.close()
            except Exception as e:
                logger.warning(f"[browser] close failed: {e}")
            cls._browser = None
        if cls._pw:
            try:
                await cls._pw.stop()
            except Exception as e:
                logger.warning(f"[browser] playwright stop failed: {e}")
            cls._pw = None
        logger.info("[browser] shutdown")
