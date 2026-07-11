"""
Greta — the Fosterlang Household Telegram bot.

Message Greta in plain English and she works out where it belongs and files it:

    milk / dish soap ............... Shopping List (Notion)
    call the plumber / pay rego .... To-Do List (Notion)
    buy a boat / trip to Japan ..... Dreams In Motion (Notion)
    friday pizza / redo garden ..... Ideas Hub (Notion)
    hair appt Wed 12 July 11:30am .. Outlook calendar

She also reminds you about upcoming Outlook events:
    • a morning briefing            (default 8am)
    • a heads-up ~1 hour before     each event
    • an afternoon update           (default 3pm)
    • /agenda  — see what's coming, on demand

Environment variables (set in Railway):
    TELEGRAM_TOKEN     (required)  from @BotFather  (TELEGRAM_BOT_TOKEN also works)
    NOTION_TOKEN       (required)  Notion internal integration secret (ntn_...)
    ANTHROPIC_API_KEY  (optional)  enables smart routing to all sections
    ALLOWED_USER_ID    (optional)  comma-separated Telegram user IDs (also who gets reminders)
    CLASSIFIER_MODEL   (optional)  default: claude-haiku-4-5-20251001
    # Outlook calendar (Microsoft Graph, client-credentials):
    MS_CLIENT_ID, MS_CLIENT_SECRET, MS_TENANT_ID, MS_USER_EMAIL
    TIMEZONE           (optional)  IANA tz (default: Australia/Sydney)
    MORNING_HOUR       (optional)  default 8    AFTERNOON_HOUR default 15
    PRE_EVENT_MIN      (optional)  minutes before an event to remind (default 60)
"""

import os
import re
import json
import logging
from datetime import datetime, timedelta, time as dtime

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None

import httpx
import dateparser
from dateparser.search import search_dates
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
log = logging.getLogger("greta")

# --- Configuration -----------------------------------------------------------

TELEGRAM_TOKEN = (
    os.environ.get("TELEGRAM_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN") or ""
).strip()
if not TELEGRAM_TOKEN:
    raise SystemExit("Set TELEGRAM_TOKEN (or TELEGRAM_BOT_TOKEN) in the environment.")

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "").strip()
if not NOTION_TOKEN:
    raise SystemExit("Set NOTION_TOKEN in the environment.")

NOTION_VERSION = "2022-06-28"

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
CLASSIFIER_MODEL = os.environ.get("CLASSIFIER_MODEL", "claude-haiku-4-5-20251001").strip()
SMART = bool(ANTHROPIC_API_KEY)


def _int_env(name, default):
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


_allowed_raw = (
    os.environ.get("ALLOWED_USER_IDS") or os.environ.get("ALLOWED_USER_ID", "")
).strip()
ALLOWED_USER_IDS = (
    {int(x) for x in _allowed_raw.replace(" ", "").split(",") if x} if _allowed_raw else None
)

MS_CLIENT_ID = os.environ.get("MS_CLIENT_ID", "").strip()
MS_CLIENT_SECRET = os.environ.get("MS_CLIENT_SECRET", "").strip()
MS_TENANT_ID = os.environ.get("MS_TENANT_ID", "").strip()
MS_USER_EMAIL = os.environ.get("MS_USER_EMAIL", "").strip()
TIMEZONE = os.environ.get("TIMEZONE", "Australia/Sydney").strip()
CALENDAR_ENABLED = all([MS_CLIENT_ID, MS_CLIENT_SECRET, MS_TENANT_ID, MS_USER_EMAIL])

MORNING_HOUR = _int_env("MORNING_HOUR", 8)
AFTERNOON_HOUR = _int_env("AFTERNOON_HOUR", 15)
PRE_EVENT_MIN = _int_env("PRE_EVENT_MIN", 60)
# Reminders need a calendar to read and someone to message.
REMINDERS_ENABLED = CALENDAR_ENABLED and bool(ALLOWED_USER_IDS)

LIST_TITLES = {
    "shopping": "Shopping List",
    "todo": "To-Do List",
    "dream": "Dreams In Motion",
    "idea": "Ideas Hub",
}
LIST_LABEL = dict(LIST_TITLES)
SECTIONS = set(LIST_TITLES) | {"calendar"}

FILLER_PREFIXES = ("add ", "buy ", "get ", "grab ", "need ", "pick up ", "we need ")
TIME_RE = re.compile(r"\b(\d{1,2}\s*:\s*\d{2}\s*(am|pm)?|\d{1,2}\s*(am|pm))\b", re.I)
DURATION_RE = re.compile(r"for\s+(\d+)\s*(min|mins|minute|minutes|hour|hours|hr|hrs)", re.I)

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": NOTION_VERSION,
    "Content-Type": "application/json",
}

_db_ids: dict[str, str] = {}
_reminded: set[str] = set()


def _tz():
    if ZoneInfo is not None:
        try:
            return ZoneInfo(TIMEZONE)
        except Exception:  # pragma: no cover
            return None
    return None


def now_local() -> datetime:
    tz = _tz()
    return datetime.now(tz) if tz else datetime.now()


# --- Notion ------------------------------------------------------------------

async def resolve_databases() -> None:
    async with httpx.AsyncClient(timeout=30) as client:
        for key, title in LIST_TITLES.items():
            try:
                resp = await client.post(
                    "https://api.notion.com/v1/search",
                    headers=NOTION_HEADERS,
                    json={"query": title, "filter": {"property": "object", "value": "database"}},
                )
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                log.error("Notion search failed for '%s': %s", title, exc)
                continue
            for db in resp.json().get("results", []):
                db_title = "".join(t.get("plain_text", "") for t in db.get("title", [])).strip()
                if db_title.lower() == title.lower():
                    _db_ids[key] = db["id"]
                    log.info("Resolved '%s' -> %s", title, db["id"])
                    break
            else:
                log.warning("Could NOT find a Notion database titled '%s'.", title)


async def add_to_list(key: str, text: str) -> bool:
    db_id = _db_ids.get(key)
    if not db_id:
        await resolve_databases()
        db_id = _db_ids.get(key)
    if not db_id:
        return False
    payload = {
        "parent": {"database_id": db_id},
        "properties": {"Name": {"title": [{"text": {"content": text}}]}},
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.notion.com/v1/pages", headers=NOTION_HEADERS, json=payload
        )
    if resp.status_code >= 300:
        log.error("Notion create failed (%s): %s", resp.status_code, resp.text)
        return False
    return True


# --- Microsoft Graph (Outlook calendar) --------------------------------------

async def graph_token() -> str:
    url = f"https://login.microsoftonline.com/{MS_TENANT_ID}/oauth2/v2.0/token"
    data = {
        "client_id": MS_CLIENT_ID,
        "client_secret": MS_CLIENT_SECRET,
        "scope": "https://graph.microsoft.com/.default",
        "grant_type": "client_credentials",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, data=data)
        r.raise_for_status()
        return r.json()["access_token"]


async def create_event(subject, start_dt, end_dt):
    try:
        token = await graph_token()
    except Exception as exc:  # noqa: BLE001
        log.error("Graph token error: %s", exc)
        return False, "couldn't sign in to Microsoft"
    url = f"https://graph.microsoft.com/v1.0/users/{MS_USER_EMAIL}/events"
    body = {
        "subject": subject,
        "start": {"dateTime": start_dt.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": TIMEZONE},
        "end": {"dateTime": end_dt.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": TIMEZONE},
    }
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            url,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=body,
        )
    if r.status_code >= 300:
        log.error("Graph create event failed (%s): %s", r.status_code, r.text)
        if r.status_code in (401, 403):
            return False, "the app needs Calendars.ReadWrite permission in Azure"
        return False, "Microsoft rejected the event"
    return True, "ok"


async def list_events(start_dt, end_dt):
    """Return Outlook events between two datetimes (tz-aware), soonest first."""
    try:
        token = await graph_token()
    except Exception as exc:  # noqa: BLE001
        log.error("Graph token error (list): %s", exc)
        return []
    url = f"https://graph.microsoft.com/v1.0/users/{MS_USER_EMAIL}/calendarView"
    params = {
        "startDateTime": start_dt.isoformat(),
        "endDateTime": end_dt.isoformat(),
        "$orderby": "start/dateTime",
        "$top": "50",
    }
    headers = {"Authorization": f"Bearer {token}", "Prefer": f'outlook.timezone="{TIMEZONE}"'}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url, headers=headers, params=params)
    if r.status_code >= 300:
        log.error("Graph list events failed (%s): %s", r.status_code, r.text)
        return []
    return r.json().get("value", [])


def _event_start(ev):
    s = (ev.get("start", {}) or {}).get("dateTime", "")[:19]
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def parse_event_from_text(text: str):
    """Fallback date parser -> (subject, start_dt, end_dt) or None."""
    dur_min = 60
    m = DURATION_RE.search(text)
    if m:
        n = int(m.group(1))
        dur_min = n * 60 if m.group(2).lower().startswith(("h", "hr")) else n
    text_wo_dur = DURATION_RE.sub(" ", text)
    found = search_dates(
        text_wo_dur,
        languages=["en"],
        settings={"PREFER_DATES_FROM": "future", "RETURN_AS_TIMEZONE_AWARE": False},
    )
    if not found:
        return None
    phrase, start_dt = found[-1]
    subject = strip_filler(text_wo_dur.replace(phrase, " "))
    subject = re.sub(r"\b(on|at|the)\b", " ", subject, flags=re.I)
    subject = re.sub(r"\s+", " ", subject).strip(" ,.-")
    return (subject or "Appointment", start_dt, start_dt + timedelta(minutes=dur_min))


# --- AI classifier -----------------------------------------------------------

CLASSIFY_SYSTEM = (
    "You are Greta, a family household assistant. Read the user's message and put it "
    "in exactly ONE section, with a cleaned short title.\n\n"
    "Sections:\n"
    "- shopping: groceries/household items to buy (milk, dish soap, batteries)\n"
    "- todo: tasks, chores, errands, reminders to do (call plumber, pay rego, mow lawn)\n"
    "- dream: big long-term family goals/aspirations (buy a boat, trip to Japan, get married)\n"
    "- idea: fun ideas or suggestions to consider (pizza night, redo the garden, weekend away)\n"
    "- calendar: an appointment/event with a specific date and time (hair appt Wed 11:30am)\n\n"
    'Return ONLY minified JSON: {{"section":"shopping|todo|dream|idea|calendar",'
    '"title":"short cleaned title","start":"YYYY-MM-DDTHH:MM or null",'
    '"duration_min":integer or null}}\n'
    "Strip filler like 'add', 'remember to', 'we need to'. For calendar resolve the "
    "date/time to local ISO and default duration to 60 unless stated. "
    "Today is {today} ({tz})."
)


async def classify(text: str):
    system = CLASSIFY_SYSTEM.format(today=now_local().strftime("%A %Y-%m-%d %H:%M"), tz=TIMEZONE)
    body = {
        "model": CLASSIFIER_MODEL,
        "max_tokens": 200,
        "system": system,
        "messages": [{"role": "user", "content": text}],
    }
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post("https://api.anthropic.com/v1/messages", headers=headers, json=body)
            r.raise_for_status()
            content = r.json()["content"][0]["text"]
        match = re.search(r"\{.*\}", content, re.S)
        data = json.loads(match.group(0)) if match else None
        if data and data.get("section") in SECTIONS:
            return data
    except Exception as exc:  # noqa: BLE001
        log.error("Classifier error: %s", exc)
    return None


# --- Helpers -----------------------------------------------------------------

def authorized(update: Update) -> bool:
    if ALLOWED_USER_IDS is None:
        return True
    user = update.effective_user
    return bool(user and user.id in ALLOWED_USER_IDS)


def strip_filler(text: str) -> str:
    lowered = text.lower()
    for prefix in FILLER_PREFIXES:
        if lowered.startswith(prefix):
            return text[len(prefix):].strip()
    return text


def fmt(dt) -> str:
    return dt.strftime("%a %d %b, %I:%M %p").replace(" 0", " ")


def _clock(dt) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")


def format_agenda(header: str, events) -> str:
    if not events:
        return header + "\n\nNothing on the calendar — enjoy the breathing room. 🎉"
    lines = [header]
    current_day = None
    for ev in events:
        dt = _event_start(ev)
        if dt is None:
            continue
        day = dt.strftime("%A %d %b")
        if day != current_day:
            lines.append(f"\n📅 {day}")
            current_day = day
        lines.append(f"  • {_clock(dt)} — {ev.get('subject', '(no title)')}")
    return "\n".join(lines)


async def file_to_list(update: Update, key: str, text: str) -> None:
    text = (text or "").strip()
    if not text:
        await update.message.reply_text("Give me something to add, e.g. milk")
        return
    if await add_to_list(key, text):
        await update.message.reply_text(f"Added “{text}” to {LIST_LABEL[key]} ✅")
    else:
        await update.message.reply_text(
            f"Couldn't reach {LIST_LABEL[key]} in Notion — check Greta is connected to the hub page."
        )


async def file_to_calendar(update: Update, subject, start_dt, end_dt) -> None:
    ok, msg = await create_event(subject or "Appointment", start_dt, end_dt)
    if ok:
        await update.message.reply_text(
            f"📅 Added “{subject}” to your Outlook calendar\n{fmt(start_dt)} – {_clock(end_dt)}"
        )
    else:
        await update.message.reply_text(f"Couldn't add that to the calendar — {msg}.")


async def route_calendar(update: Update, text, data=None) -> None:
    if not CALENDAR_ENABLED:
        await file_to_list(update, "todo", (data or {}).get("title") or strip_filler(text))
        return
    start_dt = end_dt = subject = None
    if data and data.get("start"):
        try:
            start_dt = datetime.fromisoformat(data["start"])
        except Exception:  # noqa: BLE001
            start_dt = dateparser.parse(data["start"])
        if start_dt:
            dur = int(data.get("duration_min") or 60)
            end_dt = start_dt + timedelta(minutes=dur)
            subject = data.get("title")
    if start_dt is None:
        parsed = parse_event_from_text(text)
        if not parsed:
            await update.message.reply_text(
                "I couldn't work out the date/time. Try e.g. “hair appt Wed 12 July 11:30am for 30 min”."
            )
            return
        subject, start_dt, end_dt = parsed
    await file_to_calendar(update, subject, start_dt, end_dt)


# --- Reminders (scheduled jobs) ----------------------------------------------

async def _broadcast(context, text):
    for uid in (ALLOWED_USER_IDS or set()):
        try:
            await context.bot.send_message(chat_id=uid, text=text)
        except Exception as exc:  # noqa: BLE001
            log.error("Reminder send to %s failed: %s", uid, exc)


async def morning_briefing(context):
    now = now_local()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    events = await list_events(start, start + timedelta(days=2))
    await _broadcast(context, format_agenda("☀️ Good morning! Here's what's coming up:", events))


async def afternoon_update(context):
    now = now_local()
    end = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=2)
    events = await list_events(now, end)
    await _broadcast(context, format_agenda("👋 Afternoon check-in — still ahead:", events))


async def check_upcoming(context):
    now = now_local()
    events = await list_events(now, now + timedelta(minutes=PRE_EVENT_MIN + 5))
    now_naive = now.replace(tzinfo=None)
    for ev in events:
        dt = _event_start(ev)
        if dt is None:
            continue
        mins = (dt - now_naive).total_seconds() / 60
        eid = ev.get("id", "")
        if 0 <= mins <= PRE_EVENT_MIN and eid and eid not in _reminded:
            _reminded.add(eid)
            await _broadcast(
                context,
                f"⏰ Coming up in ~{int(round(mins))} min: {ev.get('subject', '(event)')} at {_clock(dt)}",
            )


# --- Command handlers --------------------------------------------------------

async def cmd_shop(u, c):
    if authorized(u): await file_to_list(u, "shopping", " ".join(c.args))
async def cmd_todo(u, c):
    if authorized(u): await file_to_list(u, "todo", " ".join(c.args))
async def cmd_dream(u, c):
    if authorized(u): await file_to_list(u, "dream", " ".join(c.args))
async def cmd_idea(u, c):
    if authorized(u): await file_to_list(u, "idea", " ".join(c.args))
async def cmd_cal(u, c):
    if authorized(u): await route_calendar(u, " ".join(c.args))


async def cmd_agenda(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    if not CALENDAR_ENABLED:
        await update.message.reply_text("The calendar isn't switched on yet.")
        return
    now = now_local()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    events = await list_events(start, start + timedelta(days=3))
    await update.message.reply_text(format_agenda("📅 Here's your agenda:", events))


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    await update.message.reply_text(
        "Hi, I'm Greta 👋 Just message me in plain English and I'll file it in the right place:\n\n"
        "• “milk” → Shopping\n"
        "• “call the plumber” → To-Do\n"
        "• “trip to Japan” → Dreams\n"
        "• “friday pizza night” → Ideas\n"
        "• “dentist Tuesday 2pm for 30 min” → Outlook calendar\n\n"
        "I'll also remind you about calendar events (morning, ~1 hr before, and mid-afternoon).\n"
        "• /agenda — see what's coming up\n"
        "• force a section: /shop /todo /dream /idea /cal"
    )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        await update.message.reply_text("Sorry — you're not on Greta's guest list. 🙈")
        return
    text = (update.message.text or "").strip()
    if not text:
        return

    data = await classify(text) if SMART else None
    section = data.get("section") if data else None

    if section is None:
        section = "calendar" if (CALENDAR_ENABLED and TIME_RE.search(text)) else "shopping"

    if section == "calendar":
        await route_calendar(update, text, data)
        return

    title = (data or {}).get("title") if data else None
    if not title:
        title = strip_filler(text) if section == "shopping" else text
    await file_to_list(update, section, title)


# --- Startup -----------------------------------------------------------------

async def post_init(app: Application) -> None:
    log.info("Greta starting up — resolving Notion databases…")
    await resolve_databases()
    log.info(
        "Smart routing: %s | Calendar: %s | Reminders: %s",
        SMART, CALENDAR_ENABLED, REMINDERS_ENABLED,
    )


def main() -> None:
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler(["shop", "shopping"], cmd_shop))
    app.add_handler(CommandHandler("todo", cmd_todo))
    app.add_handler(CommandHandler("dream", cmd_dream))
    app.add_handler(CommandHandler("idea", cmd_idea))
    app.add_handler(CommandHandler(["cal", "appt", "event", "calendar"], cmd_cal))
    app.add_handler(CommandHandler(["agenda", "today", "upcoming"], cmd_agenda))
    app.add_handler(CommandHandler(["help", "start"], cmd_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    if REMINDERS_ENABLED and app.job_queue is not None:
        tz = _tz()
        app.job_queue.run_daily(morning_briefing, time=dtime(MORNING_HOUR, 0, tzinfo=tz))
        app.job_queue.run_daily(afternoon_update, time=dtime(AFTERNOON_HOUR, 0, tzinfo=tz))
        app.job_queue.run_repeating(check_upcoming, interval=900, first=120)
        log.info(
            "Reminders on: morning %02d:00, afternoon %02d:00, %d-min pre-event.",
            MORNING_HOUR, AFTERNOON_HOUR, PRE_EVENT_MIN,
        )

    log.info("Greta is listening for messages.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
