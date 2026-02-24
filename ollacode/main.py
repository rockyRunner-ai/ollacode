"""CLI interface — Rich terminal UI + approval system."""

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


# ─── Rich theme ──────────────────────────────────────────────
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

# ─── prompt_toolkit style ────────────────────────────────────
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
[dim]  Lightweight coding assistant v{version}[/dim]
[dim]  Model: [cyan]{model}[/cyan]  |  /help for commands[/dim]
{memory_status}"""

HELP_TEXT = """\
[bold cyan]📖 Usage[/bold cyan]

  Type a message to chat with the coding assistant.

[bold cyan]📌 Commands[/bold cyan]

  [green]/help[/green]        Show this help
  [green]/clear[/green]       Reset conversation history
  [green]/model[/green]       Show model info and token usage
  [green]/approve[/green]     Toggle auto-approve mode
  [green]/quit[/green]        Exit
  [green]Ctrl+C[/green]      Interrupt / exit

[bold cyan]📋 Project Memory[/bold cyan]

  Create [green]OLLACODE.md[/green] in your project root
  to automatically load project context.
"""


async def cli_approval_callback(tool_name: str, description: str) -> bool:
    """Request tool execution approval in CLI."""
    console.print()
    console.print(
        Panel(
            description,
            title=f"[bold yellow]🔐 Approval required — {tool_name}[/bold yellow]",
            border_style="yellow",
            padding=(0, 2),
        )
    )

    try:
        response = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: input("  Approve? (y/n/a=always) ❯ ").strip().lower(),
        )
    except (EOFError, KeyboardInterrupt):
        return False

    if response in ("a", "always"):
        console.print("[success]  ✅ Auto-approving all future actions.[/success]")
        return True
    elif response in ("y", "yes"):
        console.print("[success]  ✅ Approved[/success]")
        return True
    else:
        console.print("[deny]  ❌ Denied[/deny]")
        return False


async def run_cli(config: Config, auto_approve: bool = False) -> None:
    """Run the CLI conversation loop."""
    engine = ConversationEngine(config)
    engine.auto_approve = auto_approve

    if not auto_approve:
        async def approval_wrapper(tool_name: str, desc: str) -> bool:
            result = await cli_approval_callback(tool_name, desc)
            return result

        engine.set_approval_callback(approval_wrapper)

    # Check Ollama server
    console.print("\n[dim]🔌 Checking Ollama server...[/dim]")
    if not await engine.client.check_health():
        console.print(
            "[error]❌ Cannot connect to Ollama server![/error]\n"
            f"[dim]   Server: {config.ollama_host}[/dim]\n"
            "[dim]   Run 'ollama serve' to start the server.[/dim]"
        )
        await engine.close()
        return

    memory_status = ""
    if engine.has_project_memory:
        memory_status = "[dim]  📋 [green]OLLACODE.md loaded[/green][/dim]"
    else:
        memory_status = "[dim]  📋 OLLACODE.md not found (create in project root)[/dim]"

    approve_status = "[green]ON[/green]" if auto_approve else "[yellow]OFF[/yellow]"
    memory_status += f"\n[dim]  🔐 Auto-approve: {approve_status}[/dim]"
    memory_status += f"\n[dim]  📊 Max tokens: {config.max_context_tokens} | Compact: {'ON' if config.compact_mode else 'OFF'}[/dim]"

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
                console.print("\n[dim]Ctrl+C — use /quit to exit[/dim]")
                continue

            user_input = user_input.strip()
            if not user_input:
                continue

            # Command handling
            if user_input.startswith("/"):
                cmd = user_input.lower().split()[0]
                if cmd in ("/quit", "/exit", "/q"):
                    console.print("[dim]👋 Goodbye![/dim]")
                    break
                elif cmd == "/clear":
                    engine.clear()
                    console.print("[success]✅ Conversation history cleared.[/success]")
                    continue
                elif cmd == "/help":
                    console.print(HELP_TEXT)
                    continue
                elif cmd == "/model":
                    console.print(
                        f"[info]Model: [cyan]{config.ollama_model}[/cyan]\n"
                        f"Server: [cyan]{config.ollama_host}[/cyan]\n"
                        f"Messages: [cyan]{engine.message_count}[/cyan]\n"
                        f"Est. tokens: [cyan]{engine.estimated_tokens}[/cyan] / {config.max_context_tokens}\n"
                        f"Project memory: [cyan]{'loaded' if engine.has_project_memory else 'none'}[/cyan]\n"
                        f"Compact mode: [cyan]{config.compact_mode}[/cyan]\n"
                        f"Auto-approve: [cyan]{engine.auto_approve}[/cyan][/info]"
                    )
                    continue
                elif cmd == "/approve":
                    engine.auto_approve = not engine.auto_approve
                    if engine.auto_approve:
                        engine.set_approval_callback(None)
                        console.print("[success]🔓 Auto-approve ON — all tool actions auto-approved.[/success]")
                    else:
                        engine.set_approval_callback(approval_wrapper)
                        console.print("[warning]🔐 Auto-approve OFF — will ask before tool execution.[/warning]")
                    continue
                else:
                    console.print(f"[warning]Unknown command: {cmd}[/warning]")
                    continue

            # Streaming response
            console.print()
            full_response = ""
            try:
                with Live(
                    Text("⏳ Thinking...", style="dim"),
                    console=console,
                    refresh_per_second=8,
                    transient=True,
                ) as live:
                    async for token in engine.chat_stream(user_input):
                        full_response += token
                        live.update(
                            Markdown(full_response, code_theme="monokai")
                        )

            except KeyboardInterrupt:
                console.print("\n[warning]⚠️ Response interrupted.[/warning]")
                continue
            except Exception as e:
                console.print(f"\n[error]❌ Error: {e}[/error]")
                continue

            # Render final response in panel
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
    """Entry point."""
    parser = argparse.ArgumentParser(
        prog="ollacode",
        description="Lightweight CLI coding assistant powered by Ollama",
    )
    subparsers = parser.add_subparsers(dest="mode", help="Run mode")

    # CLI mode (default)
    cli_parser = subparsers.add_parser("cli", help="CLI chat mode")
    cli_parser.add_argument(
        "--model", default=None, help="Model to use (default: qwen3-coder:30b)"
    )
    cli_parser.add_argument(
        "--auto-approve", action="store_true",
        help="Auto-approve all tool executions"
    )

    # Telegram mode
    tg_parser = subparsers.add_parser("telegram", help="Telegram bot mode")
    tg_parser.add_argument(
        "--model", default=None, help="Model to use (default: qwen3-coder:30b)"
    )

    args = parser.parse_args()
    config = Config.load()

    # Model override
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
