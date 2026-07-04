# Greta — Fosterlang Household Telegram bot

Message Greta on Telegram and she files it into the right list in your Notion hub,
**instantly**. She writes to Notion directly with her own integration token, so she
keeps working even when your computer is off.

```
/shop milk               ->  Shopping List
/todo take out the bins  ->  To-Do List
/dream buy a boat        ->  Dreams In Motion
/idea friday pizza       ->  Ideas Hub
```

A plain message with no command goes to your default list (Shopping List unless you change `DEFAULT_LIST`).

Greta finds your four databases automatically by their **titles** — you do **not**
need to copy any database IDs. You just have to connect the integration to the hub
page once (Step 2 below).

---

## One-time setup

### 1. Create a Notion integration (gives Greta her Notion key)
1. Go to **https://www.notion.so/profile/integrations** → **New integration**.
2. Name it `Greta`, pick your workspace, type **Internal**, and create it.
3. Copy the **Internal Integration Secret** (starts with `ntn_` or `secret_`).
   This is your `NOTION_TOKEN`. Keep it private — treat it like a password.

### 2. Connect the integration to your hub (so Greta can see the lists)
1. Open **The Fosterlang Household** page in Notion.
2. Click the **•••** menu (top-right) → **Connections** → **Connect to** → choose **Greta**.
3. Confirm. This gives Greta access to the page and the Shopping / To-Do / Dreams /
   Ideas databases inside it.

> If Greta later says she "couldn't reach" a list, it's almost always this step —
> re-open the page's ••• → Connections and make sure **Greta** is listed.

### 3. Telegram
- You already created the bot with **@BotFather**; keep its **token** handy
  (that's `TELEGRAM_BOT_TOKEN`).
- (Optional but recommended) Message **@userinfobot** on Telegram to get your numeric
  user ID, and Nicky's. Put them in `ALLOWED_USER_IDS` (comma-separated) so only you
  two can add things via Greta.

### 4. Put this code in your repo
Add these files (`bot.py`, `requirements.txt`, `Procfile`) to your GitHub repo that's
connected to Railway, then commit & push. Railway will redeploy automatically.

### 5. Set the variables in Railway
In Railway → your service → **Variables**, add (see `.env.example`):

| Variable | Value |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | from @BotFather |
| `NOTION_TOKEN` | the integration secret from Step 1 |
| `ALLOWED_USER_IDS` | e.g. `111111111,222222222` (optional) |
| `DEFAULT_LIST` | `shopping` (optional) |

Make sure the service **start command** is `python bot.py` (the included `Procfile`
sets this as a `worker`). Because Greta uses long-polling she needs **no** public URL
or webhook — one always-on worker is all it takes.

### 6. Try it
Message your bot on Telegram:
```
/help
/shop oat milk
todo call the plumber
```
You should get a ✅ reply, and the item should appear in Notion within a second or two.

---

## How it works (short version)
- **Telegram → Greta:** Telegram pushes each message to the running bot (long-polling),
  so it's effectively instant.
- **Greta → Notion:** on startup she searches Notion for the four databases by title and
  caches their IDs; each message becomes a new row via the Notion API
  (`POST /v1/pages`).
- **Security:** if `ALLOWED_USER_IDS` is set, Greta ignores everyone else.

## Troubleshooting
- **"Couldn't reach <list>"** → integration isn't connected to the hub page (Step 2), or
  a database was renamed. Greta matches on exact titles: `Shopping List`, `To-Do List`,
  `Dreams In Motion`, `Ideas Hub`. If you rename one in Notion, update `LIST_TITLES` in
  `bot.py`.
- **No reply at all** → check Railway logs; usually a missing/incorrect
  `TELEGRAM_BOT_TOKEN`, or another copy of the bot is already polling with the same token.
- **"Not on Greta's guest list"** → your Telegram ID isn't in `ALLOWED_USER_IDS`.
