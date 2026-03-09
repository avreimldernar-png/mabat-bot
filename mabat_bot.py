"""
מבט מבחוץ — בוט טלגרם
תכונות: Groq/Llama חינמי, מגבלת שאלות ביום, ביט, ויראליות, פורמט מאוחד
"""

import os
import logging
import json
import asyncio
from datetime import datetime, date, timedelta
from pathlib import Path
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
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
ADMIN_ID           = int(os.environ.get("ADMIN_TELEGRAM_ID", "0"))

DAILY_FREE         = 3     # שאלות חינמיות ביום (500 ראשונים) לכל משתמש
STARS_PER_PACK     = 50    # כוכבי טלגרם לחבילה
QUESTIONS_PER_PACK = 20    # שאלות בחבילה
DAILY_GLOBAL_CAP   = 5000  # מגבלת חירום — נכנסת לפעולה רק במצב קיצוני
PAYBOX_PRICE_ILS   = 20    # מחיר בשקלים לחודש
BIT_PHONE          = os.environ.get("BIT_PHONE", "")  # מספר ביט — הגדר ב-Railway

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
            "paid_until": "",          # תאריך YYYY-MM-DD — גישה מלאה עד
            "mode": "objective",
            "total_questions": 0,
            "referred_by": None,
            "referral_count": 0,
            "join_date": str(date.today()),
        }
    return data["users"][uid]

def get_daily_limit(data):
    """3 שאלות ל-500 הראשונים, אחר כך 2."""
    free_users = sum(1 for u in data["users"].values() if not u.get("paid_until"))
    return DAILY_FREE if free_users <= 500 else 2

def can_ask(data, uid):
    """בודק אם המשתמש יכול לשאול. מחזיר (bool, סיבה)."""
    today = str(date.today())

    # איפוס מונה גלובלי יומי
    if data["global"]["date"] != today:
        data["global"] = {"date": today, "count": 0}

    user = get_user(data, uid)

    # משלמים — פטורים מכל מגבלה, תמיד עוברים
    if user["paid_until"] and user["paid_until"] >= today:
        return True, "paid_unlimited"

    # מגבלה גלובלית — חלה רק על משתמשים חינמיים
    if data["global"]["count"] >= DAILY_GLOBAL_CAP:
        return False, "global_cap"

    # שאלות נוספות מכוכבים
    if user["extra_questions"] > 0:
        return True, "extra"

    # שאלות חינמיות יומיות (3 ל-500 ראשונים, אחר כך 2)
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
GROQ_MODEL = "compound-beta"

def today_str():
    return datetime.now().strftime("%d.%m.%Y")

# פרומפט אחד מאוחד — מחזיר שני חלקים
SYSTEM_MAIN = """אתה עיתונאי בינלאומי שחושף לישראלים את הפער בין הנרטיבים.
התאריך היום: {today}.

חוקים קריטיים:
- מידע מהשנה האחרונה בלבד. אם אין — אמור במפורש.
- עברית פשוטה, זורמת, לא אקדמית.
- לעולם אל תמציא ציטוטים.

החזר תשובה בשני חלקים מופרדים על ידי הסימן <<<PART2>>>

===חלק א — מבט מהיר===
פורמט קבוע:

📍 *[כותרת — מה קורה]*

🇮🇱 *ישראל:* "[ציטוט קצר או תיאור]"
🌍 *המערב:* "[ציטוט קצר או תיאור]"
🌙 *העולם הערבי:* "[ציטוט קצר או תיאור]"

🔍 *הפער:* [משפט אחד — מה שישראלים לא שומעים]

<<<PART2>>>

===חלק ב — מה שמאחורי הכותרות===
פורמט קבוע:

📰 *הסיפור המלא*

🔹 [ציטוט 1 מתורגם — משפט שלם]
— [מקור], [תאריך]

🔹 [ציטוט 2 מתורגם — משפט שלם]
— [מקור], [תאריך]

🔹 [ציטוט 3 מתורגם — משפט שלם]
— [מקור], [תאריך]

━━━━━━━━━━━━━━━━
💡 *מה בולט:* [מה הכיסוי הבינלאומי מדגיש שפחות שומעים בישראל]

🔒 *יש עוד:* [משפט אחד מסקרן שמרמז על זווית נוספת — בלי לחשוף אותה. משהו שגרם לך לעצור ולחשוב. ניסוח בסגנון: "יש דיווח שפורסם רק ב-X ולא הגיע לישראל..."]

אם אין מידע עדכני — כתוב בשני החלקים: "לא מצאתי כיסוי עדכני. נסה לנסח אחרת." """

# פרומפט להרחבה לפי בחירת משתמש
SYSTEM_EXPAND = """אתה ממשיך שיחה על הנושא שהמשתמש שאל עליו.
התאריך היום: {today}.
הרחב לפי הבקשה הספציפית — עברית פשוטה, קצר וממוקד.
אם אין מידע עדכני — אמור בכנות."""

async def ask_gemini(query: str, expand_prompt: str = None) -> tuple:
    """מחזיר (חלק_א, חלק_ב) או (טקסט_הרחבה, None)"""
    if expand_prompt:
        system = SYSTEM_EXPAND.format(today=today_str())
        prompt = f"{system}\n\nנושא: {query}\nבקשה: {expand_prompt}"
        try:
            response = await asyncio.to_thread(
                groq_client.chat.completions.create,
                model=GROQ_MODEL,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=1000,
                temperature=0.3,
            )
            return response.choices[0].message.content.strip(), None
        except Exception as e:
            logger.error(f"Groq expand error: {e}")
            return "⚠️ שגיאה זמנית.", None

    system = SYSTEM_MAIN.format(today=today_str())
    prompt = f"{system}\n\nשאלת המשתמש: {query}"
    try:
        response = await asyncio.to_thread(
            groq_client.chat.completions.create,
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1500,
            temperature=0.3,
        )
        text = response.choices[0].message.content.strip()
        if "<<<PART2>>>" in text:
            parts = text.split("<<<PART2>>>")
            return parts[0].strip(), parts[1].strip()
        return text, None
    except Exception as e:
        logger.error(f"Groq error: {e}")
        return "⚠️ שגיאה זמנית. נסה שוב בעוד רגע.", None

# ── מקלדות ────────────────────────────────────────────────────────────────────

def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📰 מה חדש עכשיו?", callback_data="latest")],
        [InlineKeyboardButton("💳 20 שאלות נוספות — 5 ₪", callback_data="buy_pack")],
        [InlineKeyboardButton("💚 חודש ללא הגבלה — 20 ₪", callback_data="buy_paybox")],
        [InlineKeyboardButton("📤 שתף חבר — קבל 3 שאלות בונוס", callback_data="referral")],
    ])

def expand_keyboard(query: str):
    """כפתורי הרחבה שמופיעים אחרי כל תשובה"""
    import urllib.parse
    q = urllib.parse.quote(query)
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🌙 זווית ערבית", callback_data=f"expand_arab|{query[:40]}"),
            InlineKeyboardButton("🗺️ מפת אינטרסים", callback_data=f"expand_interests|{query[:40]}"),
        ],
        [InlineKeyboardButton("🔒 הידיעה שישראל לא סיקרה", callback_data=f"expand_hidden|{query[:40]}")],
    ])

def limit_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 20 שאלות — 5 ₪", callback_data="buy_pack")],
        [InlineKeyboardButton("💚 חודש ללא הגבלה — 20 ₪", callback_data="buy_paybox")],
        [InlineKeyboardButton("📤 שתף חבר — קבל 3 שאלות בונוס", callback_data="referral")],
    ])

WELCOME = """👁️ *מבט מבחוץ*

מה כותבים על ישראל בעולם — בעברית, בלי פילטרים.

כתוב כל נושא או אירוע. תקבל:
🔹 מבט מהיר — שלושה נרטיבים בשורה אחת
🔹 הסיפור המלא — ציטוטים, מקורות, מה שלא מגיע לישראל
🔹 אפשרות להעמיק לכל כיוון שמעניין אותך

✅ *3 שאלות ביום* — הטבה ל-500 המצטרפים הראשונים"""

# ── handlers ──────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    data = load_data()
    user = get_user(data, uid)

    # טיפול בקישור הפניה
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
            "arab": "הרחב על הכיסוי בתקשורת הערבית והמוסלמית בלבד — מקורות, זוויות, ציטוטים",
            "interests": "הסבר מפת האינטרסים — מי מרוויח מהנרטיב הזה, מי מפסיד, ומדוע כל צד מציג זאת כך",
            "hidden": "הבא את הידיעה או הזווית שלא סוקרה בישראל — מקור ספציפי, מה נאמר, ולמה זה לא הגיע לכאן",
        }
        expand_prompt = expand_map.get(expand_type, "הרחב על הנושא")

        thinking = await q.message.reply_text("🔍 מחפש...")
        result, _ = await ask_gemini(original_query, expand_prompt)
        await thinking.edit_text(result[:3900], parse_mode="Markdown")
        return

    elif q.data == "latest":
        await _process_query(
            update, context,
            f"מה שלוש החדשות הבינלאומיות הכי חשובות על ישראל ומזרח התיכון היום {today_str()}?",
            uid, data, user, from_callback=True
        )

    elif q.data in ("buy_stars", "buy_pack"):
        text = (
            f"💳 *20 שאלות נוספות — 5 ₪*\n\n"
            f"שלח 5 ₪ בביט למספר: {BIT_PHONE}\n\n"
            f"⚠️ חשוב: בהערה כתוב את המספר הזה:\n`{uid}`\n\n"
            f"תוך 24 שעות יתווספו 20 שאלות לחשבונך."
        )
        await q.message.reply_text(text, parse_mode="Markdown")

    elif q.data == "buy_paybox":
        text = (
            f"💚 *גישה חודשית ללא הגבלה*\n\n"
            f"מחיר: 20 ₪ לחודש\n\n"
            f"שלח תשלום בביט למספר: {BIT_PHONE}\n\n"
            f"⚠️ חשוב: בהערה כתוב את המספר הזה:\n`{uid}`\n\n"
            f"תוך 24 שעות תקבל אישור ותוכל לשאול ללא הגבלה."
        )
        await q.message.reply_text(text, parse_mode="Markdown")

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
        await q.message.reply_text(text, parse_mode="Markdown")

async def pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)

async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    data = load_data()
    user = get_user(data, uid)
    user["extra_questions"] += QUESTIONS_PER_PACK
    save_data(data)
    await update.message.reply_text(
        f"✅ תשלום התקבל! נוספו לך {QUESTIONS_PER_PACK} שאלות.\n"
        f"יתרה זמינה: {user['extra_questions']} שאלות נוספות + {DAILY_FREE} שאלות יומיות."
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    data = load_data()
    user = get_user(data, uid)
    await _process_query(update, context, update.message.text, uid, data, user)

async def _process_query(update, context, query, uid, data, user, from_callback=False):
    allowed, reason = can_ask(data, uid)
    reply = update.callback_query.message if from_callback else update.message

    if not allowed:
        if reason == "global_cap":
            msg = (
                "⏳ *הבוט עמוס מאוד כרגע*\n\n"
                "יותר מדי שאלות הגיעו בו-זמנית. נסה שוב בעוד כמה דקות או מחר בבוקר.\n\n"
                "מנויי פייבוקס לא מושפעים מעומס זה."
            )
        else:
            msg = (
                "📵 *מכסת השאלות היומית נוצלה*\n\n"
                f"נוצלו כל השאלות של היום. חזור מחר — או הוסף שאלות עכשיו:\n\n"
                "איך להמשיך:"
            )
        await reply.reply_text(msg, parse_mode="Markdown", reply_markup=limit_keyboard())
        return

    # זיהוי שאלה עמומה מדי — לא נספרת במכסה
    if len(query.strip()) <= 6 and not any(c in query for c in ["?", "!"]):
        await reply.reply_text(
            "🤔 *קצת עמום לי...*\n\n"
            "תן לי יותר הקשר — על מה בדיוק?\n\n"
            "לדוגמה: במקום _איראן_ — נסה _תוכנית הגרעין האיראנית_",
            parse_mode="Markdown"
        )
        return

    thinking = await reply.reply_text("🔍 מחפש בכותרות הבינלאומיות...")

    part1, part2 = await ask_gemini(query)
    use_question(data, uid, reason)

    # שליחת חלק א — מבט מהיר
    await thinking.edit_text(part1[:3900], parse_mode="Markdown")

    # שליחת חלק ב — עומק + פיתיון
    if part2:
        await reply.reply_text(part2[:3900], parse_mode="Markdown",
                               reply_markup=expand_keyboard(query))
    else:
        await reply.reply_text("לפרטים נוספים — שאל שאלת המשך.",
                               reply_markup=expand_keyboard(query))

    # הצעת שיתוף אחת מכל 5 שאלות
    user = get_user(load_data(), uid)
    if user["total_questions"] > 0 and user["total_questions"] % 5 == 0:
        bot_username = (await context.bot.get_me()).username
        ref_link = f"https://t.me/{bot_username}?start=ref_{uid}"
        await reply.reply_text(
            f"📤 _אהבת? שתף חבר וקבל 3 שאלות בונוס:_\n`{ref_link}`",
            parse_mode="Markdown"
        )

    # תזכורת עדינה אחרי שנוצלה המכסה
    if reason == "free":
        user = get_user(load_data(), uid)
        if user["daily_used"] >= get_daily_limit(load_data()):
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

        # אם יש גישה פעילה — מוסיפים על מה שנשאר
        today = str(date.today())
        current_until = user.get("paid_until", "")
        if current_until and current_until >= today:
            base = date.fromisoformat(current_until)
        else:
            base = date.today()

        new_until = (base + timedelta(days=days)).strftime("%Y-%m-%d")
        user["paid_until"] = new_until
        save_data(data)

        # אישור למנהל
        await update.message.reply_text(
            f"✅ אושר.\n"
            f"משתמש: {target_id}\n"
            f"חודשים שנוספו: {months}\n"
            f"גישה פעילה עד: {new_until}"
        )

        # הודעה אוטומטית למשתמש
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
    """פקודת מנהל: /stats"""
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
        f"מנויים פעילים (פייבוקס): {paid_active}\n"
        f"שאלות היום: {global_count} / {DAILY_GLOBAL_CAP}\n"
        f"סה\"כ שאלות (כל הזמנים): {total_q}",
        parse_mode="Markdown"
    )

# ── הרצה ──────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("approve_paybox", approve_paybox))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(PreCheckoutQueryHandler(pre_checkout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("🤖 מבט מבחוץ — עולה...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
