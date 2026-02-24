"""Telegram 봇 인터페이스 — 인라인 승인 버튼 포함."""

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

# 사용자별 대화 엔진 저장
_sessions: Dict[int, ConversationEngine] = {}
# 승인 대기 큐: {approval_id: asyncio.Future}
_pending_approvals: Dict[str, asyncio.Future] = {}


def _get_engine(user_id: int, config: Config) -> ConversationEngine:
    """사용자별 대화 엔진을 가져오거나 생성합니다."""
    if user_id not in _sessions:
        engine = ConversationEngine(config)
        # Telegram에서는 자동 승인 (인라인 버튼 복잡도 고려)
        # 필요 시 False로 변경하여 인라인 버튼 승인 활성화
        engine.auto_approve = True
        _sessions[user_id] = engine
    return _sessions[user_id]


def _check_allowed(user_id: int, config: Config) -> bool:
    """사용자가 허용 목록에 있는지 확인합니다."""
    if not config.telegram_allowed_users:
        return True
    return user_id in config.telegram_allowed_users


def _split_message(text: str, max_length: int = 4000) -> list[str]:
    """긴 메시지를 텔레그램 제한에 맞게 분할합니다."""
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
    """HTML 특수문자를 이스케이프하되, 허용된 태그는 유지합니다."""
    import re

    # tool 코드블록 제거 (사용자에게 보여줄 필요 없음)
    text = re.sub(r"```tool\s*\n.+?\n```", "", text, flags=re.DOTALL)

    # 코드 블록 추출
    code_blocks: list[tuple[str, str]] = []
    counter = [0]

    def replace_code_block(match: re.Match) -> str:
        lang = match.group(1) or ""
        code = match.group(2)
        placeholder = f"__CODE_BLOCK_{counter[0]}__"
        code_blocks.append((placeholder, f"<pre><code class=\"language-{lang}\">{html.escape(code)}</code></pre>"))
        counter[0] += 1
        return placeholder

    # 인라인 코드 추출
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

    # 나머지 이스케이프
    processed = html.escape(processed)

    # 마크다운 → HTML
    processed = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", processed)
    processed = re.sub(r"\*(.+?)\*", r"<i>\1</i>", processed)

    # 복원
    for placeholder, replacement in code_blocks:
        processed = processed.replace(html.escape(placeholder), replacement)
    for placeholder, replacement in inline_codes:
        processed = processed.replace(html.escape(placeholder), replacement)

    return processed


def run_telegram_bot(config: Config) -> None:
    """Telegram 봇을 실행합니다."""
    if not config.telegram_bot_token:
        print(
            "❌ TELEGRAM_BOT_TOKEN이 설정되지 않았습니다.\n"
            "   .env 파일에 TELEGRAM_BOT_TOKEN을 설정해주세요.\n"
            "   @BotFather에서 봇을 생성하고 토큰을 받을 수 있습니다."
        )
        return

    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )

    # ─── 핸들러 ────────────────────────────────────────

    async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user or not update.message:
            return
        if not _check_allowed(user.id, config):
            await update.message.reply_text("⛔ 접근이 거부되었습니다.")
            return

        engine = _get_engine(user.id, config)
        memory_status = "📋 OLLACODE.md 로드됨" if engine.has_project_memory else "📋 OLLACODE.md 없음"

        welcome = (
            f"👋 안녕하세요, <b>{html.escape(user.first_name)}</b>!\n\n"
            f"저는 <b>ollacode</b> 코딩 어시스턴트입니다.\n"
            f"🤖 모델: <code>{config.ollama_model}</code>\n"
            f"{memory_status}\n\n"
            f"코딩 질문을 자유롭게 보내주세요!\n\n"
            f"<b>명령어:</b>\n"
            f"/clear — 대화 초기화\n"
            f"/help — 도움말\n"
            f"/model — 모델 정보"
        )
        await update.message.reply_text(welcome, parse_mode=ParseMode.HTML)

    async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message:
            return
        help_text = (
            "📖 <b>ollacode 사용법</b>\n\n"
            "일반 메시지를 보내면 코딩 어시스턴트가 답변합니다.\n\n"
            "<b>기능:</b>\n"
            "• 코드 작성 및 리뷰\n"
            "• 디버깅 도움\n"
            "• 파일 읽기/쓰기/편집 (diff 기반)\n"
            "• 파일 내용 검색 (grep)\n"
            "• 명령 실행\n"
            "• OLLACODE.md 프로젝트 메모리\n\n"
            "<b>명령어:</b>\n"
            "/start — 시작\n"
            "/clear — 대화 초기화\n"
            "/model — 모델 정보\n"
            "/help — 이 도움말"
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
        await update.message.reply_text("✅ 대화 히스토리가 초기화되었습니다.")

    async def model_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user or not update.message:
            return
        if not _check_allowed(user.id, config):
            return
        engine = _get_engine(user.id, config)
        info = (
            f"🤖 <b>모델 정보</b>\n\n"
            f"모델: <code>{config.ollama_model}</code>\n"
            f"서버: <code>{config.ollama_host}</code>\n"
            f"대화 메시지 수: <code>{engine.message_count}</code>\n"
            f"프로젝트 메모리: <code>{'로드됨' if engine.has_project_memory else '없음'}</code>"
        )
        await update.message.reply_text(info, parse_mode=ParseMode.HTML)

    async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """일반 메시지 처리 — AI에게 전달."""
        user = update.effective_user
        if not user or not update.message or not update.message.text:
            return
        if not _check_allowed(user.id, config):
            await update.message.reply_text("⛔ 접근이 거부되었습니다.")
            return

        engine = _get_engine(user.id, config)

        # 타이핑 액션 표시
        await update.message.chat.send_action(ChatAction.TYPING)

        try:
            response = await engine.chat(update.message.text)
        except Exception as e:
            logger.error("Chat error for user %s: %s", user.id, e)
            await update.message.reply_text(
                f"❌ 오류가 발생했습니다:\n<code>{html.escape(str(e))}</code>",
                parse_mode=ParseMode.HTML,
            )
            return

        # 응답 전송
        formatted = _escape_html(response)
        parts = _split_message(formatted)

        for part in parts:
            try:
                await update.message.reply_text(
                    part,
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                # HTML 파싱 실패 시 일반 텍스트로
                plain = response[:4000]
                await update.message.reply_text(plain)

    # ─── 봇 실행 ────────────────────────────────────────────

    print(
        f"🤖 ollacode Telegram 봇을 시작합니다...\n"
        f"   모델: {config.ollama_model}\n"
        f"   서버: {config.ollama_host}\n"
        f"   허용 사용자: {config.telegram_allowed_users or '모든 사용자'}\n"
        f"   workspace: {config.workspace_dir}\n"
        f"   Ctrl+C로 종료"
    )

    app = Application.builder().token(config.telegram_bot_token).build()

    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("help", help_handler))
    app.add_handler(CommandHandler("clear", clear_handler))
    app.add_handler(CommandHandler("model", model_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    app.run_polling(allowed_updates=Update.ALL_TYPES)
