import os
import json
import threading
import psycopg2
import socketserver
from http.server import BaseHTTPRequestHandler
from openai import AsyncOpenAI
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

# --- НАСТРОЙКИ ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

# Подключаем ИИ и обязательно "представляемся"
llm_client = AsyncOpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
    default_headers={
        "HTTP-Referer": "https://github.com/DROZ-debug/Passion-Worlds",
        "X-Title": "Passion-Worlds Bot"
    }
)

# --- БАЗА ДАННЫХ ---
def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            character_id INT DEFAULT NULL,
            dialog_history TEXT DEFAULT '[]'
        )
    ''')
    c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS energy INT DEFAULT 20")
    c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS nsfw_mode BOOLEAN DEFAULT FALSE")
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS characters (
            id SERIAL PRIMARY KEY,
            name VARCHAR(50),
            short_desc TEXT,
            prompt TEXT
        )
    ''')
    
    c.execute("SELECT COUNT(*) FROM characters")
    if c.fetchone()[0] == 0:
        c.execute("""
            INSERT INTO characters (name, short_desc, prompt) VALUES 
            ('Ева (Eva Lovely)', 'Нежная и заботливая.', 'Ты Ева. Тебе 20 лет. Ты очень милая, заботливая и романтичная. Отвечай коротко, тепло, используй ласковые слова и эмодзи.'),
            ('Рокси', 'Дерзкая геймерша.', 'Ты Рокси. Тебе 21 год. Ты дерзкая, саркастичная девушка-геймер. Любишь подкалывать пользователя. Отвечай коротко.')
        """)
    conn.commit()
    conn.close()

init_db()

# --- МЕНЮ И КНОПКИ ---
def get_main_keyboard():
    keyboard = [
        [KeyboardButton("🌌 Каталог миров"), KeyboardButton("👤 Мой профиль")],
        [KeyboardButton("🛑 Сбросить память"), KeyboardButton("⚙️ Настройки NSFW")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# --- ЛОГИКА КОМАНД И КНОПОК ---
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    with get_db_connection() as conn:
        with conn.cursor() as c:
            c.execute("INSERT INTO users (user_id) VALUES (%s) ON CONFLICT DO NOTHING", (user_id,))
        conn.commit()
    
    await update.message.reply_text(
        "✨ Добро пожаловать в <b>Passion-Worlds</b>!\n\nИспользуй меню внизу, чтобы управлять ботом.",
        parse_mode='HTML',
        reply_markup=get_main_keyboard()
    )

async def show_catalog(message):
    with get_db_connection() as conn:
        with conn.cursor() as c:
            c.execute("SELECT id, name, short_desc FROM characters")
            chars = c.fetchall()
            
    kb = [[InlineKeyboardButton(name, callback_data=f"char_{char_id}")] for char_id, name, desc in chars]
    text = "🔮 <b>Выбери свою спутницу:</b>\n\n"
    for char_id, name, desc in chars:
        text += f"▪️ <b>{name}</b> — <i>{desc}</i>\n"
        
    await message.reply_text(text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(kb))

async def show_profile(message, user_id):
    with get_db_connection() as conn:
        with conn.cursor() as c:
            c.execute("SELECT character_id, energy, nsfw_mode FROM users WHERE user_id = %s", (user_id,))
            user_data = c.fetchone()
            
    char_name = "Не выбрана"
    if user_data[0]:
        with get_db_connection() as conn:
            with conn.cursor() as c:
                c.execute("SELECT name FROM characters WHERE id = %s", (user_data[0],))
                char_res = c.fetchone()
                if char_res: char_name = char_res[0]
                
    nsfw_status = "ВКЛЮЧЕН 🔞" if user_data[2] else "ВЫКЛЮЧЕН 🟢"
    
    text = (f"👤 <b>Твой профиль:</b>\n\n"
            f"💕 Текущая девушка: <b>{char_name}</b>\n"
            f"⚡️ Энергия: <b>{user_data[1]} сообщений</b>\n"
            f"⚙️ Режим NSFW: <b>{nsfw_status}</b>")
    await message.reply_text(text, parse_mode='HTML')

async def toggle_nsfw(message, user_id):
    with get_db_connection() as conn:
        with conn.cursor() as c:
            c.execute("SELECT nsfw_mode FROM users WHERE user_id = %s", (user_id,))
            current_mode = c.fetchone()[0]
            new_mode = not current_mode
            c.execute("UPDATE users SET nsfw_mode = %s WHERE user_id = %s", (new_mode, user_id))
        conn.commit()
        
    status = "ВКЛЮЧЕН 🔞" if new_mode else "ВЫКЛЮЧЕН 🟢"
    await message.reply_text(f"Режим NSFW {status}")

async def reset_memory(message, user_id):
    with get_db_connection() as conn:
        with conn.cursor() as c:
            c.execute("SELECT character_id FROM users WHERE user_id = %s", (user_id,))
            char_id = c.fetchone()[0]
            
            if not char_id:
                return await message.reply_text("Сначала выбери девушку в каталоге!")
                
            c.execute("SELECT prompt FROM characters WHERE id = %s", (char_id,))
            prompt = c.fetchone()[0]
            
            new_history = json.dumps([{"role": "system", "content": prompt}], ensure_ascii=False)
            c.execute("UPDATE users SET dialog_history = %s WHERE user_id = %s", (new_history, user_id))
        conn.commit()
        
    await message.reply_text("🔄 Память стерта. История начинается с чистого листа!")

# --- ОБРАБОТЧИК КНОПОК И ТЕКСТА ---
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text
    
    if text == "🌌 Каталог миров":
        return await show_catalog(update.message)
    elif text == "👤 Мой профиль":
        return await show_profile(update.message, user_id)
    elif text == "⚙️ Настройки NSFW":
        return await toggle_nsfw(update.message, user_id)
    elif text == "🛑 Сбросить память":
        return await reset_memory(update.message, user_id)

    with get_db_connection() as conn:
        with conn.cursor() as c:
            c.execute("SELECT character_id, dialog_history, energy, nsfw_mode FROM users WHERE user_id = %s", (user_id,))
            user_data = c.fetchone()
            
    if not user_data or not user_data[0]:
        return await update.message.reply_text("Сначала выбери девушку: нажми «🌌 Каталог миров»")
        
    char_id, history_json, energy, nsfw_mode = user_data
    
    if energy <= 0:
        return await update.message.reply_text("⚡️ Энергия закончилась!")

    history = json.loads(history_json)
    history.append({"role": "user", "content": text})
    if len(history) > 11:
        history = [history[0]] + history[-10:]

    await context.bot.send_chat_action(chat_id=user_id, action='typing')

    # ИСПОЛЬЗУЕМ СТАБИЛЬНЫЕ МОДЕЛИ
    model_name = "gryphe/mythomax-l2-13b:free" if nsfw_mode else "meta-llama/llama-3.1-8b-instruct:free"

    try:
        response = await llm_client.chat.completions.create(
            model=model_name,
            messages=history,
        )
        ai_reply = response.choices[0].message.content
        history.append({"role": "assistant", "content": ai_reply})
        
        with get_db_connection() as conn:
            with conn.cursor() as c:
                c.execute("UPDATE users SET dialog_history = %s, energy = energy - 1 WHERE user_id = %s", 
                          (json.dumps(history, ensure_ascii=False), user_id))
            conn.commit()
            
        await update.message.reply_text(ai_reply)

    except Exception as e:
        error_text = str(e)
        print(f"Ошибка ИИ: {error_text}")
        await update.message.reply_text(f"⚠️ <b>Техническая ошибка (скинь её разработчику):</b>\n<code>{error_text}</code>", parse_mode='HTML')

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data.startswith("char_"):
        char_id = int(query.data.split("_")[1])
        user_id = update.effective_user.id
        
        with get_db_connection() as conn:
            with conn.cursor() as c:
                c.execute("SELECT name, prompt FROM characters WHERE id = %s", (char_id,))
                char_data = c.fetchone()
                
                new_history = json.dumps([{"role": "system", "content": char_data[1]}], ensure_ascii=False)
                c.execute("UPDATE users SET character_id = %s, dialog_history = %s WHERE user_id = %s", (char_id, new_history, user_id))
            conn.commit()
            
        await query.message.edit_text(f"✅ Ты выбрал <b>{char_data[0]}</b>.\n\nПросто напиши ей сообщение!", parse_mode='HTML')

# --- ВЕБ-СЕРВЕР ---
class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Passion-Worlds Bot is running!")
    def log_message(self, format, *args): pass

def run_dummy_server():
    server_address = ("0.0.0.0", int(os.environ.get("PORT", 10000)))
    with socketserver.TCPServer(server_address, DummyHandler) as httpd:
        httpd.serve_forever()

if __name__ == "__main__":
    threading.Thread(target=run_dummy_server, daemon=True).start()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    
    print("🚀 Бот Passion-Worlds запущен (Патч 1.2)!")
    app.run_polling()
