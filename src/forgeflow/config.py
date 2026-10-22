"""Configuration loading for ForgeFlow.

Two separate concerns:

* ``AppConfig`` reads ``config/repositories.yaml``: the repository allowlist,
  ranking weights, and skill keywords. Nothing secret belongs in that file.
* ``Settings`` reads the environment (and ``.env`` if present) for credentials.

The GitHub token is held as a ``SecretStr`` so it cannot be printed or logged
accidentally. Nothing in this module ever writes a token to disk or output.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from forgeflow.models import DEFAULT_WEIGHTS, SCORE_FACTORS


class ConfigError(ValueError):
    """Raised when the configuration file is missing or invalid."""


class AppConfig(BaseModel):
    """Validated contents of ``config/repositories.yaml``."""

    model_config = ConfigDict(extra="forbid")

    repositories: list[str]
    weights: dict[str, float] = Field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    skill_keywords: dict[str, list[str]] = Field(default_factory=dict)
    exclude_labels: list[str] = Field(default_factory=list)
    preferred_labels: list[str] = Field(default_factory=list)

    @field_validator("repositories")
    @classmethod
    def _check_repositories(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("repositories allowlist must not be empty")
        seen: set[str] = set()
        cleaned: list[str] = []
        for entry in value:
            parts = entry.split("/")
            if len(parts) != 2 or not all(parts):
                raise ValueError(f"repository must look like 'owner/name', got {entry!r}")
            if entry in seen:
                raise ValueError(f"duplicate repository in allowlist: {entry!r}")
            seen.add(entry)
            cleaned.append(entry)
        return cleaned

    @model_validator(mode="after")
    def _check_weights(self) -> AppConfig:
        if set(self.weights) != set(SCORE_FACTORS):
            missing = sorted(set(SCORE_FACTORS) - set(self.weights))
            extra = sorted(set(self.weights) - set(SCORE_FACTORS))
            raise ValueError(
                f"weights must define exactly {sorted(SCORE_FACTORS)}; "
                f"missing={missing} extra={extra}"
            )
        if any(value < 0.0 for value in self.weights.values()):
            raise ValueError("weights must not be negative")
        total = sum(self.weights.values())
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"weights must sum to 1.0, got {total}")
        return self


class Settings(BaseSettings):
    """Environment-backed settings.

    Reads ``GITHUB_TOKEN`` from the environment, falling back to a ``.env``
    file in the working directory.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    github_token: SecretStr | None = Field(default=None)


def load_config(path: str | Path) -> AppConfig:
    """Load and validate the YAML configuration.

    Raises ``ConfigError`` with a clear message for a missing file, unreadable
    YAML, or invalid contents.
    """
    config_path = Path(path)
    if not config_path.is_file():
        raise ConfigError(f"config file not found: {config_path}")
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {config_path}: {exc}") from exc
    if raw is None:
        raise ConfigError(f"config file is empty: {config_path}")
    if not isinstance(raw, dict):
        raise ConfigError(f"config root must be a mapping, got {type(raw).__name__}")
    try:
        return AppConfig(**raw)
    except ValidationError as exc:
        raise ConfigError(f"invalid config in {config_path}: {exc}") from exc


def load_settings() -> Settings:
    """Load environment settings. Never logs or prints the token."""
    return Settings()
