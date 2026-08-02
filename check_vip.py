"""Monitor Cinema City Glilot's official VIP schedule for a target date.

Queries the same JSON endpoints the cinema-city.co.il booking UI calls, so no
browser is needed and there is no HTML/knockout markup to keep up with.
"""

from __future__ import annotations

import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

SITE = "https://www.cinema-city.co.il"
# Glilot: `id` is the site theater id, `theathereid` is the ticketing system id.
THEATER_ID = int(os.getenv("THEATER_ID", "1"))
TIX_THEATER_ID = int(os.getenv("TIX_THEATER_ID", "1170"))
VENUE_TYPE_NAME = os.getenv("VENUE_TYPE", "VIP")
THEATER_NAME = os.getenv("THEATER_NAME", "סינמה סיטי גלילות")

SCHEDULE_URL = f"{SITE}/Timehour?id={THEATER_ID}&theathereid={TIX_THEATER_ID}&vid=2"
OUTPUT_DIR = Path("artifacts")
TZ = ZoneInfo("Asia/Jerusalem")

# Mirrors the `cron: "23,53 5-18 * * *"` in .github/workflows/check.yml. Kept
# in UTC exactly as the cron is, so "next check" stays right across DST -- the
# local window is 08:00-21:00 in summer and 07:00-20:00 in winter. The odd
# minutes are deliberate: GitHub queues every repository's :00 cron at once,
# so asking off-peak measurably improves the chance of actually running.
SCHEDULE_HOURS_UTC = range(5, 19)
SCHEDULE_MINUTES_UTC = (23, 53)

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

# Indexed by date.weekday(), i.e. Monday first.
HEBREW_WEEKDAYS = [
    "יום שני",
    "יום שלישי",
    "יום רביעי",
    "יום חמישי",
    "יום שישי",
    "שבת",
    "יום ראשון",
]

# Machine-readable statuses stay English in result.json; these are what a
# human actually reads in Telegram and in the GitHub issue.
HEBREW_STATUS = {
    "available": "כרטיסים זמינים להזמנה",
    "target_date_not_on_sale": "התאריך עדיין לא נפתח למכירה",
    "date_on_sale_but_no_screenings": "התאריך פתוח אך לא נמצאו הקרנות",
    "movie_not_in_vip_lineup": "הסרט אינו מוצג כרגע באולמות ה-VIP",
    "venue_type_not_offered": "בית הקולנוע אינו מציע אולם VIP",
}

# Traffic lights for the pinned status panel: green only when you can book.
STATUS_EMOJI = {
    "available": "🟢",
    "target_date_not_on_sale": "🟡",
    "date_on_sale_but_no_screenings": "🟡",
    "movie_not_in_vip_lineup": "🔴",
    "venue_type_not_offered": "🔴",
}


@dataclass(frozen=True)
class Screening:
    """A bookable VIP screening."""

    movie: str
    date: str
    time: str
    booking_url: str


@dataclass
class CheckResult:
    """Serializable result of one availability check."""

    checked_at: str
    target_date: str
    target_weekday: str
    source_url: str
    theater: str
    venue_type: str
    vip_offered: bool
    movie_in_vip_lineup: bool
    date_detected: bool
    screenings: list[Screening] = field(default_factory=list)
    vip_dates_on_sale: list[str] = field(default_factory=list)
    movie_query: list[str] = field(default_factory=list)
    matched_movies: list[str] = field(default_factory=list)
    status: str = "unknown"
    notes: list[str] = field(default_factory=list)


def _get_json(path: str, params: dict[str, Any]) -> Any:
    """GET a cinema-city JSON endpoint, retrying transient failures."""
    url = f"{SITE}{path}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "he-IL,he;q=0.9,en;q=0.8",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": SCHEDULE_URL,
        },
    )

    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            last_error = error
            if attempt < 2:
                time.sleep(2 * (attempt + 1))

    raise RuntimeError(f"Failed to fetch {path}: {last_error}")


def _resolve_target_date(raw: str) -> date:
    """Accept an ISO date, a weekday name, or 'today'/'tomorrow'."""
    value = raw.strip().lower()
    today = datetime.now(TZ).date()

    if value in {"", "today"}:
        return today
    if value == "tomorrow":
        return today + timedelta(days=1)

    if value in WEEKDAYS:
        # The next occurrence of that weekday, today included.
        ahead = (WEEKDAYS[value] - today.weekday()) % 7
        return today + timedelta(days=ahead)

    try:
        return date.fromisoformat(value)
    except ValueError:
        raise SystemExit(
            f"TARGET_DATE={raw!r} is not usable. Give a YYYY-MM-DD date, a weekday "
            f"name ({', '.join(WEEKDAYS)}), 'today', or 'tomorrow'."
        ) from None


def _normalize_title(value: str) -> str:
    """Strip punctuation/spacing so Hebrew and Latin titles compare cleanly."""
    value = value.casefold()
    value = value.replace("־", "-").replace("–", "-").replace("—", "-")
    value = value.replace(" ", " ")
    return re.sub(r"[\W_]+", "", value, flags=re.UNICODE)


def _movie_matches(movie: str, queries: list[str]) -> bool:
    """Return whether a movie title matches one of the requested aliases."""
    normalized_movie = _normalize_title(movie)
    return any(
        _normalize_title(query) in normalized_movie
        for query in queries
        if query.strip()
    )


def _venue_type_id(name: str) -> int | None:
    """Look up the venue-type id (e.g. VIP) offered by this theater."""
    venue_types = _get_json(
        "/tickets/GetVenueTypesByTheater", {"theaterId": THEATER_ID}
    )
    for venue_type in venue_types:
        if _normalize_title(venue_type.get("Name", "")) == _normalize_title(name):
            return venue_type["VenueTypeId"]
    return None


def _vip_movies(venue_type_id: int) -> list[dict[str, Any]]:
    """List every movie currently on sale in this theater's VIP halls."""
    return _get_json(
        "/tickets/MoviesByTheaterAndVenueType",
        {"theaterId": THEATER_ID, "venueTypeId": venue_type_id},
    )


def _dates_on_sale(venue_type_id: int, movie_id: int) -> list[str]:
    """Return the DD/MM/YYYY dates this movie is bookable in VIP."""
    raw = _get_json(
        "/tickets/GetDatesByTheaterMovieVenueType",
        {
            "theaterId": THEATER_ID,
            "movieId": movie_id,
            "venueTypeId": venue_type_id,
        },
    )

    dates: list[str] = []
    for entry in raw:
        # Entries look like "יום ה 06/08/2026".
        match = re.search(r"\d{2}/\d{2}/\d{4}", entry or "")
        if match:
            dates.append(match.group(0))
    return dates


def _screenings(
    venue_type_id: int, movie_id: int, target: date
) -> list[Screening]:
    """Fetch bookable VIP showtimes for one movie on the target date."""
    stamp = target.strftime("%d/%m/%Y")
    events = _get_json(
        "/tickets/Events",
        {
            "TheatreId": TIX_THEATER_ID,
            "VenueTypeId": venue_type_id,
            "MovieId": movie_id,
            "Date": stamp,
        },
    )

    screenings: list[Screening] = []
    for movie in events:
        title = (movie.get("Name") or "").replace(" ", " ").strip()
        for slot in movie.get("Dates") or []:
            # The endpoint has returned neighbouring days before; pin the date.
            if not (slot.get("Date") or "").startswith(stamp):
                continue
            event_id = slot.get("EventId")
            if not event_id:
                continue
            screenings.append(
                Screening(
                    movie=title,
                    date=target.isoformat(),
                    time=slot.get("Hour") or "Unknown time",
                    booking_url=(
                        f"{SITE}/order/?eventID={event_id}"
                        f"&theaterId={slot.get('TheaterId', TIX_THEATER_ID)}"
                    ),
                )
            )

    screenings.sort(key=lambda item: item.time)
    return screenings


def check(target: date, movie_queries: list[str]) -> CheckResult:
    """Perform one availability check against Cinema City's official API."""
    result = CheckResult(
        checked_at=datetime.now(TZ).isoformat(timespec="seconds"),
        target_date=target.isoformat(),
        target_weekday=target.strftime("%A"),
        source_url=SCHEDULE_URL,
        theater=THEATER_NAME,
        venue_type=VENUE_TYPE_NAME,
        vip_offered=False,
        movie_in_vip_lineup=False,
        date_detected=False,
        movie_query=movie_queries,
    )

    venue_type_id = _venue_type_id(VENUE_TYPE_NAME)
    if venue_type_id is None:
        result.status = "venue_type_not_offered"
        result.notes.append(
            f"לא נמצא אולם {VENUE_TYPE_NAME} ב{THEATER_NAME}."
        )
        return result
    result.vip_offered = True

    matched = [
        movie
        for movie in _vip_movies(venue_type_id)
        if _movie_matches(movie.get("Name", ""), movie_queries)
    ]
    if not matched:
        result.status = "movie_not_in_vip_lineup"
        result.notes.append(
            f"אף סרט באולמות ה-{VENUE_TYPE_NAME} של {THEATER_NAME} "
            f"לא תואם לחיפוש: {', '.join(movie_queries)}."
        )
        return result
    result.movie_in_vip_lineup = True
    result.matched_movies = [movie.get("Name", "?") for movie in matched]

    stamp = target.strftime("%d/%m/%Y")
    for movie in matched:
        on_sale = _dates_on_sale(venue_type_id, movie["MovieId"])
        for value in on_sale:
            if value not in result.vip_dates_on_sale:
                result.vip_dates_on_sale.append(value)

        if stamp in on_sale:
            result.screenings.extend(
                _screenings(venue_type_id, movie["MovieId"], target)
            )

    result.date_detected = stamp in result.vip_dates_on_sale

    if result.screenings:
        result.status = "available"
    elif result.date_detected:
        result.status = "date_on_sale_but_no_screenings"
        result.notes.append(
            f"התאריך {stamp} מופיע כפתוח למכירה, אך לא התקבלו הקרנות להזמנה."
        )
    else:
        result.status = "target_date_not_on_sale"
        titles = ", ".join(result.matched_movies)
        result.notes.append(
            f"הסרט {titles} מוקרן באולמות ה-{VENUE_TYPE_NAME} של {THEATER_NAME}, "
            f"אך {_hebrew_date(target)} עדיין לא נפתח למכירה."
        )
        if result.vip_dates_on_sale:
            result.notes.append(
                "תאריכים שכבר נפתחו: " + ", ".join(result.vip_dates_on_sale)
            )

    return result


def _hebrew_date(value: date) -> str:
    """Render a date the way Israeli sites do: 'יום חמישי 06/08/2026'."""
    return f"{HEBREW_WEEKDAYS[value.weekday()]} {value.strftime('%d/%m/%Y')}"


def _next_check(after: datetime) -> datetime:
    """The next slot the workflow's cron asks for, in local time.

    Asks for, not gets: GitHub treats scheduled workflows as best-effort and
    routinely delays or drops them, which is why the panel labels this as
    planned rather than stating it as fact.
    """
    moment = after.astimezone(timezone.utc).replace(second=0, microsecond=0)
    hour = moment.replace(minute=0)
    for _ in range(48):  # two days of hours always contains a slot
        if hour.hour in SCHEDULE_HOURS_UTC:
            for minute in SCHEDULE_MINUTES_UTC:
                slot = hour.replace(minute=minute)
                if slot > moment:
                    return slot.astimezone(TZ)
        hour += timedelta(hours=1)
    raise AssertionError("SCHEDULE_HOURS_UTC is empty")


def _render_status(result: CheckResult, target: date) -> str:
    """Render the pinned Telegram status panel, in Telegram's HTML mode.

    This one message is edited in place after every run, so it has to read
    standalone: what is being watched, what the answer is right now, and when
    the question was last asked. Times are absolute -- a relative "12 minutes
    ago" would freeze into the message and start lying immediately.
    """
    escape = html.escape
    checked = datetime.fromisoformat(result.checked_at)
    watching = result.matched_movies or result.movie_query

    lines = [
        f"📡 <b>ניטור {escape(result.venue_type)}</b> · {escape(result.theater)}",
        f"🎬 {escape(', '.join(watching)) if watching else '—'}",
        f"📅 יעד: {escape(_hebrew_date(target))}",
        "",
        f"{STATUS_EMOJI.get(result.status, '⚪️')} <b>"
        f"{escape(HEBREW_STATUS.get(result.status, result.status))}</b>",
    ]

    for screening in result.screenings:
        lines.append(
            f'🎟 <a href="{escape(screening.booking_url)}">'
            f"{escape(screening.time)}</a>"
        )

    lines.extend(
        [
            "",
            f"🕐 נבדק לאחרונה: {escape(checked.strftime('%d/%m %H:%M'))}",
            f"⏭ הבדיקה הבאה (מתוכננת): "
            f"{escape(_next_check(checked).strftime('%d/%m %H:%M'))}",
        ]
    )
    if result.vip_dates_on_sale:
        lines.append(
            "📆 תאריכים שנפתחו: " + escape(", ".join(result.vip_dates_on_sale))
        )

    return "\n".join(lines)


def _render_markdown(result: CheckResult, target: date) -> str:
    """Render the Hebrew summary written to artifacts/result.md."""
    status_he = HEBREW_STATUS.get(result.status, result.status)
    lines = [
        f"# {result.theater} — אולמות {result.venue_type}",
        "",
        f"**תאריך מבוקש:** {_hebrew_date(target)}",
        f"**סטטוס:** {status_he}",
        f"**נבדק:** {result.checked_at}",
        f"[לוח ההקרנות הרשמי]({result.source_url})",
        "",
    ]

    if result.screenings:
        lines.append("## הקרנות זמינות")
        for screening in result.screenings:
            lines.append(
                f"- **{screening.time}** — {screening.movie} — "
                f"[להזמנת כרטיסים]({screening.booking_url})"
            )
        lines.append("")

    if result.vip_dates_on_sale:
        lines.extend(
            [
                f"## תאריכים שכבר נפתחו למכירה ב-{result.venue_type}",
                ", ".join(result.vip_dates_on_sale),
                "",
            ]
        )

    if result.notes:
        lines.extend(["## פרטים נוספים", *[f"- {note}" for note in result.notes]])

    return "\n".join(lines).rstrip() + "\n"


def _render_notification(result: CheckResult, target: date) -> str:
    """Render the Hebrew Telegram body, using Telegram's HTML parse mode.

    Times link straight to the booking page, so the long order URLs never
    appear as text -- which also avoids bidi mangling in a Hebrew message.
    """
    escape = html.escape
    status_he = HEBREW_STATUS.get(result.status, result.status)

    if result.status != "available":
        return (
            f"<b>{escape(result.theater)}</b> · אולמות {escape(result.venue_type)}\n"
            f"{escape(_hebrew_date(target))}\n"
            f"{escape(status_he)}"
        )

    # Group by movie so the title is stated once instead of on every row.
    by_movie: dict[str, list[Screening]] = {}
    for screening in result.screenings:
        by_movie.setdefault(screening.movie, []).append(screening)

    lines = [
        f"🎬 <b>נפתחו כרטיסי {escape(result.venue_type)}</b>",
        f"{escape(result.theater)} · {escape(_hebrew_date(target))}",
    ]
    for movie, showings in by_movie.items():
        lines.append("")
        lines.append(f"<b>{escape(movie)}</b>")
        for screening in showings:
            lines.append(
                f'🎟 <a href="{escape(screening.booking_url)}">'
                f"{escape(screening.time)}</a>"
            )

    lines.append("")
    lines.append("<i>לחצו על השעה להזמנת כרטיסים</i>")
    return "\n".join(lines)


def main() -> int:
    """Run the monitor and write JSON/Markdown outputs for GitHub Actions."""
    target = _resolve_target_date(os.getenv("TARGET_DATE", "thursday"))
    movie_queries = [
        value.strip()
        for value in os.getenv(
            "MOVIE_QUERY", "Spider-Man,Spiderman,ספיידרמן"
        ).split(",")
        if value.strip()
    ]

    result = check(target, movie_queries)

    payload: dict[str, Any] = asdict(result)
    OUTPUT_DIR.mkdir(exist_ok=True)
    (OUTPUT_DIR / "result.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (OUTPUT_DIR / "result.md").write_text(
        _render_markdown(result, target), encoding="utf-8"
    )
    (OUTPUT_DIR / "notify.txt").write_text(
        _render_notification(result, target), encoding="utf-8"
    )
    (OUTPUT_DIR / "status.txt").write_text(
        _render_status(result, target), encoding="utf-8"
    )

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
