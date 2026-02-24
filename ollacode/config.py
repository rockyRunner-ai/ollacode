"""Configuration management module."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


@dataclass
class Config:
    """Application configuration."""

    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "qwen3-coder:30b"
    telegram_bot_token: str = ""
    telegram_allowed_users: list[int] = field(default_factory=list)
    workspace_dir: Path = field(default_factory=lambda: Path.cwd())

    # Memory optimization settings
    max_context_tokens: int = 8192
    compact_mode: bool = True

    @classmethod
    def load(cls) -> Config:
        """Load configuration from environment variables and .env file."""
        # Load .env file if present
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

        max_tokens = int(os.getenv("MAX_CONTEXT_TOKENS", "8192"))
        compact = os.getenv("COMPACT_MODE", "true").lower() in ("true", "1", "yes")

        return cls(
            ollama_host=os.getenv("OLLAMA_HOST", "http://localhost:11434"),
            ollama_model=os.getenv("OLLAMA_MODEL", "qwen3-coder:30b"),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            telegram_allowed_users=allowed_users,
            workspace_dir=workspace_path,
            max_context_tokens=max_tokens,
            compact_mode=compact,
        )
