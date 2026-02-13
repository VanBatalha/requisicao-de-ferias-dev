from __future__ import annotations

import datetime as dt
import re
import unicodedata
from typing import Optional

def safe_lower(s: str) -> str:
    return (s or "").strip().lower()

def normalize_text(s: str) -> str:
    s = (s or "").strip()
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    return s

def parse_date(date_str: str) -> Optional[dt.date]:
    if not date_str:
        return None
    date_str = date_str.strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return dt.datetime.strptime(date_str, fmt).date()
        except ValueError:
            continue
    return None

def format_date(d: Optional[dt.date]) -> str:
    return d.strftime("%Y-%m-%d") if d else ""

def to_int(v, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return default

def only_digits(s: str) -> str:
    return re.sub(r"\D+", "", s or "")
