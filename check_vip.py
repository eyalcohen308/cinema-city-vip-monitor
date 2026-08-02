"""Monitor VIP screenings of a target date across Israeli cinema chains.

Two chains are watched: Cinema City (Glilot) and Yes Planet (Rishon LeZion).
Both expose the same JSON endpoints their own booking UIs call, so there is no
browser, no HTML to parse, and no dependency beyond the standard library.

Each chain is checked independently and reported side by side: one opening its
box office says nothing about the other, and the alert has to fire per cinema.
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

OUTPUT_DIR = Path("artifacts")
TZ = ZoneInfo("Asia/Jerusalem")

# How far ahead to ask a box office "what is on sale?". Comfortably past any
# real release window, and both APIs want an explicit horizon.
HORIZON_DAYS = 60

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

VENUE_TYPE_NAME = os.getenv("VENUE_TYPE", "VIP")

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
    "date_on_sale_but_no_screenings": "התאריך פתוח אך אין הקרנות של הסרט",
    "movie_not_in_vip_lineup": "הסרט אינו מוצג כרגע באולמות ה-VIP",
    "venue_type_not_offered": "בית הקולנוע אינו מציע אולם VIP",
    "check_failed": "לא ניתן היה לבדוק — האתר לא הגיב",
}

# Best news first. The overall status is whichever cinema is furthest up this
# list, so one chain opening is never masked by the other still being shut.
STATUS_PRIORITY = (
    "available",
    "date_on_sale_but_no_screenings",
    "target_date_not_on_sale",
    "movie_not_in_vip_lineup",
    "venue_type_not_offered",
    "check_failed",
)

# Traffic lights for the pinned status panel: green only when you can book.
STATUS_EMOJI = {
    "available": "🟢",
    "target_date_not_on_sale": "🟡",
    "date_on_sale_but_no_screenings": "🟡",
    "movie_not_in_vip_lineup": "🔴",
    "venue_type_not_offered": "🔴",
    "check_failed": "⚠️",
}


@dataclass(frozen=True)
class Screening:
    """A bookable VIP screening."""

    movie: str
    date: str
    time: str
    booking_url: str
    hall: str = ""


@dataclass
class CinemaResult:
    """Result of checking one cinema for one target date."""

    slug: str
    theater: str
    venue_type: str
    source_url: str
    vip_offered: bool = False
    movie_in_vip_lineup: bool = False
    date_detected: bool = False
    screenings: list[Screening] = field(default_factory=list)
    vip_dates_on_sale: list[str] = field(default_factory=list)
    matched_movies: list[str] = field(default_factory=list)
    status: str = "unknown"
    notes: list[str] = field(default_factory=list)


@dataclass
class Report:
    """Everything one run found, across every cinema."""

    checked_at: str
    target_date: str
    target_weekday: str
    movie_query: list[str]
    status: str = "unknown"
    cinemas: list[CinemaResult] = field(default_factory=list)


def _get_json(url: str, referer: str) -> Any:
    """GET a JSON endpoint, retrying transient failures."""
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "he-IL,he;q=0.9,en;q=0.8",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": referer,
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

    raise RuntimeError(f"Failed to fetch {url}: {last_error}")


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
    value = value.replace(" ", " ")
    return re.sub(r"[\W_]+", "", value, flags=re.UNICODE)


def _movie_matches(movie: str, queries: list[str]) -> bool:
    """Return whether a movie title matches one of the requested aliases."""
    normalized_movie = _normalize_title(movie)
    return any(
        _normalize_title(query) in normalized_movie
        for query in queries
        if query.strip()
    )


def _hebrew_date(value: date) -> str:
    """Render a date the way Israeli sites do: 'יום חמישי 06/08/2026'."""
    return f"{HEBREW_WEEKDAYS[value.weekday()]} {value.strftime('%d/%m/%Y')}"


def _short_dates(values: list[str]) -> str:
    """Render ISO dates compactly for a message: '02/08, 03/08'."""
    return ", ".join(
        date.fromisoformat(value).strftime("%d/%m") for value in values
    )


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


# --------------------------------------------------------------------------
# Cinema City (Glilot)
# --------------------------------------------------------------------------

CC_SITE = "https://www.cinema-city.co.il"
# Glilot: `id` is the site theater id, `theathereid` is the ticketing system id.
CC_THEATER_ID = int(os.getenv("THEATER_ID", "1"))
CC_TIX_THEATER_ID = int(os.getenv("TIX_THEATER_ID", "1170"))
CC_THEATER_NAME = os.getenv("THEATER_NAME", "סינמה סיטי גלילות")
CC_SCHEDULE_URL = (
    f"{CC_SITE}/Timehour?id={CC_THEATER_ID}&theathereid={CC_TIX_THEATER_ID}&vid=2"
)


def _cc_json(path: str, params: dict[str, Any]) -> Any:
    """GET a cinema-city JSON endpoint."""
    url = f"{CC_SITE}{path}?{urllib.parse.urlencode(params)}"
    return _get_json(url, referer=CC_SCHEDULE_URL)


def _cc_venue_type_id(name: str) -> int | None:
    """Look up the venue-type id (e.g. VIP) offered by this theater."""
    venue_types = _cc_json(
        "/tickets/GetVenueTypesByTheater", {"theaterId": CC_THEATER_ID}
    )
    for venue_type in venue_types:
        if _normalize_title(venue_type.get("Name", "")) == _normalize_title(name):
            return venue_type["VenueTypeId"]
    return None


def _cc_dates_on_sale(venue_type_id: int, movie_id: int) -> list[str]:
    """Return the ISO dates this movie is bookable in VIP."""
    raw = _cc_json(
        "/tickets/GetDatesByTheaterMovieVenueType",
        {
            "theaterId": CC_THEATER_ID,
            "movieId": movie_id,
            "venueTypeId": venue_type_id,
        },
    )

    dates: list[str] = []
    for entry in raw:
        # Entries look like "יום ה 06/08/2026".
        match = re.search(r"(\d{2})/(\d{2})/(\d{4})", entry or "")
        if match:
            day, month, year = match.groups()
            dates.append(f"{year}-{month}-{day}")
    return dates


def _cc_screenings(
    venue_type_id: int, movie_id: int, target: date
) -> list[Screening]:
    """Fetch every bookable VIP showtime for one movie on the target date."""
    stamp = target.strftime("%d/%m/%Y")
    events = _cc_json(
        "/tickets/Events",
        {
            "TheatreId": CC_TIX_THEATER_ID,
            "VenueTypeId": venue_type_id,
            "MovieId": movie_id,
            "Date": stamp,
        },
    )

    screenings: list[Screening] = []
    for movie in events:
        title = (movie.get("Name") or "").replace(" ", " ").strip()
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
                        f"{CC_SITE}/order/?eventID={event_id}"
                        f"&theaterId={slot.get('TheaterId', CC_TIX_THEATER_ID)}"
                    ),
                )
            )

    return screenings


def check_cinema_city(target: date, movie_queries: list[str]) -> CinemaResult:
    """Check Cinema City Glilot's VIP halls for the target date."""
    result = CinemaResult(
        slug="cinema-city-glilot",
        theater=CC_THEATER_NAME,
        venue_type=VENUE_TYPE_NAME,
        source_url=CC_SCHEDULE_URL,
    )

    venue_type_id = _cc_venue_type_id(VENUE_TYPE_NAME)
    if venue_type_id is None:
        result.status = "venue_type_not_offered"
        result.notes.append(f"לא נמצא אולם {VENUE_TYPE_NAME} ב{CC_THEATER_NAME}.")
        return result
    result.vip_offered = True

    matched = [
        movie
        for movie in _cc_json(
            "/tickets/MoviesByTheaterAndVenueType",
            {"theaterId": CC_THEATER_ID, "venueTypeId": venue_type_id},
        )
        if _movie_matches(movie.get("Name", ""), movie_queries)
    ]
    if not matched:
        result.status = "movie_not_in_vip_lineup"
        result.notes.append(
            f"אף סרט באולמות ה-{VENUE_TYPE_NAME} של {CC_THEATER_NAME} "
            f"לא תואם לחיפוש: {', '.join(movie_queries)}."
        )
        return result
    result.movie_in_vip_lineup = True
    result.matched_movies = [movie.get("Name", "?") for movie in matched]

    stamp = target.isoformat()
    for movie in matched:
        for value in _cc_dates_on_sale(venue_type_id, movie["MovieId"]):
            if value not in result.vip_dates_on_sale:
                result.vip_dates_on_sale.append(value)

        if stamp in result.vip_dates_on_sale:
            result.screenings.extend(
                _cc_screenings(venue_type_id, movie["MovieId"], target)
            )

    result.vip_dates_on_sale.sort()
    result.date_detected = stamp in result.vip_dates_on_sale
    _finish(result, target, movie_queries)
    return result


# --------------------------------------------------------------------------
# Yes Planet / Planet Cinema (Rishon LeZion)
# --------------------------------------------------------------------------

PLANET_SITE = "https://www.planetcinema.co.il"
# 10100 is the chain's tenant id in the shared "quickbook" data service.
PLANET_API = f"{PLANET_SITE}/il/data-api-service/v1/quickbook/10100"
PLANET_TICKETS = "https://tickets5.planetcinema.co.il"
PLANET_CINEMA_ID = os.getenv("PLANET_CINEMA_ID", "1072")
PLANET_THEATER_NAME = os.getenv("PLANET_THEATER_NAME", "פלאנט ראשון לציון")
# The chain's own attribute vocabulary. Matched exactly, never as a substring:
# "vip-light" is a different (cheaper) product at other branches entirely.
PLANET_VIP_ATTR = "vip"


def _planet_json(path: str) -> Any:
    """GET a Planet quickbook endpoint, already scoped to VIP and Hebrew."""
    url = f"{PLANET_API}{path}?attr={PLANET_VIP_ATTR}&lang=he_IL"
    return _get_json(url, referer=f"{PLANET_SITE}/")


def _planet_horizon(today: date) -> str:
    return (today + timedelta(days=HORIZON_DAYS)).isoformat()


def _planet_vip_dates(today: date) -> list[str]:
    """ISO dates with any VIP screening on sale at this cinema."""
    payload = _planet_json(
        f"/dates/in-cinema/{PLANET_CINEMA_ID}/until/{_planet_horizon(today)}"
    )
    return list((payload.get("body") or {}).get("dates") or [])


def _planet_screenings(target: date, movie_queries: list[str]) -> tuple[
    list[Screening], list[str]
]:
    """VIP showtimes of the wanted movie on one date, plus the titles matched.

    The events list covers every film screening that day, so it has to be
    joined back to the matching films by id rather than taken wholesale.
    """
    payload = _planet_json(
        f"/film-events/in-cinema/{PLANET_CINEMA_ID}/at-date/{target.isoformat()}"
    )
    body = payload.get("body") or {}

    wanted = {
        film["id"]: (film.get("name") or "?").strip()
        for film in body.get("films") or []
        if film.get("id") and _movie_matches(film.get("name") or "", movie_queries)
    }
    if not wanted:
        return [], []

    screenings = []
    for event in body.get("events") or []:
        if event.get("filmId") not in wanted:
            continue
        if PLANET_VIP_ATTR not in (event.get("attributeIds") or []):
            continue
        event_id = event.get("id")
        if not event_id:
            continue
        stamp = event.get("eventDateTime") or ""
        screenings.append(
            Screening(
                movie=wanted[event["filmId"]],
                date=target.isoformat(),
                time=stamp[11:16] or "Unknown time",
                # The API's own bookingLink points at /api/order/... which 404s.
                booking_url=f"{PLANET_TICKETS}/order/{event_id}?lang=he",
                hall=(event.get("auditorium") or "").strip(),
            )
        )
    return screenings, sorted(set(wanted.values()))


def check_planet(target: date, movie_queries: list[str]) -> CinemaResult:
    """Check Yes Planet Rishon LeZion's VIP halls for the target date."""
    result = CinemaResult(
        slug="planet-rishon",
        theater=PLANET_THEATER_NAME,
        venue_type=VENUE_TYPE_NAME,
        source_url=f"{PLANET_SITE}/il/cinemas/{PLANET_CINEMA_ID}",
    )

    today = datetime.now(TZ).date()
    result.vip_dates_on_sale = sorted(_planet_vip_dates(today))
    if not result.vip_dates_on_sale:
        result.status = "venue_type_not_offered"
        result.notes.append(
            f"לא נמצאו הקרנות {VENUE_TYPE_NAME} ב{PLANET_THEATER_NAME}."
        )
        return result
    result.vip_offered = True
    result.date_detected = target.isoformat() in result.vip_dates_on_sale

    if result.date_detected:
        result.screenings, result.matched_movies = _planet_screenings(
            target, movie_queries
        )
        result.movie_in_vip_lineup = bool(result.matched_movies)
    else:
        # The date is not open yet, so the target tells us nothing about
        # whether the movie plays VIP here at all. Ask the furthest date that
        # *is* open -- one extra call, and it turns "not on sale" into the far
        # more useful "it plays here, the date just has not been released".
        _, titles = _planet_screenings(
            date.fromisoformat(result.vip_dates_on_sale[-1]), movie_queries
        )
        result.matched_movies = titles
        result.movie_in_vip_lineup = bool(titles)

    _finish(result, target, movie_queries)
    return result


# --------------------------------------------------------------------------
# Shared status + rendering
# --------------------------------------------------------------------------


def _finish(result: CinemaResult, target: date, movie_queries: list[str]) -> None:
    """Derive the final status and the human-readable notes for one cinema."""
    result.screenings.sort(key=lambda item: item.time)

    if result.screenings:
        result.status = "available"
        return

    if not result.movie_in_vip_lineup:
        result.status = "movie_not_in_vip_lineup"
        result.notes.append(
            f"אף סרט באולמות ה-{result.venue_type} של {result.theater} "
            f"לא תואם לחיפוש: {', '.join(movie_queries)}."
        )
    elif result.date_detected:
        result.status = "date_on_sale_but_no_screenings"
        result.notes.append(
            f"{_hebrew_date(target)} פתוח למכירה ב{result.theater}, "
            f"אך אין הקרנות {result.venue_type} של הסרט."
        )
    else:
        result.status = "target_date_not_on_sale"
        result.notes.append(
            f"{', '.join(result.matched_movies)} מוקרן באולמות ה-"
            f"{result.venue_type} של {result.theater}, אך "
            f"{_hebrew_date(target)} עדיין לא נפתח למכירה."
        )

    if result.vip_dates_on_sale:
        result.notes.append(
            "תאריכים שכבר נפתחו: " + _short_dates(result.vip_dates_on_sale)
        )


def _screening_label(screening: Screening) -> str:
    """'21:30 · VIP 23' when the hall is known, otherwise just the time."""
    return f"{screening.time} · {screening.hall}" if screening.hall else screening.time


def _render_markdown(report: Report, target: date) -> str:
    """Render the Hebrew summary written to artifacts/result.md."""
    lines = [
        f"# אולמות {VENUE_TYPE_NAME} — {_hebrew_date(target)}",
        "",
        f"**סטטוס כללי:** {HEBREW_STATUS.get(report.status, report.status)}",
        f"**נבדק:** {report.checked_at}",
        "",
    ]

    for cinema in report.cinemas:
        lines.extend(
            [
                f"## {cinema.theater}",
                "",
                f"**סטטוס:** {HEBREW_STATUS.get(cinema.status, cinema.status)}",
                f"[לוח ההקרנות הרשמי]({cinema.source_url})",
                "",
            ]
        )
        if cinema.screenings:
            lines.append("### הקרנות זמינות")
            for screening in cinema.screenings:
                lines.append(
                    f"- **{_screening_label(screening)}** — {screening.movie} — "
                    f"[להזמנת כרטיסים]({screening.booking_url})"
                )
            lines.append("")
        if cinema.vip_dates_on_sale:
            lines.extend(
                [
                    "### תאריכים שכבר נפתחו למכירה",
                    _short_dates(cinema.vip_dates_on_sale),
                    "",
                ]
            )
        if cinema.notes:
            lines.extend([f"- {note}" for note in cinema.notes] + [""])

    return "\n".join(lines).rstrip() + "\n"


def _render_alert(cinema: CinemaResult, target: date) -> str:
    """Render the Telegram body for one cinema that has just opened.

    Times link straight to the booking page, so the long order URLs never
    appear as text -- which also avoids bidi mangling in a Hebrew message.
    """
    escape = html.escape

    by_movie: dict[str, list[Screening]] = {}
    for screening in cinema.screenings:
        by_movie.setdefault(screening.movie, []).append(screening)

    lines = [
        f"🎬 <b>נפתחו כרטיסי {escape(cinema.venue_type)}</b>",
        f"{escape(cinema.theater)} · {escape(_hebrew_date(target))}",
    ]
    for movie, showings in by_movie.items():
        lines.append("")
        lines.append(f"<b>{escape(movie)}</b>")
        for screening in showings:
            lines.append(
                f'🎟 <a href="{escape(screening.booking_url)}">'
                f"{escape(_screening_label(screening))}</a>"
            )

    lines.append("")
    lines.append("<i>לחצו על השעה להזמנת כרטיסים</i>")
    return "\n".join(lines)


def _render_status(report: Report, target: date) -> str:
    """Render the pinned Telegram status panel, in Telegram's HTML mode.

    This one message is edited in place after every run, so it has to read
    standalone: what is being watched, where each cinema stands right now, and
    when the question was last asked. Times are absolute -- a relative "12
    minutes ago" would freeze into the message and start lying immediately.
    """
    escape = html.escape
    checked = datetime.fromisoformat(report.checked_at)

    watching = []
    for cinema in report.cinemas:
        for title in cinema.matched_movies:
            if title not in watching:
                watching.append(title)

    lines = [
        f"📡 <b>ניטור {escape(VENUE_TYPE_NAME)}</b>",
        f"🎬 {escape(', '.join(watching or report.movie_query))}",
        f"📅 יעד: {escape(_hebrew_date(target))}",
    ]

    for cinema in report.cinemas:
        lines.append("")
        lines.append(
            f"{STATUS_EMOJI.get(cinema.status, '⚪️')} "
            f"<b>{escape(cinema.theater)}</b>"
        )
        lines.append(escape(HEBREW_STATUS.get(cinema.status, cinema.status)))
        for screening in cinema.screenings:
            lines.append(
                f'🎟 <a href="{escape(screening.booking_url)}">'
                f"{escape(_screening_label(screening))}</a>"
            )
        if cinema.vip_dates_on_sale and cinema.status != "available":
            lines.append(
                "📆 נפתחו: " + escape(_short_dates(cinema.vip_dates_on_sale))
            )

    lines.extend(
        [
            "",
            f"🕐 נבדק לאחרונה: {escape(checked.strftime('%d/%m %H:%M'))}",
            f"⏭ הבדיקה הבאה (מתוכננת): "
            f"{escape(_next_check(checked).strftime('%d/%m %H:%M'))}",
        ]
    )
    return "\n".join(lines)


def _write_alerts(report: Report, target: date) -> list[dict[str, str]]:
    """Write one Telegram body per cinema that is newly bookable.

    The workflow announces each cinema separately, so Glilot opening first
    can never suppress the alert for Rishon opening later.
    """
    alerts = []
    for cinema in report.cinemas:
        if cinema.status != "available":
            continue
        telegram_file = OUTPUT_DIR / f"alert-{cinema.slug}.txt"
        telegram_file.write_text(_render_alert(cinema, target), encoding="utf-8")
        alerts.append(
            {
                "slug": cinema.slug,
                "theater": cinema.theater,
                "date": target.isoformat(),
                "title": (
                    f"🎬 נפתחו כרטיסי {cinema.venue_type} ב{cinema.theater}"
                    f" — {target.isoformat()}"
                ),
                "telegram_file": str(telegram_file),
            }
        )
    return alerts


def _guard(check, slug: str, theater: str):
    """Run one cinema's check, turning its outage into a reported status.

    Two chains means two ways to break, and a Planet outage must not take
    Cinema City's alerts down with it -- before this, one raise ended the run
    and nothing got announced or refreshed.
    """

    def run(target: date, movie_queries: list[str]) -> CinemaResult:
        try:
            return check(target, movie_queries)
        except (RuntimeError, KeyError, TypeError, ValueError) as error:
            return CinemaResult(
                slug=slug,
                theater=theater,
                venue_type=VENUE_TYPE_NAME,
                source_url="",
                status="check_failed",
                notes=[f"{theater}: {error}"],
            )

    return run


CHECKS = (
    _guard(check_cinema_city, "cinema-city-glilot", CC_THEATER_NAME),
    _guard(check_planet, "planet-rishon", PLANET_THEATER_NAME),
)


def main() -> int:
    """Run every cinema check and write the artifacts GitHub Actions reads."""
    target = _resolve_target_date(os.getenv("TARGET_DATE", "thursday"))
    movie_queries = [
        value.strip()
        for value in os.getenv(
            "MOVIE_QUERY", "Spider-Man,Spiderman,ספיידרמן"
        ).split(",")
        if value.strip()
    ]

    report = Report(
        checked_at=datetime.now(TZ).isoformat(timespec="seconds"),
        target_date=target.isoformat(),
        target_weekday=target.strftime("%A"),
        movie_query=movie_queries,
        cinemas=[check(target, movie_queries) for check in CHECKS],
    )
    # If every cinema failed, the monitor really is blind and the workflow
    # should fail loudly. One failing while another answers is survivable, and
    # the panel shows it, so the working chain keeps alerting.
    if all(cinema.status == "check_failed" for cinema in report.cinemas):
        raise RuntimeError(
            "Every cinema check failed: "
            + " | ".join(note for c in report.cinemas for note in c.notes)
        )

    report.status = min(
        (cinema.status for cinema in report.cinemas),
        key=lambda status: STATUS_PRIORITY.index(status)
        if status in STATUS_PRIORITY
        else len(STATUS_PRIORITY),
    )

    OUTPUT_DIR.mkdir(exist_ok=True)
    payload: dict[str, Any] = asdict(report)
    (OUTPUT_DIR / "result.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (OUTPUT_DIR / "result.md").write_text(
        _render_markdown(report, target), encoding="utf-8"
    )
    (OUTPUT_DIR / "status.txt").write_text(
        _render_status(report, target), encoding="utf-8"
    )
    (OUTPUT_DIR / "alerts.json").write_text(
        json.dumps(_write_alerts(report, target), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
