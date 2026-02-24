"""대화 엔진 — CLI와 Telegram이 공유하는 핵심 로직."""

from __future__ import annotations

from typing import AsyncGenerator, Callable, Awaitable, Optional

from .ollama_client import OllamaClient
from .config import Config
from .prompts import SYSTEM_PROMPT, load_project_memory
from .tools import ToolExecutor, parse_tool_calls


class ConversationEngine:
    """대화 세션을 관리하고 도구 호출을 처리합니다."""

    MAX_TOOL_ITERATIONS = 10  # 에이전틱 루프 최대 반복 횟수

    def __init__(self, config: Config) -> None:
        self.config = config
        self.client = OllamaClient(config)
        self.tools = ToolExecutor(config.workspace_dir)
        self.history: list[dict[str, str]] = []
        self.auto_approve = False  # 자동 승인 모드
        self._init_system_prompt()

    def _init_system_prompt(self) -> None:
        """시스템 프롬프트를 대화 히스토리에 추가합니다."""
        # 프로젝트 메모리 로드
        project_context = load_project_memory(str(self.config.workspace_dir))
        full_prompt = SYSTEM_PROMPT + project_context

        self.history = [
            {"role": "system", "content": full_prompt},
        ]

    def clear(self) -> None:
        """대화 히스토리를 초기화합니다."""
        self._init_system_prompt()

    async def close(self) -> None:
        """리소스를 정리합니다."""
        await self.client.close()

    def set_approval_callback(
        self, callback: Optional[Callable[[str, str], Awaitable[bool]]]
    ) -> None:
        """도구 실행 승인 콜백을 설정합니다."""
        self.tools.approval_callback = callback

    async def chat(self, user_message: str) -> str:
        """사용자 메시지를 처리하고 최종 응답을 반환합니다.

        고도화된 에이전틱 루프:
        - 도구 호출 감지 → 실행 → 결과 기반 후속 응답
        - 오류 발생 시 자동 수정 시도
        - 최대 MAX_TOOL_ITERATIONS회 반복
        """
        self.history.append({"role": "user", "content": user_message})

        response = ""
        for iteration in range(self.MAX_TOOL_ITERATIONS):
            response = await self.client.chat(self.history)
            self.history.append({"role": "assistant", "content": response})

            # 도구 호출 감지
            tool_calls = parse_tool_calls(response)
            if not tool_calls:
                return response

            # 도구 실행 및 결과 수집
            tool_results = []
            has_error = False
            for call in tool_calls:
                tool_name = call.pop("tool", "")
                result = await self.tools.execute(tool_name, call)
                tool_results.append(f"**[{tool_name} 결과]**\n{result}")
                if "❌" in result:
                    has_error = True

            # 도구 결과를 컨텍스트에 추가
            results_text = "\n\n---\n\n".join(tool_results)

            follow_up = "[도구 실행 결과]\n\n" + results_text
            if has_error:
                follow_up += (
                    "\n\n⚠️ 일부 도구에서 오류가 발생했습니다. "
                    "오류를 분석하고 수정을 시도해주세요."
                )
            else:
                follow_up += "\n\n위 결과를 바탕으로 사용자에게 답변해주세요."

            self.history.append({"role": "user", "content": follow_up})

        return response  # 최대 반복 후 마지막 응답 반환

    async def chat_stream(self, user_message: str) -> AsyncGenerator[str, None]:
        """스트리밍 응답을 생성합니다.

        고도화된 에이전틱 루프:
        - 첫 응답은 스트리밍, 도구 실행 후 후속도 스트리밍
        - 오류 시 자동 수정 시도
        """
        self.history.append({"role": "user", "content": user_message})

        for iteration in range(self.MAX_TOOL_ITERATIONS):
            # 스트리밍 응답
            full_response = ""
            async for token in self.client.chat_stream(self.history):
                full_response += token
                yield token

            self.history.append({"role": "assistant", "content": full_response})

            # 도구 호출 감지
            tool_calls = parse_tool_calls(full_response)
            if not tool_calls:
                return

            # 도구 실행
            tool_results = []
            has_error = False
            for call in tool_calls:
                tool_name = call.pop("tool", "")
                yield f"\n\n⚙️ *도구 실행 중: {tool_name}...*\n"
                result = await self.tools.execute(tool_name, call)
                tool_results.append(f"**[{tool_name} 결과]**\n{result}")
                if "❌" in result:
                    has_error = True

                # 짧은 결과만 사용자에게 표시
                if len(result) < 500:
                    yield f"\n{result}\n"
                else:
                    yield f"\n✅ {tool_name} 완료 (결과 길이: {len(result)}자)\n"

            # 후속 응답을 위한 컨텍스트
            results_text = "\n\n---\n\n".join(tool_results)
            follow_up = "[도구 실행 결과]\n\n" + results_text
            if has_error:
                follow_up += (
                    "\n\n⚠️ 일부 도구에서 오류가 발생했습니다. "
                    "오류를 분석하고 수정을 시도해주세요."
                )
            else:
                follow_up += "\n\n위 결과를 바탕으로 사용자에게 답변해주세요."

            self.history.append({"role": "user", "content": follow_up})
            yield "\n\n---\n\n"

    @property
    def message_count(self) -> int:
        """시스템 프롬프트를 제외한 메시지 수."""
        return len(self.history) - 1

    @property
    def has_project_memory(self) -> bool:
        """OLLACODE.md가 로드되었는지 확인합니다."""
        if self.history:
            return "프로젝트 컨텍스트" in self.history[0].get("content", "")
        return False
