# Cinema City Glilot VIP monitor

Checks **Cinema City's official site only** for VIP screenings of a given movie at Glilot,
and opens a GitHub issue the moment the target date goes on sale.

It calls the same JSON endpoints (`/tickets/*`) that cinema-city.co.il's own booking UI
calls, so there is no browser, no scraping, and no dependencies beyond the standard library.

## Run locally

```bash
python check_vip.py
cat artifacts/result.md
```

The defaults are Thursday and Spider-Man. To override:

```bash
TARGET_DATE=2026-08-06 \
MOVIE_QUERY='Spider-Man,Spiderman,ספיידרמן' \
python check_vip.py
```

## Configuration

All settings are environment variables.

| Variable | Default | Notes |
| --- | --- | --- |
| `TARGET_DATE` | `thursday` | `YYYY-MM-DD`, a weekday name (next occurrence, today included), `today`, or `tomorrow` |
| `MOVIE_QUERY` | `Spider-Man,Spiderman,ספיידרמן` | Comma-separated aliases; punctuation and spacing are ignored when matching |
| `VENUE_TYPE` | `VIP` | Resolved against the theater's live venue-type list |
| `THEATER_ID` / `TIX_THEATER_ID` | `1` / `1170` | Glilot's site id and ticketing-system id |
| `THEATER_NAME` | `Cinema City Glilot` | Label used in the report |

## Status values

`artifacts/result.json` and `artifacts/result.md` carry one of:

- `available` — bookable VIP showtimes exist on the target date. **This is the only status that opens an issue.**
- `target_date_not_on_sale` — the movie plays VIP, but that date has not been released yet.
- `date_on_sale_but_no_screenings` — the date is listed, but no showtimes came back.
- `movie_not_in_vip_lineup` — no VIP movie matched `MOVIE_QUERY`.
- `venue_type_not_offered` — the theater lists no VIP venue type.

Every result also includes `vip_dates_on_sale`, so you can see exactly how far ahead the
box office has opened while you wait.

## GitHub Actions

`.github/workflows/check.yml` runs hourly at `:23`, **only between 08:00 and 22:00
Israel time** (`cron: "23 5-18 * * *"` — GitHub cron is always UTC, and Israel is UTC+3
in summer). Nothing fires overnight. Choose **Actions → Check Cinema City VIP → Run
workflow** to pass a one-off date or movie at any hour.

When the target date goes on sale you get:

- a **Telegram message** with the showtimes and booking links, and
- a **GitHub issue** containing `result.md`.

The issue doubles as the "already announced" marker, so Telegram fires exactly once per
target date rather than every hour after it opens. If the run itself fails, a separate
Telegram message tells you the monitor has stopped watching — otherwise a broken monitor
is indistinguishable from "not on sale yet".

All human-facing output — the Telegram message, the issue title and body, and
`result.md` — is in Hebrew. `result.json` stays English so the workflow can branch on
`status`.

### Telegram setup

1. Message [@BotFather](https://t.me/BotFather) → `/newbot`, pick a name, copy the token
   (looks like `8123456789:AAH...`).
2. **Send your new bot a message** — Telegram bots cannot start a conversation with you.
   Then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy
   `result[0].message.chat.id` (a number; negative for groups).
3. In the repository: **Settings → Secrets and variables → Actions → New repository
   secret**, and add:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`

Verify before relying on it:

```bash
curl -X POST "https://api.telegram.org/bot<TOKEN>/sendMessage" \
  --data-urlencode "chat_id=<CHAT_ID>" \
  --data-urlencode "text=בדיקה"
```

Both secrets are optional — without them the workflow still files the GitHub issue.

Nothing else needs credentials: the Cinema City endpoints are public and unauthenticated,
and `GITHUB_TOKEN` is injected automatically by Actions.

WhatsApp is deliberately not supported: Meta's Cloud API requires a Business account and
a pre-approved template for business-initiated messages, which is far more setup than
this monitor justifies.

### Operational notes

- Scheduled workflows only run on the **default branch**, and GitHub disables them after
  60 days of repository inactivity.
- GitHub is unreliable about firing the *first* scheduled run on a new repository. Do one
  manual run with `TARGET_DATE=today` to confirm the whole chain works.
- Remember to **disable the workflow** when you no longer need it.
