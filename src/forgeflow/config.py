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
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from forgeflow.models import DEFAULT_WEIGHTS, SCORE_FACTORS, ModelPricing

KILL_SWITCH_ENV = "FORGEFLOW_KILL_SWITCH"


class ConfigError(ValueError):
    """Raised when the configuration file is missing or invalid."""


class LLMConfig(BaseModel):
    """Model selection, pricing, and spending limits.

    Prices live in configuration because vendor pricing changes; a hard-coded
    constant would quietly make every cost figure wrong.
    """

    model_config = ConfigDict(extra="forbid")

    triage_model: str = "gemini-2.5-flash"
    analysis_model: str = "gemini-2.5-pro"
    pricing: dict[str, ModelPricing] = Field(default_factory=dict)
    monthly_cap_usd: float = Field(default=5.0, ge=0.0)
    max_retries: int = Field(default=2, ge=0, le=5)
    timeout_seconds: float = Field(default=60.0, gt=0.0)
    max_output_tokens: int = Field(default=4096, gt=0)

    def pricing_for(self, model: str) -> ModelPricing:
        """Return the configured price for a model.

        An unconfigured model costs 0.0 here but is flagged by
        :meth:`unknown_models`, so the operator can see the gap rather than
        silently believing spend is tracked.
        """
        return self.pricing.get(model, ModelPricing(input_per_million=0.0, output_per_million=0.0))

    def unknown_models(self) -> list[str]:
        """Models in use that have no configured price."""
        return [m for m in (self.triage_model, self.analysis_model) if m not in self.pricing]


class AppConfig(BaseModel):
    """Validated contents of ``config/repositories.yaml``."""

    model_config = ConfigDict(extra="forbid")

    repositories: list[str]
    weights: dict[str, float] = Field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    skill_keywords: dict[str, list[str]] = Field(default_factory=dict)
    exclude_labels: list[str] = Field(default_factory=list)
    opt_out_labels: list[str] = Field(default_factory=list)
    preferred_labels: list[str] = Field(default_factory=list)
    llm: LLMConfig = Field(default_factory=lambda: LLMConfig())
    kill_switch: bool = False

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
    llm_api_key: SecretStr | None = Field(default=None)
    llm_provider: str = Field(default="gemini")
    # pydantic-settings would otherwise read the bare name KILL_SWITCH. The
    # documented, namespaced variable is accepted explicitly, and the bare name
    # is still honoured so an existing environment keeps working.
    kill_switch: bool = Field(
        default=False,
        validation_alias=AliasChoices(KILL_SWITCH_ENV, "KILL_SWITCH"),
    )


class KillSwitchEngagedError(RuntimeError):
    """Raised when the kill switch is engaged and an action was requested.

    SPEC section 9 requires a kill switch. It is deliberately a hard stop rather
    than a warning: the point of a kill switch is that flipping one flag halts
    spending and execution without anyone having to remember which code paths
    matter.
    """


def kill_switch_engaged(
    config: AppConfig | None = None,
    settings: Settings | None = None,
    conn: object | None = None,
) -> tuple[bool, str]:
    """Report whether the kill switch is engaged, and why.

    Three independent sources, so any one alone is enough to stop the system:

    * ``kill_switch: true`` in ``config/repositories.yaml``
    * ``FORGEFLOW_KILL_SWITCH`` set in the environment
    * the ``kill_switch`` row in SQLite, set by ``forgeflow kill-switch on``

    The database route exists because it can be flipped while the system is
    running and without touching a file that might be under review. The
    environment route works when the database is unreachable. Any engaged source
    stops the run; the first one found is reported.
    """
    if config is not None and config.kill_switch:
        return True, "kill_switch is enabled in the configuration file"

    if conn is not None:
        from forgeflow import killswitch

        state = killswitch.read_state(conn)  # type: ignore[arg-type]
        if state.engaged:
            who = f" by {state.changed_by}" if state.changed_by else ""
            why = f": {state.reason}" if state.reason else ""
            return True, f"kill switch engaged in the database{who}{why}"

    resolved = settings if settings is not None else Settings()
    if resolved.kill_switch:
        return True, f"{KILL_SWITCH_ENV} is set in the environment"

    return False, ""


def require_kill_switch_clear(
    config: AppConfig | None = None,
    settings: Settings | None = None,
    conn: object | None = None,
) -> None:
    """Raise if the kill switch is engaged. No-op otherwise."""
    engaged, reason = kill_switch_engaged(config, settings, conn)
    if engaged:
        raise KillSwitchEngagedError(
            f"kill switch engaged ({reason}); refusing to continue. {_remedy_for(reason)}"
        )


def _remedy_for(reason: str) -> str:
    """Name the one action that clears the source that is actually engaged.

    A generic remedy is worse than none: telling an operator to unset an
    environment variable that is not set sends them down the wrong path.
    """
    if "configuration file" in reason:
        return "Set kill_switch: false in the configuration file to resume."
    if "database" in reason:
        return "Run 'forgeflow kill-switch off' to resume."
    if KILL_SWITCH_ENV in reason:
        return f"Unset {KILL_SWITCH_ENV} to resume."
    return "Clear the engaged kill-switch source to resume."


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
