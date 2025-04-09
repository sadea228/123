import logging
import random
import os # Добавлено для переменных окружения
import asyncio # Добавлено для асинхронности вебхука
import uvicorn # Добавлено для запуска веб-сервера
from fastapi import FastAPI, Request, Response # Добавлен FastAPI для вебхука
from http import HTTPStatus

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
# Убедимся, что используется правильный Application
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.helpers import escape_markdown

# Настройка логирования
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Настройки для вебхука --- 
# Получаем токен из переменной окружения
TOKEN = os.getenv("TOKEN")
if not TOKEN:
    logger.error("Необходимо установить переменную окружения TOKEN!")
    exit(1) # Выход, если токен не найден

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

# Словарь для хранения состояния игр в разных чатах
games = {}

# --- Функции бота (start, new_game, get_symbol_emoji, get_keyboard, check_winner, button_click) --- 
# Они остаются без изменений по сравнению с предыдущей версией

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /start"""
    await update.message.reply_text(
        "Али чемпион! 🎲 Для начала игры используйте команду /newgame"
    )

async def new_game(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /newgame - создаёт новую игру"""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    username = update.effective_user.username or f"player_{user_id}"
    escaped_username = escape_markdown(username, version=1)
    
    # Инициализация новой игры
    first_player = random.choice(["X", "O"])
    second_player = "O" if first_player == "X" else "X"
    
    games[chat_id] = {
        "board": list(range(1, 10)),  # Поле с номерами клеток от 1 до 9
        "current_player": first_player,  # Случайный выбор первого игрока
        "game_over": False,
        "players": {
            first_player: user_id,  # Первый игрок (инициатор)
            second_player: None     # Второй игрок (пока не определен)
        },
        "user_symbols": {  # Для быстрого поиска символа игрока
            user_id: first_player
        },
        "usernames": {  # Словарь для хранения имен пользователей
            user_id: username
        }
    }
    
    # Отправляем сообщение с игровым полем
    await update.message.reply_text(
        f"🎲 *Новая игра началась!* 🎲\n\n"
        f"👤 {escaped_username} играет за {get_symbol_emoji(first_player)}\n"
        f"⏳ Ожидаем второго игрока...\n\n"
        f"*Первым ходит*: {get_symbol_emoji(first_player)}",
        reply_markup=get_keyboard(chat_id),
        parse_mode="Markdown"
    )

def get_symbol_emoji(symbol):
    """Возвращает символ с эмодзи для отображения"""
    if symbol == "X":
        return "❌"  # Крестик
    elif symbol == "O":
        return "⭕"  # Нолик
    return symbol

def get_keyboard(chat_id):
    """Создаёт клавиатуру с игровым полем. 
       Неактивные кнопки (занятые клетки или конец игры) имеют callback_data='noop'."""
    if chat_id not in games: # Проверка на случай, если игра не найдена
        return None
        
    game_data = games[chat_id]
    board = game_data["board"]
    is_game_over = game_data["game_over"]
    keyboard = []
    
    # Создаём строки по 3 кнопки
    for i in range(0, 9, 3):
        row = []
        for j in range(3):
            cell_index = i + j
            cell = board[cell_index]
            cell_text = ""
            callback_data = "noop" # По умолчанию кнопка неактивна

            if isinstance(cell, int):
                cell_text = "⬜"  # Пустая клетка
                if not is_game_over: # Сделать кликабельной, только если игра идет
                    callback_data = str(cell_index)
            else:
                cell_text = get_symbol_emoji(cell) # Занятая клетка (X или O)
                # callback_data остается "noop"
                
            row.append(InlineKeyboardButton(cell_text, callback_data=callback_data))
        keyboard.append(row)
    
    # Добавляем кнопку для новой игры, если текущая завершена
    if is_game_over:
        keyboard.append([InlineKeyboardButton("🔄 Новая игра", callback_data="new_game")])
    
    return InlineKeyboardMarkup(keyboard)

def check_winner(board):
    """Проверяет, есть ли победитель или ничья"""
    # Выигрышные комбинации: горизонтали, вертикали и диагонали
    win_combinations = [
        [0, 1, 2], [3, 4, 5], [6, 7, 8],  # горизонтали
        [0, 3, 6], [1, 4, 7], [2, 5, 8],  # вертикали
        [0, 4, 8], [2, 4, 6]              # диагонали
    ]
    
    # Проверка на победу
    for combo in win_combinations:
        if board[combo[0]] == board[combo[1]] == board[combo[2]] and not isinstance(board[combo[0]], int):
            return board[combo[0]]  # Возвращаем символ победителя (X или O)
    
    # Проверка на ничью (если все клетки заняты)
    if not any(isinstance(cell, int) for cell in board):
        return "Ничья"
    
    return None  # Игра продолжается

async def button_click(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик нажатия на кнопки игрового поля или 'Новая игра'"""
    query = update.callback_query
    # Отвечаем на запрос сразу, чтобы убрать "часики" на кнопке
    await query.answer() 

    data = query.data
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    # 1. Обработка неактивных кнопок
    if data == "noop":
        if chat_id in games and games[chat_id]["game_over"]:
             # Можно отправить тихое уведомление, если пользователь кликает после конца игры
             await query.answer("🏁 Игра уже завершена. Начните новую игру!", show_alert=False)
        # Иначе (клик на занятую клетку во время игры) - ничего не делаем
        return

    # 2. Обработка кнопки "Новая игра"
    if data == "new_game":
        username = update.effective_user.username or f"player_{user_id}"
        # Важно: имя для хранения берем оригинальное, для отображения - экранированное
        
        first_player = random.choice(["X", "O"])
        second_player = "O" if first_player == "X" else "X"
        
        games[chat_id] = {
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
                user_id: username # Сохраняем оригинальное имя
            }
        }

        # Получаем имя инициатора и экранируем его для сообщения
        initiator_username = games[chat_id]["usernames"].get(user_id, f"player_{user_id}")
        escaped_initiator_username = escape_markdown(initiator_username, version=1)
        
        await query.edit_message_text(
            f"🎲 *Новая игра началась!* 🎲\n\n"
            f"👤 {escaped_initiator_username} играет за {get_symbol_emoji(first_player)}\n"
            f"⏳ Ожидаем второго игрока...\n\n"
            f"*Первым ходит*: {get_symbol_emoji(first_player)}",
            reply_markup=get_keyboard(chat_id),
            parse_mode="Markdown"
        )
        # query.answer() уже был вызван в начале
        return

    # --- Логика обработки хода --- 

    # 3. Проверка существования игры
    if chat_id not in games:
        logger.warning(f"Button click in chat {chat_id} but no game found.")
        # Можно добавить уведомление пользователю через query.answer, если необходимо
        await query.answer("🚫 Не могу найти активную игру.", show_alert=True)
        return

    game_data = games[chat_id]

    # 4. Проверка, не завершена ли игра (дополнительная проверка)
    if game_data["game_over"]:
        # Обычно это обработается 'noop', но на всякий случай
        await query.answer("🏁 Игра уже завершена.", show_alert=False)
        return

    # 5. Обработка данных с кнопки (индекс ячейки)
    try:
        cell_index = int(data)
        if not (0 <= cell_index <= 8):
            raise ValueError("Invalid cell index")
    except ValueError:
        logger.error(f"Invalid callback data received: {data} in chat {chat_id}")
        await query.answer("⚠️ Ошибка обработки кнопки.", show_alert=False)
        return # Игнорируем неверные данные

    # Получаем имя пользователя для проверок и обновлений
    username = update.effective_user.username or f"player_{user_id}"
    current_player = game_data["current_player"]
    board = game_data["board"]
    
    # 6. Логика присоединения второго игрока
    is_new_player = user_id not in game_data["user_symbols"]
    player_symbol = game_data["user_symbols"].get(user_id)
    
    if is_new_player:
        assigned_symbol = None
        for symbol, player_id in game_data["players"].items():
            if player_id is None:
                game_data["players"][symbol] = user_id
                game_data["user_symbols"][user_id] = symbol
                game_data["usernames"][user_id] = username # Сохраняем оригинальное имя
                assigned_symbol = symbol
                player_symbol = symbol # Устанавливаем символ для дальнейших проверок
                break
        
        if assigned_symbol:
            # Игрок успешно присоединился
            logger.info(f"Player {username} ({user_id}) joined game in chat {chat_id} as {assigned_symbol}")
            player_info = []
            for sym, pid in game_data["players"].items():
                if pid is not None:
                    p_name = game_data["usernames"].get(pid, f"player_{pid}")
                    escaped_p_name = escape_markdown(p_name, version=1)
                    player_info.append(f"👤 {escaped_p_name}: {get_symbol_emoji(sym)}")
            
            status_text = f"🎮 *Крестики-Нолики* 🎮\n\n"
            status_text += "\n".join(player_info) + "\n\n"
            status_text += f"🎲 *Текущий ход*: {get_symbol_emoji(game_data['current_player'])}"

            await query.edit_message_text(
                status_text,
                reply_markup=get_keyboard(chat_id),
                parse_mode="Markdown"
            )

            join_msg = f"Вы присоединились как {get_symbol_emoji(assigned_symbol)}"
            if assigned_symbol != game_data['current_player']:
                join_msg += ". Сейчас ход противника!"
                await query.answer(join_msg, show_alert=False)
                return # Ждем хода противника
            else:
                join_msg += ". Ваш ход!"
                await query.answer(join_msg, show_alert=False)
                # Игрок присоединился и сейчас его ход, продолжаем обработку
        else:
            # Мест нет
            await query.answer("👥 В этой игре уже участвуют два игрока!", show_alert=True)
            return

    # 7. Проверка, чей ход
    # player_symbol был получен выше или установлен при присоединении
    if player_symbol is None:
        # Пользователь не является игроком (наблюдатель)
        await query.answer("Вы не участвуете в этой игре.", show_alert=False)
        return
    if player_symbol != current_player:
        await query.answer(f"⏳ Сейчас не ваш ход! Ходит {get_symbol_emoji(current_player)}", show_alert=False)
        return
    
    # 8. Проверка, свободна ли клетка
    if not isinstance(board[cell_index], int):
        await query.answer("🚫 Эта клетка уже занята!", show_alert=False)
        return
    
    # --- Все проверки пройдены, делаем ход ---
    logger.info(f"Player {username} ({user_id}) makes move at {cell_index} in chat {chat_id}")
    
    # 9. Делаем ход
    board[cell_index] = current_player
    
    # 10. Проверяем результат игры
    result = check_winner(board)
    
    # 11. Обновляем сообщение и состояние игры
    final_message = ""
    if result:
        # Игра завершена (победа или ничья)
        game_data["game_over"] = True
        logger.info(f"Game ended in chat {chat_id}. Result: {result}")
        winner_name_escaped = "???" 
        
        if result == "Ничья":
            final_message = ("🤝 *Игра завершена. Ничья!*\n\n"
                           "Никому не удалось победить.")
        else: # Есть победитель
            winner_symbol_emoji = get_symbol_emoji(result)
            winner_id = game_data["players"].get(result)
            if winner_id:
                 winner_name = game_data["usernames"].get(winner_id, f"player_{winner_id}")
                 winner_name_escaped = escape_markdown(winner_name, version=1)
            else: # На всякий случай
                 logger.error(f"Could not find winner ID for symbol {result} in chat {chat_id}")
                 winner_name_escaped = escape_markdown(f"Игрок {result}", version=1)

            final_message = (f"🎉 *Игра завершена!* 🎉\n\n"
                           f"🏆 *Победитель*: {winner_symbol_emoji} ({winner_name_escaped})\n\n"
                           f"Нажмите кнопку ниже, чтобы начать новую игру.")

        await query.edit_message_text(
            final_message,
            reply_markup=get_keyboard(chat_id), # Клавиатура обновится (кнопки станут noop + появится 'Новая игра')
            parse_mode="Markdown"
        )

    else:
        # Игра продолжается, передаем ход
        game_data["current_player"] = "O" if current_player == "X" else "X"
        next_player_emoji = get_symbol_emoji(game_data["current_player"])
        
        # Обновляем сообщение с текущим состоянием
        player_info = []
        for sym, pid in game_data["players"].items():
             if pid is not None:
                 p_name = game_data["usernames"].get(pid, f"player_{pid}")
                 escaped_p_name = escape_markdown(p_name, version=1)
                 player_info.append(f"👤 {escaped_p_name}: {get_symbol_emoji(sym)}")

        status_text = f"🎮 *Крестики-Нолики* 🎮\n\n"
        status_text += "\n".join(player_info) + "\n\n"
        status_text += f"🎲 *Текущий ход*: {next_player_emoji}"
        
        await query.edit_message_text(
            status_text,
            reply_markup=get_keyboard(chat_id), # Клавиатура обновится (покажет новый ход)
            parse_mode="Markdown"
        )

    # query.answer() был вызван в самом начале
    return

# --- Основная логика запуска с вебхуком --- 

async def main() -> None:
    """Настраивает и запускает бота с вебхуком."""
    
    # Создаём экземпляр Application
    application = Application.builder().token(TOKEN).build()

    # Добавляем обработчики команд
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("newgame", new_game))
    application.add_handler(CallbackQueryHandler(button_click))

    # --- ВАЖНО: Инициализируем приложение ПЕРЕД установкой вебхука и запуском сервера ---
    logger.info("Инициализация приложения...")
    await application.initialize()
    logger.info("Приложение инициализировано.")

    # Настройка вебхука при старте
    try:
        logger.info(f"Установка вебхука на URL: {WEBHOOK_ENDPOINT_URL}")
        await application.bot.set_webhook(url=WEBHOOK_ENDPOINT_URL, 
                                          allowed_updates=Update.ALL_TYPES,
                                          # Можно добавить secret_token для безопасности
                                          # secret_token="ВАШ_СУПЕР_СЕКРЕТНЫЙ_ТОКЕН"
                                         )
        logger.info("Вебхук успешно установлен.")
    except Exception as e:
        logger.error(f"Ошибка установки вебхука: {e}")
        # В зависимости от ситуации, можно либо выйти, либо продолжить без вебхука (для локальной отладки)
        # exit(1)

    # --- Настройка веб-сервера FastAPI --- 
    fastapi_app = FastAPI()

    @fastapi_app.post(WEBHOOK_PATH) # Обработчик POST запросов на /webhook
    async def telegram_webhook(request: Request):
        """Принимает обновления от Telegram и передает их обработчику."""
        try:
            body = await request.json()
            update = Update.de_json(body, application.bot)
            logger.debug(f"Получено обновление: {update}")
            # Убедимся, что приложение готово к обработке
            if not application.initialized:
                 logger.error("Приложение не инициализировано перед обработкой обновления!")
                 return Response(status_code=HTTPStatus.INTERNAL_SERVER_ERROR)
            await application.process_update(update)
            return Response(status_code=HTTPStatus.OK)
        except Exception as e:
            logger.error(f"Ошибка обработки входящего вебхука: {e}", exc_info=True)
            return Response(status_code=HTTPStatus.INTERNAL_SERVER_ERROR)

    @fastapi_app.get("/") # Простой GET эндпоинт для проверки, что сервер работает
    async def health_check():
        return {"status": "Али чемпион! Бот работает!"}

    # --- Запуск веб-сервера Uvicorn --- 
    # Используем настройки для Render (host='0.0.0.0')
    config = uvicorn.Config(
        app=fastapi_app, 
        port=PORT, 
        host="0.0.0.0",
        # Можно добавить reload=True для локальной разработки
        # reload=True 
    )
    server = uvicorn.Server(config)
    
    # --- ВАЖНО: Запускаем обработку завершения работы ПЕРЕД запуском сервера --- 
    # Это позволит корректно удалить вебхук при остановке
    application.start = False # Говорим PTB, что мы не запускаем его стандартный цикл
    await application.start() # Запускает фоновые задачи, если они есть
    
    logger.info(f"Запуск веб-сервера на {config.host}:{config.port}...")
    await server.serve()
    
    # Код ниже будет выполнен после остановки сервера (например, CTRL+C или сигнал от Render)
    logger.info("Остановка приложения...")
    await application.stop()
    logger.info("Приложение остановлено.")
    
    # Удаление вебхука при завершении (рекомендуется)
    try:
       logger.info("Удаление вебхука...")
       # Проверяем, был ли бот инициализирован, прежде чем удалять
       if application.bot:
           await application.bot.delete_webhook()
           logger.info("Вебхук удален.")
       else:
           logger.warning("Экземпляр бота не был создан, удаление вебхука пропущено.")
    except Exception as e:
       logger.error(f"Ошибка удаления вебхука: {e}")


if __name__ == "__main__":
    # Запускаем асинхронную функцию main
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен вручную.")
    except Exception as e:
        logger.critical(f"Критическая ошибка при запуске бота: {e}", exc_info=True) 