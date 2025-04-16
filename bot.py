import logging
import random
import os # Добавлено для переменных окружения
import asyncio # Добавлено для асинхронности вебхука
import uvicorn # Добавлено для запуска веб-сервера
from fastapi import FastAPI, Request, Response # Добавлен FastAPI для вебхука
from http import HTTPStatus
import time # Keep existing time import if needed elsewhere
from datetime import datetime, timedelta # Added for job queue scheduling
import sys # Для проверки наличия токена

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, Message, BotCommand
# Убедимся, что используется правильный Application
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, JobQueue
from telegram.helpers import escape_markdown
import telegram # Added for error types

# Настройка логирования
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.DEBUG
)
logger = logging.getLogger(__name__)

# --- Константы и темы ---
DEFAULT_THEME_KEY = "classic"
EMPTY_CELL_SYMBOL = "empty" # Внутренний ключ для пустой клетки в теме

THEMES = {
    "classic": {
        "name": "Классика",
        "X": "❌",
        "O": "⭕",
        EMPTY_CELL_SYMBOL: "⬜",
        "X_win": "⭐❌⭐",
        "O_win": "⭐⭕⭐"
    },
    "animals": {
        "name": "Животные",
        "X": "🐱",
        "O": "🐶",
        EMPTY_CELL_SYMBOL: "🐾",
        "X_win": "🏆🐱🏆",
        "O_win": "🏆🐶🏆"
    },
    "food": {
        "name": "Еда",
        "X": "🍕",
        "O": "🍔",
        EMPTY_CELL_SYMBOL: "▫️",
        "X_win": "🌟🍕🌟",
        "O_win": "🌟🍔🌟"
    }
    # Добавьте сюда другие темы при желании
}

# Получаем токен из переменной окружения
TOKEN = os.getenv("TOKEN")
if not TOKEN:
    logger.error("Необходимо установить переменную окружения TOKEN!")
    sys.exit(1) # Выход, если токен не найден

# URL для вебхука (Render предоставляет RENDER_EXTERNAL_URL)
WEBHOOK_URL = os.getenv("RENDER_EXTERNAL_URL") 
# Локальный URL для тестирования (если RENDER_EXTERNAL_URL не задан)
if not WEBHOOK_URL:
   logger.warning("RENDER_EXTERNAL_URL не установлена. Используется заглушка для локального теста. Установите WEBHOOK_URL вручную для продакшена.")
   WEBHOOK_URL = "https://your-local-tunnel-or-ip" # Замените, если тестируете локально с туннелем

# Порт для веб-сервера (Render предоставляет PORT)
PORT = int(os.getenv("PORT", "8080"))

# Путь для вебхука (должен совпадать в set_webhook и в endpoint)
WEBHOOK_PATH = "/webhook"
WEBHOOK_ENDPOINT_URL = f"{WEBHOOK_URL}{WEBHOOK_PATH}"

# Словарь для хранения состояния игр {chat_id: game_data}
games: dict[int, dict] = {} 

# --- FastAPI приложение (глобальное) ---
fastapi_app = FastAPI()

banned_users = set()

chat_stats = {}

@fastapi_app.get("/")
async def health_check():
    return {"status": "Али чемпион! Бот работает!"}

# --- Глобальный обработчик вебхука (принимает application) ---
async def handle_telegram_update(request: Request, application: Application):
     """Принимает обновления от Telegram и передает их PTB."""
     try:
         body = await request.json()
         update = Update.de_json(body, application.bot)
         logger.debug(f"Получено обновление: {update}")
         await application.process_update(update)
         return Response(status_code=HTTPStatus.OK)
     except Exception as e:
         logger.error(f"Ошибка обработки входящего вебхука: {e}", exc_info=True)
         return Response(status_code=HTTPStatus.INTERNAL_SERVER_ERROR)

# --- Функции бота (start, new_game, get_symbol_emoji, get_keyboard, check_winner, button_click) --- 
# Они остаются без изменений по сравнению с предыдущей версией

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /start"""
    await update.message.reply_text(
        "Али чемпион! 🎲 Для начала игры используйте команду /newgame\n"
        "🎨 Сменить символы игры: /themes" # Добавили информацию о темах
    )

async def new_game(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /newgame - создаёт новую игру"""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    username = update.effective_user.username or f"player_{user_id}"
    # Проверка на бан по username или user_id
    if str(user_id) in banned_users or (update.effective_user.username and update.effective_user.username in banned_users):
        await update.message.reply_text("⛔ Вы забанены и не можете начинать игры.")
        return
    # --- Проверка: не бот ли инициатор ---
    if hasattr(context, 'bot') and getattr(context.bot, 'id', None) == user_id:
        logger.warning(f"Bot attempted to start a new game in chat {user_id}. Ignoring.")
        if hasattr(update, 'message') and update.message:
            await update.message.reply_text("Бот не может быть игроком! Ожидайте действий от настоящих пользователей.")
        return

    # --- Проверка на активную игру ---
    if chat_id in games and not games[chat_id].get('game_over', True):
         game_message_id = games[chat_id].get('message_id')
         warning_text = "⏳ В этом чате уже идет игра! Дождитесь ее завершения или отмены."
         try:
             await update.message.reply_text(
                 warning_text,
                 reply_to_message_id=game_message_id # Reply to the game message if possible
             )
         except telegram.error.BadRequest as e:
             if "Message to be replied not found" in str(e):
                 logger.warning(f"Original game message {game_message_id} not found in chat {chat_id}. Sending new message.")
                 await update.message.reply_text(warning_text) # Send as a new message
             else:
                 logger.error(f"BadRequest when trying to reply in new_game: {e}")
                 # Можно отправить сообщение об ошибке пользователю или просто проигнорировать
                 await update.message.reply_text("Произошла ошибка при попытке начать новую игру.")
         except Exception as e:
             logger.error(f"Unexpected error when trying to reply in new_game: {e}")
             await update.message.reply_text("Произошла непредвиденная ошибка.")

         logger.warning(f"User {username} ({user_id}) tried to start a new game in chat {chat_id} while another is active.")
         return

    # --- Отмена старого таймера и удаление старой игры ---
    if user_id in games: # Добавляем проверку на существование chat_id
        old_job = games[user_id].get('timeout_job')
        if old_job:
            try:
                old_job.schedule_removal()
                logger.info(f"Removed previous timeout job for chat {user_id} before starting new game.")
            except Exception as e: # Ловим любые ошибки, включая JobLookupError
                logger.warning(f"Could not remove previous timeout job for chat {user_id} (maybe already removed or finished?): {e}")
        del games[user_id] # Удаляем данные старой игры (теперь безопасно)
        logger.info(f"Removed old game data for chat {user_id} before starting new game.")

    # Определяем тему для игры на основе выбора первого игрока
    initiator_theme_key = context.user_data.get('chosen_theme', DEFAULT_THEME_KEY)
    game_theme_emojis = THEMES.get(initiator_theme_key, THEMES[DEFAULT_THEME_KEY])

    # Инициализация новой игры
    first_player = random.choice(["X", "O"])
    second_player = "O" if first_player == "X" else "X"
    
    game_data = {
        "board": list(range(1, 10)),
        "current_player": first_player,
        "game_over": False,
        "players": {
            first_player: user_id,
            second_player: None
        },
        "user_symbols": {
            user_id: first_player
        },
        "usernames": {
            user_id: username
        },
        "message_id": None, # Добавлено для хранения ID сообщения игры
        "timeout_job": None, # Добавлено для хранения задачи тайм-аута
        "theme_emojis": game_theme_emojis # Добавлено для хранения эмодзи текущей игры
    }
    games[user_id] = game_data

    # Отправляем сообщение с игровым полем
    try:
        # Используем эмодзи из выбранной темы
        first_player_emoji = get_symbol_emoji(first_player, game_theme_emojis)
        
        sent_message = await update.message.reply_text(
            f"🎲 *Новая игра началась!* 🎲\n\n"
            f"🎨 Темы: *{game_theme_emojis['name']} {game_theme_emojis['X']}/{game_theme_emojis['O']}*\n\n"
            f"👤 {escape_markdown(username, version=1)} играет за {first_player_emoji}\n"
            f"⏳ Ожидаем второго игрока...\n\n"
            f"*Первым ходит*: {first_player_emoji}\n\n"
            f"⏱️ *Время на игру*: 90 секунд", # Добавили инфо о времени
            reply_markup=get_keyboard(user_id),
            parse_mode="Markdown"
        )
        game_data['message_id'] = sent_message.message_id
        logger.info(f"New game started by {username} ({user_id}) in chat {user_id}. Message ID: {sent_message.message_id}")

        # --- Запускаем таймер ---
        job_context = {'chat_id': user_id, 'message_id': sent_message.message_id}
        timeout_job = context.job_queue.run_once(
            game_timeout,
            when=timedelta(seconds=90),
            data=job_context,
            name=f"game_timeout_{user_id}"
        )
        game_data['timeout_job'] = timeout_job
        logger.info(f"Scheduled timeout job for game in chat {user_id}")

    except telegram.error.BadRequest as e:
         logger.error(f"Failed to send new game message in chat {user_id}: {e}")
         # Если не удалось отправить сообщение, удаляем игру
         del games[user_id]
    except Exception as e:
        logger.error(f"Unexpected error starting game in chat {user_id}: {e}", exc_info=True)
        if user_id in games:
            del games[user_id]

def get_symbol_emoji(symbol, game_theme_emojis: dict):
    """Возвращает символ с эмодзи для отображения, используя тему текущей игры."""
    if symbol == "X":
        return game_theme_emojis.get("X", "❌") # Фоллбэк на случай отсутствия ключа
    elif symbol == "O":
        return game_theme_emojis.get("O", "⭕") # Фоллбэк на случай отсутствия ключа
    elif isinstance(symbol, int): # Пустая клетка
        return game_theme_emojis.get(EMPTY_CELL_SYMBOL, "⬜") # Фоллбэк
    return str(symbol) # На случай, если передано что-то другое

def get_keyboard(chat_id, winning_indices: list | None = None):
    """Создаёт клавиатуру с игровым полем.
       Неактивные кнопки (занятые клетки или конец игры) имеют callback_data='noop'.
       Подсвечивает выигрышную комбинацию, если переданы winning_indices.
    """
    if chat_id not in games: # Проверка на случай, если игра не найдена
        logger.warning(f"get_keyboard called for non-existent game in chat {chat_id}")
        return None

    game_data = games[chat_id]
    board = game_data["board"]
    is_game_over = game_data["game_over"]
    # Получаем эмодзи для текущей игры
    theme_emojis = game_data.get("theme_emojis", THEMES[DEFAULT_THEME_KEY]) # Фоллбэк на дефолтную тему
    keyboard = []
    logger.debug(f"[get_keyboard chat={chat_id}] Board: {board}, Theme: {theme_emojis.get('name', 'Unknown')}, Winning: {winning_indices}")

    # Создаём строки по 3 кнопки
    for i in range(0, 9, 3):
        row = []
        for j in range(3):
            cell_index = i + j
            cell = board[cell_index]
            cell_text = ""
            callback_data = "noop" # По умолчанию кнопка неактивна

            # Определяем текст кнопки (с подсветкой или без)
            if isinstance(cell, int):
                cell_text = get_symbol_emoji(cell, theme_emojis)
                if not is_game_over: # Сделать кликабельной, только если игра идет
                    callback_data = str(cell_index)
            else:
                # Если игра завершена победой и эта клетка в выигрышной линии
                if is_game_over and winning_indices and cell_index in winning_indices:
                    win_symbol_key = f"{cell}_win"
                    cell_text = theme_emojis.get(win_symbol_key, get_symbol_emoji(cell, theme_emojis)) # Фоллбэк на обычный символ
                else:
                    cell_text = get_symbol_emoji(cell, theme_emojis)
                # callback_data остается "noop"

            # Логируем значение ячейки и результат
            logger.debug(f"[get_keyboard chat={chat_id}] Cell[{cell_index}]: {repr(cell)} -> Emoji: {repr(cell_text)}, Callback: {callback_data}")

            row.append(InlineKeyboardButton(cell_text, callback_data=callback_data))
        keyboard.append(row)

    # Добавляем кнопки управления игрой
    control_row = []
    if is_game_over:
        control_row.append(InlineKeyboardButton("🔄 Новая игра", callback_data="new_game"))
    else:
        # Добавляем кнопку смены темы только во время активной игры
        control_row.append(InlineKeyboardButton("🎨 Сменить тему", callback_data="change_theme_prompt"))
        # Можно добавить кнопку отмены/сдачи, если нужно
        # control_row.append(InlineKeyboardButton("❌ Отмена", callback_data="cancel_game"))

    if control_row: # Добавляем строку управления, если она не пустая
        keyboard.append(control_row)

    return InlineKeyboardMarkup(keyboard)

def check_winner(board):
    """Проверяет, есть ли победитель или ничья.
    Возвращает: (winner_symbol, winning_indices) или ("Ничья", None) или (None, None)
    """
    # Выигрышные комбинации: горизонтали, вертикали и диагонали
    win_combinations = [
        [0, 1, 2], [3, 4, 5], [6, 7, 8],  # горизонтали
        [0, 3, 6], [1, 4, 7], [2, 5, 8],  # вертикали
        [0, 4, 8], [2, 4, 6]              # диагонали
    ]

    # Проверка на победу
    for combo in win_combinations:
        if board[combo[0]] == board[combo[1]] == board[combo[2]] and not isinstance(board[combo[0]], int):
            return board[combo[0]], combo  # Возвращаем символ победителя и комбинацию

    # Проверка на ничью (если все клетки заняты)
    if not any(isinstance(cell, int) for cell in board):
        return "Ничья", None # Возвращаем "Ничья" и None для комбинации

    return None, None  # Игра продолжается

async def button_click(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик нажатия на кнопки игрового поля или 'Новая игра'"""
    query = update.callback_query
    user = update.effective_user
    user_id = user.id
    username = user.username or f"player_{user_id}"
    if str(user_id) in banned_users or (user.username and user.username in banned_users):
        try:
            await query.answer("⛔ Вы забанены и не можете играть.", show_alert=True)
        except Exception:
            pass
        return
    # Отвечаем на запрос сразу, чтобы убрать "часики", но ловим ошибку, если он устарел
    try:
        await query.answer()
    except telegram.error.BadRequest as e:
        # Если запрос слишком старый, просто логируем и продолжаем
        # Остальные проверки ниже должны обработать устаревшее состояние игры
        logger.warning(f"Failed to answer callback query (likely too old): {e}")
        # Не выходим, так как нужно проверить состояние игры дальше

    data = query.data
    chat_id = update.effective_chat.id
    # Получаем ID сообщения только если оно есть (может отсутствовать в старых апдейтах)
    message_id = query.message.message_id if query.message else None

    # --- Проверка: Существует ли игра для этого чата? ---
    if chat_id not in games:
        logger.warning(f"Button click received for non-existent game in chat {chat_id}. Data: {data}")
        await query.answer("🤔 Эта игра уже не существует или была отменена.", show_alert=True)
        # Попытка удалить "мертвое" сообщение с кнопками, если оно есть
        if message_id:
            try:
                await context.bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
                logger.info(f"Removed keyboard from potentially stale game message {message_id} in chat {chat_id}")
            except telegram.error.BadRequest:
                 logger.warning(f"Could not remove keyboard from message {message_id} in chat {chat_id} (likely already deleted or no markup).")
            except Exception as e:
                logger.error(f"Error removing keyboard from message {message_id} in chat {chat_id}: {e}")
        return

    game_data = games[chat_id]
    # Получаем ID сообщения ИЗ СОХРАНЕННЫХ ДАННЫХ ИГРЫ
    game_message_id = game_data.get('message_id')

    # --- Проверка: Актуально ли сообщение? ---
    # Сравниваем ID сообщения из коллбэка с ID, сохраненным при старте игры
    # Это предотвращает взаимодействие со старыми сообщениями от предыдущих игр
    if message_id and game_message_id and message_id != game_message_id:
        logger.warning(f"Button click received on an OLD game message ({message_id}, expected {game_message_id}) in chat {chat_id}. Data: {data}. User: {user_id}")
        await query.answer("Эта клавиатура от старой игры. Начните новую!", show_alert=True)
        # Попытка удалить кнопки со старого сообщения
        try:
             await context.bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
        except Exception as e:
             logger.warning(f"Could not remove keyboard from old message {message_id} in chat {chat_id}: {e}")
        return

    # Получаем тему текущей игры
    game_theme_emojis = game_data.get("theme_emojis", THEMES[DEFAULT_THEME_KEY])

    # 1. Обработка неактивных кнопок ('noop')
    if data == "noop":
        if game_data["game_over"]:
            # Можно отправить тихое уведомление, если пользователь кликает после конца игры
            await query.answer("🏁 Игра уже завершена. Начните новую игру!", show_alert=False)
        # Иначе (клик на занятую клетку во время игры) - ничего не делаем
        return

    # 2. Обработка кнопки "Новая игра" ('new_game')
    if data == "new_game":
        logger.info(f"User {username} ({user_id}) initiating new game via button in chat {chat_id}.")

        # --- Создаем новую игру (вызываем async new_game) ---
        # new_game сама обработает удаление старой игры и отмену таймера.
        fake_message = query.message # Используем сообщение, к которому прикреплена кнопка
        if not fake_message:
             await query.answer("Не удалось получить информацию для старта новой игры.", show_alert=True)
             logger.error(f"Could not get message object from callback query to start new game in chat {chat_id}")
             return

        fake_update = Update(
            update_id=update.update_id,
            message=fake_message
        )
        await new_game(fake_update, context)
        return # Выходим, new_game сделала всё необходимое

    # 3. Обработка хода игрока (нажатие на клетку поля)
    if data.isdigit():
        cell_index = int(data)
        username = update.effective_user.username or f"player_{user_id}" # Получаем имя пользователя

        # --- Различные проверки перед ходом ---
        if game_data["game_over"]:
            await query.answer("🏁 Игра завершена! Начните новую.", show_alert=True)
            logger.warning(f"User {username} ({user_id}) tried to make a move in finished game in chat {chat_id}.")
            return

        current_player_symbol = game_data["current_player"]
        current_player_id = game_data["players"].get(current_player_symbol)

        # -- Проверка: Второй игрок присоединился? --
        second_player_symbol = "O" if current_player_symbol == "X" else "X"
        second_player_id = game_data["players"].get(second_player_symbol)

        if not second_player_id:
            logger.debug(f"[button_click chat={chat_id}] No second player yet. "
                         f"Clicker user_id={user_id}, Current player symbol={current_player_symbol}, "
                         f"Current player_id={current_player_id}. Comparing user_id != current_player_id.")
            # Если нажавший НЕ является первым игроком (который сейчас ходит)
            if user_id != current_player_id:
                # Проверяем, не является ли пользователь ботом
                if context.bot.id == user_id:
                    # Если это бот, отклоняем его присоединение к игре
                    logger.warning(f"[button_click chat={chat_id}] Bot attempted to join game as P2. Rejecting.")
                    await query.answer("Бот не может присоединиться к игре как игрок!", show_alert=True)
                    return
                
                # !!! ДОБАВЛЕНО ДИАГНОСТИЧЕСКОЕ ЛОГИРОВАНИЕ !!!
                logger.warning(f"[button_click chat={chat_id}] *** UNEXPECTED JOIN *** "
                             f"Joining user {user_id} ({username}) as P2. "
                             f"Current player was {current_player_id}. "
                             f"This block should NOT execute if user_id == current_player_id.")
                # Присоединяем нажавшего как второго игрока
                game_data["players"][second_player_symbol] = user_id
                game_data["user_symbols"][user_id] = second_player_symbol
                game_data["usernames"][user_id] = username
                second_player_id = user_id # Обновляем для дальнейших проверок
                logger.info(f"Player 2 ({username}, {user_id}) joined the game in chat {chat_id} playing as {second_player_symbol}.")

                # Убираем таймер, так как второй игрок присоединился
                timeout_job = game_data.get('timeout_job')
                if timeout_job:
                    timeout_job.schedule_removal()
                    game_data['timeout_job'] = None # Убираем ссылку на задачу
                    logger.info(f"Removed timeout job for chat {chat_id} as second player joined.")

                 # Отправляем обновленное сообщение с информацией о втором игроке
                initiator_id = game_data["players"][current_player_symbol] # ID первого игрока
                initiator_username = game_data["usernames"].get(initiator_id, f"player_{initiator_id}")
                escaped_initiator = escape_markdown(initiator_username, version=1)
                escaped_second = escape_markdown(username, version=1)
                # Используем эмодзи из темы
                p1_emoji = get_symbol_emoji(current_player_symbol, game_theme_emojis)
                p2_emoji = get_symbol_emoji(second_player_symbol, game_theme_emojis)
                current_player_emoji = get_symbol_emoji(game_data["current_player"], game_theme_emojis) # Текущий ходящий

                try:
                    await query.edit_message_text(
                        f"🎲 *Игра началась!* 🎲\n\n"
                        f"🎨 Темы: *{game_theme_emojis['name']} {game_theme_emojis['X']}/{game_theme_emojis['O']}*\n\n"
                        f"👤 {escaped_initiator} играет за {p1_emoji}\n"
                        f"👤 {escaped_second} играет за {p2_emoji}\n\n"
                        f"*Ходит*: {current_player_emoji}", # Показываем, кто ходит
                        reply_markup=get_keyboard(chat_id), # Обновляем поле
                        parse_mode="Markdown"
                    )
                except telegram.error.RetryAfter as e:
                     logger.warning(f"Flood control exceeded trying to update message after player 2 joined: {e}")
                     await asyncio.sleep(e.retry_after)
                     try: # Повторная попытка
                        await query.edit_message_text(
                             f"🎲 *Игра началась!* 🎲\n\n"
                             f"🎨 Темы: *{game_theme_emojis['name']} {game_theme_emojis['X']}/{game_theme_emojis['O']}*\n\n"
                             f"👤 {escaped_initiator} играет за {p1_emoji}\n"
                             f"👤 {escaped_second} играет за {p2_emoji}\n\n"
                             f"*Ходит*: {current_player_emoji}",
                             reply_markup=get_keyboard(chat_id),
                             parse_mode="Markdown"
                         )
                     except Exception as inner_e:
                         logger.error(f"Failed to edit message even after retry: {inner_e}")
                except telegram.error.BadRequest as e:
                     logger.error(f"Failed to edit message after player 2 joined (maybe deleted?): {e}")
                except Exception as e:
                     logger.error(f"Unexpected error editing message after player 2 joined: {e}", exc_info=True)

                # После присоединения второго игрока выходим, ход будет сделан следующим нажатием
                return

            # Если нажавший ЯВЛЯЕТСЯ первым игроком
            else:
                 logger.debug(f"[button_click chat={chat_id}] First player clicked before second joined. User ID {user_id}. Sending wait message.")
                 await query.answer("⏳ Дождитесь второго игрока!", show_alert=False)
                 return # <-- Важно: выходим, не даем ходить
        # -- Конец проверки второго игрока --

        # -- Проверка: Ход текущего игрока? --
        if user_id != current_player_id:
            current_player_username = game_data["usernames"].get(current_player_id, f"player_{current_player_id}")
            await query.answer(f"⏱️ Не ваш ход! Сейчас ходит {current_player_username}", show_alert=False)
            return

        # -- Проверка: Клетка свободна? --
        board = game_data["board"]
        if not isinstance(board[cell_index], int):
            await query.answer("Эта клетка уже занята!", show_alert=True)
            return

        # --- Выполнение хода ---
        board[cell_index] = current_player_symbol
        logger.info(f"Player {username} ({user_id}) marked cell {cell_index} with {current_player_symbol} in chat {chat_id}.")

        # --- Проверка победителя ---
        winner, winning_indices = check_winner(board)
        if winner:
            game_data["game_over"] = True
            # Отменяем таймер, если он был активен (хотя он должен был отмениться при входе второго игрока)
            timeout_job = game_data.get('timeout_job')
            if timeout_job:
                timeout_job.schedule_removal()
                game_data['timeout_job'] = None
                logger.info(f"Removed timeout job for chat {chat_id} as game ended with a winner.")

            keyboard_to_show = None # Инициализация переменной для клавиатуры
            if winner == "Ничья":
                message_text = f"🏁 *Ничья!* 🏁\n\nИгра завершена.\n\nТемы: *{game_theme_emojis['name']} {game_theme_emojis['X']}/{game_theme_emojis['O']}*"
                logger.info(f"Game in chat {chat_id} ended in a draw.")
                keyboard_to_show = get_keyboard(chat_id) # Обычная клавиатура для ничьей
            else: # Есть победитель
                 winner_id = game_data["players"][winner]
                 winner_username = game_data["usernames"].get(winner_id, f"player_{winner_id}")
                 escaped_winner = escape_markdown(winner_username, version=1)
                 winner_emoji = get_symbol_emoji(winner, game_theme_emojis) # Эмодзи победителя
                 message_text = f"🏆 *Победитель - {escaped_winner} ({winner_emoji})!* 🏆\n\nИгра завершена.\n\nТемы: *{game_theme_emojis['name']} {game_theme_emojis['X']}/{game_theme_emojis['O']}*"
                 logger.info(f"Game in chat {chat_id} won by {winner_username} ({winner_id}) playing as {winner}.")
                 # Передаем winning_indices в get_keyboard для подсветки
                 keyboard_to_show = get_keyboard(chat_id, winning_indices=winning_indices)

            # Обновляем сообщение с результатом и кнопкой "Новая игра"
            try:
                await query.edit_message_text(
                    message_text,
                    reply_markup=keyboard_to_show, # Используем подготовленную клавиатуру
                    parse_mode="Markdown"
                )
            except telegram.error.RetryAfter as e:
                 logger.warning(f"Flood control exceeded trying to update message on game end: {e}")
                 await asyncio.sleep(e.retry_after)
                 try: # Повторная попытка
                     await query.edit_message_text(message_text, reply_markup=keyboard_to_show, parse_mode="Markdown")
                 except Exception as inner_e:
                     logger.error(f"Failed to edit message on game end even after retry: {inner_e}")
            except telegram.error.BadRequest as e:
                logger.error(f"Failed to edit message on game end (maybe deleted?): {e}")
            except Exception as e:
                logger.error(f"Unexpected error editing message on game end: {e}", exc_info=True)

            chat_id = update.effective_chat.id
            stats = chat_stats.setdefault(chat_id, {"games": 0, "wins": 0, "draws": 0, "top_players": {}})
            stats["games"] += 1
            if winner == "Ничья":
                stats["draws"] += 1
            else:
                stats["wins"] += 1
                winner_id = game_data["players"][winner]
                winner_name = game_data["usernames"].get(winner_id, str(winner_id))
                stats["top_players"][winner_name] = stats["top_players"].get(winner_name, 0) + 1

        else:
            # --- Передача хода ---
            game_data["current_player"] = second_player_symbol
            next_player_id = game_data["players"][second_player_symbol]
            next_player_username = game_data["usernames"].get(next_player_id, f"player_{next_player_id}")
            escaped_next_player = escape_markdown(next_player_username, version=1)

            # Обновляем сообщение с новым полем и информацией о следующем ходе
            # Используем эмодзи из темы
            p1_id = game_data["players"]["X"]
            p2_id = game_data["players"]["O"]
            p1_username = game_data["usernames"].get(p1_id, f"player_{p1_id}")
            p2_username = game_data["usernames"].get(p2_id, f"player_{p2_id}")
            p1_emoji = get_symbol_emoji("X", game_theme_emojis)
            p2_emoji = get_symbol_emoji("O", game_theme_emojis)
            next_player_emoji = get_symbol_emoji(game_data["current_player"], game_theme_emojis)

            message_text = (
                 f"🎲 *Игра идет!* 🎲\n\n"
                 f"🎨 Темы: *{game_theme_emojis['name']} {game_theme_emojis['X']}/{game_theme_emojis['O']}*\n\n"
                 f"👤 {escape_markdown(p1_username, version=1)} ({p1_emoji}) vs {escape_markdown(p2_username, version=1)} ({p2_emoji})\n\n"
                 f"*Ходит*: {escaped_next_player} ({next_player_emoji})"
            )
            try:
                await query.edit_message_text(
                    message_text,
                    reply_markup=get_keyboard(chat_id),
                    parse_mode="Markdown"
                )
            except telegram.error.RetryAfter as e:
                 logger.warning(f"Flood control exceeded trying to update message on turn change: {e}")
                 await asyncio.sleep(e.retry_after)
                 try: # Повторная попытка
                     await query.edit_message_text(message_text, reply_markup=get_keyboard(chat_id), parse_mode="Markdown")
                 except Exception as inner_e:
                     logger.error(f"Failed to edit message on turn change even after retry: {inner_e}")
            except telegram.error.BadRequest as e:
                 # Частая ошибка - сообщение не изменилось. Игнорируем ее.
                 if "Message is not modified" not in str(e):
                      logger.error(f"Failed to edit message on turn change (maybe deleted or unmodified?): {e}")
            except Exception as e:
                 logger.error(f"Unexpected error editing message on turn change: {e}", exc_info=True)

    return # Ход обработан

# --- Новая функция для обработки тайм-аута ---
async def game_timeout(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Функция, вызываемая по таймеру, если второй игрок не присоединился."""
    job_context = context.job.data
    chat_id = job_context['chat_id']
    message_id = job_context.get('message_id') # Получаем message_id из контекста задачи

    if chat_id in games:
        game_data = games[chat_id]
        # Проверяем, действительно ли игра еще ожидает второго игрока и не завершена
        if not game_data.get('game_over') and not game_data['players'].get(game_data.get('current_player', 'X') if game_data.get('current_player', 'X') == 'O' else 'O'): # Проверка наличия второго игрока
            game_data['game_over'] = True
            game_theme_emojis = game_data.get("theme_emojis", THEMES[DEFAULT_THEME_KEY]) # Получаем тему
            logger.info(f"Game in chat {chat_id} timed out waiting for the second player.")

            # Пытаемся отредактировать исходное сообщение
            if message_id:
                try:
                    await context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=f"⌛ *Время вышло!* ⌛\n\nВторой игрок не присоединился. Игра отменена.\n\nТемы: *{game_theme_emojis['name']} {game_theme_emojis['X']}/{game_theme_emojis['O']}*",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Новая игра", callback_data="new_game")]]), # Добавляем кнопку "Новая игра"
                        parse_mode="Markdown"
                    )
                    logger.info(f"Edited game message {message_id} in chat {chat_id} to show timeout.")
                    game_data['timeout_job'] = None # Очищаем ссылку на выполненную задачу
                except telegram.error.BadRequest as e:
                    logger.error(f"Failed to edit message {message_id} on timeout (maybe deleted?): {e}")
                    # Даже если не удалось отредактировать, задача выполнена
                    game_data['timeout_job'] = None
                    # Если редактирование не удалось, отправляем новое сообщение
                    try:
                        await context.bot.send_message(
                            chat_id=chat_id,
                            text=f"⌛ *Время вышло!* ⌛\n\nВторой игрок не присоединился. Игра отменена.\n\nТемы: *{game_theme_emojis['name']} {game_theme_emojis['X']}/{game_theme_emojis['O']}*",
                             reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Новая игра", callback_data="new_game")]]),
                             parse_mode="Markdown"
                        )
                        game_data['timeout_job'] = None # Очищаем ссылку на выполненную задачу
                    except Exception as send_e:
                         logger.error(f"Failed to send timeout message in chat {chat_id}: {send_e}")
                         # Все равно очищаем, т.к. таймер сработал
                         game_data['timeout_job'] = None

                except Exception as e:
                     logger.error(f"Unexpected error editing message on timeout in chat {chat_id}: {e}", exc_info=True)
                     game_data['timeout_job'] = None # Очищаем ссылку на всякий случай
            else:
                logger.warning(f"Message ID not found for timed out game in chat {chat_id}, cannot edit original message.")
                # Отправляем новое сообщение, т.к. старое редактировать не можем
                try:
                    await context.bot.send_message(
                        chat_id=chat_id,
                         text=f"⌛ *Время вышло!* ⌛\n\nВторой игрок не присоединился. Игра отменена.\n\nТемы: *{game_theme_emojis['name']} {game_theme_emojis['X']}/{game_theme_emojis['O']}*",
                         reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Новая игра", callback_data="new_game")]]),
                         parse_mode="Markdown"
                    )
                    game_data['timeout_job'] = None # Очищаем ссылку на выполненную задачу
                except Exception as send_e:
                     logger.error(f"Failed to send timeout message in chat {chat_id}: {send_e}")
                     game_data['timeout_job'] = None # Очищаем ссылку на всякий случай

        elif game_data.get('timeout_job'):
             # Если таймер сработал, но игра уже началась или завершилась, просто логируем
             logger.info(f"Timeout job executed for chat {chat_id}, but the game state was already active or finished. No action taken.")
             # Убираем ссылку на задачу на всякий случай
             game_data['timeout_job'] = None

    else:
        logger.warning(f"Timeout job executed for chat {chat_id}, but no game data found.")

# --- Новые функции для тем ---
async def themes_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /themes - показывает доступные темы и кнопку для выбора."""
    user_id = update.effective_user.id
    chosen_theme_key = context.user_data.get('chosen_theme', DEFAULT_THEME_KEY)
    current_theme = THEMES.get(chosen_theme_key, THEMES[DEFAULT_THEME_KEY])

    buttons = []
    for key, theme in THEMES.items():
        button_text = f"{theme['name']} {theme['X']}/{theme['O']}"
        # Добавляем звездочку к текущей теме
        if key == chosen_theme_key:
             button_text = f"✅ {button_text}"
        buttons.append([InlineKeyboardButton(button_text, callback_data=f"theme_select_{key}")])

    keyboard = InlineKeyboardMarkup(buttons)
    await update.message.reply_text(
        f"🎨 *Выбор темы игры* 🎨\n\n"
        f"Текущая тема: *{current_theme['name']} {current_theme['X']}/{current_theme['O']}*\n\n"
        f"Выберите новую тему:",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

async def select_theme_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик нажатия кнопки выбора темы."""
    query = update.callback_query
    await query.answer() # Убираем часики

    theme_key = query.data.split("theme_select_")[-1]
    user_id = update.effective_user.id

    if theme_key in THEMES:
        context.user_data['chosen_theme'] = theme_key
        chosen_theme = THEMES[theme_key]
        logger.info(f"User {update.effective_user.username} ({user_id}) selected theme: {theme_key}")

        # Обновляем сообщение с кнопками, чтобы показать выбор
        buttons = []
        for key, theme in THEMES.items():
            button_text = f"{theme['name']} {theme['X']}/{theme['O']}"
            # Добавляем звездочку к выбранной теме
            if key == theme_key:
                 button_text = f"✅ {button_text}"
            buttons.append([InlineKeyboardButton(button_text, callback_data=f"theme_select_{key}")])
        keyboard = InlineKeyboardMarkup(buttons)

        try:
            await query.edit_message_text(
                f"🎨 *Выбор темы игры* 🎨\n\n"
                f"✅ Тема выбрана: *{chosen_theme['name']} {chosen_theme['X']}/{chosen_theme['O']}*\n\n"
                f"Выберите другую тему или начните игру:",
                reply_markup=keyboard,
                parse_mode="Markdown"
            )
        except telegram.error.BadRequest as e:
             # Сообщение могло быть удалено или слишком старое
             logger.warning(f"Failed to edit themes message: {e}")
             await update.effective_chat.send_message(
                 f"✅ Тема изменена на: *{chosen_theme['name']} {chosen_theme['X']}/{chosen_theme['O']}*",
                 parse_mode="Markdown"
             )

    else:
        logger.warning(f"Invalid theme key received: {theme_key}")
        await query.answer("Некорректная тема!", show_alert=True)

# --- Новые функции для смены темы во время игры ---

async def change_theme_prompt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик кнопки 'Сменить тему' во время игры."""
    query = update.callback_query
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    if chat_id not in games:
        await query.answer("Игра не найдена.", show_alert=True)
        return

    game_data = games[chat_id]

    # Проверка, что игра не завершена
    if game_data.get('game_over', True):
        await query.answer("Игра уже завершена.", show_alert=True)
        return

    # Проверка, является ли пользователь игроком
    player_symbols = [sym for sym, pid in game_data["players"].items() if pid == user_id]
    if not player_symbols:
        await query.answer("Только игроки могут менять тему.", show_alert=True)
        return
        
    await query.answer() # Убираем часики

    # Показываем кнопки выбора темы вместо игрового поля
    buttons = []
    current_game_theme_key = None
    # Найдем ключ текущей темы игры для отметки
    current_emojis = game_data.get("theme_emojis", THEMES[DEFAULT_THEME_KEY])
    for key, theme in THEMES.items():
        if theme == current_emojis:
            current_game_theme_key = key
            break
            
    for key, theme in THEMES.items():
        button_text = f"{theme['name']} {theme['X']}/{theme['O']}"
        # Отмечаем текущую тему игры
        if key == current_game_theme_key:
            button_text = f"🎮 {button_text}" # Используем другой значок для темы игры
        buttons.append([InlineKeyboardButton(button_text, callback_data=f"theme_select_ingame_{key}")])
    
    # Добавляем кнопку отмены смены темы
    buttons.append([InlineKeyboardButton("Назад к игре", callback_data="cancel_theme_change")])
    
    keyboard = InlineKeyboardMarkup(buttons)
    
    try:
        await query.edit_message_text(
            f"🎨 *Смена темы во время игры* 🎨\n\n"
            f"Выберите новую тему для текущей игры. Это также обновит вашу тему по умолчанию для будущих игр.",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
        logger.info(f"User {user_id} initiated theme change prompt in game in chat {chat_id}")
    except telegram.error.BadRequest as e:
        logger.error(f"Failed to show theme selection prompt in chat {chat_id}: {e}")
        await query.answer("Не удалось отобразить выбор темы.", show_alert=True)

async def select_theme_ingame_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик выбора темы во время игры."""
    query = update.callback_query
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    theme_key = query.data.split("theme_select_ingame_")[-1]

    if chat_id not in games:
        await query.answer("Игра не найдена.", show_alert=True)
        return
        
    game_data = games[chat_id]

    # Проверка, является ли пользователь игроком
    player_symbols = [sym for sym, pid in game_data["players"].items() if pid == user_id]
    if not player_symbols:
        await query.answer("Только игроки могут подтвердить смену темы.", show_alert=True)
        return
        
    if theme_key not in THEMES:
        await query.answer("Некорректная тема.", show_alert=True)
        logger.warning(f"Invalid ingame theme key received: {theme_key} from user {user_id}")
        return

    await query.answer(f"Тема '{THEMES[theme_key]['name']}' применена!") 

    # 1. Обновляем тему текущей игры
    game_data['theme_emojis'] = THEMES[theme_key]
    # 2. Обновляем предпочтение пользователя
    context.user_data['chosen_theme'] = theme_key
    logger.info(f"User {user_id} changed ingame theme to {theme_key} in chat {chat_id}. User preference also updated.")

    # 3. Восстанавливаем сообщение игры с новой темой и старой клавиатурой
    game_theme_emojis = game_data['theme_emojis'] # Уже обновлено
    # Формируем текст статуса (логика похожа на button_click при смене хода)
    current_player_symbol = game_data['current_player']
    current_player_id = game_data['players'].get(current_player_symbol)
    current_player_username = game_data['usernames'].get(current_player_id, f"player_{current_player_id}")
    escaped_current_player = escape_markdown(current_player_username, version=1)
    
    p1_id = game_data["players"].get("X")
    p2_id = game_data["players"].get("O")
    p1_username = game_data["usernames"].get(p1_id, f"player_{p1_id}") if p1_id else "?"
    p2_username = game_data["usernames"].get(p2_id, f"player_{p2_id}") if p2_id else "Ожидание"
    
    p1_emoji = get_symbol_emoji("X", game_theme_emojis)
    p2_emoji = get_symbol_emoji("O", game_theme_emojis)
    current_player_emoji = get_symbol_emoji(current_player_symbol, game_theme_emojis)
    
    # Проверяем, есть ли второй игрок для корректного отображения
    if p2_id: 
        message_text = (
             f"🎲 *Игра идет!* 🎲\n\n"
             f"🎨 Тема: *{game_theme_emojis['name']} {game_theme_emojis['X']}/{game_theme_emojis['O']}* (изменена)\n\n"
             f"👤 {escape_markdown(p1_username, version=1)} ({p1_emoji}) vs {escape_markdown(p2_username, version=1)} ({p2_emoji})\n\n"
             f"*Ходит*: {escaped_current_player} ({current_player_emoji})"
        )
    else: # Если второго игрока еще нет
        message_text = (
            f"🎲 *Новая игра началась!* 🎲\n\n"
            f"🎨 Тема: *{game_theme_emojis['name']} {game_theme_emojis['X']}/{game_theme_emojis['O']}* (изменена)\n\n"
            f"👤 {escape_markdown(p1_username, version=1)} играет за {p1_emoji}\n"
            f"⏳ Ожидаем второго игрока...\n\n"
            f"*Первым ходит*: {current_player_emoji}\n\n"
            f"⏱️ *Время на игру*: 90 секунд"
        )
        
    try:
        await query.edit_message_text(
            message_text,
            reply_markup=get_keyboard(chat_id), # Восстанавливаем игровую клавиатуру
            parse_mode="Markdown"
        )
    except telegram.error.BadRequest as e:
        logger.error(f"Failed to restore game message after ingame theme change in chat {chat_id}: {e}")
    except Exception as e:
        logger.error(f"Unexpected error restoring game message after ingame theme change in chat {chat_id}: {e}", exc_info=True)

async def cancel_theme_change_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик кнопки 'Назад к игре' при смене темы."""
    query = update.callback_query
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    if chat_id not in games:
        await query.answer("Игра не найдена.", show_alert=True)
        return
        
    game_data = games[chat_id]
    
    # Проверка, является ли пользователь игроком (хотя бы чтоб наверняка)
    player_symbols = [sym for sym, pid in game_data["players"].items() if pid == user_id]
    if not player_symbols and user_id not in game_data["players"].values(): # Двойная проверка
        await query.answer("Только игроки могут отменить смену темы.", show_alert=True)
        return

    await query.answer("Смена темы отменена.")
    logger.info(f"User {user_id} cancelled ingame theme change in chat {chat_id}.")

    # Восстанавливаем сообщение игры с текущей темой и клавиатурой
    game_theme_emojis = game_data.get("theme_emojis", THEMES[DEFAULT_THEME_KEY])
    # Формируем текст статуса (как в select_theme_ingame_callback, но без пометки "изменена")
    current_player_symbol = game_data['current_player']
    current_player_id = game_data['players'].get(current_player_symbol)
    current_player_username = game_data['usernames'].get(current_player_id, f"player_{current_player_id}")
    escaped_current_player = escape_markdown(current_player_username, version=1)
    
    p1_id = game_data["players"].get("X")
    p2_id = game_data["players"].get("O")
    p1_username = game_data["usernames"].get(p1_id, f"player_{p1_id}") if p1_id else "?"
    p2_username = game_data["usernames"].get(p2_id, f"player_{p2_id}") if p2_id else "Ожидание"
    
    p1_emoji = get_symbol_emoji("X", game_theme_emojis)
    p2_emoji = get_symbol_emoji("O", game_theme_emojis)
    current_player_emoji = get_symbol_emoji(current_player_symbol, game_theme_emojis)
    
    if p2_id: 
        message_text = (
             f"🎲 *Игра идет!* 🎲\n\n"
             f"🎨 Тема: *{game_theme_emojis['name']} {game_theme_emojis['X']}/{game_theme_emojis['O']}*\n\n"
             f"👤 {escape_markdown(p1_username, version=1)} ({p1_emoji}) vs {escape_markdown(p2_username, version=1)} ({p2_emoji})\n\n"
             f"*Ходит*: {escaped_current_player} ({current_player_emoji})"
        )
    else: # Если второго игрока еще нет
        message_text = (
            f"🎲 *Новая игра началась!* 🎲\n\n"
            f"🎨 Тема: *{game_theme_emojis['name']} {game_theme_emojis['X']}/{game_theme_emojis['O']}*\n\n"
            f"👤 {escape_markdown(p1_username, version=1)} играет за {p1_emoji}\n"
            f"⏳ Ожидаем второго игрока...\n\n"
            f"*Первым ходит*: {current_player_emoji}\n\n"
            f"⏱️ *Время на игру*: 90 секунд"
        )

    try:
        await query.edit_message_text(
            message_text,
            reply_markup=get_keyboard(chat_id), # Восстанавливаем игровую клавиатуру
            parse_mode="Markdown"
        )
    except telegram.error.BadRequest as e:
        logger.error(f"Failed to restore game message after cancelling theme change in chat {chat_id}: {e}")
    except Exception as e:
         logger.error(f"Unexpected error restoring game message after cancelling theme change in chat {chat_id}: {e}", exc_info=True)

async def reset_game(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Сбросить игру в текущем чате (только для владельца)."""
    if update.effective_user.username != "sadea12":
        await update.message.reply_text("⛔ Эта команда доступна только владельцу бота.")
        return
    chat_id = update.effective_chat.id
    if chat_id in games:
        del games[chat_id]
        await update.message.reply_text("♻️ Игра в этом чате сброшена.")
        logger.info(f"Игра в чате {chat_id} сброшена владельцем.")
    else:
        await update.message.reply_text("В этом чате нет активной игры.")

async def ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Забанить пользователя по username или user_id (только для владельца)."""
    if update.effective_user.username != "sadea12":
        await update.message.reply_text("⛔ Эта команда доступна только владельцу бота.")
        return
    if not context.args:
        await update.message.reply_text("Использование: /ban <@username или user_id>")
        return
    target = context.args[0].lstrip('@')
    banned_users.add(target)
    await update.message.reply_text(f"Пользователь {target} забанен.")
    logger.info(f"Пользователь {target} забанен владельцем.")

async def unban_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Разбанить пользователя по username или user_id (только для владельца)."""
    if update.effective_user.username != "sadea12":
        await update.message.reply_text("⛔ Эта команда доступна только владельцу бота.")
        return
    if not context.args:
        await update.message.reply_text("Использование: /unban <@username или user_id>")
        return
    target = context.args[0].lstrip('@')
    if target in banned_users:
        banned_users.remove(target)
        await update.message.reply_text(f"Пользователь {target} разбанен.")
        logger.info(f"Пользователь {target} разбанен владельцем.")
    else:
        await update.message.reply_text(f"Пользователь {target} не был в бане.")

async def chat_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.username != "sadea12":
        await update.message.reply_text("⛔ Эта команда доступна только владельцу бота.")
        return
    chat_id = update.effective_chat.id
    stats = chat_stats.get(chat_id)
    if not stats:
        await update.message.reply_text("В этом чате еще нет статистики.")
        return
    msg = [f"📊 Статистика по чату {chat_id}:"]
    msg.append(f"Всего игр: {stats['games']}")
    msg.append(f"Побед: {stats['wins']}")
    msg.append(f"Ничьих: {stats['draws']}")
    if stats['top_players']:
        msg.append("Топ-игроки по победам:")
        for user, count in sorted(stats['top_players'].items(), key=lambda x: -x[1]):
            msg.append(f"- {user}: {count}")
    await update.message.reply_text("\n".join(msg))

async def main() -> None:
    """Настраивает и запускает бота с вебхуком."""

    if not TOKEN:
        logger.critical("Переменная окружения TOKEN не установлена!")
        sys.exit(1)

    job_queue = JobQueue()
    application = Application.builder().token(TOKEN).job_queue(job_queue).build()

    # --- Регистрация обработчиков PTB ---
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("newgame", new_game))
    application.add_handler(CommandHandler("themes", themes_command))
    application.add_handler(CommandHandler("resetgame", reset_game))
    application.add_handler(CommandHandler("ban", ban_user))
    application.add_handler(CommandHandler("unban", unban_user))
    application.add_handler(CommandHandler("chatstats", chat_stats_command))
    application.add_handler(CallbackQueryHandler(button_click, pattern=r"^(noop|[0-8]|new_game)$"))
    application.add_handler(CallbackQueryHandler(change_theme_prompt_callback, pattern=r"^change_theme_prompt$"))
    application.add_handler(CallbackQueryHandler(select_theme_ingame_callback, pattern=r"^theme_select_ingame_"))
    application.add_handler(CallbackQueryHandler(cancel_theme_change_callback, pattern=r"^cancel_theme_change$"))
    application.add_handler(CallbackQueryHandler(select_theme_callback, pattern=r"^theme_select_"))

    logger.info("Инициализация PTB приложения...")
    await application.initialize()
    logger.info("PTB Приложение инициализировано.")

    # --- Регистрация команд бота ---
    commands = [
        BotCommand("start", "👋 Запустить бота"),
        BotCommand("newgame", "🎲 Начать новую игру"),
        BotCommand("themes", "🎨 Выбрать тему (эмодзи)"),
        BotCommand("resetgame", "♻️ Сбросить игру (только владелец)"),
        BotCommand("ban", "🚫 Бан пользователя (только владелец)"),
        BotCommand("unban", "✅ Разбан пользователя (только владелец)"),
        BotCommand("chatstats", "📊 Статистика по чату (только владелец)")
    ]
    try:
        await application.bot.set_my_commands(commands)
        logger.info("Команды бота успешно зарегистрированы.")
    except Exception as e:
        logger.error(f"Ошибка регистрации команд: {e}")

    # --- Настройка и установка вебхука (если URL задан) ---
    if WEBHOOK_ENDPOINT_URL:
        try:
            logger.info(f"Установка вебхука PTB на URL: {WEBHOOK_ENDPOINT_URL}")
            await application.bot.set_webhook(
                url=WEBHOOK_ENDPOINT_URL,
                allowed_updates=Update.ALL_TYPES
            )
            logger.info("Вебхук PTB успешно установлен.")

            # --- Регистрация маршрута вебхука FastAPI --- 
            # Определяем функцию-обработчик внутри main, чтобы она имела доступ к 'application'
            async def fastapi_webhook_endpoint(request: Request):
                return await handle_telegram_update(request, application)
            
            # Добавляем маршрут в FastAPI приложение
            fastapi_app.add_api_route(
                path=WEBHOOK_PATH, 
                endpoint=fastapi_webhook_endpoint, 
                methods=["POST"]
            )
            logger.info(f"FastAPI эндпоинт {WEBHOOK_PATH} зарегистрирован.")

        except Exception as e:
            logger.error(f"Ошибка установки вебхука или регистрации маршрута: {e}")
            # sys.exit(1) # Рассмотрите возможность остановки при ошибке вебхука
    else:
        logger.warning("WEBHOOK_ENDPOINT_URL не настроен. Вебхук не будет установлен!")

    # --- Настройка Uvicorn ---
    config = uvicorn.Config(
        app=fastapi_app, # Используем глобальное FastAPI приложение
        port=PORT,
        host="0.0.0.0",
        # reload=True # Для локальной разработки
    )
    server = uvicorn.Server(config)

    # --- Запуск PTB и Uvicorn ---
    await application.start()
    logger.info(f"Запуск веб-сервера на {config.host}:{config.port}...")
    await server.serve()

    # --- Остановка ---
    logger.info("Остановка приложения...")
    await application.stop()
    logger.info("PTB Приложение остановлено.")
    # (Код удаления вебхука опционален)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен вручную.")
    except Exception as e:
        logger.critical(f"Критическая ошибка при запуске: {e}", exc_info=True) 