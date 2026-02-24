"""Coding tools module — file operations, search, command execution, diff editing."""

from __future__ import annotations

import asyncio
import difflib
import glob
import json
import os
import re
from pathlib import Path
from typing import Callable, Awaitable, Optional


class ToolError(Exception):
    """Error during tool execution."""


# ─── Tools that require user approval ─────────────────────────
TOOLS_REQUIRING_APPROVAL = {"write_file", "edit_file", "run_command"}


class ToolExecutor:
    """Coding tool executor. Operates only within workspace_dir."""

    def __init__(self, workspace_dir: Path) -> None:
        self.workspace_dir = workspace_dir.resolve()
        # Approval callback: (tool_name, description) -> bool
        # None means auto-approve mode
        self.approval_callback: Optional[
            Callable[[str, str], Awaitable[bool]]
        ] = None

    def _resolve_path(self, path_str: str) -> Path:
        """Resolve path relative to workspace and prevent escape."""
        p = Path(path_str)
        if not p.is_absolute():
            p = self.workspace_dir / p
        p = p.resolve()

        # Security: block access outside workspace
        if not str(p).startswith(str(self.workspace_dir)):
            raise ToolError(
                f"⛔ Security error: cannot access path outside workspace.\n"
                f"  Requested: {path_str}\n"
                f"  Workspace: {self.workspace_dir}"
            )
        return p

    async def _request_approval(self, tool_name: str, description: str) -> bool:
        """Request user approval before tool execution."""
        if self.approval_callback is None:
            return True  # auto-approve mode
        return await self.approval_callback(tool_name, description)

    async def execute(self, tool_name: str, params: dict) -> str:
        """Execute a tool and return the result."""
        handlers = {
            "read_file": self._read_file,
            "write_file": self._write_file,
            "edit_file": self._edit_file,
            "list_directory": self._list_directory,
            "search_files": self._search_files,
            "grep_search": self._grep_search,
            "run_command": self._run_command,
        }

        handler = handlers.get(tool_name)
        if handler is None:
            return f"❌ Unknown tool: {tool_name}"

        try:
            return await handler(params)
        except ToolError as e:
            return str(e)
        except Exception as e:
            return f"❌ Tool error ({tool_name}): {e}"

    async def _read_file(self, params: dict) -> str:
        path = self._resolve_path(params.get("path", ""))
        if not path.exists():
            return f"❌ File not found: {path}"
        if not path.is_file():
            return f"❌ Not a file: {path}"

        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return f"❌ Cannot read binary file: {path}"

        lines = content.split("\n")
        line_count = len(lines)

        # Support start_line / end_line params
        start = max(1, int(params.get("start_line", 1))) - 1
        end = min(line_count, int(params.get("end_line", 200)))
        display_lines = lines[start:end]

        # Add line numbers
        numbered = "\n".join(
            f"{start+i+1:4d} | {line}" for i, line in enumerate(display_lines)
        )

        if line_count > end:
            return (
                f"📄 **{path.name}** ({line_count} lines, showing L{start+1}-{end})\n"
                f"```\n{numbered}\n```\n... ({line_count - end} more lines)"
            )
        return f"📄 **{path.name}** ({line_count} lines)\n```\n{numbered}\n```"

    async def _write_file(self, params: dict) -> str:
        path = self._resolve_path(params.get("path", ""))
        content = params.get("content", "")

        # Request approval
        existed = path.exists()
        action = "modify" if existed else "create"
        line_count = len(content.split("\n"))
        description = f"📝 File {action}: {path.name} ({line_count} lines)"

        if existed:
            old_content = path.read_text(encoding="utf-8")
            diff = _generate_diff(old_content, content, path.name)
            description += f"\n{diff}"

        if not await self._request_approval("write_file", description):
            return "⏭️ User rejected file write."

        # Create parent directories
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"✅ File {action} done: {path.name} ({line_count} lines)"

    async def _edit_file(self, params: dict) -> str:
        """Diff-based file editing — partial modification via search/replace."""
        path = self._resolve_path(params.get("path", ""))
        if not path.exists():
            return f"❌ File not found: {path}"
        if not path.is_file():
            return f"❌ Not a file: {path}"

        search = params.get("search", "")
        replace = params.get("replace", "")

        if not search:
            return "❌ 'search' parameter is required."

        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return f"❌ Cannot edit binary file: {path}"

        # Find search string
        count = content.count(search)
        if count == 0:
            # Try to find similar lines
            close = difflib.get_close_matches(
                search.split("\n")[0],
                content.split("\n"),
                n=3,
                cutoff=0.6,
            )
            hint = ""
            if close:
                hint = "\nSimilar lines:\n" + "\n".join(f"  → {c}" for c in close)
            return f"❌ Search string not found.{hint}"

        if count > 1:
            return f"⚠️ Search string found {count} times. Please be more specific."

        # Generate diff preview
        new_content = content.replace(search, replace, 1)
        diff = _generate_diff(content, new_content, path.name)
        description = f"✏️ Edit file: {path.name}\n{diff}"

        if not await self._request_approval("edit_file", description):
            return "⏭️ User rejected edit."

        # Apply
        path.write_text(new_content, encoding="utf-8")
        return f"✅ File edited: {path.name} (1 change applied)"

    async def _list_directory(self, params: dict) -> str:
        path = self._resolve_path(params.get("path", "."))
        if not path.exists():
            return f"❌ Directory not found: {path}"
        if not path.is_dir():
            return f"❌ Not a directory: {path}"

        entries = sorted(path.iterdir(), key=lambda x: (not x.is_dir(), x.name))
        lines = []
        for entry in entries[:100]:
            if entry.name.startswith("."):
                continue
            icon = "📁" if entry.is_dir() else "📄"
            size = ""
            if entry.is_file():
                size_bytes = entry.stat().st_size
                if size_bytes < 1024:
                    size = f" ({size_bytes}B)"
                elif size_bytes < 1024 * 1024:
                    size = f" ({size_bytes / 1024:.1f}KB)"
                else:
                    size = f" ({size_bytes / (1024 * 1024):.1f}MB)"
            lines.append(f"  {icon} {entry.name}{size}")

        total = len(list(path.iterdir()))
        header = f"📂 **{path.name or '/'}** ({total} items)"
        return header + "\n" + "\n".join(lines)

    async def _search_files(self, params: dict) -> str:
        pattern = params.get("pattern", "*")
        base = self._resolve_path(params.get("path", "."))

        if not base.exists():
            return f"❌ Path not found: {base}"

        matches = sorted(glob.glob(str(base / "**" / pattern), recursive=True))
        # Filter to workspace only
        matches = [
            m for m in matches
            if str(Path(m).resolve()).startswith(str(self.workspace_dir))
        ]

        if not matches:
            return f"🔍 No files matching '{pattern}'."

        lines = []
        for m in matches[:50]:
            rel = os.path.relpath(m, self.workspace_dir)
            lines.append(f"  📄 {rel}")

        result = f"🔍 '{pattern}' results ({len(matches)} files)"
        if len(matches) > 50:
            result += " — showing first 50"
        return result + "\n" + "\n".join(lines)

    async def _grep_search(self, params: dict) -> str:
        """Search text inside files (grep alternative)."""
        query = params.get("query", "")
        base = self._resolve_path(params.get("path", "."))

        if not query:
            return "❌ 'query' parameter is required."
        if not base.exists():
            return f"❌ Path not found: {base}"

        results = []
        search_files = []

        if base.is_file():
            search_files = [base]
        else:
            # Recursive file search (skip binary/hidden)
            for root, dirs, files in os.walk(str(base)):
                dirs[:] = [
                    d for d in dirs
                    if not d.startswith(".")
                    and d not in {"node_modules", "__pycache__", ".git", "venv", ".venv"}
                ]
                for f in files:
                    if f.startswith("."):
                        continue
                    fp = Path(root) / f
                    if str(fp.resolve()).startswith(str(self.workspace_dir)):
                        search_files.append(fp)

        for fp in search_files[:500]:
            try:
                content = fp.read_text(encoding="utf-8")
            except (UnicodeDecodeError, PermissionError):
                continue

            for i, line in enumerate(content.split("\n"), 1):
                if query.lower() in line.lower():
                    rel = os.path.relpath(str(fp), str(self.workspace_dir))
                    results.append(f"  {rel}:{i}: {line.strip()[:120]}")
                    if len(results) >= 20:
                        break
            if len(results) >= 20:
                break

        if not results:
            return f"🔍 '{query}' not found."

        header = f"🔍 '{query}' results ({len(results)} matches)"
        return header + "\n" + "\n".join(results)

    async def _run_command(self, params: dict) -> str:
        command = params.get("command", "")
        if not command:
            return "❌ No command provided."

        # Block dangerous commands
        dangerous = ["rm -rf /", "mkfs", "dd if=", ":(){ ", "fork bomb"]
        for d in dangerous:
            if d in command.lower():
                return f"⛔ Dangerous command detected: {command}"

        # Request approval
        description = f"⚙️ Run command: `{command}`"
        if not await self._request_approval("run_command", description):
            return "⏭️ User rejected command execution."

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.workspace_dir),
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60.0)
        except asyncio.TimeoutError:
            return f"⏰ Command timed out (60s): {command}"
        except Exception as e:
            return f"❌ Command failed: {e}"

        result_parts = [f"⚙️ `{command}` (exit code: {proc.returncode})"]

        stdout_text = stdout.decode("utf-8", errors="replace").strip()
        stderr_text = stderr.decode("utf-8", errors="replace").strip()

        if stdout_text:
            if len(stdout_text) > 1500:
                stdout_text = stdout_text[:1500] + "\n... (output truncated)"
            result_parts.append(f"```\n{stdout_text}\n```")

        if stderr_text:
            if len(stderr_text) > 800:
                stderr_text = stderr_text[:800] + "\n... (stderr truncated)"
            result_parts.append(f"**stderr:**\n```\n{stderr_text}\n```")

        return "\n".join(result_parts)


# ─── Utility functions ────────────────────────────────────────

def _generate_diff(old: str, new: str, filename: str = "") -> str:
    """Generate a unified diff between two texts."""
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    diff = difflib.unified_diff(
        old_lines, new_lines,
        fromfile=f"a/{filename}",
        tofile=f"b/{filename}",
        n=3,
    )
    diff_str = "".join(diff)
    if not diff_str:
        return "(no changes)"
    if len(diff_str) > 1000:
        diff_str = diff_str[:1000] + "\n... (diff truncated)"
    return f"```diff\n{diff_str}\n```"


def parse_tool_calls(text: str) -> list[dict]:
    """Parse tool call blocks from LLM response.

    ```tool
    {"tool": "read_file", "path": "some/file.py"}
    ```
    """
    tool_blocks = re.findall(r"```tool\s*\n(.+?)\n```", text, re.DOTALL)
    calls = []
    for block in tool_blocks:
        try:
            data = json.loads(block.strip())
            if isinstance(data, dict) and "tool" in data:
                calls.append(data)
        except json.JSONDecodeError:
            continue
    return calls
