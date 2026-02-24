"""Ollama API 비동기 클라이언트."""

from __future__ import annotations

import json
from typing import AsyncGenerator

import httpx

from .config import Config


class OllamaClient:
    """Ollama REST API 클라이언트."""

    def __init__(self, config: Config) -> None:
        self.base_url = config.ollama_host
        self.model = config.ollama_model
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(connect=10.0, read=300.0, write=10.0, pool=10.0),
        )

    async def close(self) -> None:
        """HTTP 클라이언트를 종료합니다."""
        await self._client.aclose()

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.7,
    ) -> str:
        """비스트리밍 채팅 요청을 보내고 전체 응답을 반환합니다."""
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": temperature,
            },
        }

        resp = await self._client.post("/api/chat", json=payload)
        resp.raise_for_status()
        data = resp.json()
        return data.get("message", {}).get("content", "")

    async def chat_stream(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.7,
    ) -> AsyncGenerator[str, None]:
        """스트리밍 채팅 요청을 보내고 토큰을 하나씩 yield합니다."""
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "options": {
                "temperature": temperature,
            },
        }

        async with self._client.stream("POST", "/api/chat", json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue

                content = chunk.get("message", {}).get("content", "")
                if content:
                    yield content

                if chunk.get("done", False):
                    break

    async def check_health(self) -> bool:
        """Ollama 서버 상태를 확인합니다."""
        try:
            resp = await self._client.get("/")
            return resp.status_code == 200
        except httpx.RequestError:
            return False
