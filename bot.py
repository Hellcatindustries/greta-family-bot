"""
Greta — the Fosterlang Household Telegram bot.

Message Greta on Telegram and she files it into the right list in your Notion hub
("The Fosterlang Household"):

    add milk                 -> Shopping List   (plain message, no command needed)
    /shop oat milk           -> Shopping List
    /todo take out the bins  -> To-Do List
    /dream buy a boat        -> Dreams In Motion
    /idea friday pizza       -> Ideas Hub

A plain message with no slash-command goes to your Shopping List. Leading words like
"add", "buy", "get", "need", "grab" are stripped, so "add milk" stores just "milk".

Greta finds your four Notion databases automatically by their titles, so there are
no database IDs to copy. You only need to connect the integration to the hub page once.

Environment variables (set these in Railway):
    TELEGRAM_TOKEN   (required)  from @BotFather   (TELEGRAM_BOT_TOKEN also accepted)
    NOTION_TOKEN     (required)  internal integration secret (starts ntn_ or secret_)
    ALLOWED_USER_ID  (optional)  comma-separated Telegram user IDs allowed to use Greta
                                  (ALLOWED_USER_IDS also accepted)
    DEFAULT_LIST     (optional)  shopping | todo | dream | idea   (default: shopping)
"""

import os
import logging

import httpx
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
)
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
DEFAULT_LIST = os.environ.get("DEFAULT_LIST", "shopping").strip().lower()

_allowed_raw = (
    os.environ.get("ALLOWED_USER_IDS") or os.environ.get("ALLOWED_USER_ID", "")
).strip()
ALLOWED_USER_IDS = (
    {int(x) for x in _allowed_raw.replace(" ", "").split(",") if x} if _allowed_raw else None
)

# Friendly key -> the database TITLE exactly as it appears in your Notion hub.
LIST_TITLES = {
    "shopping": "Shopping List",
    "todo": "To-Do List",
    "dream": "Dreams In Motion",
    "idea": "Ideas Hub",
}
LIST_LABEL = dict(LIST_TITLES)

# Words to trim off the front of a plain shopping message ("add milk" -> "milk").
FILLER_PREFIXES = ("add ", "buy ", "get ", "grab ", "need ", "pick up ", "we need ")

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": NOTION_VERSION,
    "Content-Type": "application/json",
}

# Filled in at startup: list key -> Notion database id
_db_ids: dict[str, str] = {}


# --- Notion helpers ----------------------------------------------------------

async def resolve_databases() -> None:
    """Look up each household database by title and cache its id."""
    async with httpx.AsyncClient(timeout=30) as client:
        for key, title in LIST_TITLES.items():
            try:
                resp = await client.post(
                    "https://api.notion.com/v1/search",
                    headers=NOTION_HEADERS,
                    json={
                        "query": title,
                        "filter": {"property": "object", "value": "database"},
                    },
                )
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                log.error("Notion search failed for '%s': %s", title, exc)
                continue

            for db in resp.json().get("results", []):
                db_title = "".join(
                    t.get("plain_text", "") for t in db.get("title", [])
                ).strip()
                if db_title.lower() == title.lower():
                    _db_ids[key] = db["id"]
                    log.info("Resolved '%s' -> %s", title, db["id"])
                    break
            else:
                log.warning(
                    "Could NOT find a Notion database titled '%s'. "
                    "Is the Greta integration connected to the hub page?",
                    title,
                )


async def add_to_list(key: str, text: str) -> bool:
    """Create a new row (page) with `text` as its Name in the given list."""
    db_id = _db_ids.get(key)
    if not db_id:
        await resolve_databases()  # maybe it was shared after startup
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


# --- Telegram helpers --------------------------------------------------------

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


async def handle(update: Update, key: str, text: str) -> None:
    if not authorized(update):
        await update.message.reply_text("Sorry — you're not on Greta's guest list. 🙈")
        return

    text = (text or "").strip()
    if not text:
        await update.message.reply_text(f"Give me something to add, e.g. /{key} milk")
        return

    if await add_to_list(key, text):
        await update.message.reply_text(f"Added “{text}” to {LIST_LABEL[key]} ✅")
    else:
        await update.message.reply_text(
            f"Couldn't reach {LIST_LABEL[key]} in Notion. "
            "Check the Greta integration is connected to the hub page."
        )


# --- Command handlers --------------------------------------------------------

async def cmd_shop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await handle(update, "shopping", " ".join(context.args))


async def cmd_todo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await handle(update, "todo", " ".join(context.args))


async def cmd_dream(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await handle(update, "dream", " ".join(context.args))


async def cmd_idea(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await handle(update, "idea", " ".join(context.args))


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    await update.message.reply_text(
        "Hi, I'm Greta 👋 I add things to your Fosterlang Household Notion.\n\n"
        "Just message me and it goes to your Shopping List, e.g. “add milk”.\n\n"
        "Or aim it at a specific list:\n"
        "• /shop <item>  — Shopping List\n"
        "• /todo <task>  — To-Do List\n"
        "• /dream <goal> — Dreams In Motion\n"
        "• /idea <idea>  — Ideas Hub"
    )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    key = DEFAULT_LIST if DEFAULT_LIST in LIST_TITLES else "shopping"
    text = update.message.text
    if key == "shopping":
        text = strip_filler(text)
    await handle(update, key, text)


# --- Startup -----------------------------------------------------------------

async def post_init(app: Application) -> None:
    log.info("Greta starting up — resolving Notion databases…")
    await resolve_databases()


def main() -> None:
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler(["shop", "shopping"], cmd_shop))
    app.add_handler(CommandHandler("todo", cmd_todo))
    app.add_handler(CommandHandler("dream", cmd_dream))
    app.add_handler(CommandHandler("idea", cmd_idea))
    app.add_handler(CommandHandler(["help", "start"], cmd_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    log.info("Greta is listening for messages.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
