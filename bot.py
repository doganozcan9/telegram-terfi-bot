import asyncio
import json
import logging
import os
import random
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, List

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")


def run_web_server() -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Bot is running")

        def log_message(self, format, *args):
            return

    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), Handler)
    server.serve_forever()


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


def get_user_state(context: ContextTypes.DEFAULT_TYPE) -> Dict[str, Any]:
    return context.user_data.setdefault(
        "quiz_state",
        {
            "mode": None,
            "selected_topic": None,
            "selected_difficulty": None,
            "score": 0,
            "asked": 0,
            "queue": [],
            "wrong_questions": [],
            "current_question": None,
            "history": [],
        },
    )


def reset_quiz_state(state: Dict[str, Any]) -> None:
    wrongs = state.get("wrong_questions", [])
    history = state.get("history", [])
    state.clear()
    state.update(
        {
            "mode": None,
            "selected_topic": None,
            "selected_difficulty": None,
            "score": 0,
            "asked": 0,
            "queue": [],
            "wrong_questions": wrongs,
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


def question_text(q: Dict[str, Any], asked_no: int | None = None) -> str:
    lines = []

    if asked_no is not None:
        lines.append(f"Soru {asked_no}")

    lines.append(q["question"])

    for idx, option in enumerate(q["options"]):
        letter = chr(65 + idx)
        lines.append(f"{letter}) {option}")

    lines.append(f"\nKonu: {q['topic']} | Zorluk: {q['difficulty']}")
    return "\n".join(lines)


def answer_keyboard(q: Dict[str, Any]) -> InlineKeyboardMarkup:
    rows = []

    for idx, _ in enumerate(q["options"]):
        letter = chr(65 + idx)
        rows.append([InlineKeyboardButton(letter, callback_data=f"answer|{letter}")])

    rows.append([InlineKeyboardButton("Soruyu Geç", callback_data="skip")])
    return InlineKeyboardMarkup(rows)


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Karma Test Başlat", callback_data="menu|random")],
            [InlineKeyboardButton("Konu Seç", callback_data="menu|topic")],
            [InlineKeyboardButton("Zorluk Seç", callback_data="menu|difficulty")],
            [InlineKeyboardButton("Yanlışlarım", callback_data="menu|wrong")],
            [InlineKeyboardButton("İstatistik", callback_data="menu|stats")],
        ]
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = get_user_state(context)
    reset_quiz_state(state)

    if update.message:
        await update.message.reply_text(
            "Terfi sınavı botuna hoş geldin.\n\nBir mod seç:",
            reply_markup=main_menu_keyboard()
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(
            "/start - ana menü\n"
            "/help - yardım\n"
            "/stats - istatistik\n"
            "/stop - testi bitir"
        )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = get_user_state(context)

    total_answered = len(state["history"])
    correct = sum(1 for item in state["history"] if item["is_correct"])
    wrong = total_answered - correct
    accuracy = (correct / total_answered * 100) if total_answered else 0

    if update.message:
        await update.message.reply_text(
            f"Toplam cevaplanan: {total_answered}\n"
            f"Doğru: {correct}\n"
            f"Yanlış: {wrong}\n"
            f"Başarı oranı: %{accuracy:.1f}\n"
            f"Biriken yanlış soru sayısı: {len(state['wrong_questions'])}"
        )


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = get_user_state(context)

    text = (
        f"Test bitti.\n"
        f"Soru: {state['asked']}\n"
        f"Doğru: {state['score']}\n"
        f"Yanlış: {state['asked'] - state['score']}"
    )

    reset_quiz_state(state)

    if update.message:
        await update.message.reply_text(text, reply_markup=main_menu_keyboard())


async def send_next_question(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = get_user_state(context)

    if not state["queue"]:
        accuracy = (state["score"] / state["asked"] * 100) if state["asked"] else 0
        target = update.effective_message if update.effective_message else update.callback_query.message

        await target.reply_text(
            f"Test tamamlandı.\n"
            f"Toplam soru: {state['asked']}\n"
            f"Doğru: {state['score']}\n"
            f"Yanlış: {state['asked'] - state['score']}\n"
            f"Başarı: %{accuracy:.1f}",
            reply_markup=main_menu_keyboard()
        )
        return

    q = state["queue"].pop(0)
    state["current_question"] = q
    state["asked"] += 1

    target = update.effective_message if update.effective_message else update.callback_query.message
    await target.reply_text(
        question_text(q, state["asked"]),
        reply_markup=answer_keyboard(q)
    )


async def handle_menu_click(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    state = get_user_state(context)

    _, action = query.data.split("|", 1)

    if action == "random":
        reset_quiz_state(state)
        state["mode"] = "random"

        pool = QUESTIONS.copy()
        random.shuffle(pool)
        state["queue"] = pool[:10]

        await query.message.reply_text("10 soruluk karma test başladı.")
        await send_next_question(update, context)
        return

    if action == "topic":
        rows = [[InlineKeyboardButton(topic, callback_data=f"topic|{topic}")] for topic in unique_topics()]
        rows.append([InlineKeyboardButton("Hepsi", callback_data="topic|hepsi")])

        await query.message.reply_text(
            "Bir konu seç:",
            reply_markup=InlineKeyboardMarkup(rows)
        )
        return

    if action == "difficulty":
        rows = [[InlineKeyboardButton(diff, callback_data=f"difficulty|{diff}")] for diff in unique_difficulties()]
        rows.append([InlineKeyboardButton("Hepsi", callback_data="difficulty|hepsi")])

        await query.message.reply_text(
            "Bir zorluk seç:",
            reply_markup=InlineKeyboardMarkup(rows)
        )
        return

    if action == "wrong":
        if not state["wrong_questions"]:
            await query.message.reply_text("Henüz biriken yanlış soru yok.")
            return

        reset_quiz_state(state)
        state["mode"] = "wrong"
        state["queue"] = state["wrong_questions"].copy()
        random.shuffle(state["queue"])

        await query.message.reply_text("Yanlışlarım modu başladı.")
        await send_next_question(update, context)
        return

    if action == "stats":
        total_answered = len(state["history"])
        correct = sum(1 for item in state["history"] if item["is_correct"])
        wrong = total_answered - correct
        accuracy = (correct / total_answered * 100) if total_answered else 0

        await query.message.reply_text(
            f"Toplam cevaplanan: {total_answered}\n"
            f"Doğru: {correct}\n"
            f"Yanlış: {wrong}\n"
            f"Başarı oranı: %{accuracy:.1f}\n"
            f"Biriken yanlış soru sayısı: {len(state['wrong_questions'])}"
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

    pool = filter_questions(topic=selected_topic)
    random.shuffle(pool)
    state["queue"] = pool[:10]

    if not state["queue"]:
        await query.message.reply_text("Bu konu için soru bulunamadı.")
        return

    await query.message.reply_text(f"Konu modu başladı: {selected_topic}")
    await send_next_question(update, context)


async def handle_difficulty_click(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    state = get_user_state(context)

    _, selected_difficulty = query.data.split("|", 1)

    reset_quiz_state(state)
    state["mode"] = "difficulty"
    state["selected_difficulty"] = selected_difficulty

    pool = filter_questions(difficulty=selected_difficulty)
    random.shuffle(pool)
    state["queue"] = pool[:10]

    if not state["queue"]:
        await query.message.reply_text("Bu zorluk için soru bulunamadı.")
        return

    await query.message.reply_text(f"Zorluk modu başladı: {selected_difficulty}")
    await send_next_question(update, context)


async def handle_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    state = get_user_state(context)
    current_question = state.get("current_question")

    if not current_question:
        await query.message.reply_text("Aktif soru bulunamadı. /start ile yeniden başla.")
        return

    if query.data == "skip":
        state["wrong_questions"].append(current_question)
        state["history"].append(
            {
                "question_id": current_question["id"],
                "selected": None,
                "correct": current_question["answer"],
                "is_correct": False,
            }
        )

        await query.message.reply_text(
            f"Geçildi.\n"
            f"Doğru cevap: {current_question['answer']}\n"
            f"Açıklama: {current_question['explanation']}"
        )

        state["current_question"] = None
        await send_next_question(update, context)
        return

    _, selected = query.data.split("|", 1)
    is_correct = selected == current_question["answer"]

    if is_correct:
        state["score"] += 1
        result_text = f"Doğru.\nAçıklama: {current_question['explanation']}"
    else:
        state["wrong_questions"].append(current_question)
        result_text = (
            f"Yanlış. Senin cevabın: {selected}\n"
            f"Doğru cevap: {current_question['answer']}\n"
            f"Açıklama: {current_question['explanation']}"
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
    await query.message.reply_text(result_text)
    await send_next_question(update, context)


def main() -> None:
    if not TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN tanımlı değil.")

    # Python 3.14 event loop workaround
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("stop", stop_command))

    application.add_handler(CallbackQueryHandler(handle_menu_click, pattern=r"^menu\|"))
    application.add_handler(CallbackQueryHandler(handle_topic_click, pattern=r"^topic\|"))
    application.add_handler(CallbackQueryHandler(handle_difficulty_click, pattern=r"^difficulty\|"))
    application.add_handler(CallbackQueryHandler(handle_answer, pattern=r"^(answer\||skip$)"))

    logger.warning("Bot başlatılıyor...")

    web_thread = threading.Thread(target=run_web_server, daemon=True)
    web_thread.start()

    application.run_polling(stop_signals=None)


if __name__ == "__main__":
    main()