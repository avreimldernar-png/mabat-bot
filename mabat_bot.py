"""
מבט מבחוץ — בוט טלגרם
גרסה 2.0 — פרומפט חדש, adaptive response, פיתיון דינמי, broadcast
"""

import os
import logging
import json
import asyncio
import httpx
from datetime import datetime, date, timedelta
from pathlib import Path
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    PreCheckoutQueryHandler, ContextTypes, filters
)
from groq import Groq

# ── לוגים ─────────────────────────────────────────────────────────────────────

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ── הגדרות סביבה ──────────────────────────────────────────────────────────────

TELEGRAM_TOKEN     = os.environ["TELEGRAM_TOKEN"]
GROQ_KEY           = os.environ["GROQ_API_KEY"]
NEWS_API_KEY       = os.environ.get("NEWS_API_KEY", "")
ADMIN_ID           = int(os.environ.get("ADMIN_TELEGRAM_ID", "0"))
BIT_PHONE          = os.environ.get("BIT_PHONE", "")

DAILY_FREE         = 3
QUESTIONS_PER_PACK = 20
DAILY_GLOBAL_CAP   = 5000

# ── אחסון נתונים ──────────────────────────────────────────────────────────────

DATA_FILE = Path("bot_data.json")

def load_data():
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    return {"users": {}, "global": {"date": "", "count": 0}}

def save_data(data):
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def get_user(data, uid):
    uid = str(uid)
    if uid not in data["users"]:
        data["users"][uid] = {
            "daily_date": "",
            "daily_used": 0,
            "extra_questions": 0,
            "paid_until": "",
            "total_questions": 0,
            "referred_by": None,
            "referral_count": 0,
            "join_date": str(date.today()),
        }
    return data["users"][uid]

def get_daily_limit(data):
    free_users = sum(1 for u in data["users"].values() if not u.get("paid_until"))
    return DAILY_FREE if free_users <= 500 else 2

def can_ask(data, uid):
    today = str(date.today())
    if data["global"]["date"] != today:
        data["global"] = {"date": today, "count": 0}
    user = get_user(data, uid)
    if user["paid_until"] and user["paid_until"] >= today:
        return True, "paid_unlimited"
    if data["global"]["count"] >= DAILY_GLOBAL_CAP:
        return False, "global_cap"
    if user["extra_questions"] > 0:
        return True, "extra"
    daily_limit = get_daily_limit(data)
    if user["daily_date"] != today:
        user["daily_date"] = today
        user["daily_used"] = 0
    if user["daily_used"] < daily_limit:
        return True, "free"
    return False, "limit_reached"

def use_question(data, uid, reason):
    data["global"]["count"] += 1
    user = get_user(data, uid)
    user["total_questions"] += 1
    if reason == "extra":
        user["extra_questions"] -= 1
    elif reason == "free":
        user["daily_used"] += 1
    save_data(data)

# ── Groq ──────────────────────────────────────────────────────────────────────

groq_client = Groq(api_key=GROQ_KEY)
GROQ_MODEL = "llama-3.3-70b-versatile"  # מודל אמין לעיבוד טקסט

def today_str():
    return datetime.now().strftime("%d.%m.%Y")

# ══════════════════════════════════════════════════════════════════════════════
# חיפוש אמיתי ב-NewsAPI
# ══════════════════════════════════════════════════════════════════════════════

async def translate_query(query: str) -> str:
    """מתרגם שאלה בעברית לאנגלית לצורך חיפוש."""
    try:
        response = await asyncio.to_thread(
            groq_client.chat.completions.create,
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": f"Translate to English for news search (2-5 words only, no explanation): {query}"}],
            max_tokens=20,
            temperature=0,
        )
        return response.choices[0].message.content.strip().strip('"')
    except Exception:
        return query

async def fetch_news(query: str) -> str:
    """מתרגם שאלה לאנגלית, מחפש כתבות ב-NewsAPI ומחזיר טקסט מסוכם."""
    try:
        en_query = await translate_query(query)
        logger.info(f"NewsAPI query: '{query}' → '{en_query}'")
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://newsapi.org/v2/everything",
                params={
                    "q": f"{en_query} Israel",
                    "language": "en",
                    "sortBy": "publishedAt",
                    "pageSize": 10,
                    "apiKey": NEWS_API_KEY,
                }
            )
            data = resp.json()

        articles = data.get("articles", [])
        if not articles:
            return ""

        # בונה טקסט מהכתבות
        lines = []
        for a in articles[:8]:
            source = a.get("source", {}).get("name", "")
            title = a.get("title", "")
            desc = a.get("description", "") or ""
            pub = a.get("publishedAt", "")[:10]
            if title and source:
                lines.append(f"[{source}, {pub}]: {title}. {desc[:200]}")

        return "\n".join(lines)

    except Exception as e:
        logger.error(f"NewsAPI error: {e}")
        return ""

# ══════════════════════════════════════════════════════════════════════════════
# הפרומפט הראשי — הלב של הבוט
# ══════════════════════════════════════════════════════════════════════════════

SYSTEM_MAIN = """אתה "מבט מבחוץ" — עיתונאי שמנתח כיסוי בינלאומי על ישראל. תאריך: {today}.

קיבלת כתבות אמיתיות מתקשורת בינלאומית. נתח אותן והחזר:

חוקים:
- השתמש רק במידע מהכתבות שקיבלת. אל תוסיף מידע שאינו שם.
- אם הכתבות לא מספיקות — אמור בכנות.
- כל ציטוט: שם מקור + תאריך מהכתבה.
- הכל בעברית בלבד.

פורמט — שני חלקים מופרדים ב-<<<PART2>>>:

📍 *[כותרת בעברית — מה קורה]*
🇮🇱 *ישראל:* "[הנרטיב הישראלי לפי הכתבות]"
🌍 *המערב:* "[הנרטיב המערבי לפי הכתבות — מקור]"
🌙 *העולם הערבי:* "[הנרטיב הערבי לפי הכתבות — מקור]"
🔍 *הפער:* [מה ישראלים לא שומעים]

<<<PART2>>>

📰 *הסיפור המלא*
🔹 [ציטוט מתורגם] — *[מקור]*, [תאריך]
🔹 [ציטוט מתורגם] — *[מקור]*, [תאריך]
🔹 [ציטוט מתורגם] — *[מקור]*, [תאריך]
━━━━━━━━━━━━━━━━
💡 *מה בולט:* [משפט-שניים]
🔒 *{bait}*"""

BAIT_PROMPT = """לנושא "{query}" — כתוב משפט פיתיון אחד קצר (עד 15 מילה) שמרמז על זווית שלא סוקרה. רק המשפט, בעברית."""

SYSTEM_EXPAND = """המשך על "{query}" — {today}. עברית. התבסס על הכתבות שקיבלת."""

# ══════════════════════════════════════════════════════════════════════════════
# קריאות API
# ══════════════════════════════════════════════════════════════════════════════

async def generate_bait(query: str) -> str:
    """יוצר פיתיון דינמי ספציפי לנושא."""
    try:
        response = await asyncio.to_thread(
            groq_client.chat.completions.create,
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": BAIT_PROMPT.format(query=query)}],
            max_tokens=60,
            temperature=0.7,
        )
        bait = response.choices[0].message.content.strip()
        # הסר מרכאות אם יש
        bait = bait.strip('"').strip("'")
        return bait if bait else "יש עוד זווית אחת שלא פורסמה בישראל — שאל ואסביר."
    except Exception:
        return "יש עוד זווית אחת שלא פורסמה בישראל — שאל ואסביר."

async def ask_groq(query: str, expand_prompt: str = None, articles: str = "") -> tuple:
    """מחזיר (חלק_א, חלק_ב) או (טקסט_הרחבה, None)"""

    if expand_prompt:
        prompt = (
            f"{SYSTEM_EXPAND.format(query=query, today=today_str())}\n\n"
            f"כתבות:\n{articles}\n\nבקשה: {expand_prompt}"
        )
        try:
            response = await asyncio.to_thread(
                groq_client.chat.completions.create,
                model=GROQ_MODEL,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=800,
                temperature=0.3,
            )
            return response.choices[0].message.content.strip(), None
        except Exception as e:
            logger.error(f"Groq expand error: {e}")
            return "⚠️ שגיאה זמנית. נסה שוב.", None

    bait = await generate_bait(query)
    system = SYSTEM_MAIN.format(today=today_str(), bait=bait)

    if not articles:
        return "⚠️ לא מצאתי כתבות בינלאומיות על הנושא הזה. נסה לנסח אחרת.", None

    prompt = f"{system}\n\nכתבות שנמצאו:\n{articles}\n\nשאלת המשתמש: {query}"

    try:
        response = await asyncio.to_thread(
            groq_client.chat.completions.create,
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1200,
            temperature=0.3,
        )
        text = response.choices[0].message.content.strip()

        if "<<<PART2>>>" in text:
            parts = text.split("<<<PART2>>>", 1)
            return parts[0].strip(), parts[1].strip()
        elif "📰" in text:
            idx = text.index("📰")
            return text[:idx].strip(), text[idx:].strip()
        else:
            mid = len(text) // 2
            return text[:mid].strip(), text[mid:].strip()

    except Exception as e:
        logger.error(f"Groq main error: {e}")
        return "⚠️ שגיאה זמנית. נסה שוב בעוד רגע.", None

# ── מקלדות ────────────────────────────────────────────────────────────────────

def expand_keyboard(query: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🌙 הכיסוי הערבי", callback_data=f"expand_arab|{query[:60]}"),
         InlineKeyboardButton("🗺️ מפת אינטרסים", callback_data=f"expand_interests|{query[:60]}")],
        [InlineKeyboardButton("🔍 מה לא סוקר בישראל", callback_data=f"expand_hidden|{query[:60]}")],
    ])

def limit_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 20 שאלות — 5 ₪", callback_data="buy_pack")],
        [InlineKeyboardButton("💚 חודש ללא הגבלה — 20 ₪", callback_data="buy_paybox")],
        [InlineKeyboardButton("📤 שתף חבר — קבל 3 שאלות בונוס", callback_data="referral")],
    ])

def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🌍 מה חדש עכשיו?", callback_data="latest")],
    ])

WELCOME = """👁️ *מבט מבחוץ*

מה כותבים על ישראל בעולם — בעברית, בלי פילטרים.

שאל כל שאלה על נושא שמעניין אותך.
תקבל שלושה נרטיבים על אותו אירוע — ישראל, המערב, העולם הערבי — עם ציטוטים אמיתיים ומקורות.

✅ *3 שאלות ביום* — הטבה ל-500 המצטרפים הראשונים"""

# ── handlers ──────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    data = load_data()
    user = get_user(data, uid)

    args = context.args
    if args and args[0].startswith("ref_") and not user["referred_by"]:
        referrer_id = args[0][4:]
        user["referred_by"] = referrer_id
        referrer = get_user(data, referrer_id)
        referrer["extra_questions"] += 3
        referrer["referral_count"] += 1
        try:
            await context.bot.send_message(
                int(referrer_id),
                "🎉 חבר הצטרף דרך הקישור שלך! קיבלת 3 שאלות בונוס."
            )
        except Exception:
            pass

    save_data(data)
    await update.message.reply_text(WELCOME, parse_mode="Markdown", reply_markup=main_keyboard())

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    data = load_data()
    user = get_user(data, uid)

    if q.data.startswith("expand_"):
        parts = q.data.split("|", 1)
        expand_type = parts[0].replace("expand_", "")
        original_query = parts[1] if len(parts) > 1 else "הנושא"

        expand_map = {
            "arab": "הרחב על הכיסוי בתקשורת הערבית והמוסלמית — מקורות, זוויות, ציטוטים בעברית",
            "interests": "הסבר מפת האינטרסים — מי מרוויח מהנרטיב הזה, מי מפסיד, ולמה כל צד מציג את זה כך",
            "hidden": "הבא דיווח ספציפי שלא סוקר בישראל — שם המקור, מה נאמר, ולמה זה לא הגיע לכאן",
        }
        expand_prompt = expand_map.get(expand_type, "הרחב על הנושא")

        thinking = await context.bot.send_message(q.message.chat_id, "🔍 מחפש...")
        articles = await fetch_news(original_query)
        result, _ = await ask_groq(original_query, expand_prompt=expand_prompt, articles=articles)
        await thinking.edit_text(result[:3900], parse_mode="Markdown")
        return

    elif q.data == "latest":
        await _process_query(
            update, context,
            f"מה הנושא הבינלאומי הכי בוער היום {today_str()} שנוגע לישראל ולא מדובר עליו מספיק בתקשורת הישראלית?",
            uid, data, user, from_callback=True
        )

    elif q.data in ("buy_stars", "buy_pack"):
        text = (
            f"💳 *20 שאלות נוספות — 5 ₪*\n\n"
            f"שלח 5 ₪ בביט למספר: {BIT_PHONE}\n\n"
            f"⚠️ חשוב: בהערה כתוב את המספר הזה:\n`{uid}`\n\n"
            f"תוך 24 שעות יתווספו 20 שאלות לחשבונך."
        )
        await context.bot.send_message(q.message.chat_id, text, parse_mode="Markdown")

    elif q.data == "buy_paybox":
        text = (
            f"💚 *גישה חודשית ללא הגבלה — 20 ₪*\n\n"
            f"שלח 20 ₪ בביט למספר: {BIT_PHONE}\n\n"
            f"⚠️ חשוב: בהערה כתוב את המספר הזה:\n`{uid}`\n\n"
            f"תוך 24 שעות תקבל אישור ותוכל לשאול ללא הגבלה."
        )
        await context.bot.send_message(q.message.chat_id, text, parse_mode="Markdown")

    elif q.data == "referral":
        bot_username = (await context.bot.get_me()).username
        ref_link = f"https://t.me/{bot_username}?start=ref_{uid}"
        text = (
            "📤 *שתף וקבל שאלות בונוס*\n\n"
            "שלח לחבר את הקישור הזה.\n"
            "כשהוא מצטרף — אתה מקבל 3 שאלות בונוס מיידית.\n\n"
            f"הקישור האישי שלך:\n`{ref_link}`\n\n"
            f"הצטרפו דרכך עד כה: {user.get('referral_count', 0)} אנשים"
        )
        await context.bot.send_message(q.message.chat_id, text, parse_mode="Markdown")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    data = load_data()
    user = get_user(data, uid)
    await _process_query(update, context, update.message.text, uid, data, user)

async def _process_query(update, context, query, uid, data, user, from_callback=False):
    allowed, reason = can_ask(data, uid)
    if from_callback:
        chat_id = update.callback_query.message.chat_id
        # wrapper שמאפשר שליחה גמישה בין callback למסר רגיל
        class _Reply:
            def __init__(self, bot, cid):
                self._bot = bot
                self._cid = cid
            async def reply_text(self, text, **kwargs):
                return await self._bot.send_message(self._cid, text, **kwargs)
        reply = _Reply(context.bot, chat_id)
    else:
        reply = update.message

    if not allowed:
        if reason == "global_cap":
            msg = (
                "⏳ *הבוט עמוס מאוד כרגע*\n\n"
                "יותר מדי שאלות הגיעו בו-זמנית. נסה שוב בעוד כמה דקות."
            )
        else:
            msg = (
                "📵 *מכסת השאלות היומית נוצלה*\n\n"
                "חזור מחר — או הוסף שאלות עכשיו:"
            )
        await reply.reply_text(msg, parse_mode="Markdown", reply_markup=limit_keyboard())
        return

    # שאלה עמומה — לא נספרת
    if len(query.strip()) <= 5 and not any(c in query for c in ["?", "!"]):
        await reply.reply_text(
            "🤔 *קצת עמום לי...*\n\n"
            "תן לי יותר הקשר — על מה בדיוק?\n\n"
            "לדוגמה: במקום _איראן_ — נסה _תוכנית הגרעין האיראנית_",
            parse_mode="Markdown"
        )
        return

    # שולח "מחפש" תמיד דרך send_message ישיר
    chat_id = update.callback_query.message.chat_id if from_callback else update.message.chat_id
    thinking = await context.bot.send_message(chat_id, "🔍 מחפש בכותרות הבינלאומיות...")

    # חיפוש כתבות אמיתיות
    articles = await fetch_news(query)
    part1, part2 = await ask_groq(query, articles=articles)

    # הגנה מפני תשובה ריקה
    if not part1 or len(part1.strip()) < 10:
        await thinking.edit_text("⚠️ לא הצלחתי למצוא מידע על הנושא הזה. נסה לנסח אחרת.")
        return

    use_question(data, uid, reason)

    await thinking.edit_text(part1[:3900], parse_mode="Markdown")

    if part2:
        await reply.reply_text(part2[:3900], parse_mode="Markdown",
                               reply_markup=expand_keyboard(query))
    else:
        await reply.reply_text("לפרטים נוספים — שאל שאלת המשך.",
                               reply_markup=expand_keyboard(query))

    # הצעת שיתוף אחת מכל 5 שאלות
    fresh_data = load_data()
    fresh_user = get_user(fresh_data, uid)
    if fresh_user["total_questions"] > 0 and fresh_user["total_questions"] % 5 == 0:
        bot_username = (await context.bot.get_me()).username
        ref_link = f"https://t.me/{bot_username}?start=ref_{uid}"
        await reply.reply_text(
            f"📤 _אהבת? שתף חבר וקבל 3 שאלות בונוס:_\n`{ref_link}`",
            parse_mode="Markdown"
        )

    # תזכורת עדינה כשנגמרות שאלות
    if reason == "free":
        fresh_data2 = load_data()
        fresh_user2 = get_user(fresh_data2, uid)
        if fresh_user2["daily_used"] >= get_daily_limit(fresh_data2):
            await reply.reply_text(
                "💡 _נוצלו כל השאלות של היום. חזור מחר, או הוסף שאלות עכשיו:_",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("💳 20 שאלות — 5 ₪", callback_data="buy_pack"),
                    InlineKeyboardButton("📤 שתף וקבל בונוס", callback_data="referral"),
                ]])
            )

# ── פקודות מנהל ───────────────────────────────────────────────────────────────

async def approve_paybox(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /approve_paybox [מזהה_משתמש] [מספר_חודשים]
    דוגמאות:
      /approve_paybox 123456789       ← חודש אחד
      /approve_paybox 123456789 3     ← שלושה חודשים
    """
    if update.effective_user.id != ADMIN_ID:
        return

    try:
        args = context.args
        if not args:
            await update.message.reply_text("שימוש: /approve_paybox [מזהה] [חודשים — ברירת מחדל 1]")
            return

        target_id = str(args[0])
        months = int(args[1]) if len(args) > 1 else 1
        days = months * 30

        data = load_data()
        user = get_user(data, target_id)

        today = str(date.today())
        current_until = user.get("paid_until", "")
        if current_until and current_until >= today:
            base = date.fromisoformat(current_until)
        else:
            base = date.today()

        new_until = (base + timedelta(days=days)).strftime("%Y-%m-%d")
        user["paid_until"] = new_until
        save_data(data)

        await update.message.reply_text(
            f"✅ אושר.\n"
            f"משתמש: {target_id}\n"
            f"חודשים שנוספו: {months}\n"
            f"גישה פעילה עד: {new_until}"
        )

        month_word = "חודש" if months == 1 else f"{months} חודשים"
        await context.bot.send_message(
            int(target_id),
            f"✅ *התשלום התקבל — תודה!*\n\n"
            f"יש לך גישה ללא הגבלה ל-{month_word}.\n"
            f"הגישה פעילה עד: {new_until} 🎉",
            parse_mode="Markdown"
        )

    except Exception as e:
        await update.message.reply_text(f"שגיאה: {e}")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/stats — סטטיסטיקות"""
    if update.effective_user.id != ADMIN_ID:
        return

    data = load_data()
    today = str(date.today())
    total_users = len(data["users"])
    active_today = sum(
        1 for u in data["users"].values()
        if u.get("daily_date") == today and u.get("daily_used", 0) > 0
    )
    paid_active = sum(
        1 for u in data["users"].values()
        if u.get("paid_until", "") >= today
    )
    global_count = data["global"]["count"] if data["global"]["date"] == today else 0
    total_q = sum(u.get("total_questions", 0) for u in data["users"].values())

    await update.message.reply_text(
        f"📊 *סטטיסטיקות — מבט מבחוץ*\n\n"
        f"משתמשים רשומים: {total_users}\n"
        f"פעילים היום: {active_today}\n"
        f"מנויים פעילים: {paid_active}\n"
        f"שאלות היום: {global_count} / {DAILY_GLOBAL_CAP}\n"
        f"סה\"כ שאלות (כל הזמנים): {total_q}",
        parse_mode="Markdown"
    )

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /broadcast [הודעה]
    שולח הודעה לכל המשתמשים הרשומים.
    """
    if update.effective_user.id != ADMIN_ID:
        return

    if not context.args:
        await update.message.reply_text("שימוש: /broadcast [טקסט ההודעה]")
        return

    message_text = " ".join(context.args)
    data = load_data()
    all_uids = list(data["users"].keys())

    sent = 0
    failed = 0
    for uid_str in all_uids:
        try:
            await context.bot.send_message(
                int(uid_str),
                message_text,
                parse_mode="Markdown"
            )
            sent += 1
            await asyncio.sleep(0.05)  # מניעת rate-limit
        except Exception:
            failed += 1

    await update.message.reply_text(
        f"✅ שליחה הושלמה.\n"
        f"נשלח ל: {sent} משתמשים\n"
        f"נכשל: {failed}"
    )

# ── הרצה ──────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("approve_paybox", approve_paybox))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(PreCheckoutQueryHandler(lambda u, c: u.pre_checkout_query.answer(ok=True)))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("🤖 מבט מבחוץ v2.0 — עולה...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
