import os
import json
import threading
import psycopg2
import socketserver
from http.server import BaseHTTPRequestHandler
from openai import AsyncOpenAI
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

# --- ⚠️ НАСТРОЙКИ (Берутся из Render) ⚠️ ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

# Подключение к OpenRouter (Мозг бота)
llm_client = AsyncOpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
)

# --- БАЗА ДАННЫХ (Neon) ---
def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    # Таблица пользователей (хранит выбранную девушку и память диалога)
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            character_id INT DEFAULT NULL,
            dialog_history TEXT DEFAULT '[]'
        )
    ''')
    # Таблица героинь (Каталог Passion-Worlds)
    c.execute('''
        CREATE TABLE IF NOT EXISTS characters (
            id SERIAL PRIMARY KEY,
            name VARCHAR(50),
            short_desc TEXT,
            prompt TEXT
        )
    ''')
    
    # Добавляем базовых персонажей, если каталог пуст
    c.execute("SELECT COUNT(*) FROM characters")
    if c.fetchone()[0] == 0:
        c.execute("""
            INSERT INTO characters (name, short_desc, prompt) VALUES 
            ('Ева (Eva Lovely)', 'Нежная, заботливая и романтичная девушка.', 'Ты Ева. Тебе 20 лет. Ты очень милая, заботливая и романтичная девушка пользователя. Отвечай коротко, тепло, используй ласковые слова и милые эмодзи.'),
            ('Рокси', 'Дерзкая геймерша с сарказмом.', 'Ты Рокси. Тебе 21 год. Ты дерзкая, саркастичная девушка-геймер. Любишь подкалывать пользователя, но в глубине души он тебе нравится. Отвечай коротко, как в чате телеграма.')
        """)
    conn.commit()
    conn.close()

init_db()

# --- ЛОГИКА БОТА ---

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    # Регистрируем юзера
    with get_db_connection() as conn:
        with conn.cursor() as c:
            c.execute("INSERT INTO users (user_id) VALUES (%s) ON CONFLICT DO NOTHING", (user_id,))
        conn.commit()
    
    kb = [[InlineKeyboardButton("🌌 Открыть каталог миров (Выбор девушки)", callback_data="open_catalog")]]
    await update.message.reply_text(
        "✨ Добро пожаловать в <b>Passion-Worlds</b>!\n\nЗдесь ты можешь выбрать виртуальную спутницу и начать уникальную историю.",
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def catalog_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "open_catalog":
        with get_db_connection() as conn:
            with conn.cursor() as c:
                c.execute("SELECT id, name, short_desc FROM characters")
                chars = c.fetchall()
        
        kb = [[InlineKeyboardButton(name, callback_data=f"char_{char_id}")] for char_id, name, desc in chars]
        
        text = "🔮 <b>Доступные спутницы:</b>\n\n"
        for char_id, name, desc in chars:
            text += f"▪️ <b>{name}</b> — <i>{desc}</i>\n"
            
        await query.message.edit_text(text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(kb))

    elif query.data.startswith("char_"):
        char_id = int(query.data.split("_")[1])
        user_id = update.effective_user.id
        
        with get_db_connection() as conn:
            with conn.cursor() as c:
                c.execute("SELECT name, prompt FROM characters WHERE id = %s", (char_id,))
                char_data = c.fetchone()
                
                # Обновляем выбор юзера и стираем прошлую память, записывая новый системный промпт
                new_history = json.dumps([{"role": "system", "content": char_data[1]}], ensure_ascii=False)
                c.execute("UPDATE users SET character_id = %s, dialog_history = %s WHERE user_id = %s", (char_id, new_history, user_id))
            conn.commit()
            
        await query.message.edit_text(f"✅ Ты выбрал <b>{char_data[0]}</b>.\n\nПросто напиши ей сообщение, чтобы начать диалог!", parse_mode='HTML')


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_text = update.message.text
    
    if not user_text:
        return

    # Достаем данные юзера
    with get_db_connection() as conn:
        with conn.cursor() as c:
            c.execute("SELECT character_id, dialog_history FROM users WHERE user_id = %s", (user_id,))
            user_data = c.fetchone()
            
    if not user_data or not user_data[0]:
        return await update.message.reply_text("Сначала выбери девушку в каталоге: /start")

    # Читаем память
    history = json.loads(user_data[1])
    history.append({"role": "user", "content": user_text})
    
    # Ограничиваем память, чтобы не сжигать лимиты (помним 1 системный промпт + 10 сообщений)
    if len(history) > 11:
        history = [history[0]] + history[-10:]

    await context.bot.send_chat_action(chat_id=user_id, action='typing')

    try:
        # Запрашиваем ответ у ИИ (используем бесплатную модель Google Gemma 2)
        response = await llm_client.chat.completions.create(
            model="google/gemma-2-9b-it:free",
            messages=history,
        )
        ai_reply = response.choices[0].message.content
        
        # Сохраняем ответ в память
        history.append({"role": "assistant", "content": ai_reply})
        
        with get_db_connection() as conn:
            with conn.cursor() as c:
                c.execute("UPDATE users SET dialog_history = %s WHERE user_id = %s", (json.dumps(history, ensure_ascii=False), user_id))
            conn.commit()
            
        await update.message.reply_text(ai_reply)

    except Exception as e:
        print(f"Ошибка ИИ: {e}")
        await update.message.reply_text("Ой, я немного задумалась... Повтори, пожалуйста 🥺")


# --- ВЕБ-СЕРВЕР ДЛЯ RENDER (Чтобы не засыпал) ---
class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Passion-Worlds Bot is running!")
    def log_message(self, format, *args): pass

def run_dummy_server():
    server_address = ("0.0.0.0", int(os.environ.get("PORT", 10000)))
    with socketserver.TCPServer(server_address, DummyHandler) as httpd:
        httpd.serve_forever()

if __name__ == "__main__":
    # Запускаем сервер в фоне
    threading.Thread(target=run_dummy_server, daemon=True).start()
    
    # Запускаем бота
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CallbackQueryHandler(catalog_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    
    print("🚀 Бот Passion-Worlds запущен!")
    app.run_polling()
