import asyncio
import json
import logging
import os
import random
import sqlite3
from pathlib import Path
from typing import Any, Dict, List

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.WARNING,
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

BASE_DIR = Path(__file__).resolve().parent
QUESTIONS_FILE = BASE_DIR / "questions.json"
DB_FILE = BASE_DIR / "quiz_bot.db"

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
PORT = int(os.getenv("PORT", "10000"))

DAILY_QUIZ_COUNT = 10


def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            daily_quiz_enabled INTEGER DEFAULT 0
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_stats (
            user_id INTEGER PRIMARY KEY,
            total_answered INTEGER DEFAULT 0,
            correct_count INTEGER DEFAULT 0,
            wrong_count INTEGER DEFAULT 0,
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS wrong_questions (
            user_id INTEGER,
            question_id INTEGER,
            PRIMARY KEY (user_id, question_id)
        )
    """)

    conn.commit()
    conn.close()


def ensure_user(user_id: int, username: str | None, full_name: str | None) -> None:
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO users (user_id, username, full_name, daily_quiz_enabled)
        VALUES (?, ?, ?, 0)
        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,
            full_name=excluded.full_name
    """, (user_id, username, full_name))

    cur.execute("""
        INSERT INTO user_stats (user_id, total_answered, correct_count, wrong_count)
        VALUES (?, 0, 0, 0)
        ON CONFLICT(user_id) DO NOTHING
    """, (user_id,))

    conn.commit()
    conn.close()


def get_user_stats_from_db(user_id: int) -> Dict[str, int]:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT total_answered, correct_count, wrong_count
        FROM user_stats
        WHERE user_id = ?
    """, (user_id,))
    row = cur.fetchone()
    conn.close()

    if not row:
        return {"total_answered": 0, "correct_count": 0, "wrong_count": 0}

    return {
        "total_answered": row["total_answered"],
        "correct_count": row["correct_count"],
        "wrong_count": row["wrong_count"],
    }


def update_user_stats(user_id: int, is_correct: bool) -> None:
    conn = get_db_connection()
    cur = conn.cursor()

    if is_correct:
        cur.execute("""
            UPDATE user_stats
            SET total_answered = total_answered + 1,
                correct_count = correct_count + 1
            WHERE user_id = ?
        """, (user_id,))
    else:
        cur.execute("""
            UPDATE user_stats
            SET total_answered = total_answered + 1,
                wrong_count = wrong_count + 1
            WHERE user_id = ?
        """, (user_id,))

    conn.commit()
    conn.close()


def add_wrong_question(user_id: int, question_id: int) -> None:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT OR IGNORE INTO wrong_questions (user_id, question_id)
        VALUES (?, ?)
    """, (user_id, question_id))
    conn.commit()
    conn.close()


def remove_wrong_question(user_id: int, question_id: int) -> None:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        DELETE FROM wrong_questions
        WHERE user_id = ? AND question_id = ?
    """, (user_id, question_id))
    conn.commit()
    conn.close()


def get_wrong_question_ids(user_id: int) -> List[int]:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT question_id
        FROM wrong_questions
        WHERE user_id = ?
    """, (user_id,))
    rows = cur.fetchall()
    conn.close()
    return [row["question_id"] for row in rows]


def set_daily_quiz_enabled(user_id: int, enabled: bool) -> None:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        UPDATE users
        SET daily_quiz_enabled = ?
        WHERE user_id = ?
    """, (1 if enabled else 0, user_id))
    conn.commit()
    conn.close()


def get_daily_quiz_enabled(user_id: int) -> bool:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT daily_quiz_enabled
        FROM users
        WHERE user_id = ?
    """, (user_id,))
    row = cur.fetchone()
    conn.close()
    return bool(row["daily_quiz_enabled"]) if row else False


def load_questions() -> List[Dict[str, Any]]:
    if not QUESTIONS_FILE.exists():
        raise FileNotFoundError(f"questions.json bulunamadı: {QUESTIONS_FILE}")

    with QUESTIONS_FILE.open("r", encoding="utf-8") as f:
        questions = json.load(f)

    if not isinstance(questions, list) or not questions:
        raise ValueError("questions.json boş veya geçersiz formatta.")

    required = {
        "id",
        "question",
        "options",
        "answer",
        "explanation",
        "topic",
        "difficulty",
    }

    for idx, q in enumerate(questions, start=1):
        if not isinstance(q, dict):
            raise ValueError(f"{idx}. soru nesne formatında değil.")

        missing = required - set(q.keys())
        if missing:
            raise ValueError(f"{idx}. soruda eksik alan var: {missing}")

        if not isinstance(q["options"], list) or not (2 <= len(q["options"]) <= 5):
            raise ValueError(f"{idx}. soruda options alanı 2-5 arası seçenek içermeli.")

        valid_answers = [chr(65 + i) for i in range(len(q["options"]))]
        if q["answer"] not in valid_answers:
            raise ValueError(
                f"{idx}. soruda answer alanı seçeneklerle uyumlu değil. "
                f"Geçerli cevaplar: {valid_answers}"
            )

    return questions


QUESTIONS = load_questions()
QUESTION_MAP = {q["id"]: q for q in QUESTIONS}


def get_user_state(context: ContextTypes.DEFAULT_TYPE) -> Dict[str, Any]:
    return context.user_data.setdefault(
        "quiz_state",
        {
            "mode": None,
            "selected_topic": None,
            "selected_difficulty": None,
            "question_count": 10,
            "score": 0,
            "asked": 0,
            "queue": [],
            "current_question": None,
            "history": [],
        },
    )


def reset_quiz_state(state: Dict[str, Any]) -> None:
    history = state.get("history", [])
    state.clear()
    state.update(
        {
            "mode": None,
            "selected_topic": None,
            "selected_difficulty": None,
            "question_count": 10,
            "score": 0,
            "asked": 0,
            "queue": [],
            "current_question": None,
            "history": history,
        }
    )


def unique_topics() -> List[str]:
    return sorted({q["topic"] for q in QUESTIONS})


def unique_difficulties() -> List[str]:
    preferred_order = ["kolay", "orta", "zor"]
    all_diffs = sorted({q["difficulty"] for q in QUESTIONS})
    return sorted(
        all_diffs,
        key=lambda x: preferred_order.index(x) if x in preferred_order else 999
    )


def filter_questions(
    topic: str | None = None,
    difficulty: str | None = None,
) -> List[Dict[str, Any]]:
    filtered = QUESTIONS

    if topic and topic != "hepsi":
        filtered = [q for q in filtered if q["topic"] == topic]

    if difficulty and difficulty != "hepsi":
        filtered = [q for q in filtered if q["difficulty"] == difficulty]

    return filtered


def build_queue(pool: List[Dict[str, Any]], question_count: int) -> List[Dict[str, Any]]:
    shuffled = pool.copy()
    random.shuffle(shuffled)
    return shuffled[: min(question_count, len(shuffled))]


def get_wrong_questions_for_user(user_id: int) -> List[Dict[str, Any]]:
    wrong_ids = get_wrong_question_ids(user_id)
    return [QUESTION_MAP[qid] for qid in wrong_ids if qid in QUESTION_MAP]


def question_text(q: Dict[str, Any], asked_no: int | None = None, total_count: int | None = None) -> str:
    header = "📘 *Terfi Sınavı Sorusu*"
    progress = ""
    if asked_no is not None and total_count is not None:
        progress = f"\n┌ Soru: *{asked_no}/{total_count}*"

    topic_line = f"\n├ Konu: `{q['topic']}`"
    difficulty_emoji = "🟢" if q["difficulty"] == "kolay" else "🔴" if q["difficulty"] == "zor" else "🟡"
    difficulty_line = f"\n└ Zorluk: {difficulty_emoji} *{q['difficulty'].capitalize()}*"
    question_block = f"\n\n*{q['question']}*"

    return header + progress + topic_line + difficulty_line + question_block


def options_text(q: Dict[str, Any]) -> str:
    lines = []
    for idx, option in enumerate(q["options"]):
        letter = chr(65 + idx)
        lines.append(f"*{letter})* {option}")
    return "\n".join(lines)


def answer_keyboard(q: Dict[str, Any]) -> InlineKeyboardMarkup:
    option_buttons = []
    row = []

    for idx, _ in enumerate(q["options"]):
        letter = chr(65 + idx)
        row.append(InlineKeyboardButton(f"{letter}", callback_data=f"answer|{letter}"))

        if len(row) == 2:
            option_buttons.append(row)
            row = []

    if row:
        option_buttons.append(row)

    option_buttons.append(
        [
            InlineKeyboardButton("⏭ Soruyu Geç", callback_data="skip"),
            InlineKeyboardButton("⛔ Testi Bitir", callback_data="menu|finish_test"),
        ]
    )
    option_buttons.append(
        [
            InlineKeyboardButton("📊 İstatistik", callback_data="menu|stats"),
        ]
    )

    return InlineKeyboardMarkup(option_buttons)


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🎲 Karma Test Başlat", callback_data="menu|random")],
            [InlineKeyboardButton("🧩 Konu Seç", callback_data="menu|topic")],
            [InlineKeyboardButton("⚡ Zorluk Seç", callback_data="menu|difficulty")],
            [InlineKeyboardButton("🔁 Yanlışlarım", callback_data="menu|wrong")],
            [InlineKeyboardButton("📊 İstatistik", callback_data="menu|stats")],
            [InlineKeyboardButton("🌞 Günlük Deneme Aç", callback_data="menu|daily_on")],
            [InlineKeyboardButton("🌙 Günlük Deneme Kapat", callback_data="menu|daily_off")],
            [InlineKeyboardButton("📝 Bugünün Denemesi", callback_data="menu|daily_now")],
        ]
    )


def question_count_keyboard(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("10 Soru", callback_data=f"{prefix}|10")],
            [InlineKeyboardButton("20 Soru", callback_data=f"{prefix}|20")],
            [InlineKeyboardButton("50 Soru", callback_data=f"{prefix}|50")],
        ]
    )


def stats_text_for_user(user_id: int) -> str:
    stats = get_user_stats_from_db(user_id)
    total_answered = stats["total_answered"]
    correct = stats["correct_count"]
    wrong = stats["wrong_count"]
    accuracy = (correct / total_answered * 100) if total_answered else 0
    wrong_count = len(get_wrong_question_ids(user_id))
    daily_status = "Açık" if get_daily_quiz_enabled(user_id) else "Kapalı"

    return (
        "📊 *İstatistiklerin*\n\n"
        f"Toplam cevaplanan: *{total_answered}*\n"
        f"Doğru: *{correct}*\n"
        f"Yanlış: *{wrong}*\n"
        f"Başarı oranı: *%{accuracy:.1f}*\n"
        f"Biriken yanlış soru sayısı: *{wrong_count}*\n"
        f"Günlük deneme: *{daily_status}*"
    )


async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text("🏓 Bot aktif ve çalışıyor!")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = get_user_state(context)
    reset_quiz_state(state)

    if update.effective_user:
        ensure_user(
            update.effective_user.id,
            update.effective_user.username,
            update.effective_user.full_name,
        )

    if update.message:
        await update.message.reply_text(
            "🎯 *Terfi Sınavı Botuna Hoş Geldin*\n\n"
            "Buradan test modunu seçebilirsin:\n"
            "• Karma test\n"
            "• Konuya göre test\n"
            "• Zorluğa göre test\n"
            "• Yanlışlarını tekrar et\n"
            "• Günlük deneme başlat\n\n"
            "Aşağıdaki menüyü kullan 👇",
            reply_markup=main_menu_keyboard(),
            parse_mode=ParseMode.MARKDOWN,
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(
            "/start - ana menü\n"
            "/help - yardım\n"
            "/stats - istatistik\n"
            "/stop - testi bitir\n"
            "/daily_on - günlük denemeyi aç\n"
            "/daily_off - günlük denemeyi kapat\n"
            "/daily_now - bugünün denemesini başlat\n"
            "/ping - bot kontrol"
        )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return

    ensure_user(
        update.effective_user.id,
        update.effective_user.username,
        update.effective_user.full_name,
    )

    await update.message.reply_text(
        stats_text_for_user(update.effective_user.id),
        parse_mode=ParseMode.MARKDOWN,
    )


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = get_user_state(context)

    text = (
        "⛔ *Test bitti.*\n\n"
        f"Toplam soru: *{state['asked']}*\n"
        f"Doğru: *{state['score']}*\n"
        f"Yanlış: *{state['asked'] - state['score']}*"
    )

    reset_quiz_state(state)

    if update.message:
        await update.message.reply_text(
            text,
            reply_markup=main_menu_keyboard(),
            parse_mode=ParseMode.MARKDOWN,
        )


async def daily_on_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return

    ensure_user(
        update.effective_user.id,
        update.effective_user.username,
        update.effective_user.full_name,
    )
    set_daily_quiz_enabled(update.effective_user.id, True)
    await update.message.reply_text("🌞 Günlük deneme açıldı.")


async def daily_off_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return

    ensure_user(
        update.effective_user.id,
        update.effective_user.username,
        update.effective_user.full_name,
    )
    set_daily_quiz_enabled(update.effective_user.id, False)
    await update.message.reply_text("🌙 Günlük deneme kapatıldı.")


async def daily_now_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return

    ensure_user(
        update.effective_user.id,
        update.effective_user.username,
        update.effective_user.full_name,
    )

    state = get_user_state(context)
    reset_quiz_state(state)
    state["mode"] = "daily"
    state["question_count"] = DAILY_QUIZ_COUNT
    pool = QUESTIONS.copy()
    state["queue"] = build_queue(pool, DAILY_QUIZ_COUNT)

    await update.message.reply_text(f"📝 Bugünün {DAILY_QUIZ_COUNT} soruluk denemesi başladı.")
    await send_next_question(update, context)


async def send_next_question(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = get_user_state(context)

    if not state["queue"]:
        accuracy = (state["score"] / state["asked"] * 100) if state["asked"] else 0
        target = update.effective_message if update.effective_message else update.callback_query.message

        await target.reply_text(
            "🏁 *Test tamamlandı.*\n\n"
            f"Toplam soru: *{state['asked']}*\n"
            f"Doğru: *{state['score']}*\n"
            f"Yanlış: *{state['asked'] - state['score']}*\n"
            f"Başarı: *%{accuracy:.1f}*",
            reply_markup=main_menu_keyboard(),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    q = state["queue"].pop(0)
    state["current_question"] = q
    state["asked"] += 1
    total_count = state["asked"] + len(state["queue"])

    target = update.effective_message if update.effective_message else update.callback_query.message
    await target.reply_text(
        question_text(q, state["asked"], total_count) + "\n\n" + options_text(q),
        reply_markup=answer_keyboard(q),
        parse_mode=ParseMode.MARKDOWN,
    )


async def handle_menu_click(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    state = get_user_state(context)

    if not update.effective_user:
        return

    ensure_user(
        update.effective_user.id,
        update.effective_user.username,
        update.effective_user.full_name,
    )

    _, action = query.data.split("|", 1)

    if action == "random":
        reset_quiz_state(state)
        state["mode"] = "random"
        await query.message.reply_text(
            "🎲 Kaç soru çözmek istiyorsun?",
            reply_markup=question_count_keyboard("count_random"),
        )
        return

    if action == "topic":
        rows = [[InlineKeyboardButton(f"📚 {topic}", callback_data=f"topic|{topic}")] for topic in unique_topics()]
        rows.append([InlineKeyboardButton("📦 Hepsi", callback_data="topic|hepsi")])

        await query.message.reply_text(
            "🧩 Bir konu seç:",
            reply_markup=InlineKeyboardMarkup(rows)
        )
        return

    if action == "difficulty":
        rows = [[InlineKeyboardButton(f"⚡ {diff.capitalize()}", callback_data=f"difficulty|{diff}")] for diff in unique_difficulties()]
        rows.append([InlineKeyboardButton("📦 Hepsi", callback_data="difficulty|hepsi")])

        await query.message.reply_text(
            "⚡ Bir zorluk seç:",
            reply_markup=InlineKeyboardMarkup(rows)
        )
        return

    if action == "wrong":
        wrong_questions = get_wrong_questions_for_user(update.effective_user.id)
        if not wrong_questions:
            await query.message.reply_text("Henüz biriken yanlış soru yok.")
            return

        reset_quiz_state(state)
        state["mode"] = "wrong"
        await query.message.reply_text(
            "🔁 Yanlışlarından kaç soru çözmek istiyorsun?",
            reply_markup=question_count_keyboard("count_wrong")
        )
        return

    if action == "stats":
        await query.message.reply_text(
            stats_text_for_user(update.effective_user.id),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if action == "daily_on":
        set_daily_quiz_enabled(update.effective_user.id, True)
        await query.message.reply_text("🌞 Günlük deneme açıldı.")
        return

    if action == "daily_off":
        set_daily_quiz_enabled(update.effective_user.id, False)
        await query.message.reply_text("🌙 Günlük deneme kapatıldı.")
        return

    if action == "daily_now":
        reset_quiz_state(state)
        state["mode"] = "daily"
        state["question_count"] = DAILY_QUIZ_COUNT
        pool = QUESTIONS.copy()
        state["queue"] = build_queue(pool, DAILY_QUIZ_COUNT)

        await query.message.reply_text(f"📝 Bugünün {DAILY_QUIZ_COUNT} soruluk denemesi başladı.")
        await send_next_question(update, context)
        return

    if action == "finish_test":
        text = (
            "⛔ *Test sonlandırıldı.*\n\n"
            f"Toplam soru: *{state['asked']}*\n"
            f"Doğru: *{state['score']}*\n"
            f"Yanlış: *{state['asked'] - state['score']}*"
        )
        reset_quiz_state(state)
        await query.message.reply_text(
            text,
            reply_markup=main_menu_keyboard(),
            parse_mode=ParseMode.MARKDOWN
        )
        return


async def handle_topic_click(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    state = get_user_state(context)

    _, selected_topic = query.data.split("|", 1)

    reset_quiz_state(state)
    state["mode"] = "topic"
    state["selected_topic"] = selected_topic

    await query.message.reply_text(
        f"📚 Konu seçildi: *{selected_topic}*\nKaç soru çözmek istiyorsun?",
        reply_markup=question_count_keyboard("count_topic"),
        parse_mode=ParseMode.MARKDOWN,
    )


async def handle_difficulty_click(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    state = get_user_state(context)

    _, selected_difficulty = query.data.split("|", 1)

    reset_quiz_state(state)
    state["mode"] = "difficulty"
    state["selected_difficulty"] = selected_difficulty

    await query.message.reply_text(
        f"⚡ Zorluk seçildi: *{selected_difficulty.capitalize()}*\nKaç soru çözmek istiyorsun?",
        reply_markup=question_count_keyboard("count_difficulty"),
        parse_mode=ParseMode.MARKDOWN,
    )


async def handle_count_click(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    state = get_user_state(context)

    if not update.effective_user:
        return

    mode_key, count_str = query.data.split("|", 1)
    selected_count = int(count_str)
    state["question_count"] = selected_count

    if mode_key == "count_random":
        pool = QUESTIONS.copy()
        state["queue"] = build_queue(pool, selected_count)
        await query.message.reply_text(f"🎲 {selected_count} soruluk karma test başladı.")
        await send_next_question(update, context)
        return

    if mode_key == "count_topic":
        pool = filter_questions(topic=state.get("selected_topic"))
        state["queue"] = build_queue(pool, selected_count)

        if not state["queue"]:
            await query.message.reply_text("Bu konu için soru bulunamadı.")
            return

        await query.message.reply_text(
            f"📚 Konu modu başladı: *{state.get('selected_topic')}*\nSoru sayısı: *{selected_count}*",
            parse_mode=ParseMode.MARKDOWN,
        )
        await send_next_question(update, context)
        return

    if mode_key == "count_difficulty":
        pool = filter_questions(difficulty=state.get("selected_difficulty"))
        state["queue"] = build_queue(pool, selected_count)

        if not state["queue"]:
            await query.message.reply_text("Bu zorluk için soru bulunamadı.")
            return

        await query.message.reply_text(
            f"⚡ Zorluk modu başladı: *{state.get('selected_difficulty').capitalize()}*\nSoru sayısı: *{selected_count}*",
            parse_mode=ParseMode.MARKDOWN,
        )
        await send_next_question(update, context)
        return

    if mode_key == "count_wrong":
        pool = get_wrong_questions_for_user(update.effective_user.id)
        state["queue"] = build_queue(pool, selected_count)

        if not state["queue"]:
            await query.message.reply_text("Yanlış soru bulunamadı.")
            return

        await query.message.reply_text(
            f"🔁 Yanlışlarım modu başladı.\nSoru sayısı: *{selected_count}*",
            parse_mode=ParseMode.MARKDOWN,
        )
        await send_next_question(update, context)
        return


async def handle_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    state = get_user_state(context)
    current_question = state.get("current_question")

    if not update.effective_user:
        return

    ensure_user(
        update.effective_user.id,
        update.effective_user.username,
        update.effective_user.full_name,
    )

    if not current_question:
        await query.message.reply_text("Aktif soru bulunamadı. /start ile yeniden başla.")
        return

    user_id = update.effective_user.id

    if query.data == "skip":
        add_wrong_question(user_id, current_question["id"])
        update_user_stats(user_id, False)

        state["history"].append(
            {
                "question_id": current_question["id"],
                "selected": None,
                "correct": current_question["answer"],
                "is_correct": False,
            }
        )

        await query.message.reply_text(
            "⏭ *Soru geçildi.*\n\n"
            f"Doğru cevap: *{current_question['answer']}*\n\n"
            f"📌 Açıklama:\n{current_question['explanation']}",
            parse_mode=ParseMode.MARKDOWN,
        )

        state["current_question"] = None
        await send_next_question(update, context)
        return

    _, selected = query.data.split("|", 1)
    is_correct = selected == current_question["answer"]

    if is_correct:
        state["score"] += 1
        remove_wrong_question(user_id, current_question["id"])
        update_user_stats(user_id, True)
        result_text = (
            "✅ *Doğru cevap!*\n\n"
            f"📌 Açıklama:\n{current_question['explanation']}"
        )
    else:
        add_wrong_question(user_id, current_question["id"])
        update_user_stats(user_id, False)
        result_text = (
            "❌ *Yanlış cevap!*\n\n"
            f"Senin cevabın: *{selected}*\n"
            f"Doğru cevap: *{current_question['answer']}*\n\n"
            f"📌 Açıklama:\n{current_question['explanation']}"
        )

    state["history"].append(
        {
            "question_id": current_question["id"],
            "selected": selected,
            "correct": current_question["answer"],
            "is_correct": is_correct,
        }
    )

    state["current_question"] = None
    await query.message.reply_text(result_text, parse_mode=ParseMode.MARKDOWN)
    await send_next_question(update, context)


def main() -> None:
    if not TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN tanımlı değil.")
    if not WEBHOOK_URL:
        raise ValueError("WEBHOOK_URL tanımlı değil.")

    init_db()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("stop", stop_command))
    application.add_handler(CommandHandler("daily_on", daily_on_command))
    application.add_handler(CommandHandler("daily_off", daily_off_command))
    application.add_handler(CommandHandler("daily_now", daily_now_command))
    application.add_handler(CommandHandler("ping", ping))

    application.add_handler(CallbackQueryHandler(handle_menu_click, pattern=r"^menu\|"))
    application.add_handler(CallbackQueryHandler(handle_topic_click, pattern=r"^topic\|"))
    application.add_handler(CallbackQueryHandler(handle_difficulty_click, pattern=r"^difficulty\|"))
    application.add_handler(CallbackQueryHandler(handle_count_click, pattern=r"^count_"))
    application.add_handler(CallbackQueryHandler(handle_answer, pattern=r"^(answer\||skip$)"))

    logger.warning("Bot webhook modunda başlatılıyor...")

    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=TOKEN,
        webhook_url=f"{WEBHOOK_URL}/{TOKEN}",
        secret_token="terfi-bot-secret-123",
        stop_signals=None,
    )


if __name__ == "__main__":
    main()