"""코딩 도구 모듈 — 파일 조작, 검색, 명령 실행, Diff 편집."""

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
    """도구 실행 중 발생한 오류."""


# ─── 승인이 필요한 도구 목록 ──────────────────────────────────
TOOLS_REQUIRING_APPROVAL = {"write_file", "edit_file", "run_command"}


class ToolExecutor:
    """코딩 도구 실행기. workspace_dir 내에서만 동작합니다."""

    def __init__(self, workspace_dir: Path) -> None:
        self.workspace_dir = workspace_dir.resolve()
        # 승인 콜백: (tool_name, description) -> bool
        # None이면 자동 승인 (auto-approve 모드)
        self.approval_callback: Optional[
            Callable[[str, str], Awaitable[bool]]
        ] = None

    def _resolve_path(self, path_str: str) -> Path:
        """경로를 workspace 기준으로 해석하고, 탈출을 방지합니다."""
        p = Path(path_str)
        if not p.is_absolute():
            p = self.workspace_dir / p
        p = p.resolve()

        # 보안: workspace 외부 접근 차단
        if not str(p).startswith(str(self.workspace_dir)):
            raise ToolError(
                f"⛔ 보안 오류: workspace 외부 경로에 접근할 수 없습니다.\n"
                f"  요청 경로: {path_str}\n"
                f"  workspace: {self.workspace_dir}"
            )
        return p

    async def _request_approval(self, tool_name: str, description: str) -> bool:
        """도구 실행 전 사용자 승인을 요청합니다."""
        if self.approval_callback is None:
            return True  # auto-approve 모드
        return await self.approval_callback(tool_name, description)

    async def execute(self, tool_name: str, params: dict) -> str:
        """도구를 실행하고 결과를 반환합니다."""
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
            return f"❌ 알 수 없는 도구: {tool_name}"

        try:
            return await handler(params)
        except ToolError as e:
            return str(e)
        except Exception as e:
            return f"❌ 도구 실행 오류 ({tool_name}): {e}"

    async def _read_file(self, params: dict) -> str:
        path = self._resolve_path(params.get("path", ""))
        if not path.exists():
            return f"❌ 파일을 찾을 수 없습니다: {path}"
        if not path.is_file():
            return f"❌ 파일이 아닙니다: {path}"

        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return f"❌ 바이너리 파일은 읽을 수 없습니다: {path}"

        lines = content.split("\n")
        line_count = len(lines)
        # 줄 번호 추가
        numbered = "\n".join(
            f"{i+1:4d} | {line}" for i, line in enumerate(lines[:500])
        )
        if line_count > 500:
            return (
                f"📄 **{path.name}** ({line_count}줄, 처음 500줄 표시)\n"
                f"```\n{numbered}\n```\n... ({line_count - 500}줄 더 있음)"
            )
        return f"📄 **{path.name}** ({line_count}줄)\n```\n{numbered}\n```"

    async def _write_file(self, params: dict) -> str:
        path = self._resolve_path(params.get("path", ""))
        content = params.get("content", "")

        # 승인 요청
        existed = path.exists()
        action = "수정" if existed else "생성"
        line_count = len(content.split("\n"))
        description = f"📝 파일 {action}: {path.name} ({line_count}줄)"

        if existed:
            # diff 생성
            old_content = path.read_text(encoding="utf-8")
            diff = _generate_diff(old_content, content, path.name)
            description += f"\n{diff}"

        if not await self._request_approval("write_file", description):
            return "⏭️ 사용자가 파일 쓰기를 거부했습니다."

        # 부모 디렉토리 생성
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"✅ 파일 {action} 완료: {path.name} ({line_count}줄)"

    async def _edit_file(self, params: dict) -> str:
        """Diff 기반 파일 편집 — search/replace 블록으로 부분 수정."""
        path = self._resolve_path(params.get("path", ""))
        if not path.exists():
            return f"❌ 파일을 찾을 수 없습니다: {path}"
        if not path.is_file():
            return f"❌ 파일이 아닙니다: {path}"

        search = params.get("search", "")
        replace = params.get("replace", "")

        if not search:
            return "❌ 'search' 파라미터가 필요합니다."

        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return f"❌ 바이너리 파일은 편집할 수 없습니다: {path}"

        # search 문자열 찾기
        count = content.count(search)
        if count == 0:
            # 유사한 부분 찾기 시도
            close = difflib.get_close_matches(
                search.split("\n")[0],
                content.split("\n"),
                n=3,
                cutoff=0.6,
            )
            hint = ""
            if close:
                hint = "\n유사한 줄:\n" + "\n".join(f"  → {c}" for c in close)
            return f"❌ 검색 문자열을 찾을 수 없습니다.{hint}"

        if count > 1:
            return f"⚠️ 검색 문자열이 {count}번 발견되었습니다. 더 구체적으로 지정해주세요."

        # diff 미리보기 생성
        new_content = content.replace(search, replace, 1)
        diff = _generate_diff(content, new_content, path.name)
        description = f"✏️ 파일 편집: {path.name}\n{diff}"

        if not await self._request_approval("edit_file", description):
            return "⏭️ 사용자가 편집을 거부했습니다."

        # 적용
        path.write_text(new_content, encoding="utf-8")
        return f"✅ 파일 편집 완료: {path.name} (1개 변경)"

    async def _list_directory(self, params: dict) -> str:
        path = self._resolve_path(params.get("path", "."))
        if not path.exists():
            return f"❌ 디렉토리를 찾을 수 없습니다: {path}"
        if not path.is_dir():
            return f"❌ 디렉토리가 아닙니다: {path}"

        entries = sorted(path.iterdir(), key=lambda x: (not x.is_dir(), x.name))
        lines = []
        for entry in entries[:100]:  # 최대 100개
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
        header = f"📂 **{path.name or '/'}** ({total}개 항목)"
        return header + "\n" + "\n".join(lines)

    async def _search_files(self, params: dict) -> str:
        pattern = params.get("pattern", "*")
        base = self._resolve_path(params.get("path", "."))

        if not base.exists():
            return f"❌ 경로를 찾을 수 없습니다: {base}"

        matches = sorted(glob.glob(str(base / "**" / pattern), recursive=True))
        # workspace 내부만 필터
        matches = [
            m for m in matches
            if str(Path(m).resolve()).startswith(str(self.workspace_dir))
        ]

        if not matches:
            return f"🔍 '{pattern}' 패턴에 일치하는 파일이 없습니다."

        lines = []
        for m in matches[:50]:
            rel = os.path.relpath(m, self.workspace_dir)
            lines.append(f"  📄 {rel}")

        result = f"🔍 '{pattern}' 검색 결과 ({len(matches)}개)"
        if len(matches) > 50:
            result += f" — 처음 50개만 표시"
        return result + "\n" + "\n".join(lines)

    async def _grep_search(self, params: dict) -> str:
        """파일 내용에서 텍스트 검색 (grep 대체)."""
        query = params.get("query", "")
        base = self._resolve_path(params.get("path", "."))

        if not query:
            return "❌ 'query' 파라미터가 필요합니다."
        if not base.exists():
            return f"❌ 경로를 찾을 수 없습니다: {base}"

        results = []
        search_files = []

        if base.is_file():
            search_files = [base]
        else:
            # 재귀적으로 파일 검색 (바이너리/숨김 제외)
            for root, dirs, files in os.walk(str(base)):
                # 숨김/무시 디렉토리 건너뛰기
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

        for fp in search_files[:500]:  # 최대 500파일 검색
            try:
                content = fp.read_text(encoding="utf-8")
            except (UnicodeDecodeError, PermissionError):
                continue

            for i, line in enumerate(content.split("\n"), 1):
                if query.lower() in line.lower():
                    rel = os.path.relpath(str(fp), str(self.workspace_dir))
                    results.append(f"  {rel}:{i}: {line.strip()[:120]}")
                    if len(results) >= 50:
                        break
            if len(results) >= 50:
                break

        if not results:
            return f"🔍 '{query}'를 찾을 수 없습니다."

        header = f"🔍 '{query}' 검색 결과 ({len(results)}건)"
        return header + "\n" + "\n".join(results)

    async def _run_command(self, params: dict) -> str:
        command = params.get("command", "")
        if not command:
            return "❌ 실행할 명령어가 없습니다."

        # 위험한 명령어 차단
        dangerous = ["rm -rf /", "mkfs", "dd if=", ":(){", "fork bomb"]
        for d in dangerous:
            if d in command.lower():
                return f"⛔ 위험한 명령어가 감지되었습니다: {command}"

        # 승인 요청
        description = f"⚙️ 명령 실행: `{command}`"
        if not await self._request_approval("run_command", description):
            return "⏭️ 사용자가 명령 실행을 거부했습니다."

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.workspace_dir),
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60.0)
        except asyncio.TimeoutError:
            return f"⏰ 명령 실행 시간 초과 (60초): {command}"
        except Exception as e:
            return f"❌ 명령 실행 실패: {e}"

        result_parts = [f"⚙️ `{command}` (exit code: {proc.returncode})"]

        stdout_text = stdout.decode("utf-8", errors="replace").strip()
        stderr_text = stderr.decode("utf-8", errors="replace").strip()

        if stdout_text:
            if len(stdout_text) > 3000:
                stdout_text = stdout_text[:3000] + "\n... (출력 생략)"
            result_parts.append(f"```\n{stdout_text}\n```")

        if stderr_text:
            if len(stderr_text) > 1500:
                stderr_text = stderr_text[:1500] + "\n... (stderr 생략)"
            result_parts.append(f"**stderr:**\n```\n{stderr_text}\n```")

        return "\n".join(result_parts)


# ─── 유틸리티 함수 ────────────────────────────────────────────

def _generate_diff(old: str, new: str, filename: str = "") -> str:
    """두 텍스트의 unified diff를 생성합니다."""
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
        return "(변경사항 없음)"
    if len(diff_str) > 2000:
        diff_str = diff_str[:2000] + "\n... (diff 생략)"
    return f"```diff\n{diff_str}\n```"


def parse_tool_calls(text: str) -> list[dict]:
    """LLM 응답에서 도구 호출 블록을 파싱합니다.

    ```tool
    {"tool": "read_file", "path": "some/file.py"}
    ```
    형식의 블록을 찾아 리스트로 반환합니다.
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
