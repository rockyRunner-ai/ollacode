"""CLI 인터페이스 — 리치 터미널 UI + 승인 시스템."""

from __future__ import annotations

import argparse
import asyncio
import sys

from prompt_toolkit import PromptSession
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.styles import Style as PTStyle
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text
from rich.theme import Theme

from . import __version__
from .config import Config
from .engine import ConversationEngine


# ─── Rich 테마 ───────────────────────────────────────────────
THEME = Theme({
    "info": "dim cyan",
    "warning": "bold yellow",
    "error": "bold red",
    "success": "bold green",
    "accent": "bold magenta",
    "approve": "bold green",
    "deny": "bold red",
})

console = Console(theme=THEME)

# ─── prompt_toolkit 스타일 ───────────────────────────────────
PT_STYLE = PTStyle.from_dict({
    "prompt": "#00d7af bold",
    "": "#e0e0e0",
})

BANNER = r"""[bold magenta]
   ____  _ _         _____          _
  / __ \| | |       / ____|        | |
 | |  | | | | __ _ | |     ___   __| | ___
 | |  | | | |/ _` || |    / _ \ / _` |/ _ \
 | |__| | | | (_| || |___| (_) | (_| |  __/
  \____/|_|_|\__,_| \_____\___/ \__,_|\___|
[/bold magenta]
[dim]  Ollama 기반 경량 코딩 어시스턴트 v{version}[/dim]
[dim]  모델: [cyan]{model}[/cyan]  |  /help 로 도움말 확인[/dim]
{memory_status}"""

HELP_TEXT = """\
[bold cyan]📖 사용법[/bold cyan]

  일반 메시지를 입력하면 코딩 어시스턴트가 답변합니다.

[bold cyan]📌 명령어[/bold cyan]

  [green]/help[/green]        이 도움말 표시
  [green]/clear[/green]       대화 히스토리 초기화
  [green]/model[/green]       현재 모델 정보 표시
  [green]/approve[/green]     자동 승인 모드 토글
  [green]/quit[/green]        프로그램 종료
  [green]Ctrl+C[/green]      현재 응답 중단 / 프로그램 종료

[bold cyan]📋 프로젝트 메모리[/bold cyan]

  프로젝트 루트에 [green]OLLACODE.md[/green] 파일을 생성하면
  자동으로 프로젝트 컨텍스트가 로드됩니다.
"""


async def cli_approval_callback(tool_name: str, description: str) -> bool:
    """CLI에서 도구 실행 승인을 요청합니다."""
    console.print()
    console.print(
        Panel(
            description,
            title=f"[bold yellow]🔐 승인 필요 — {tool_name}[/bold yellow]",
            border_style="yellow",
            padding=(0, 2),
        )
    )

    try:
        response = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: input("  승인하시겠습니까? (y/n/a=항상승인) ❯ ").strip().lower(),
        )
    except (EOFError, KeyboardInterrupt):
        return False

    if response in ("a", "always", "항상"):
        console.print("[success]  ✅ 이후 모든 작업을 자동 승인합니다.[/success]")
        return True  # caller should set auto_approve
    elif response in ("y", "yes", "ㅇ", "네"):
        console.print("[success]  ✅ 승인됨[/success]")
        return True
    else:
        console.print("[deny]  ❌ 거부됨[/deny]")
        return False


async def run_cli(config: Config, auto_approve: bool = False) -> None:
    """CLI 대화 루프를 실행합니다."""
    engine = ConversationEngine(config)
    engine.auto_approve = auto_approve

    if not auto_approve:
        # 승인 콜백 설정
        async def approval_wrapper(tool_name: str, desc: str) -> bool:
            result = await cli_approval_callback(tool_name, desc)
            # "항상승인" 응답 처리
            if result and "항상승인" not in desc:
                pass  # 일반 승인
            return result

        engine.set_approval_callback(approval_wrapper)

    # Ollama 서버 상태 확인
    console.print("\n[dim]🔌 Ollama 서버 연결 확인 중...[/dim]")
    if not await engine.client.check_health():
        console.print(
            "[error]❌ Ollama 서버에 연결할 수 없습니다![/error]\n"
            f"[dim]   서버 주소: {config.ollama_host}[/dim]\n"
            "[dim]   'ollama serve' 명령으로 서버를 시작해주세요.[/dim]"
        )
        await engine.close()
        return

    memory_status = ""
    if engine.has_project_memory:
        memory_status = "[dim]  📋 [green]OLLACODE.md 로드됨[/green][/dim]"
    else:
        memory_status = "[dim]  📋 OLLACODE.md 없음 (프로젝트 루트에 생성하세요)[/dim]"

    approve_status = "[green]ON[/green]" if auto_approve else "[yellow]OFF[/yellow]"
    memory_status += f"\n[dim]  🔐 자동승인: {approve_status}[/dim]"

    console.print(
        BANNER.format(
            version=__version__,
            model=config.ollama_model,
            memory_status=memory_status,
        )
    )

    session: PromptSession = PromptSession(
        history=InMemoryHistory(),
        style=PT_STYLE,
    )

    try:
        while True:
            try:
                user_input = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: session.prompt(
                        [("class:prompt", "ollacode ❯ ")],
                    ),
                )
            except EOFError:
                break
            except KeyboardInterrupt:
                console.print("\n[dim]Ctrl+C — /quit 으로 종료하세요[/dim]")
                continue

            user_input = user_input.strip()
            if not user_input:
                continue

            # 명령어 처리
            if user_input.startswith("/"):
                cmd = user_input.lower().split()[0]
                if cmd in ("/quit", "/exit", "/q"):
                    console.print("[dim]👋 안녕히 가세요![/dim]")
                    break
                elif cmd == "/clear":
                    engine.clear()
                    console.print("[success]✅ 대화 히스토리가 초기화되었습니다.[/success]")
                    continue
                elif cmd == "/help":
                    console.print(HELP_TEXT)
                    continue
                elif cmd == "/model":
                    console.print(
                        f"[info]모델: [cyan]{config.ollama_model}[/cyan]\n"
                        f"서버: [cyan]{config.ollama_host}[/cyan]\n"
                        f"대화 메시지 수: [cyan]{engine.message_count}[/cyan]\n"
                        f"프로젝트 메모리: [cyan]{'로드됨' if engine.has_project_memory else '없음'}[/cyan]\n"
                        f"자동 승인: [cyan]{engine.auto_approve}[/cyan][/info]"
                    )
                    continue
                elif cmd == "/approve":
                    engine.auto_approve = not engine.auto_approve
                    if engine.auto_approve:
                        engine.set_approval_callback(None)
                        console.print("[success]🔓 자동 승인 모드 ON — 모든 도구 실행이 자동 승인됩니다.[/success]")
                    else:
                        engine.set_approval_callback(approval_wrapper)
                        console.print("[warning]🔐 자동 승인 모드 OFF — 도구 실행 전 확인을 요청합니다.[/warning]")
                    continue
                else:
                    console.print(f"[warning]알 수 없는 명령어: {cmd}[/warning]")
                    continue

            # 스트리밍 응답 처리
            console.print()
            full_response = ""
            try:
                with Live(
                    Text("⏳ 생각하는 중...", style="dim"),
                    console=console,
                    refresh_per_second=8,
                    transient=True,
                ) as live:
                    async for token in engine.chat_stream(user_input):
                        full_response += token
                        # 실시간 마크다운 렌더링
                        live.update(
                            Markdown(full_response, code_theme="monokai")
                        )

            except KeyboardInterrupt:
                console.print("\n[warning]⚠️ 응답이 중단되었습니다.[/warning]")
                continue
            except Exception as e:
                console.print(f"\n[error]❌ 오류 발생: {e}[/error]")
                continue

            # 최종 응답을 패널로 렌더링
            console.print(
                Panel(
                    Markdown(full_response, code_theme="monokai"),
                    title="[bold magenta]ollacode[/bold magenta]",
                    border_style="magenta",
                    padding=(1, 2),
                )
            )

    finally:
        await engine.close()


def main() -> None:
    """진입점."""
    parser = argparse.ArgumentParser(
        prog="ollacode",
        description="Ollama 기반 경량 코딩 어시스턴트 (CLI + Telegram)",
    )
    subparsers = parser.add_subparsers(dest="mode", help="실행 모드")

    # CLI 모드 (기본)
    cli_parser = subparsers.add_parser("cli", help="CLI 대화 모드")
    cli_parser.add_argument(
        "--model", default=None, help="사용할 모델 (기본: qwen3-coder:30b)"
    )
    cli_parser.add_argument(
        "--auto-approve", action="store_true",
        help="모든 도구 실행을 자동 승인"
    )

    # Telegram 모드
    tg_parser = subparsers.add_parser("telegram", help="Telegram 봇 모드")
    tg_parser.add_argument(
        "--model", default=None, help="사용할 모델 (기본: qwen3-coder:30b)"
    )

    args = parser.parse_args()
    config = Config.load()

    # 모델 오버라이드
    if hasattr(args, "model") and args.model:
        config.ollama_model = args.model

    mode = args.mode or "cli"

    if mode == "cli":
        auto = getattr(args, "auto_approve", False)
        asyncio.run(run_cli(config, auto_approve=auto))
    elif mode == "telegram":
        from .telegram_bot import run_telegram_bot

        run_telegram_bot(config)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
