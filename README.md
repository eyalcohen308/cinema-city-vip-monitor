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

`.github/workflows/check.yml` runs hourly on the hour, **only between 08:00 and 22:00
Israel time** (`cron: "0 5-18 * * *"` — GitHub cron is always UTC, and Israel is UTC+3
in summer). Nothing fires overnight. Choose **Actions → Check Cinema City VIP → Run
workflow** to pass a one-off date or movie at any hour.

When the target date goes on sale you get:

- a **Telegram message** with the showtimes and booking links, and
- a **GitHub issue** containing `result.md`.

The issue doubles as the "already announced" marker, so Telegram fires exactly once per
target date rather than every hour after it opens.

### The pinned status message

Every successful run also rewrites a single **pinned message** at the top of the chat, so
"is this thing still watching, and what did it last see?" is answerable at a glance
without waiting for an alert:

```
📡 ניטור VIP · סינמה סיטי גלילות
🎬 ספיידרמן: יום חדש
📅 יעד: יום חמישי 06/08/2026

🟡 התאריך עדיין לא נפתח למכירה

🕐 נבדק לאחרונה: 01/08 17:00
⏭ הבדיקה הבאה: 01/08 18:00
📆 תאריכים שנפתחו: 01/08/2026, 02/08/2026, 03/08/2026
```

It is edited in place rather than re-sent, so it never notifies and never adds to the
chat history. There is no `/status` command because a bot command needs a process
listening for it, and this monitor is a cron job with nowhere to listen — the pinned
panel gives the same answer with no extra infrastructure and no reply latency.

Two details worth knowing:

- **The pinned message is its own state store.** Nothing records its id between runs; the
  workflow asks Telegram what is currently pinned via `getChat` and checks the sender is
  the bot. Unpin or delete it and the next run posts and pins a fresh one.
- **נבדק לאחרונה is the last *successful* check.** A failed run leaves the panel stale on
  purpose and fires the separate failure alert below, so a stale timestamp sitting next to
  a red alert reads correctly.

In a group, the bot needs the **pin messages** admin right for the first run; in a direct
chat with the bot there is nothing to grant.

One further message exists so that silence is never ambiguous: **on failure**, if a run
errors, Telegram says the monitor has stopped watching.

That leaves exactly two things that ever arrive in the chat — the ticket alert and the
failure alert — and both are worth a notification. Everything else is the silent pinned
edit.

There used to be a daily "✅ הניטור פעיל" heartbeat as well; the pinned panel replaced it.
Be aware of what that trades away. GitHub silently skips scheduled runs, and disables
them entirely after 60 days of repository inactivity — neither of which raises a failure,
because a run that never starts cannot fail. The heartbeat caught that by being *absent*;
the panel catches it only by going stale, which is passive and requires you to look.
`נבדק לאחרונה` more than an hour or two old during the day means the schedule has stopped,
not that there is no news.

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
