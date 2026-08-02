# VIP cinema monitor

Watches two cinemas for VIP screenings of a given movie on a target date, and shouts the
moment either box office opens that date:

| Cinema | Chain | Halls |
| --- | --- | --- |
| סינמה סיטי גלילות | Cinema City | VIP |
| פלאנט ראשון לציון | Yes Planet | VIP 21–24 |

Both are checked independently every run. One chain opening a date says nothing about the
other, so each gets its own alert and its own line in the status panel.

It calls the same JSON endpoints the chains' own booking UIs call — `/tickets/*` on
cinema-city.co.il, and the `quickbook` data service on planetcinema.co.il — so there is no
browser, no scraping, and no dependencies beyond the standard library.

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
| `THEATER_NAME` | `סינמה סיטי גלילות` | Label used in the report |
| `PLANET_CINEMA_ID` | `1072` | Yes Planet's cinema id for Rishon LeZion |
| `PLANET_THEATER_NAME` | `פלאנט ראשון לציון` | Label used in the report |

## Status values

`artifacts/result.json` holds a per-cinema result under `cinemas[]`, each carrying one of:

- `available` — bookable VIP showtimes exist on the target date. **This is the only status that alerts.**
- `target_date_not_on_sale` — the movie plays VIP, but that date has not been released yet.
- `date_on_sale_but_no_screenings` — the date is open, but the movie is not showing in VIP.
- `movie_not_in_vip_lineup` — no VIP movie matched `MOVIE_QUERY`.
- `venue_type_not_offered` — the cinema lists no VIP screenings at all.

plus `check_failed` — that cinema's site did not answer. A chain's outage is contained to
its own line: Planet being down must not stop Cinema City alerting, so the run still
succeeds, the healthy chain still announces, and the panel shows ⚠️ against the broken one.
Only if **every** cinema fails does the run fail and trigger the failure alert.

The top-level `status` is the **best news any cinema carries**, so it reads `available` as
soon as one of them opens rather than waiting for both.

Every cinema also includes `vip_dates_on_sale`, so you can see exactly how far ahead each
box office has opened while you wait — that is the number that tells you the target date
is getting close.

### How each chain is queried

Cinema City resolves the VIP venue-type id, then the movie id, then dates, then showtimes.

Yes Planet needs only two calls, both filtered server-side by `attr=vip`:

```
/dates/in-cinema/1072/until/<horizon>?attr=vip&lang=he_IL      -> which dates are on sale
/film-events/in-cinema/1072/at-date/<date>?attr=vip&lang=he_IL -> films[] and events[]
```

Two details that are easy to get wrong there:

- **`events[]` covers every film that day**, not just yours, so it has to be joined back to
  `films[]` by `filmId`. Taking it wholesale would announce the wrong movie.
- **The API's own `bookingLink` field is stale** — it points at `/api/order/<id>`, which
  404s. The working URL is `https://tickets5.planetcinema.co.il/order/<id>?lang=he`.
- `vip` is matched as an exact attribute id, never as a substring: `vip-light` is a
  different, cheaper product at other branches.

## GitHub Actions

`.github/workflows/check.yml` runs twice an hour, **only between 06:00 and 22:00 Israel
time** (`cron: "23,53 3-18 * * *"` — GitHub cron is always UTC, and Israel is UTC+3 in
summer). Nothing fires overnight. Choose **Actions → Check Cinema City VIP → Run
workflow** to pass a one-off date or movie at any hour.

The `:23` and `:53` are deliberate. **Scheduled workflows are best-effort**, and GitHub
queues every repository's cron for a given minute at the same moment, then drops whatever
it cannot drain. Those minutes cluster hard on `:00`, then `:30`, then `:15`/`:45` —
because that is what people type — so `0 * * * *` is the single worst slot to ask for.
Measured on this repo while it was still asking for `:00`, every run that did land was 7
to 80 minutes late and several slots never ran at all.

`:23` and `:53` sit in the two widest gaps between those spikes, and `:53` is queued
*ahead* of the `:00` flood rather than behind it. Twice an hour then makes a landed check
per hour likely — but never guaranteed, which is why the status panel labels the next
check **מתוכננת** (planned) rather than stating it as fact.

To force a check immediately, use the **🔄 בדיקה עכשיו** button on the pinned Telegram
message, or **Actions → Check Cinema City VIP → Run workflow** in a browser or in the
GitHub Mobile app. Manual dispatches are not subject to the schedule queue and start
within seconds.

When the target date goes on sale at a cinema you get, **for that cinema**:

- a **Telegram message** listing every showtime with its hall and a booking link, and
- a **GitHub issue** containing `result.md`.

The issue doubles as the "already announced" marker, and the match is on **cinema *and*
date**, not date alone. That matters: if Glilot opens 06/08 first, an issue exists naming
that date — a marker keyed only on the date would then silently swallow the Rishon alert
when Planet opens the same day. Each cinema announces exactly once, independently.

### The pinned status message

Every successful run also rewrites a single **pinned message** at the top of the chat, so
"is this thing still watching, and what did it last see?" is answerable at a glance
without waiting for an alert:

```
📡 ניטור VIP
🎬 ספיידרמן: יום חדש
📅 יעד: יום חמישי 06/08/2026

🟡 סינמה סיטי גלילות
התאריך עדיין לא נפתח למכירה
📆 נפתחו: 02/08, 03/08, 04/08, 05/08

🟢 פלאנט ראשון לציון
כרטיסים זמינים להזמנה
🎟 18:30 · VIP 23
🎟 21:30 · VIP 23

🕐 נבדק לאחרונה: 02/08 19:23
⏭ הבדיקה הבאה (מתוכננת): 02/08 19:53

          [ 🔄 בדיקה עכשיו ]
```

Each cinema gets its own traffic light, so a half-open situation — one chain selling, the
other not — is visible at a glance rather than collapsed into one status. Showtimes carry
the hall name, and each time links straight to its booking page.

The button opens the workflow's Actions page, where **Run workflow** forces a check
immediately — useful precisely because the schedule is best-effort. It is a link and not
a real trigger: dispatching a workflow takes an authenticated API call, and a Telegram
button can only open a URL or call back to a listener this monitor does not have.

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
