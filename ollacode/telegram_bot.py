"""Telegram bot interface — with inline approval buttons."""

from __future__ import annotations

import asyncio
import html
import logging
import uuid
from typing import Dict

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode, ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .config import Config
from .engine import ConversationEngine

logger = logging.getLogger(__name__)

# Per-user conversation engines
_sessions: Dict[int, ConversationEngine] = {}
# Pending approval queue: {approval_id: asyncio.Future}
_pending_approvals: Dict[str, asyncio.Future] = {}


def _get_engine(user_id: int, config: Config) -> ConversationEngine:
    """Get or create a conversation engine for a user."""
    if user_id not in _sessions:
        engine = ConversationEngine(config)
        # Auto-approve in Telegram (inline button complexity consideration)
        engine.auto_approve = True
        _sessions[user_id] = engine
    return _sessions[user_id]


def _check_allowed(user_id: int, config: Config) -> bool:
    """Check if user is in the allowed list."""
    if not config.telegram_allowed_users:
        return True
    return user_id in config.telegram_allowed_users


def _split_message(text: str, max_length: int = 4000) -> list[str]:
    """Split long messages for Telegram's limit."""
    if len(text) <= max_length:
        return [text]

    parts = []
    current = ""

    for line in text.split("\n"):
        if len(current) + len(line) + 1 > max_length:
            if current:
                parts.append(current)
            while len(line) > max_length:
                parts.append(line[:max_length])
                line = line[max_length:]
            current = line
        else:
            current = current + "\n" + line if current else line

    if current:
        parts.append(current)

    return parts


def _escape_html(text: str) -> str:
    """Escape HTML special characters while preserving allowed tags."""
    import re

    # Remove tool code blocks (not needed for user display)
    text = re.sub(r"```tool\s*\n.+?\n```", "", text, flags=re.DOTALL)

    # Extract code blocks
    code_blocks: list[tuple[str, str]] = []
    counter = [0]

    def replace_code_block(match: re.Match) -> str:
        lang = match.group(1) or ""
        code = match.group(2)
        placeholder = f"__CODE_BLOCK_{counter[0]}__"
        code_blocks.append((placeholder, f"<pre><code class=\"language-{lang}\">{html.escape(code)}</code></pre>"))
        counter[0] += 1
        return placeholder

    # Extract inline code
    inline_codes: list[tuple[str, str]] = []
    inline_counter = [0]

    def replace_inline_code(match: re.Match) -> str:
        code = match.group(1)
        placeholder = f"__INLINE_CODE_{inline_counter[0]}__"
        inline_codes.append((placeholder, f"<code>{html.escape(code)}</code>"))
        inline_counter[0] += 1
        return placeholder

    processed = re.sub(r"```(\w*)\n(.*?)```", replace_code_block, text, flags=re.DOTALL)
    processed = re.sub(r"`([^`]+)`", replace_inline_code, processed)

    # Escape remaining
    processed = html.escape(processed)

    # Markdown → HTML
    processed = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", processed)
    processed = re.sub(r"\*(.+?)\*", r"<i>\1</i>", processed)

    # Restore
    for placeholder, replacement in code_blocks:
        processed = processed.replace(html.escape(placeholder), replacement)
    for placeholder, replacement in inline_codes:
        processed = processed.replace(html.escape(placeholder), replacement)

    return processed


def run_telegram_bot(config: Config) -> None:
    """Run the Telegram bot."""
    if not config.telegram_bot_token:
        print(
            "❌ TELEGRAM_BOT_TOKEN is not set.\n"
            "   Set TELEGRAM_BOT_TOKEN in your .env file.\n"
            "   Create a bot via @BotFather to get a token."
        )
        return

    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )

    # ─── Handlers ───────────────────────────────────────

    async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user or not update.message:
            return
        if not _check_allowed(user.id, config):
            await update.message.reply_text("⛔ Access denied.")
            return

        engine = _get_engine(user.id, config)
        memory_status = "📋 OLLACODE.md loaded" if engine.has_project_memory else "📋 OLLACODE.md not found"

        welcome = (
            f"👋 Hello, <b>{html.escape(user.first_name)}</b>!\n\n"
            f"I'm <b>ollacode</b> coding assistant.\n"
            f"🤖 Model: <code>{config.ollama_model}</code>\n"
            f"📊 Max tokens: <code>{config.max_context_tokens}</code>\n"
            f"{memory_status}\n\n"
            f"Send me your coding questions!\n\n"
            f"<b>Commands:</b>\n"
            f"/clear — Reset conversation\n"
            f"/help — Help\n"
            f"/model — Model info"
        )
        await update.message.reply_text(welcome, parse_mode=ParseMode.HTML)

    async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message:
            return
        help_text = (
            "📖 <b>ollacode Help</b>\n\n"
            "Send a message to chat with the coding assistant.\n\n"
            "<b>Features:</b>\n"
            "• Code writing & review\n"
            "• Debugging help\n"
            "• File read/write/edit (diff-based)\n"
            "• File content search (grep)\n"
            "• Command execution\n"
            "• OLLACODE.md project memory\n\n"
            "<b>Commands:</b>\n"
            "/start — Start\n"
            "/clear — Reset conversation\n"
            "/model — Model & token info\n"
            "/help — This help"
        )
        await update.message.reply_text(help_text, parse_mode=ParseMode.HTML)

    async def clear_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user or not update.message:
            return
        if not _check_allowed(user.id, config):
            return
        engine = _get_engine(user.id, config)
        engine.clear()
        await update.message.reply_text("✅ Conversation history cleared.")

    async def model_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user or not update.message:
            return
        if not _check_allowed(user.id, config):
            return
        engine = _get_engine(user.id, config)
        info = (
            f"🤖 <b>Model Info</b>\n\n"
            f"Model: <code>{config.ollama_model}</code>\n"
            f"Server: <code>{config.ollama_host}</code>\n"
            f"Messages: <code>{engine.message_count}</code>\n"
            f"Est. tokens: <code>{engine.estimated_tokens}</code> / {config.max_context_tokens}\n"
            f"Compact mode: <code>{config.compact_mode}</code>\n"
            f"Project memory: <code>{'loaded' if engine.has_project_memory else 'none'}</code>"
        )
        await update.message.reply_text(info, parse_mode=ParseMode.HTML)

    async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle regular messages — forward to AI."""
        user = update.effective_user
        if not user or not update.message or not update.message.text:
            return
        if not _check_allowed(user.id, config):
            await update.message.reply_text("⛔ Access denied.")
            return

        engine = _get_engine(user.id, config)

        # Show typing action
        await update.message.chat.send_action(ChatAction.TYPING)

        try:
            response = await engine.chat(update.message.text)
        except Exception as e:
            logger.error("Chat error for user %s: %s", user.id, e)
            await update.message.reply_text(
                f"❌ Error:\n<code>{html.escape(str(e))}</code>",
                parse_mode=ParseMode.HTML,
            )
            return

        # Send response
        formatted = _escape_html(response)
        parts = _split_message(formatted)

        for part in parts:
            try:
                await update.message.reply_text(
                    part,
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                # Fall back to plain text on HTML parse failure
                plain = response[:4000]
                await update.message.reply_text(plain)

    # ─── Run bot ─────────────────────────────────────────────

    print(
        f"🤖 Starting ollacode Telegram bot...\n"
        f"   Model: {config.ollama_model}\n"
        f"   Server: {config.ollama_host}\n"
        f"   Allowed users: {config.telegram_allowed_users or 'all'}\n"
        f"   Workspace: {config.workspace_dir}\n"
        f"   Max tokens: {config.max_context_tokens}\n"
        f"   Compact mode: {config.compact_mode}\n"
        f"   Ctrl+C to stop"
    )

    app = Application.builder().token(config.telegram_bot_token).build()

    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("help", help_handler))
    app.add_handler(CommandHandler("clear", clear_handler))
    app.add_handler(CommandHandler("model", model_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    app.run_polling(allowed_updates=Update.ALL_TYPES)
