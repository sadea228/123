import logging
from telegram import InlineKeyboardMarkup, InlineKeyboardButton

# Импортируем необходимые элементы из других модулей
from config import THEMES, DEFAULT_THEME_KEY, EMPTY_CELL_SYMBOL, logger
from game_state import games

def get_symbol_emoji(symbol, game_theme_emojis: dict):
    """Возвращает символ с эмодзи для отображения, используя тему текущей игры."""
    if symbol == "X":
        return game_theme_emojis.get("X", "❌")
    elif symbol == "O":
        return game_theme_emojis.get("O", "⭕")
    elif isinstance(symbol, int): # Пустая клетка
        return game_theme_emojis.get(EMPTY_CELL_SYMBOL, "⬜")
    # Фоллбэк для символов выигрыша, если они не найдены в теме
    elif symbol == "X_win":
         return game_theme_emojis.get("X_win", "⭐❌⭐")
    elif symbol == "O_win":
         return game_theme_emojis.get("O_win", "⭐⭕⭐")
    return str(symbol)

def get_keyboard(chat_id, winning_indices: list | None = None):
    """Создаёт клавиатуру с игровым полем.
       Неактивные кнопки (занятые клетки или конец игры) имеют callback_data='noop'.
       Подсвечивает выигрышную комбинацию, если переданы winning_indices.
    """
    if chat_id not in games:
        logger.warning(f"get_keyboard called for non-existent game in chat {chat_id}")
        return None

    game_data = games[chat_id]
    board = game_data["board"]
    is_game_over = game_data["game_over"]
    theme_emojis = game_data.get("theme_emojis", THEMES[DEFAULT_THEME_KEY])
    keyboard = []
    logger.debug(f"[get_keyboard chat={chat_id}] Board: {board}, Theme: {theme_emojis.get('name', 'Unknown')}, Winning: {winning_indices}")

    for i in range(0, 9, 3):
        row = []
        for j in range(3):
            cell_index = i + j
            cell = board[cell_index]
            cell_text = ""
            callback_data = "noop"

            if isinstance(cell, int):
                cell_text = get_symbol_emoji(cell, theme_emojis)
                if not is_game_over:
                    callback_data = str(cell_index)
            else:
                if is_game_over and winning_indices and cell_index in winning_indices:
                    win_symbol_key = f"{cell}_win"
                    # Используем get_symbol_emoji для получения символа выигрыша с фоллбэком
                    cell_text = get_symbol_emoji(win_symbol_key, theme_emojis)
                else:
                    cell_text = get_symbol_emoji(cell, theme_emojis)

            logger.debug(f"[get_keyboard chat={chat_id}] Cell[{cell_index}]: {repr(cell)} -> Emoji: {repr(cell_text)}, Callback: {callback_data}")
            row.append(InlineKeyboardButton(cell_text, callback_data=callback_data))
        keyboard.append(row)

    control_row = []
    if is_game_over:
        control_row.append(InlineKeyboardButton("🔄 Новая игра", callback_data="new_game"))
    else:
        control_row.append(InlineKeyboardButton("🎨 Сменить тему", callback_data="change_theme_prompt"))

    if control_row:
        keyboard.append(control_row)

    return InlineKeyboardMarkup(keyboard)

def check_winner(board):
    """Проверяет, есть ли победитель или ничья.
    Возвращает: (winner_symbol, winning_indices) или ("Ничья", None) или (None, None)
    """
    win_combinations = [
        [0, 1, 2], [3, 4, 5], [6, 7, 8],  # горизонтали
        [0, 3, 6], [1, 4, 7], [2, 5, 8],  # вертикали
        [0, 4, 8], [2, 4, 6]              # диагонали
    ]

    for combo in win_combinations:
        if board[combo[0]] == board[combo[1]] == board[combo[2]] and not isinstance(board[combo[0]], int):
            return board[combo[0]], combo

    if not any(isinstance(cell, int) for cell in board):
        return "Ничья", None

    return None, None 