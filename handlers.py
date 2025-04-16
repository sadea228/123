import random
import asyncio
from datetime import timedelta
import telegram
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from telegram.helpers import escape_markdown

# Импортируем необходимые элементы из других модулей
from config import logger, THEMES, DEFAULT_THEME_KEY, GAME_TIMEOUT_SECONDS
from game_state import games
from game_logic import get_symbol_emoji, get_keyboard, check_winner

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /start"""
    await update.message.reply_text(
        "Али чемпион! 🎲 Для начала игры используйте команду /newgame\n"
        "🎨 Сменить символы игры: /themes"
    )

async def new_game(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /newgame - создаёт новую игру"""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    # Имитируем получение сообщения, даже если его нет (например, при старте из button_click)
    message = update.effective_message
    if not message:
        logger.error(f"Cannot start new game in chat {chat_id} without a message object.")
        # Попытка отправить сообщение об ошибке, если есть чат
        if update.effective_chat:
             try:
                 await context.bot.send_message(chat_id, "Не удалось начать игру: отсутствует информация о сообщении.")
             except Exception as send_err:
                 logger.error(f"Failed to send error message to chat {chat_id}: {send_err}")
        return

    username = update.effective_user.username or f"player_{user_id}"
    escaped_username = escape_markdown(username, version=1)

    # --- Проверка на активную игру ---
    if chat_id in games and not games[chat_id].get('game_over', True):
         await message.reply_text(
             "⏳ В этом чате уже идет игра! Дождитесь ее завершения или отмены.",
             reply_to_message_id=games[chat_id].get('message_id')
         )
         logger.warning(f"User {username} ({user_id}) tried to start a new game in chat {chat_id} while another is active.")
         return

    # --- Отмена старого таймера и удаление старой игры --- (делается всегда перед стартом новой)
    if chat_id in games:
        old_job = games[chat_id].get('timeout_job')
        if old_job:
            old_job.schedule_removal()
            logger.info(f"Removed previous timeout job for chat {chat_id} before starting new game.")
        del games[chat_id]
        logger.info(f"Removed old game data for chat {chat_id} before starting new game.")

    initiator_theme_key = context.user_data.get('chosen_theme', DEFAULT_THEME_KEY)
    game_theme_emojis = THEMES.get(initiator_theme_key, THEMES[DEFAULT_THEME_KEY])

    first_player = random.choice(["X", "O"])
    second_player = "O" if first_player == "X" else "X"

    game_data = {
        "board": list(range(1, 10)),
        "current_player": first_player,
        "game_over": False,
        "players": {first_player: user_id, second_player: None},
        "user_symbols": {user_id: first_player},
        "usernames": {user_id: username},
        "message_id": None,
        "timeout_job": None,
        "theme_emojis": game_theme_emojis
    }
    games[chat_id] = game_data

    try:
        first_player_emoji = get_symbol_emoji(first_player, game_theme_emojis)
        sent_message = await message.reply_text(
            f"🎲 *Новая игра началась!* 🎲\n\n"
            f"🎨 Темы: *{game_theme_emojis['name']} {game_theme_emojis['X']}/{game_theme_emojis['O']}*\n\n"
            f"👤 {escaped_username} играет за {first_player_emoji}\n"
            f"⏳ Ожидаем второго игрока...\n\n"
            f"*Первым ходит*: {first_player_emoji}\n\n"
            f"⏱️ *Время на игру*: {GAME_TIMEOUT_SECONDS} секунд",
            reply_markup=get_keyboard(chat_id),
            parse_mode="Markdown"
        )
        game_data['message_id'] = sent_message.message_id
        logger.info(f"New game started by {username} ({user_id}) in chat {chat_id}. Message ID: {sent_message.message_id}")

        job_context = {'chat_id': chat_id, 'message_id': sent_message.message_id}
        timeout_job = context.job_queue.run_once(
            game_timeout,
            when=timedelta(seconds=GAME_TIMEOUT_SECONDS),
            data=job_context,
            name=f"game_timeout_{chat_id}"
        )
        game_data['timeout_job'] = timeout_job
        logger.info(f"Scheduled timeout job ({GAME_TIMEOUT_SECONDS}s) for game in chat {chat_id}")

    except telegram.error.BadRequest as e:
         logger.error(f"Failed to send new game message in chat {chat_id}: {e}")
         if chat_id in games: del games[chat_id]
    except Exception as e:
        logger.error(f"Unexpected error starting game in chat {chat_id}: {e}", exc_info=True)
        if chat_id in games: del games[chat_id]

async def button_click(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик нажатия на кнопки игрового поля или управления игрой."""
    query = update.callback_query
    try:
        # Пытаемся ответить на callback query как можно раньше
        await query.answer()
    except telegram.error.BadRequest as e:
        # Игнорируем ошибку, если query слишком старый
        logger.warning(f"Failed to answer callback query (likely too old or already answered): {e}")

    data = query.data
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    message = query.message # Сообщение, к которому прикреплена кнопка
    message_id = message.message_id if message else None

    # --- Проверка 1: Игра для этого чата вообще существует? ---
    if chat_id not in games:
        logger.warning(f"Button click received for potentially non-existent or starting game in chat {chat_id}. Data: {data}. Message ID: {message_id}")
        # НЕ удаляем клавиатуру здесь, так как игра может быть в процессе создания.
        # Просто выходим, если игры точно нет в данный момент.
        # Если клик был по кнопке реально старого сообщения, клавиатура
        # должна была быть удалена при завершении той игры или по таймауту.
        return # Выходим, не трогая разметку

    # --- Если игра существует (chat_id in games) ---
    game_data = games[chat_id]
    game_message_id = game_data.get('message_id')
    game_theme_emojis = game_data.get("theme_emojis", THEMES[DEFAULT_THEME_KEY])

    # --- Проверка 2: Клик был по актуальному сообщению игры? ---
    if message_id and game_message_id and message_id != game_message_id:
        logger.warning(f"Button click on OLD message ({message_id}, current game msg is {game_message_id}) in chat {chat_id}. Data: {data}. User: {user_id}")
        # Это точно клик по старому сообщению, удаляем его клавиатуру
        try:
            await context.bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
            logger.info(f"Removed keyboard from old game message {message_id} in chat {chat_id}.")
        except Exception as e:
            logger.warning(f"Could not remove keyboard from old message {message_id} in chat {chat_id}: {e}")
        return # Выходим

    # --- Обработка разных callback_data --- 

    # 1. Неактивная кнопка (занятая клетка / конец игры)
    if data == "noop":
        # Можно добавить кастомный ответ через query.answer, если нужно
        # await query.answer("Клетка занята или игра завершена", show_alert=False)
        return

    # 2. Кнопка "Новая игра"
    elif data == "new_game":
        if not game_data.get('game_over', True):
             # await query.answer("Эта игра еще не завершена!", show_alert=True) # Не спамим, если игра активна
             logger.warning(f"User {user_id} clicked 'new_game' on an active game in chat {chat_id}.")
             return
        # Вызываем new_game, передавая текущий update (или его часть)
        logger.info(f"User {user_id} initiating new game via button in chat {chat_id}.")
        # Создаем "фиктивный" update, чтобы передать message и user
        # Важно: Используем оригинальный query.message, чтобы сохранить контекст чата
        fake_update = Update(
            update_id=update.update_id,
            message=query.message # Используем сообщение от CallbackQuery
        )
        await new_game(fake_update, context)
        return

    # 3. Ход игрока (нажатие на клетку 0-8)
    elif data.isdigit():
        cell_index = int(data)
        username = update.effective_user.username or f"player_{user_id}"

        if game_data["game_over"]:
            # await query.answer("Игра завершена!", show_alert=False)
            return

        current_player_symbol = game_data["current_player"]
        current_player_id = game_data["players"].get(current_player_symbol)
        second_player_symbol = "O" if current_player_symbol == "X" else "X"
        second_player_id = game_data["players"].get(second_player_symbol)

        # Присоединение второго игрока
        if not second_player_id:
            if user_id != current_player_id:
                game_data["players"][second_player_symbol] = user_id
                game_data["user_symbols"][user_id] = second_player_symbol
                game_data["usernames"][user_id] = username
                second_player_id = user_id
                logger.info(f"Player 2 ({username}, {user_id}) joined game in chat {chat_id} as {second_player_symbol}.")

                # Отмена таймера
                timeout_job = game_data.get('timeout_job')
                if timeout_job:
                    timeout_job.schedule_removal()
                    game_data['timeout_job'] = None
                    logger.info(f"Removed timeout job for chat {chat_id} as second player joined.")

                # Обновление сообщения игры
                p1_id = game_data["players"][current_player_symbol]
                p1_username = game_data["usernames"].get(p1_id, f"player_{p1_id}")
                p1_emoji = get_symbol_emoji(current_player_symbol, game_theme_emojis)
                p2_emoji = get_symbol_emoji(second_player_symbol, game_theme_emojis)
                current_player_emoji = get_symbol_emoji(game_data["current_player"], game_theme_emojis)
                message_text = (
                    f"🎲 *Игра началась!* 🎲\n\n"
                    f"🎨 Темы: *{game_theme_emojis['name']} {game_theme_emojis['X']}/{game_theme_emojis['O']}*\n\n"
                    f"👤 {escape_markdown(p1_username, version=1)} ({p1_emoji}) vs {escape_markdown(username, version=1)} ({p2_emoji})\n\n"
                    f"*Ходит*: {current_player_emoji}"
                )
                try:
                    await query.edit_message_text(
                        message_text,
                        reply_markup=get_keyboard(chat_id),
                        parse_mode="Markdown"
                    )
                except Exception as e:
                    logger.error(f"Error updating message after P2 join in chat {chat_id}: {e}", exc_info=True)
                return # Ход будет следующим нажатием
            else:
                # await query.answer("Ожидайте второго игрока!", show_alert=False)
                return

        # Проверка очередности хода
        if user_id != current_player_id:
            # await query.answer("Не ваш ход!", show_alert=False)
            return

        # Проверка, занята ли клетка
        board = game_data["board"]
        if not isinstance(board[cell_index], int):
            # await query.answer("Клетка занята!", show_alert=True)
            return

        # Выполнение хода
        board[cell_index] = current_player_symbol
        logger.info(f"Player {username} ({user_id}) marked cell {cell_index} with {current_player_symbol} in chat {chat_id}.")

        # Проверка победителя/ничьей
        winner, winning_indices = check_winner(board)
        if winner:
            game_data["game_over"] = True
            keyboard_to_show = None
            if winner == "Ничья":
                message_text = f"🏁 *Ничья!* 🏁\n\nИгра завершена.\n\nТемы: *{game_theme_emojis['name']} {game_theme_emojis['X']}/{game_theme_emojis['O']}*"
                logger.info(f"Game in chat {chat_id} ended in a draw.")
                keyboard_to_show = get_keyboard(chat_id)
            else: # Есть победитель
                 winner_id = game_data["players"][winner]
                 winner_username = game_data["usernames"].get(winner_id, f"player_{winner_id}")
                 winner_emoji = get_symbol_emoji(winner, game_theme_emojis)
                 message_text = f"🏆 *Победитель - {escape_markdown(winner_username, version=1)} ({winner_emoji})!* 🏆\n\nИгра завершена.\n\nТемы: *{game_theme_emojis['name']} {game_theme_emojis['X']}/{game_theme_emojis['O']}*"
                 logger.info(f"Game in chat {chat_id} won by {winner_username} ({winner_id}).")
                 keyboard_to_show = get_keyboard(chat_id, winning_indices=winning_indices)

            try:
                await query.edit_message_text(
                    message_text,
                    reply_markup=keyboard_to_show,
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.error(f"Error updating message on game end in chat {chat_id}: {e}", exc_info=True)
        else:
            # Передача хода
            game_data["current_player"] = second_player_symbol
            next_player_id = game_data["players"][second_player_symbol]
            next_player_username = game_data["usernames"].get(next_player_id, f"player_{next_player_id}")
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
                 f"*Ходит*: {escape_markdown(next_player_username, version=1)} ({next_player_emoji})"
            )
            try:
                await query.edit_message_text(
                    message_text,
                    reply_markup=get_keyboard(chat_id),
                    parse_mode="Markdown"
                )
            except telegram.error.BadRequest as e:
                 if "Message is not modified" not in str(e):
                      logger.error(f"Error updating message on turn change in chat {chat_id}: {e}")
            except Exception as e:
                 logger.error(f"Unexpected error updating message on turn change in chat {chat_id}: {e}", exc_info=True)
        return

async def game_timeout(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик тайм-аута ожидания второго игрока."""
    job_context = context.job.data
    chat_id = job_context['chat_id']
    message_id = job_context.get('message_id')

    if chat_id in games:
        game_data = games[chat_id]
        # Проверяем, что игра всё еще ждет второго игрока и не завершена
        current_player = game_data.get('current_player')
        second_player_symbol = "O" if current_player == "X" else "X"
        if not game_data.get('game_over') and not game_data['players'].get(second_player_symbol):
            game_data['game_over'] = True
            game_theme_emojis = game_data.get("theme_emojis", THEMES[DEFAULT_THEME_KEY])
            logger.info(f"Game in chat {chat_id} timed out waiting for P2.")

            if message_id:
                try:
                    await context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=f"⌛ *Время вышло!* ⌛\n\nВторой игрок не присоединился ({GAME_TIMEOUT_SECONDS} сек). Игра отменена.\n\nТемы: *{game_theme_emojis['name']} {game_theme_emojis['X']}/{game_theme_emojis['O']}*",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Новая игра", callback_data="new_game")]]),
                        parse_mode="Markdown"
                    )
                    logger.info(f"Edited message {message_id} in chat {chat_id} to show timeout.")
                except Exception as e:
                    logger.error(f"Failed to edit message {message_id} on timeout: {e}")
                    # Попытка отправить новое сообщение, если редактирование не удалось
                    try:
                        await context.bot.send_message(
                             chat_id=chat_id,
                             text=f"⌛ *Время вышло!* ⌛\n\nВторой игрок не присоединился ({GAME_TIMEOUT_SECONDS} сек). Игра отменена.",
                             reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Новая игра", callback_data="new_game")]]),
                             parse_mode="Markdown"
                        )
                    except Exception as send_e:
                         logger.error(f"Failed to send timeout message in chat {chat_id}: {send_e}")
            else:
                logger.warning(f"Message ID not found for timed out game in chat {chat_id}, sending new message.")
                try:
                    await context.bot.send_message(
                        chat_id=chat_id,
                         text=f"⌛ *Время вышло!* ⌛\n\nВторой игрок не присоединился ({GAME_TIMEOUT_SECONDS} сек). Игра отменена.",
                         reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Новая игра", callback_data="new_game")]]),
                         parse_mode="Markdown"
                    )
                except Exception as send_e:
                     logger.error(f"Failed to send timeout message in chat {chat_id}: {send_e}")

            # Убираем задачу из game_data, если она там была
            if 'timeout_job' in game_data: 
                game_data['timeout_job'] = None

        elif game_data.get('timeout_job'):
             logger.info(f"Timeout job executed for chat {chat_id}, but game already started/finished. Removing job reference.")
             game_data['timeout_job'] = None
    else:
        logger.warning(f"Timeout job executed for chat {chat_id}, but no game data found.")

# --- Обработчики тем --- 

async def themes_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /themes - показывает доступные темы."""
    user_id = update.effective_user.id
    chosen_theme_key = context.user_data.get('chosen_theme', DEFAULT_THEME_KEY)
    current_theme = THEMES.get(chosen_theme_key, THEMES[DEFAULT_THEME_KEY])

    buttons = []
    for key, theme in THEMES.items():
        button_text = f"{theme['name']} {theme['X']}/{theme['O']}"
        if key == chosen_theme_key:
             button_text = f"✅ {button_text}"
        buttons.append([InlineKeyboardButton(button_text, callback_data=f"theme_select_{key}")])

    await update.message.reply_text(
        f"🎨 *Выбор темы игры* 🎨\n\n"
        f"Текущая тема: *{current_theme['name']} {current_theme['X']}/{current_theme['O']}*\n\n"
        f"Выберите новую тему:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown"
    )

async def select_theme_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик выбора темы из меню /themes."""
    query = update.callback_query
    await query.answer()

    theme_key = query.data.split("theme_select_")[-1]
    user_id = update.effective_user.id

    if theme_key in THEMES:
        context.user_data['chosen_theme'] = theme_key
        chosen_theme = THEMES[theme_key]
        logger.info(f"User {user_id} selected theme: {theme_key}")

        buttons = []
        for key, theme in THEMES.items():
            button_text = f"{theme['name']} {theme['X']}/{theme['O']}"
            if key == theme_key:
                 button_text = f"✅ {button_text}"
            buttons.append([InlineKeyboardButton(button_text, callback_data=f"theme_select_{key}")])

        try:
            await query.edit_message_text(
                f"🎨 *Выбор темы игры* 🎨\n\n"
                f"✅ Тема выбрана: *{chosen_theme['name']} {chosen_theme['X']}/{chosen_theme['O']}*\n\n"
                f"Выберите другую тему или начните игру:",
                reply_markup=InlineKeyboardMarkup(buttons),
                parse_mode="Markdown"
            )
        except telegram.error.BadRequest as e:
             logger.warning(f"Failed to edit themes message: {e}")
             if update.effective_chat:
                 try:
                     await update.effective_chat.send_message(
                         f"✅ Тема изменена на: *{chosen_theme['name']} {chosen_theme['X']}/{chosen_theme['O']}*",
                         parse_mode="Markdown"
                     )
                 except Exception as send_err:
                      logger.error(f"Failed to send theme confirmation message: {send_err}")
    else:
        logger.warning(f"Invalid theme key received: {theme_key}")
        # await query.answer("Некорректная тема!", show_alert=True)

async def change_theme_prompt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик кнопки 'Сменить тему' во время игры."""
    query = update.callback_query
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    if chat_id not in games:
        await query.answer("Игра не найдена.", show_alert=True)
        return

    game_data = games[chat_id]

    if game_data.get('game_over', True):
        await query.answer("Игра уже завершена.", show_alert=True)
        return

    player_symbols = [sym for sym, pid in game_data["players"].items() if pid == user_id]
    if not player_symbols:
        await query.answer("Только игроки могут менять тему.", show_alert=True)
        return
        
    await query.answer()

    buttons = []
    current_game_theme_key = None
    current_emojis = game_data.get("theme_emojis", THEMES[DEFAULT_THEME_KEY])
    for key, theme in THEMES.items():
        if theme == current_emojis:
            current_game_theme_key = key
            break
            
    for key, theme in THEMES.items():
        button_text = f"{theme['name']} {theme['X']}/{theme['O']}"
        if key == current_game_theme_key:
            button_text = f"🎮 {button_text}"
        buttons.append([InlineKeyboardButton(button_text, callback_data=f"theme_select_ingame_{key}")])
    
    buttons.append([InlineKeyboardButton("Назад к игре", callback_data="cancel_theme_change")])
    
    try:
        await query.edit_message_text(
            f"🎨 *Смена темы во время игры* 🎨\n\n"
            f"Выберите новую тему для текущей игры. Это также обновит вашу тему по умолчанию для будущих игр.",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="Markdown"
        )
        logger.info(f"User {user_id} initiated theme change prompt in game chat {chat_id}")
    except Exception as e:
        logger.error(f"Failed to show theme selection prompt in chat {chat_id}: {e}")
        # await query.answer("Не удалось отобразить выбор темы.", show_alert=True)

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

    player_symbols = [sym for sym, pid in game_data["players"].items() if pid == user_id]
    if not player_symbols:
        await query.answer("Только игроки могут подтвердить смену темы.", show_alert=True)
        return
        
    if theme_key not in THEMES:
        await query.answer("Некорректная тема.", show_alert=True)
        logger.warning(f"Invalid ingame theme key received: {theme_key} from user {user_id}")
        return

    await query.answer(f"Тема '{THEMES[theme_key]['name']}' применена!") 

    game_data['theme_emojis'] = THEMES[theme_key]
    context.user_data['chosen_theme'] = theme_key
    logger.info(f"User {user_id} changed ingame theme to {theme_key} in chat {chat_id}. User preference updated.")

    # Восстанавливаем сообщение игры
    await _restore_game_message(query, context, chat_id, theme_changed=True)

async def cancel_theme_change_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик кнопки 'Назад к игре' при смене темы."""
    query = update.callback_query
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    if chat_id not in games:
        await query.answer("Игра не найдена.", show_alert=True)
        return
        
    game_data = games[chat_id]
    
    player_exists = any(pid == user_id for pid in game_data["players"].values())
    if not player_exists:
        await query.answer("Только игроки могут отменить смену темы.", show_alert=True)
        return

    await query.answer("Смена темы отменена.")
    logger.info(f"User {user_id} cancelled ingame theme change in chat {chat_id}.")

    # Восстанавливаем сообщение игры
    await _restore_game_message(query, context, chat_id, theme_changed=False)

async def _restore_game_message(query: telegram.CallbackQuery, context: ContextTypes.DEFAULT_TYPE, chat_id: int, theme_changed: bool):
    """Вспомогательная функция для восстановления текста и клавиатуры игрового сообщения."""
    if chat_id not in games:
        return # Игра могла закончиться пока выбирали тему
        
    game_data = games[chat_id]
    game_theme_emojis = game_data.get("theme_emojis", THEMES[DEFAULT_THEME_KEY])
    current_player_symbol = game_data['current_player']
    current_player_id = game_data['players'].get(current_player_symbol)
    current_player_username = game_data['usernames'].get(current_player_id, f"player_{current_player_id}")
    
    p1_id = game_data["players"].get("X")
    p2_id = game_data["players"].get("O")
    p1_username = game_data["usernames"].get(p1_id, "?") if p1_id else "?"
    p2_username = game_data["usernames"].get(p2_id, "Ожидание") if p2_id else "Ожидание"
    
    p1_emoji = get_symbol_emoji("X", game_theme_emojis)
    p2_emoji = get_symbol_emoji("O", game_theme_emojis)
    current_player_emoji = get_symbol_emoji(current_player_symbol, game_theme_emojis)
    theme_status = " (изменена)" if theme_changed else ""
    
    if p2_id: 
        message_text = (
             f"🎲 *Игра идет!* 🎲\n\n"
             f"🎨 Тема: *{game_theme_emojis['name']} {game_theme_emojis['X']}/{game_theme_emojis['O']}*{theme_status}\n\n"
             f"👤 {escape_markdown(p1_username, version=1)} ({p1_emoji}) vs {escape_markdown(p2_username, version=1)} ({p2_emoji})\n\n"
             f"*Ходит*: {escape_markdown(current_player_username, version=1)} ({current_player_emoji})"
        )
    else: # Если второго игрока еще нет
        message_text = (
            f"🎲 *Новая игра началась!* 🎲\n\n"
            f"🎨 Тема: *{game_theme_emojis['name']} {game_theme_emojis['X']}/{game_theme_emojis['O']}*{theme_status}\n\n"
            f"👤 {escape_markdown(p1_username, version=1)} играет за {p1_emoji}\n"
            f"⏳ Ожидаем второго игрока...\n\n"
            f"*Первым ходит*: {current_player_emoji}\n\n"
            f"⏱️ *Время на игру*: {GAME_TIMEOUT_SECONDS} секунд"
        )
        
    try:
        await query.edit_message_text(
            message_text,
            reply_markup=get_keyboard(chat_id),
            parse_mode="Markdown"
        )
    except telegram.error.BadRequest as e:
         if "Message is not modified" not in str(e):
              logger.error(f"Failed to restore game message in chat {chat_id}: {e}")
    except Exception as e:
         logger.error(f"Unexpected error restoring game message in chat {chat_id}: {e}", exc_info=True) 