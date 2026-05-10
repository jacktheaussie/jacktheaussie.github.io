from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import urllib.request
from html import unescape
from pathlib import Path
from zoneinfo import ZoneInfo


REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

AEST = ZoneInfo("Australia/Brisbane")
UTC = dt.timezone.utc

FORECAST_URL = "https://www.bom.gov.au/places/qld/townsville-city/forecast"
FORECAST_OUT = DATA_DIR / "dashboard_forecast.json"
CALENDAR_OUT = DATA_DIR / "calendar_summary.json"
FORECAST_HTML_PATH = os.getenv("FORECAST_HTML_PATH", "").strip()


def fetch_text(url: str) -> str:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; dashboard-bot/1.0)"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", errors="replace")


def load_forecast_source() -> str:
    if FORECAST_HTML_PATH:
        path = Path(FORECAST_HTML_PATH)
        if path.exists():
            return path.read_text(encoding="utf-8", errors="replace")

    try:
        return fetch_text(FORECAST_URL)
    except Exception:
        curl_result = subprocess.run(
            [
                "curl",
                "-fsSL",
                "-A",
                "Mozilla/5.0 (compatible; dashboard-bot/1.0)",
                FORECAST_URL,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return curl_result.stdout


def clean_text(value: str) -> str:
    value = unescape(re.sub(r"<[^>]+>", " ", value))
    return re.sub(r"\s+", " ", value).strip()


def parse_forecast_html(html: str) -> dict:
    issued_match = re.search(
        r"Forecast issued at\s+(.*?)\.\s*##\s+Forecast for the rest of .*?##\s+([A-Za-z]+\s+\d{1,2}\s+[A-Za-z]+)(.*?)(?:##\s+[A-Za-z]+\s+\d{1,2}\s+[A-Za-z]+|The next routine forecast)",
        html,
        flags=re.S,
    )
    if not issued_match:
        raise ValueError("Could not locate forecast issue time or first day block.")

    issued_at = clean_text(issued_match.group(1))
    rest_day_label = clean_text(issued_match.group(2))
    rest_day_block = issued_match.group(3)

    day_pattern = re.compile(
        r"##\s+([A-Za-z]+\s+\d{1,2}\s+[A-Za-z]+)\s+(.*?)(?=##\s+[A-Za-z]+\s+\d{1,2}\s+[A-Za-z]+|The next routine forecast)",
        re.S,
    )
    blocks = [(rest_day_label, rest_day_block)] + day_pattern.findall(html)

    days = []
    for day_label, block in blocks:
        min_match = re.search(r"Min\s+(\d+)\s+.*?C", block)
        max_match = re.search(r"Max\s+(\d+)\s+.*?C", block)
        rain_match = re.search(r"Possible rainfall:\s*(.*?)\s*Chance of any rain:", block, re.S)
        chance_match = re.search(r"Chance of any rain:\s*([0-9]+%)", block)
        summary_match = re.search(
            r"###\s+Herbert and Lower Burdekin area\s+(.*?)(?:Sun protection recommended|Fire Danger|$)",
            block,
            re.S,
        )

        if not (min_match and max_match and rain_match and chance_match and summary_match):
            continue

        date_match = re.match(r"([A-Za-z]+)\s+(\d{1,2})\s+([A-Za-z]+)", day_label)
        if not date_match:
            continue

        weekday, day_number, month_name = date_match.groups()
        current_year = dt.datetime.now(AEST).year
        date_obj = dt.datetime.strptime(f"{day_number} {month_name} {current_year}", "%d %B %Y").date()

        days.append(
            {
                "label": weekday,
                "date": date_obj.isoformat(),
                "min": int(min_match.group(1)),
                "max": int(max_match.group(1)),
                "rainfall": clean_text(rain_match.group(1)),
                "rainChance": chance_match.group(1),
                "summary": clean_text(summary_match.group(1)),
            }
        )

        if len(days) == 2:
            break

    if len(days) < 2:
        raise ValueError("Could not parse two forecast days.")

    return {
        "source": FORECAST_URL,
        "issued_at": issued_at,
        "generated_at": dt.datetime.now(UTC).isoformat(),
        "days": days,
    }


def unfold_ics_lines(text: str) -> list[str]:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    unfolded: list[str] = []
    for line in lines:
        if line.startswith((" ", "\t")) and unfolded:
            unfolded[-1] += line[1:]
        else:
            unfolded.append(line)
    return unfolded


def parse_ics_datetime(value: str, params: dict[str, str]) -> tuple[dt.datetime | dt.date, bool]:
    tzid = params.get("TZID")
    if value.endswith("Z"):
        return dt.datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC), False
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        return dt.datetime.strptime(value, "%Y%m%d").date(), True
    parsed = dt.datetime.strptime(value, "%Y%m%dT%H%M%S")
    if tzid:
        try:
            return parsed.replace(tzinfo=ZoneInfo(tzid)), False
        except Exception:
            return parsed.replace(tzinfo=AEST), False
    return parsed.replace(tzinfo=AEST), False


def parse_ics(text: str, source_name: str) -> list[dict]:
    events: list[dict] = []
    current: dict | None = None

    for line in unfold_ics_lines(text):
        if line == "BEGIN:VEVENT":
            current = {"source": source_name}
            continue
        if line == "END:VEVENT":
            if current:
                events.append(current)
            current = None
            continue
        if current is None or ":" not in line:
            continue

        head, raw_value = line.split(":", 1)
        parts = head.split(";")
        key = parts[0].upper()
        params: dict[str, str] = {}
        for part in parts[1:]:
            if "=" in part:
                param_key, param_value = part.split("=", 1)
                params[param_key.upper()] = param_value

        value = raw_value.strip()
        if key in {"DTSTART", "DTEND"}:
            parsed, all_day = parse_ics_datetime(value, params)
            current[key.lower()] = parsed
            if key == "DTSTART":
                current["all_day"] = all_day
        elif key in {"SUMMARY", "LOCATION", "DESCRIPTION"}:
            current[key.lower()] = value.replace("\\n", " ").strip()

    return events


def normalise_event(event: dict) -> dict | None:
    start = event.get("dtstart")
    if not start:
        return None

    if isinstance(start, dt.date) and not isinstance(start, dt.datetime):
        start_date = start
        start_local_time = ""
        all_day = True
    else:
        start_dt = start.astimezone(AEST)
        start_date = start_dt.date()
        start_local_time = start_dt.strftime("%H:%M")
        all_day = False

    end = event.get("dtend")
    end_local_time = ""
    if isinstance(end, dt.datetime):
        end_local_time = end.astimezone(AEST).strftime("%H:%M")

    return {
        "source": event.get("source", "Calendar"),
        "summary": event.get("summary", "(No title)"),
        "location": event.get("location", ""),
        "date": start_date.isoformat(),
        "all_day": all_day,
        "start_local_time": start_local_time,
        "end_local_time": end_local_time,
    }


def build_calendar_summary() -> dict:
    today = dt.datetime.now(AEST).date()
    tomorrow = today + dt.timedelta(days=1)

    source_defs = [
        ("Personal", os.getenv("ICAL_URL_1", "").strip()),
        ("Work", os.getenv("ICAL_URL_2", "").strip()),
    ]

    all_events: list[dict] = []
    active_sources: list[dict] = []

    for source_name, url in source_defs:
        if not url:
            continue
        text = fetch_text(url)
        parsed = [normalise_event(event) for event in parse_ics(text, source_name)]
        parsed = [event for event in parsed if event]
        all_events.extend(parsed)
        active_sources.append({"name": source_name, "url_configured": True})

    day_buckets = []
    for target_date in (today, tomorrow):
        events = [event for event in all_events if event["date"] == target_date.isoformat()]
        events.sort(key=lambda event: (not event["all_day"], event["start_local_time"], event["summary"]))
        day_buckets.append(
            {
                "date": target_date.isoformat(),
                "label": f"{target_date.strftime('%A')} {target_date.day} {target_date.strftime('%b')}",
                "events": events,
            }
        )

    return {
        "generated_at": dt.datetime.now(UTC).isoformat(),
        "sources": active_sources,
        "days": day_buckets,
    }


def build_forecast_summary() -> dict:
    try:
        return parse_forecast_html(load_forecast_source())
    except Exception:
        if FORECAST_OUT.exists():
            existing = json.loads(FORECAST_OUT.read_text(encoding="utf-8"))
            existing["fallback_used_at"] = dt.datetime.now(UTC).isoformat()
            return existing
        raise


def main() -> None:
    forecast = build_forecast_summary()
    FORECAST_OUT.write_text(json.dumps(forecast, indent=2), encoding="utf-8")

    calendar = build_calendar_summary()
    CALENDAR_OUT.write_text(json.dumps(calendar, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
