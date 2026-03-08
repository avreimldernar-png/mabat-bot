"""
מבט מבחוץ — בוט טלגרם
תכונות: ג'מיני חינמי, מגבלת שאלה ביום, כוכבי טלגרם, פייבוקס, ויראליות
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
import google.generativeai as genai

# ── לוגים ─────────────────────────────────────────────────────────────────────

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ── הגדרות סביבה ──────────────────────────────────────────────────────────────

TELEGRAM_TOKEN     = os.environ["TELEGRAM_TOKEN"]
GEMINI_KEY         = os.environ["GEMINI_API_KEY"]
PAYBOX_LINK        = os.environ.get("PAYBOX_LINK", "https://payboxapp.page.link/YOUR_LINK")
ADMIN_ID           = int(os.environ.get("ADMIN_TELEGRAM_ID", "0"))

DAILY_FREE         = 3     # שאלות חינמיות ביום (500 ראשונים) לכל משתמש
STARS_PER_PACK     = 50    # כוכבי טלגרם לחבילה
QUESTIONS_PER_PACK = 20    # שאלות בחבילה
DAILY_GLOBAL_CAP   = 5000  # מגבלת חירום — נכנסת לפעולה רק במצב קיצוני
PAYBOX_PRICE_ILS   = 20    # מחיר בשקלים לחודש
BIT_PHONE          = "050-000-0000"  # ← שנה למספר הביט שלך

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

# ── ג'מיני ────────────────────────────────────────────────────────────────────

genai.configure(api_key=GEMINI_KEY)
gemini = genai.GenerativeModel(
    model_name="gemini-2.5-flash-lite",
    generation_config={"temperature": 0.4, "max_output_tokens": 1200},
)

def today_str():
    return datetime.now().strftime("%d.%m.%Y")

SYSTEM_OBJECTIVE = """אתה עיתונאי בינלאומי שמביא לישראלים את הכיסוי הבינלאומי — קצר, מדויק, ובלתי נשכח.
התאריך היום: {today}.

חוקים קריטיים:
- הבא מידע מהשנה האחרונה בלבד. אם אין — אמור במפורש. לעולם אל תביא מידע ישן בלי להזהיר.
- מקורות: רויטרס, בי-בי-סי, AP, הגארדיאן, פוליטיקו, NYT.
- עברית פשוטה, זורמת, לא אקדמית.

פורמט קבוע — תמיד בדיוק כך:

📍 *[כותרת חדה — מה קורה, שורה אחת]*

━━━━━━━━━━━━━━━━

🔹 *[ציטוט 1 — משפט אחד, מתורגם]*
— [מקור], [תאריך]

🔹 *[ציטוט 2 — משפט אחד, מתורגם]*
— [מקור], [תאריך]

🔹 *[ציטוט 3 — משפט אחד, מתורגם]*
— [מקור], [תאריך]

━━━━━━━━━━━━━━━━

💡 *מה בולט:* [משפט אחד — מה הכיסוי הבינלאומי מדגיש שפחות שומעים בישראל]

אם אין מידע עדכני — כתוב: "לא מצאתי כיסוי עדכני על זה. נסה לנסח אחרת או שאל על אירוע ספציפי." """

SYSTEM_OTHER = """אתה חושף לישראלים את הפער בין הנרטיבים — איך אותו אירוע נראה אחרת לגמרי בתקשורות שונות.
התאריך היום: {today}.

חוקים קריטיים:
- הבא מידע מהשנה האחרונה בלבד. אם אין — אמור במפורש.
- לעולם אל תביא מידע ישן בלי להזהיר.
- עברית פשוטה, זורמת, לא אקדמית.

פורמט קבוע — תמיד בדיוק כך:

📍 *[האירוע — שורה אחת]*

━━━━━━━━━━━━━━━━

🇮🇱 *בישראל קוראים לזה:*
"[ציטוט או תיאור קצר]"

🌍 *במערב קוראים לזה:*
"[ציטוט או תיאור קצר]"

🌙 *בעולם הערבי/גלובל-סאות' קוראים לזה:*
"[ציטוט או תיאור קצר]"

━━━━━━━━━━━━━━━━

🔍 *הפער:* [משפט אחד — מה ההבדל שרוב הישראלים לא רואים]

⚠️ _זהו הנרטיב כפי שמוצג בתקשורת הבינלאומית — לא עמדת הבוט._

אם אין מידע עדכני — כתוב: "לא מצאתי כיסוי עדכני על זה. נסה לנסח אחרת." """

async def ask_gemini(query: str, mode: str) -> str:
    system = (SYSTEM_OBJECTIVE if mode == "objective" else SYSTEM_OTHER).format(today=today_str())
    prompt = f"{system}\n\nשאלת המשתמש: {query}"
    try:
        response = await asyncio.to_thread(gemini.generate_content, prompt)
        return response.text.strip()
    except Exception as e:
        logger.error(f"Gemini error: {e}")
        return "⚠️ שגיאה זמנית. נסה שוב בעוד רגע."

# ── מקלדות ────────────────────────────────────────────────────────────────────

def main_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⚖️ אובייקטיבי", callback_data="mode_objective"),
            InlineKeyboardButton("🔴 הנרטיב האחר", callback_data="mode_other"),
        ],
        [InlineKeyboardButton("📰 מה חדש עכשיו?", callback_data="latest")],
        [InlineKeyboardButton("⭐ 20 שאלות נוספות — כוכבי טלגרם", callback_data="buy_stars")],
        [InlineKeyboardButton("💚 חודש ללא הגבלה — פייבוקס", callback_data="buy_paybox")],
        [InlineKeyboardButton("📤 שתף חבר — קבל 3 שאלות בונוס", callback_data="referral")],
    ])

def limit_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⭐ 20 שאלות — 50 כוכבים (מיידי)", callback_data="buy_stars")],
        [InlineKeyboardButton("💚 חודש ללא הגבלה — פייבוקס", callback_data="buy_paybox")],
        [InlineKeyboardButton("📤 שתף חבר — קבל 3 שאלות בונוס", callback_data="referral")],
    ])

WELCOME = """👁️ *מבט מבחוץ*

מה כותבים על ישראל בעולם — בעברית, בלי פילטרים.

✅ *3 שאלות ביום* — הטבה ל-500 המצטרפים הראשונים
✅ שני מצבים: אובייקטיבי 🔹 או נרטיב אחר 🔴
✅ ציטוטים מתורגמים מהעיתונות הבינלאומית

כתוב כל נושא או אירוע — ואני אחפש."""

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

    if q.data in ("mode_objective", "mode_other"):
        user["mode"] = q.data.replace("mode_", "")
        save_data(data)
        label = "⚖️ אובייקטיבי" if user["mode"] == "objective" else "🔴 הנרטיב האחר"
        await q.message.reply_text(
            f"עברת למצב: *{label}*\n\nכתוב נושא ואחפש.",
            parse_mode="Markdown"
        )

    elif q.data == "latest":
        await _process_query(
            update, context,
            f"מה החדשות הבינלאומיות הכי חשובות על ישראל ומזרח התיכון היום {today_str()}?",
            uid, data, user, from_callback=True
        )

    elif q.data == "buy_stars":
        await q.message.reply_invoice(
            title="20 שאלות נוספות",
            description="חבילת שאלות נוספות לבוט מבט מבחוץ",
            payload="questions_pack",
            currency="XTR",
            prices=[LabeledPrice("20 שאלות", STARS_PER_PACK)],
        )

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

    mode = user.get("mode", "objective")
    mode_label = "אובייקטיבי" if mode == "objective" else "הנרטיב האחר"

    # זיהוי שאלה עמומה מדי — לא נספרת במכסה
    if len(query.strip()) <= 6 and not any(c in query for c in ['?', '!']):
        await reply.reply_text(
            "🤔 *קצת עמום לי...*\n\n"
            "תן לי יותר הקשר — על מה בדיוק?\n\n"
            "לדוגמה: במקום _איראן_ — נסה _תוכנית הגרעין האיראנית_",
            parse_mode="Markdown"
        )
        return

    thinking = await reply.reply_text(f"🔍 מחפש [{mode_label}]...")

    result = await ask_gemini(query, mode)
    use_question(data, uid, reason)

    # הצעת שיתוף אחת מכל 5 שאלות
    footer = ""
    user = get_user(load_data(), uid)
    if user["total_questions"] > 0 and user["total_questions"] % 5 == 0:
        bot_username = (await context.bot.get_me()).username
        ref_link = f"https://t.me/{bot_username}?start=ref_{uid}"
        footer = f"\n\n📤 _אהבת? שתף חבר וקבל 3 שאלות בונוס:_\n`{ref_link}`"

    await thinking.edit_text(result[:3900] + footer, parse_mode="Markdown")

    # תזכורת עדינה אחרי שנוצלה השאלה החינמית
    if reason == "free":
        user = get_user(load_data(), uid)
        if user["daily_used"] >= get_daily_limit(load_data()):
            await reply.reply_text(
                "💡 _נוצלו כל השאלות של היום. חזור מחר, או הוסף שאלות עכשיו:_",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⭐ 20 שאלות — כוכבים", callback_data="buy_stars"),
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
