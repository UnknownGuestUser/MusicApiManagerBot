import html
import logging
import os
import time

import httpx
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

# =========================================================
# .env LOADER  (no external dependency)
# =========================================================
# Values already present in the real environment always win, so the systemd
# EnvironmentFile= keeps working exactly as before.
# =========================================================

ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def _load_env_file(path: str) -> None:
    """Minimal .env reader — only full-line "#" comments are comments."""
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, _, value = line.partition("=")
                name = name.strip()
                if name.startswith("export "):
                    name = name[7:].strip()
                value = value.strip().strip('"').strip("'")
                if name and name not in os.environ:
                    os.environ[name] = value
    except Exception as exc:
        print(f"⚠️ .env read error: {exc}", flush=True)


_load_env_file(ENV_FILE)


def _env(name: str, default: str = "", required: bool = False) -> str:
    """Read an environment variable, optionally failing fast."""
    value = os.environ.get(name)
    if value is None or not str(value).strip():
        if required:
            raise SystemExit(
                f"\n❌ Missing required environment variable: {name}\n"
                f"   Add it to {ENV_FILE} (or the service environment) "
                f"and restart.\n"
            )
        return default
    return str(value).strip()


def _env_int(name: str, default: int, minimum: int = None,
             maximum: int = None) -> int:
    """Read an integer environment variable with a readable error."""
    raw = _env(name)
    value = default

    if raw:
        try:
            value = int(raw)
        except ValueError:
            raise SystemExit(
                f"\n❌ {name} must be a whole number, got: {raw}\n"
                f"   Fix it in {ENV_FILE} and restart.\n"
            )

    if minimum is not None and value < minimum:
        raise SystemExit(
            f"\n❌ {name} must be at least {minimum}, got: {value}\n"
            f"   Fix it in {ENV_FILE} and restart.\n"
        )
    if maximum is not None and value > maximum:
        raise SystemExit(
            f"\n❌ {name} must be at most {maximum}, got: {value}\n"
            f"   Fix it in {ENV_FILE} and restart.\n"
        )

    return value


# =========================================================
# CONFIG  — no secret is stored in this file
# =========================================================

# The user-facing bot. Falls back to the storage bot token if you use one
# bot for everything.
BOT_TOKEN = _env("MUSIC_BOT_TOKEN") or _env("TELEGRAM_BOT_TOKEN")

BOT_NAME = _env("BOT_NAME", "Music Api Manager Bot")

# Where the FastAPI app listens. Keep it on localhost.
API_BASE = _env("MUSIC_API_BASE", "https://conferencing-output-smith-distributions.trycloudflare.com").rstrip("/")

# Must be identical to BOT_API_SECRET on the API side.
BOT_SECRET = _env("BOT_API_SECRET")

# Master password of the API (only needed by the owner /genkey and /revoke).
MASTER_PASSWORD = _env("API_MASTER")

# ---- FREE PLAN ----------------------------------------------------
FREE_DAILY_LIMIT = _env_int("FREE_DAILY_LIMIT", 100000, minimum=1)
FREE_KEY_DAYS = _env_int("FREE_KEY_DAYS", 30, minimum=1)

# ---- LINKS / IDENTITY ---------------------------------------------
WELCOME_IMAGE = _env("WELCOME_IMAGE", "https://files.catbox.moe/u25smy.jpg")

SUPPORT_CHAT = _env("SUPPORT_CHAT_URL", "https://t.me/KomalBotSupport")
SUPPORT_CHANNEL = _env("SUPPORT_CHANNEL_URL", "https://t.me/Thekomalbots")
OWNER_USERNAME = _env("OWNER_USERNAME", "@Wtf_IDontCare")
OWNER_URL = _env("OWNER_URL", "https://t.me/Wtf_IDontCare")

# The two Channel buttons in the main menu (URL buttons — no callback).
DEFAULT_CHANNEL_1 = "https://t.me/TheKomalBots"
DEFAULT_CHANNEL_2 = "https://t.me/ShrutiBots"
CHANNEL_1_URL = _env("CHANNEL_1_URL", DEFAULT_CHANNEL_1)
CHANNEL_2_URL = _env("CHANNEL_2_URL", DEFAULT_CHANNEL_2)

# Numeric Telegram ID of the owner (send /id to the bot to find yours).
# 0 = numeric check disabled; OWNER_USERNAME above still grants owner rights.
OWNER_ID = _env_int("OWNER_ID", 0, minimum=0)

# ---- TELEGRAM MESSAGE LIMITS --------------------------------------
CAPTION_LIMIT = 1024   # photo / media captions
TEXT_LIMIT = 4096      # plain text messages

# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)

log = logging.getLogger("music.bot")

# =========================================================
# SMALL HELPERS
# =========================================================


def esc(value) -> str:
    """Escape user-supplied text for HTML parse mode."""
    return html.escape(str(value if value is not None else ""), quote=False)


def num(value) -> str:
    """1234567 -> '1,234,567' (matches the usage screen style)."""
    try:
        return f"{int(value or 0):,}"
    except (TypeError, ValueError):
        return "0"


def human_time(seconds) -> str:
    """3599 -> '59m 59s', 90000 -> '1d 1h'."""
    try:
        seconds = int(max(0, float(seconds)))
    except Exception:
        return "—"
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, secs = divmod(seconds, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def is_owner(user) -> bool:
    if user is None:
        return False
    if OWNER_ID and int(user.id) == OWNER_ID:
        return True
    uname = (user.username or "").lower()
    return uname == OWNER_USERNAME.lstrip("@").lower()


# =========================================================
# API CLIENT
# =========================================================


async def api_get(path: str, **params):
    """GET on the Music API. Returns (status_code, dict). 0 = network error."""
    params["secret"] = BOT_SECRET
    url = f"{API_BASE}{path}"
    try:
        async with httpx.AsyncClient(timeout=25) as client:
            resp = await client.get(url, params=params)
            try:
                data = resp.json()
            except Exception:
                data = {"raw": resp.text[:400]}
            return resp.status_code, data
    except Exception as exc:
        log.warning("API call failed %s: %s", path, exc)
        return 0, {"error": str(exc)}


API_DOWN_TEXT = (
    "⚠️ <b>API is not responding right now.</b>\n\n"
    "Your key was not lost — please try again in a moment.\n"
    "If it keeps failing, report it in the support chat."
)


# =========================================================
# KEYBOARDS
# =========================================================


def main_menu() -> InlineKeyboardMarkup:
    """The exact main-menu layout: one full-width row, then 2-column rows."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("View Your Key", callback_data="key")],
            [
                InlineKeyboardButton("Usage", callback_data="usage"),
                InlineKeyboardButton("Upgrade", callback_data="upgrade"),
            ],
            [
                InlineKeyboardButton("API Docs", callback_data="docs"),
                InlineKeyboardButton("Support", callback_data="support"),
            ],
            [
                InlineKeyboardButton("Channel 1", url=CHANNEL_1_URL),
                InlineKeyboardButton("Channel 2", url=CHANNEL_2_URL),
            ],
        ]
    )


def back_menu() -> InlineKeyboardMarkup:
    """Every sub-screen ends with a full-width Back button."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Back", callback_data="menu")]]
    )


def key_screen_menu() -> InlineKeyboardMarkup:
    """The reference layout: three full-width rows on the key screen."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Renew", callback_data="renew")],
            [InlineKeyboardButton("Revoke Key (Leaked?)", callback_data="revoke_ask")],
            [InlineKeyboardButton("Back", callback_data="menu")],
        ]
    )


def revoke_confirm_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Yes, revoke now", callback_data="revoke_do")],
            [InlineKeyboardButton("Cancel", callback_data="key")],
        ]
    )


def upgrade_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("👑 Message Owner", url=OWNER_URL)],
            [InlineKeyboardButton("Back", callback_data="menu")],
        ]
    )


def support_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("💬 Support Chat", url=SUPPORT_CHAT)],
            [InlineKeyboardButton("📢 Support Channel", url=SUPPORT_CHANNEL)],
            [InlineKeyboardButton(f"👑 {OWNER_USERNAME}", url=OWNER_URL)],
            [InlineKeyboardButton("Back", callback_data="menu")],
        ]
    )


# =========================================================
# SCREEN TEXT
# =========================================================


def welcome_text(first_name: str, data: dict = None) -> str:
    """The /start screen: Main Menu header + greeting + key status."""
    lines = [
        "🏠 <b>Main Menu</b>",
        "",
        f"Welcome <b>{esc(first_name)}</b>!",
        "",
        f"I'm <b>{esc(BOT_NAME)}</b>. Use me to manage your API keys.",
        "",
    ]

    if data and data.get("key"):
        lines.append("🔑 You have an <b>active API key</b>:")
        lines.append("")
        lines.append(f"<code>{esc(data['key'])}</code>")
    else:
        lines.append("🔑 You don't have an API key yet.")
        lines.append(
            "Tap <b>View Your Key</b> below and one is generated for you — "
            f"<b>{FREE_DAILY_LIMIT} requests/day</b> for <b>{FREE_KEY_DAYS} days</b>."
        )

    return "\n".join(lines)


def key_status(data: dict) -> str:
    """Active / Expired / Daily limit reached — always from live values."""
    expires = data.get("expires")
    days_left = data.get("days_left")

    try:
        expired = bool(expires) and time.time() >= float(expires)
    except (TypeError, ValueError):
        expired = False

    if expired or (days_left == 0 and not data.get("active")):
        return "Expired"

    if data.get("active"):
        return "Active"

    reset_in = human_time(data.get("next_reset_in"))
    if reset_in and reset_in != "—":
        return f"Daily limit reached — resets in {reset_in}"
    return "Daily limit reached"


def days_left_text(value) -> str:
    if value is None:
        return "Unlimited"
    try:
        value = int(value)
    except (TypeError, ValueError):
        return "—"
    if value <= 0:
        return "0 days"
    return "1 day" if value == 1 else f"{value} days"


def key_text(data: dict) -> str:
    """The key screen — every figure is live, straight from the API."""
    key = data.get("key") or "—"
    daily_limit = int(data.get("daily_limit") or 0)

    return (
        "🔑 <b>Your API Key</b>\n"
        "\n"
        f"<b>API Key:</b> <code>{esc(key)}</code>\n"
        f"<b>Status:</b> {esc(key_status(data))}\n"
        f"<b>Plan:</b> {esc(data.get('plan') or 'Free')}\n"
        f"<b>Daily Limit:</b> "
        f"{num(daily_limit) if daily_limit else 'Unlimited'}\n"
        "\n"
        "<b>Today's Usage:</b>\n"
        f"Requests: {num(data.get('today_requests'))}\n"
        f"Audio: {num(data.get('today_audio'))}\n"
        f"Video: {num(data.get('today_video'))}\n"
        "\n"
        "<b>All-Time Usage:</b>\n"
        f"Total Requests: {num(data.get('total_requests'))}\n"
        f"Total Audio: {num(data.get('total_audio'))}\n"
        f"Total Video: {num(data.get('total_video'))}\n"
        "\n"
        f"<b>Created:</b> {esc(data.get('created_str') or '—')}\n"
        f"<b>Expires:</b> {esc(data.get('expires_str') or '—')}\n"
        f"<b>Days Left:</b> {esc(days_left_text(data.get('days_left')))}\n"
        "\n"
        f"🌐 <code>{esc(API_BASE)}/download?url=Kesariya&amp;type=audio"
        f"&amp;api_key={esc(key)}</code>\n"
        "\n"
        "Keep this key private — anyone holding it can spend your requests."
    )


def usage_text(data: dict = None) -> str:
    """The usage screen: today + all-time, split into requests/audio/video."""
    if not data or not data.get("key"):
        return (
            "📊 <b>Your Usage</b>\n"
            "\n"
            "You don't have an API key yet.\n"
            "Tap <b>View Your Key</b> to generate one."
        )

    return (
        "📊 <b>Your Usage</b>\n"
        "\n"
        "<b>Today:</b>\n"
        f"Requests: {num(data.get('today_requests'))}\n"
        f"Audio: {num(data.get('today_audio'))}\n"
        f"Video: {num(data.get('today_video'))}\n"
        "\n"
        "<b>All-Time:</b>\n"
        f"Total: {num(data.get('total_requests'))}\n"
        f"Audio: {num(data.get('total_audio'))}\n"
        f"Video: {num(data.get('total_video'))}\n"
        "\n"
        f"♻️ Daily counter resets in <b>{human_time(data.get('next_reset_in'))}</b>."
    )


REVOKE_CONFIRM_TEXT = (
    "⚠️ <b>Revoke this key?</b>\n"
    "\n"
    "Your current key stops working <b>immediately</b>.\n"
    "\n"
    "A replacement is issued straight away with the <b>same plan, the same "
    "expiry and the same usage</b> — you keep your remaining quota and your "
    "days left, only the secret changes.\n"
    "\n"
    "If your key leaked, revoke it now and paste the new one into your app."
)


def renew_locked_text(data: dict) -> str:
    return (
        "⏳ <b>Renew is not available yet</b>\n"
        "\n"
        f"Your key is still active until <b>{esc(data.get('expires_str') or '—')}</b>"
        f" — <b>{esc(days_left_text(data.get('days_left')))}</b> left.\n"
        "\n"
        "Renew unlocks once the current key expires. Renewing early is blocked "
        "on purpose: it would reset your daily usage."
    )


UPGRADE_TEXT = (
    "⬆️ <b>Upgrade</b>\n"
    "\n"
    f"The free plan gives you <b>{FREE_DAILY_LIMIT} requests per day</b> for "
    f"<b>{FREE_KEY_DAYS} days</b>, on both the audio and the video endpoint.\n"
    "\n"
    "<b>Upgrade unlocks:</b>\n"
    "• Higher daily limits\n"
    "• An unlimited key with no daily cap\n"
    "• Higher concurrency for bulk jobs\n"
    "• Priority support\n"
    "• White-label / custom deployments\n"
    "\n"
    "Message the owner to arrange it — you are talking to the person who "
    "runs the service, not a ticket queue."
)

DOCS_TEXT = (
    "📖 <b>API Docs</b>\n"
    "\n"
    "🌐 <b>Base URL</b>\n"
    "<code>{base}</code>\n"
    "\n"
    "🔑 <b>Auth</b> — <code>?api_key=KEY</code> or <code>X-API-Key</code> header\n"
    "\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "🎧 <b>Audio</b>\n"
    "<code>{base}/download?url=Kesariya&amp;type=audio&amp;api_key=KEY</code>\n"
    "\n"
    "🎬 <b>Video</b>\n"
    "<code>{base}/download?url=https://youtu.be/VIDEO_ID&amp;type=video&amp;api_key=KEY</code>\n"
    "\n"
    "🔁 <b>Stream (token)</b>\n"
    "<code>{base}/stream/VIDEO_ID?type=audio&amp;token=TOKEN&amp;api_key=KEY</code>\n"
    "\n"
    "📊 <b>Status</b> — <code>{base}/status</code>\n"
    "\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "ℹ️ <b>Notes</b>\n"
    "• <code>url</code> = song name, YouTube link or video ID\n"
    "• <code>type</code> = <code>audio</code> (default) or <code>video</code>\n"
    "• A song name resolves to the best matching track\n"
    "• Repeats come from cache instantly\n"
    "• Tokens last 20 min, bound to one video ID\n"
    "• Over limit → HTTP <b>429</b> + retry-after seconds\n"
    "• Wrong or missing key → HTTP <b>403</b>\n"
    "• Files up to 2 GB"
)

SUPPORT_TEXT = (
    "🆘 <b>Support</b>\n"
    "\n"
    "💬 <b>Support Chat</b>\n"
    "Key problems, bugs, limit increases, integration help.\n"
    f"{SUPPORT_CHAT}\n"
    "\n"
    "📢 <b>Support Channel</b>\n"
    "Announcements, uptime notices and new features.\n"
    f"{SUPPORT_CHANNEL}\n"
    "\n"
    "👑 <b>Owner</b>\n"
    "Business enquiries, reseller pricing, custom deployments.\n"
    f"{OWNER_USERNAME} — {OWNER_URL}"
)

HELP_TEXT = (
    "🤖 <b>MUSIC.API BOT — COMMANDS</b>\n"
    "\n"
    "/start — welcome &amp; main menu\n"
    "/getkey — generate your free API key\n"
    "/mykey — key, quota, reset timer, expiry\n"
    "/usage — today &amp; all-time usage\n"
    "/docs — full API documentation\n"
    "/support — support chat, channels &amp; owner\n"
    "/id — your Telegram numeric ID\n"
    "/help — this message\n"
    "\n"
    "👑 <b>Owner only</b>\n"
    "/stats — key &amp; request statistics\n"
    "/genkey &lt;tg_id&gt; [days] [daily] — custom key\n"
    "/revoke &lt;key&gt; — delete a key\n"
    "\n"
    f"🎁 Free plan: <b>{FREE_DAILY_LIMIT} requests/day</b>, "
    f"<b>{FREE_KEY_DAYS} days</b>, auto-reset every 24h."
)


# =========================================================
# SCREEN RENDERING
# =========================================================


async def show(query, text: str, markup):
    """Edit the current message when possible, otherwise send a new one."""
    msg = query.message

    # A photo message can only be edited as a caption, and Telegram caps
    # captions at 1024 chars. Anything longer must be sent as its own text
    # message instead of failing with "message caption is too long".
    as_caption = bool(msg.photo) or msg.caption is not None
    if as_caption and len(text) > CAPTION_LIMIT:
        as_caption = False

    try:
        if as_caption:
            await query.edit_message_caption(
                caption=text, parse_mode=ParseMode.HTML, reply_markup=markup
            )
        else:
            await query.edit_message_text(
                text=text, parse_mode=ParseMode.HTML,
                reply_markup=markup, disable_web_page_preview=True,
            )
        return
    except BadRequest as exc:
        if "not modified" in str(exc).lower():
            return
    except Exception as exc:
        log.warning("edit failed: %s", exc)

    try:
        await msg.reply_text(
            text, parse_mode=ParseMode.HTML,
            reply_markup=markup, disable_web_page_preview=True,
        )
    except BadRequest as exc:
        log.warning("reply failed: %s", exc)


async def fetch_usage(tg_id: int):
    """Usage snapshot, or None when the user has no key / the API is down."""
    status, data = await api_get("/bot/usage", tg_id=tg_id)
    if status == 200 and data.get("key"):
        return data
    return None


# =========================================================
# COMMANDS
# =========================================================


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # The homepage deep-links to ?start=key, which should land straight on
    # the key screen instead of the welcome image.
    if context.args and str(context.args[0]).lower() == "key":
        return await cmd_getkey(update, context)

    user = update.effective_user
    name = (user.first_name or user.username or "friend") if user else "friend"

    data = await fetch_usage(user.id)
    caption = welcome_text(name, data)

    try:
        await update.effective_message.reply_photo(
            photo=WELCOME_IMAGE,
            caption=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu(),
        )
        return
    except Exception as exc:
        log.warning("welcome photo failed: %s", exc)

    # Fallback: plain text welcome if the image cannot be fetched.
    await update.effective_message.reply_text(
        caption,
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu(),
        disable_web_page_preview=True,
    )


async def cmd_getkey(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    target = update.effective_message

    status, data = await api_get(
        "/bot/key",
        tg_id=user.id,
        username=user.username or "",
        first_name=user.first_name or "",
        daily=FREE_DAILY_LIMIT,
        days=FREE_KEY_DAYS,
    )

    if status == 0 or status >= 500:
        await target.reply_text(API_DOWN_TEXT, parse_mode=ParseMode.HTML)
        return

    if status != 200 or not data.get("key"):
        await target.reply_text(
            "⚠️ Could not issue a key right now.\n\n"
            f"<code>{esc(data.get('error') or data.get('detail') or status)}</code>\n\n"
            "Please try again, or ping the support chat.",
            parse_mode=ParseMode.HTML,
        )
        return

    if data.get("existing"):
        header = "✅ <b>You already have a key</b> — here it is again.\n\n"
    else:
        header = "🎉 <b>Your free API key is ready!</b>\n\n"

    await target.reply_text(
        header + key_text(data),
        parse_mode=ParseMode.HTML,
        reply_markup=key_screen_menu(),
        disable_web_page_preview=True,
    )


async def cmd_mykey(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    status, data = await api_get("/bot/usage", tg_id=user.id)

    if status == 0 or status >= 500:
        await update.effective_message.reply_text(API_DOWN_TEXT, parse_mode=ParseMode.HTML)
        return

    if status == 404 or not data.get("key"):
        await update.effective_message.reply_text(
            welcome_text(user.first_name or user.username or "friend", None),
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu(),
            disable_web_page_preview=True,
        )
        return

    await update.effective_message.reply_text(
        key_text(data),
        parse_mode=ParseMode.HTML,
        reply_markup=key_screen_menu(),
        disable_web_page_preview=True,
    )


async def cmd_usage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    status, data = await api_get("/bot/usage", tg_id=user.id)

    if status == 0 or status >= 500:
        await update.effective_message.reply_text(API_DOWN_TEXT, parse_mode=ParseMode.HTML)
        return

    if status != 200 or not data.get("key"):
        await update.effective_message.reply_text(
            usage_text(None),
            parse_mode=ParseMode.HTML,
            reply_markup=back_menu(),
        )
        return

    await update.effective_message.reply_text(
        usage_text(data),
        parse_mode=ParseMode.HTML,
        reply_markup=back_menu(),
        disable_web_page_preview=True,
    )


async def cmd_docs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        DOCS_TEXT.format(base=esc(API_BASE)),
        parse_mode=ParseMode.HTML,
        reply_markup=back_menu(),
        disable_web_page_preview=True,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        HELP_TEXT,
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu(),
        disable_web_page_preview=True,
    )


async def cmd_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        SUPPORT_TEXT,
        parse_mode=ParseMode.HTML,
        reply_markup=support_menu(),
        disable_web_page_preview=True,
    )


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.effective_message.reply_text(
        "🆔 <b>Your Telegram ID</b>\n\n"
        f"<code>{user.id}</code>\n\n"
        "Username: " + (f"@{esc(user.username)}" if user.username else "<i>none</i>") + "\n"
        "Send this ID to the owner if you need the owner commands enabled.",
        parse_mode=ParseMode.HTML,
    )


# =========================================================
# OWNER COMMANDS
# =========================================================


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user):
        await update.effective_message.reply_text("⛔ Owner only.")
        return

    status, data = await api_get("/bot/stats")
    if status != 200:
        await update.effective_message.reply_text(API_DOWN_TEXT, parse_mode=ParseMode.HTML)
        return

    await update.effective_message.reply_text(
        "📈 <b>MUSIC.API — KEY STATISTICS</b>\n"
        "\n"
        f"🔑 Keys issued: <b>{num(data.get('total_keys'))}</b>\n"
        f"✅ Active now: <b>{num(data.get('active_keys'))}</b>\n"
        f"⛔ Expired / exhausted: <b>{num(data.get('dead_keys'))}</b>\n"
        f"👥 Unique users: <b>{num(data.get('unique_users'))}</b>\n"
        "\n"
        "<b>Today</b>\n"
        f"Requests: {num(data.get('requests_today'))}\n"
        f"Audio: {num(data.get('audio_today'))}\n"
        f"Video: {num(data.get('video_today'))}\n"
        "\n"
        "<b>All-Time</b>\n"
        f"Total: {num(data.get('requests_total'))}\n"
        f"Audio: {num(data.get('audio_total'))}\n"
        f"Video: {num(data.get('video_total'))}\n"
        "\n"
        "🕒 " + esc(time.strftime("%Y-%m-%d %H:%M:%S")),
        parse_mode=ParseMode.HTML,
    )


async def cmd_genkey(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user):
        await update.effective_message.reply_text("⛔ Owner only.")
        return

    args = context.args or []
    if not args:
        await update.effective_message.reply_text(
            "Usage: <code>/genkey &lt;telegram_id&gt; [days] [daily]</code>\n\n"
            "Example: <code>/genkey 123456789 30 100</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        tg_id = int(args[0])
        days = int(args[1]) if len(args) > 1 else FREE_KEY_DAYS
        daily = int(args[2]) if len(args) > 2 else FREE_DAILY_LIMIT
    except ValueError:
        await update.effective_message.reply_text(
            "⚠️ Use numbers only, e.g. <code>/genkey 123456789 30 100</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    # force=1 re-mints early, which the API only accepts together with the
    # master password — the bot secret alone must never be able to do this.
    status, data = await api_get(
        "/bot/key", tg_id=tg_id, daily=daily, days=days,
        force=1, master=MASTER_PASSWORD,
    )

    if status != 200 or not data.get("key"):
        await update.effective_message.reply_text(
            "⚠️ Failed: "
            f"<code>{esc(data.get('error') or data.get('detail') or status)}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    await update.effective_message.reply_text(
        "✅ <b>Key issued</b>\n\n"
        f"User: <code>{tg_id}</code>\n"
        f"Key: <code>{esc(data['key'])}</code>\n"
        f"Daily limit: <b>{num(daily)}</b>\n"
        f"Validity: <b>{days} days</b>\n"
        f"Expires: <b>{esc(data.get('expires_str'))}</b>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_revoke(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user):
        await update.effective_message.reply_text("⛔ Owner only.")
        return

    args = context.args or []
    if not args:
        await update.effective_message.reply_text(
            "Usage: <code>/revoke &lt;API_KEY&gt;</code>", parse_mode=ParseMode.HTML
        )
        return

    key = args[0].strip()
    status, data = await api_get("/delkey", master=MASTER_PASSWORD, key=key)

    if status == 200:
        text = f"🗑️ Key revoked:\n<code>{esc(key)}</code>"
    else:
        text = (
            "⚠️ Could not revoke that key.\n"
            f"<code>{esc(data.get('detail') or data.get('error') or status)}</code>"
        )

    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


# =========================================================
# CALLBACKS  (the main menu)
# =========================================================


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user

    try:
        await query.answer()
    except Exception:
        pass

    action = query.data or "menu"
    name = user.first_name or user.username or "friend"

    # ---- View Your Key: generate on first tap, then keep showing it ----
    if action == "key":
        status, data = await api_get(
            "/bot/key",
            tg_id=user.id,
            username=user.username or "",
            first_name=user.first_name or "",
            daily=FREE_DAILY_LIMIT,
            days=FREE_KEY_DAYS,
        )

        if status == 0 or status >= 500:
            await show(query, API_DOWN_TEXT, back_menu())
            return

        if status != 200 or not data.get("key"):
            await show(
                query,
                "⚠️ Could not issue a key right now.\n\n"
                f"<code>{esc(data.get('error') or data.get('detail') or status)}</code>\n\n"
                "Please try again in a moment.",
                back_menu(),
            )
            return

        header = (
            "✅ <b>You already have a key</b> — here it is again.\n\n"
            if data.get("existing")
            else "🎉 <b>Your free API key is ready!</b>\n\n"
        )
        await show(query, header + key_text(data), key_screen_menu())
        return

    # ---- Usage ----
    if action == "usage":
        data = await fetch_usage(user.id)
        await show(query, usage_text(data), back_menu())
        return

    # ---- Upgrade ----
    if action == "upgrade":
        await show(query, UPGRADE_TEXT, upgrade_menu())
        return

    # ---- API Docs ----
    if action == "docs":
        await show(query, DOCS_TEXT.format(base=esc(API_BASE)), back_menu())
        return

    # ---- Support ----
    if action == "support":
        await show(query, SUPPORT_TEXT, support_menu())
        return

    # ---- Renew (only unlocked once the key has expired) ----
    if action == "renew":
        status, data = await api_get("/bot/renew", tg_id=user.id)

        if status == 0 or status >= 500:
            await show(query, API_DOWN_TEXT, back_menu())
            return

        if status == 404 or data.get("error"):
            await show(
                query,
                "🔑 <b>No key yet</b>\n\nTap <b>View Your Key</b> to generate one.",
                back_menu(),
            )
            return

        if data.get("status") == "still_active":
            await show(query, renew_locked_text(data), key_screen_menu())
            return

        await show(query, "🎉 <b>Key renewed!</b>\n\n" + key_text(data),
                   key_screen_menu())
        return

    # ---- Revoke a leaked key (confirm, then rotate) ----
    if action == "revoke_ask":
        await show(query, REVOKE_CONFIRM_TEXT, revoke_confirm_menu())
        return

    if action == "revoke_do":
        status, data = await api_get("/bot/rotate", tg_id=user.id)

        if status == 0 or status >= 500:
            await show(query, API_DOWN_TEXT, back_menu())
            return

        if status == 404 or data.get("error"):
            await show(
                query,
                "🔑 <b>No key yet</b> — there is nothing to revoke.",
                back_menu(),
            )
            return

        await show(
            query,
            "✅ <b>Key revoked and replaced</b>\n\n"
            f"<b>Old key:</b> <code>{esc(data.get('old_key'))}</code> — dead\n"
            "<i>Update it everywhere you use it.</i>\n\n"
            + key_text(data),
            key_screen_menu(),
        )
        return

    # ---- Back / main menu ----
    data = await fetch_usage(user.id)
    await show(query, welcome_text(name, data), main_menu())


# =========================================================
# STARTUP
# =========================================================


async def post_init(app: Application):
    await app.bot.set_my_commands(
        [
            BotCommand("start", "Welcome & main menu"),
            BotCommand("getkey", "Generate your free API key"),
            BotCommand("mykey", "Your key, quota & reset timer"),
            BotCommand("usage", "Today & all-time usage"),
            BotCommand("docs", "Full API documentation"),
            BotCommand("support", "Support chat, channels & owner"),
            BotCommand("help", "All commands"),
            BotCommand("id", "Show your Telegram ID"),
        ]
    )
    me = await app.bot.get_me()
    log.info("Bot started as @%s (id=%s)", me.username, me.id)
    log.info("API base: %s", API_BASE)
    log.info(
        "Free plan: %s requests/day, %s days, auto-reset every 24h",
        FREE_DAILY_LIMIT,
        FREE_KEY_DAYS,
    )
    if CHANNEL_1_URL == DEFAULT_CHANNEL_1 and CHANNEL_2_URL == DEFAULT_CHANNEL_2:
        log.info(
            "Channel buttons use the default URLs — set CHANNEL_1_URL and "
            "CHANNEL_2_URL in the environment to point at your own channels."
        )


def main():
    if not BOT_TOKEN or ":" not in BOT_TOKEN:
        raise SystemExit(
            "❌ No bot token found.\n"
            f"   Set MU_BOT_TOKEN (or TELEGRAM_BOT_TOKEN) in {ENV_FILE} "
            "and restart."
        )

    if not BOT_SECRET:
        raise SystemExit(
            "❌ BOT_API_SECRET is not set.\n"
            "   It must be identical to the API's BOT_API_SECRET. "
            f"Add it to {ENV_FILE} and restart."
        )

    if not MASTER_PASSWORD:
        log.warning(
            "API_MASTER is empty — the owner /genkey and /revoke commands "
            "will fail until you set it."
        )

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler(["getkey", "newkey"], cmd_getkey))
    app.add_handler(CommandHandler(["mykey", "key"], cmd_mykey))
    app.add_handler(CommandHandler(["usage", "myusage"], cmd_usage))
    app.add_handler(CommandHandler(["docs", "api", "documentation"], cmd_docs))
    app.add_handler(CommandHandler(["help", "commands"], cmd_help))
    app.add_handler(CommandHandler(["support", "contact"], cmd_support))
    app.add_handler(CommandHandler("id", cmd_id))

    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("genkey", cmd_genkey))
    app.add_handler(CommandHandler("revoke", cmd_revoke))

    app.add_handler(CallbackQueryHandler(on_callback))

    log.info("Polling…")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
