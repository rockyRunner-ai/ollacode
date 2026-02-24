"""설정 관리 모듈."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


@dataclass
class Config:
    """애플리케이션 설정."""

    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "qwen3-coder:30b"
    telegram_bot_token: str = ""
    telegram_allowed_users: list[int] = field(default_factory=list)
    workspace_dir: Path = field(default_factory=lambda: Path.cwd())

    @classmethod
    def load(cls) -> Config:
        """환경변수와 .env 파일에서 설정을 로드합니다."""
        # .env 파일이 있으면 로드
        env_path = Path.cwd() / ".env"
        if env_path.exists():
            load_dotenv(env_path)
        
        allowed_users_str = os.getenv("TELEGRAM_ALLOWED_USERS", "")
        allowed_users: list[int] = []
        if allowed_users_str.strip():
            allowed_users = [
                int(uid.strip())
                for uid in allowed_users_str.split(",")
                if uid.strip().isdigit()
            ]

        workspace = os.getenv("WORKSPACE_DIR", ".")
        workspace_path = Path(workspace).resolve()

        return cls(
            ollama_host=os.getenv("OLLAMA_HOST", "http://localhost:11434"),
            ollama_model=os.getenv("OLLAMA_MODEL", "qwen3-coder:30b"),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            telegram_allowed_users=allowed_users,
            workspace_dir=workspace_path,
        )
