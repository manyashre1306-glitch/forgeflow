"""Tests for configuration loading and the shipped config file."""

from __future__ import annotations

import pytest

from forgeflow.config import AppConfig, ConfigError, load_config
from forgeflow.models import DEFAULT_WEIGHTS, SCORE_FACTORS


def write_config(tmp_path, text: str):
    path = tmp_path / "repos.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_loads_shipped_config() -> None:
    """The real config/repositories.yaml must load and validate."""
    config = load_config("config/repositories.yaml")
    assert len(config.repositories) >= 5
    assert sum(config.weights.values()) == pytest.approx(1.0)
    assert set(config.weights) == set(SCORE_FACTORS)


def test_shipped_config_has_opt_out_labels() -> None:
    """SPEC section 2B requires an opt-out path for repository contribution rules."""
    config = load_config("config/repositories.yaml")
    assert config.opt_out_labels
    assert "no-bot" in config.opt_out_labels


def test_shipped_config_has_exclude_labels() -> None:
    config = load_config("config/repositories.yaml")
    assert "duplicate" in config.exclude_labels
    assert "wontfix" in config.exclude_labels


def test_shipped_config_repositories_are_owner_name() -> None:
    config = load_config("config/repositories.yaml")
    for entry in config.repositories:
        assert entry.count("/") == 1
        owner, name = entry.split("/")
        assert owner and name


def test_missing_file_raises_clear_error(tmp_path) -> None:
    with pytest.raises(ConfigError, match="config file not found"):
        load_config(tmp_path / "missing.yaml")


def test_empty_file_raises_clear_error(tmp_path) -> None:
    path = write_config(tmp_path, "")
    with pytest.raises(ConfigError, match="empty"):
        load_config(path)


def test_invalid_yaml_raises_clear_error(tmp_path) -> None:
    path = write_config(tmp_path, "repositories: [unclosed")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(path)


def test_empty_allowlist_rejected(tmp_path) -> None:
    path = write_config(tmp_path, "repositories: []\n")
    with pytest.raises(ConfigError):
        load_config(path)


def test_duplicate_repository_rejected(tmp_path) -> None:
    path = write_config(tmp_path, "repositories:\n  - a/b\n  - a/b\n")
    with pytest.raises(ConfigError):
        load_config(path)


def test_bad_repository_format_rejected(tmp_path) -> None:
    path = write_config(tmp_path, "repositories:\n  - not-a-repo\n")
    with pytest.raises(ConfigError):
        load_config(path)


def test_weights_must_sum_to_one(tmp_path) -> None:
    weights = dict(DEFAULT_WEIGHTS)
    weights["skill_relevance"] = 0.90
    body = "repositories:\n  - a/b\nweights:\n"
    body += "".join(f"  {key}: {value}\n" for key, value in weights.items())
    path = write_config(tmp_path, body)
    with pytest.raises(ConfigError):
        load_config(path)


def test_weights_must_cover_every_factor(tmp_path) -> None:
    path = write_config(tmp_path, "repositories:\n  - a/b\nweights:\n  skill_relevance: 1.0\n")
    with pytest.raises(ConfigError):
        load_config(path)


def test_unknown_key_rejected(tmp_path) -> None:
    path = write_config(tmp_path, "repositories:\n  - a/b\nunexpected_key: true\n")
    with pytest.raises(ConfigError):
        load_config(path)


def test_defaults_when_weights_omitted() -> None:
    config = AppConfig(repositories=["a/b"])
    assert config.weights == DEFAULT_WEIGHTS
