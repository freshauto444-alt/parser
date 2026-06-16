# parsers/base/helpers.py — small text/number/url helpers. Extracted from base.py.

import re
import html
import uuid as uuid_lib
from typing import Optional

# ══════════════════════════════════════════════════════════════════════════════
#  UTILITIES
# ══════════════════════════════════════════════════════════════════════════════

def decode_html(text: str) -> str:
    if not text:
        return ""
    return html.unescape(html.unescape(text)).strip()


def clean_int(text: str) -> Optional[int]:
    if not text:
        return None
    nums = re.sub(r"[^\d]", "", text)
    return int(nums) if nums else None


def extract_uuid(url: str) -> Optional[str]:
    m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", url, re.I)
    return m.group(1) if m else None


def sek_to_eur(sek: float, rate: float = 0.088) -> float:
    return round(sek * rate)


