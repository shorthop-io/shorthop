"""iCalendar (RFC 5545) output for on-call shifts. Pure — no database, no Flask.

A calendar app re-reads the feed on its own schedule, so every event needs a STABLE `UID`:
the same shift must keep the same UID on every fetch or the calendar duplicates it. The UID
is derived from what the shift IS (feed, schedule, person, start, end) — a shift that moves
is a different event, which is exactly how the calendar should show it.

Times are written in UTC (`…Z`): the calendar app shows them in the viewer's own zone, and
a floating local time would be wrong for anyone not in the schedule's zone.
"""
import datetime as dt
import hashlib

PRODID = "-//shorthop//on-call//EN"


def _esc(text: str) -> str:
    """TEXT escaping: backslash, semicolon, comma, newline (RFC 5545 §3.3.11)."""
    return (str(text).replace("\\", "\\\\").replace(";", "\;").replace(",", "\\,")
            .replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n"))


def _fold(line: str) -> str:
    """Lines longer than 75 OCTETS are folded with CRLF + space, never inside a UTF-8 char."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    parts, cur = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(cur) + len(b) > (75 if not parts else 74):
            parts.append(cur)
            cur = b""
        cur += b
    parts.append(cur)
    return "\r\n ".join(p.decode("utf-8") for p in parts)


def _ts(at: dt.datetime) -> str:
    if at.tzinfo is None:
        at = at.replace(tzinfo=dt.timezone.utc)
    return at.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def uid(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:32] + "@shorthop"


def calendar(name: str, events: list[dict], stamp: dt.datetime, description: str = "",
             refresh_minutes: int = 60) -> str:
    """events: [{uid, start, end, summary, description?, url?}]. Returns the .ics body (CRLF)."""
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{PRODID}", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH", f"X-WR-CALNAME:{_esc(name)}",
             # How often a subscribing calendar should re-read (Apple / Outlook honour these).
             f"REFRESH-INTERVAL;VALUE=DURATION:PT{refresh_minutes}M",
             f"X-PUBLISHED-TTL:PT{refresh_minutes}M"]
    if description:
        lines.append(f"X-WR-CALDESC:{_esc(description)}")
    for e in events:
        lines += ["BEGIN:VEVENT", f"UID:{e['uid']}", f"DTSTAMP:{_ts(stamp)}",
                  f"DTSTART:{_ts(e['start'])}", f"DTEND:{_ts(e['end'])}",
                  f"SUMMARY:{_esc(e['summary'])}", "TRANSP:TRANSPARENT"]
        if e.get("description"):
            lines.append(f"DESCRIPTION:{_esc(e['description'])}")
        if e.get("url"):
            lines.append(f"URL:{e['url']}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(x) for x in lines) + "\r\n"
