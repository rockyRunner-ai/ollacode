"""Conversation engine — core logic shared by CLI and Telegram."""

from __future__ import annotations

from typing import AsyncGenerator, Callable, Awaitable, Optional

from .ollama_client import OllamaClient
from .config import Config
from .prompts import SYSTEM_PROMPT, load_project_memory
from .tools import ToolExecutor, parse_tool_calls


def _estimate_tokens(text: str) -> int:
    """Rough token count estimation.

    Heuristic: ~4 chars per token for English, ~2 chars per token for CJK.
    This is intentionally simple — no tokenizer dependency needed.
    """
    cjk_count = sum(1 for c in text if '\u4e00' <= c <= '\u9fff' or
                    '\uac00' <= c <= '\ud7af' or
                    '\u3040' <= c <= '\u309f' or
                    '\u30a0' <= c <= '\u30ff')
    ascii_count = len(text) - cjk_count
    return int(ascii_count / 4 + cjk_count / 1.5)


def _estimate_history_tokens(history: list[dict[str, str]]) -> int:
    """Estimate total tokens in conversation history."""
    return sum(_estimate_tokens(msg.get("content", "")) for msg in history)


class ConversationEngine:
    """Manages conversation sessions and handles tool calls."""

    MAX_TOOL_ITERATIONS = 10  # Agentic loop max iterations

    # Tool result compression threshold (chars)
    RESULT_COMPACT_THRESHOLD = 800

    # Number of recent messages to always preserve during compaction
    PRESERVE_RECENT = 6

    def __init__(self, config: Config) -> None:
        self.config = config
        self.client = OllamaClient(config)
        self.tools = ToolExecutor(config.workspace_dir)
        self.history: list[dict[str, str]] = []
        self.auto_approve = False
        self._init_system_prompt()

    def _init_system_prompt(self) -> None:
        """Initialize conversation history with system prompt."""
        project_context = load_project_memory(str(self.config.workspace_dir))
        full_prompt = SYSTEM_PROMPT + project_context

        self.history = [
            {"role": "system", "content": full_prompt},
        ]

    def clear(self) -> None:
        """Reset conversation history."""
        self._init_system_prompt()

    async def close(self) -> None:
        """Clean up resources."""
        await self.client.close()

    def set_approval_callback(
        self, callback: Optional[Callable[[str, str], Awaitable[bool]]]
    ) -> None:
        """Set the tool execution approval callback."""
        self.tools.approval_callback = callback

    def _compact_tool_result(self, tool_name: str, result: str) -> str:
        """Compress a tool result if it exceeds threshold."""
        if not self.config.compact_mode:
            return result
        if len(result) <= self.RESULT_COMPACT_THRESHOLD:
            return result

        # Keep first and last portions
        preview = result[:300]
        suffix = result[-200:] if len(result) > 500 else ""
        truncated = (
            f"[{tool_name} result — {len(result)} chars, compressed]\n"
            f"{preview}\n... (truncated) ...\n{suffix}"
        )
        return truncated

    def _maybe_compact_history(self) -> None:
        """Compact conversation history if it exceeds token limit.

        Strategy:
        - Always keep: system prompt (index 0) + last PRESERVE_RECENT messages
        - Middle messages: replace with a single summary message
        - Tool results in old messages are aggressively compressed
        """
        if not self.config.compact_mode:
            return

        total_tokens = _estimate_history_tokens(self.history)
        threshold = int(self.config.max_context_tokens * 0.8)  # trigger at 80%

        if total_tokens <= threshold:
            return

        # Need to compact. Keep system prompt + recent messages.
        if len(self.history) <= self.PRESERVE_RECENT + 1:
            # Not enough messages to compact
            return

        # Messages to summarize: everything between system prompt and recent
        system_msg = self.history[0]
        old_messages = self.history[1:-self.PRESERVE_RECENT]
        recent_messages = self.history[-self.PRESERVE_RECENT:]

        # Build a compact summary of old messages
        summary_parts = []
        for msg in old_messages:
            role = msg["role"]
            content = msg["content"]
            if role == "user" and content.startswith("[Tool execution results]"):
                # Tool results — just note which tools ran
                summary_parts.append("[tool results processed]")
            elif role == "assistant":
                # Keep first line of assistant responses
                first_line = content.split("\n")[0][:150]
                summary_parts.append(f"Assistant: {first_line}")
            elif role == "user":
                # Keep user messages but truncated
                summary_parts.append(f"User: {content[:100]}")

        summary_text = (
            "[Previous conversation summary]\n"
            + "\n".join(summary_parts[-10:])  # Keep last 10 items max
        )

        # Rebuild history
        self.history = [system_msg, {"role": "user", "content": summary_text}] + recent_messages

    async def chat(self, user_message: str) -> str:
        """Process user message and return final response.

        Enhanced agentic loop:
        - Detect tool calls → execute → follow-up based on results
        - Auto-retry on errors
        - Max MAX_TOOL_ITERATIONS iterations
        """
        self.history.append({"role": "user", "content": user_message})
        self._maybe_compact_history()

        response = ""
        for iteration in range(self.MAX_TOOL_ITERATIONS):
            response = await self.client.chat(self.history)
            self.history.append({"role": "assistant", "content": response})

            # Detect tool calls
            tool_calls = parse_tool_calls(response)
            if not tool_calls:
                return response

            # Execute tools and collect results
            tool_results = []
            has_error = False
            for call in tool_calls:
                tool_name = call.pop("tool", "")
                result = await self.tools.execute(tool_name, call)
                # Compress result for history
                compact_result = self._compact_tool_result(tool_name, result)
                tool_results.append(f"**[{tool_name} result]**\n{compact_result}")
                if "❌" in result:
                    has_error = True

            # Add tool results to context
            results_text = "\n\n---\n\n".join(tool_results)

            follow_up = "[Tool execution results]\n\n" + results_text
            if has_error:
                follow_up += (
                    "\n\n⚠️ Some tools returned errors. "
                    "Please analyze and attempt to fix."
                )
            else:
                follow_up += "\n\nPlease respond to the user based on the above results."

            self.history.append({"role": "user", "content": follow_up})

        return response  # Return last response after max iterations

    async def chat_stream(self, user_message: str) -> AsyncGenerator[str, None]:
        """Generate streaming response.

        Enhanced agentic loop:
        - First response streams, tool follow-ups also stream
        - Auto-retry on errors
        """
        self.history.append({"role": "user", "content": user_message})
        self._maybe_compact_history()

        for iteration in range(self.MAX_TOOL_ITERATIONS):
            # Streaming response
            full_response = ""
            async for token in self.client.chat_stream(self.history):
                full_response += token
                yield token

            self.history.append({"role": "assistant", "content": full_response})

            # Detect tool calls
            tool_calls = parse_tool_calls(full_response)
            if not tool_calls:
                return

            # Execute tools
            tool_results = []
            has_error = False
            for call in tool_calls:
                tool_name = call.pop("tool", "")
                yield f"\n\n⚙️ *Running: {tool_name}...*\n"
                result = await self.tools.execute(tool_name, call)
                compact_result = self._compact_tool_result(tool_name, result)
                tool_results.append(f"**[{tool_name} result]**\n{compact_result}")
                if "❌" in result:
                    has_error = True

                # Show short results to user
                if len(result) < 500:
                    yield f"\n{result}\n"
                else:
                    yield f"\n✅ {tool_name} done ({len(result)} chars)\n"

            # Context for follow-up response
            results_text = "\n\n---\n\n".join(tool_results)
            follow_up = "[Tool execution results]\n\n" + results_text
            if has_error:
                follow_up += (
                    "\n\n⚠️ Some tools returned errors. "
                    "Please analyze and attempt to fix."
                )
            else:
                follow_up += "\n\nPlease respond to the user based on the above results."

            self.history.append({"role": "user", "content": follow_up})
            yield "\n\n---\n\n"

    @property
    def message_count(self) -> int:
        """Message count excluding system prompt."""
        return len(self.history) - 1

    @property
    def has_project_memory(self) -> bool:
        """Check if OLLACODE.md was loaded."""
        if self.history:
            return "Project Context" in self.history[0].get("content", "")
        return False

    @property
    def estimated_tokens(self) -> int:
        """Estimated token count for current history."""
        return _estimate_history_tokens(self.history)
