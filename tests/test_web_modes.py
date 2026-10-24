"""Mode resolution and the hosted/local capability gate.

The website's whole safety story rests on hosted mode being the default and on
the refusals being enforced in code rather than by a page choosing not to draw a
button. These tests exercise :mod:`web.modes` directly.
"""

from __future__ import annotations

import pytest
from web.modes import (
    MODE_ENV,
    HostedModeRefused,
    ModeInfo,
    WebMode,
    require_hosted_context_allowed,
    require_local,
    resolve_mode,
)


class TestResolveMode:
    def test_default_is_hosted(self) -> None:
        """An unconfigured deployment must be the stricter mode."""
        info = resolve_mode({})
        assert info.mode is WebMode.HOSTED
        assert info.is_hosted is True
        assert info.is_local is False
        assert "default" in info.source

    def test_explicit_hosted(self) -> None:
        info = resolve_mode({MODE_ENV: "hosted"})
        assert info.is_hosted is True
        assert MODE_ENV in info.source

    def test_explicit_local(self) -> None:
        info = resolve_mode({MODE_ENV: "local"})
        assert info.is_local is True

    @pytest.mark.parametrize("value", ["LOCAL", " local ", "Local"])
    def test_local_is_case_and_space_insensitive(self, value: str) -> None:
        assert resolve_mode({MODE_ENV: value}).is_local is True

    @pytest.mark.parametrize("value", ["", "  ", "production", "public", "LOCALHOST"])
    def test_unrecognised_value_degrades_to_hosted(self, value: str) -> None:
        """A misspelled value must never unlock the powerful mode."""
        info = resolve_mode({MODE_ENV: value})
        assert info.is_hosted is True

    def test_banner_names_the_mode(self) -> None:
        assert "HOSTED" in resolve_mode({}).banner
        assert "LOCAL" in resolve_mode({MODE_ENV: "local"}).banner


class TestRequireLocal:
    def test_local_mode_passes(self) -> None:
        require_local(ModeInfo(mode=WebMode.LOCAL, source="test"), "anything")

    def test_hosted_mode_refuses(self) -> None:
        with pytest.raises(HostedModeRefused, match="unavailable in hosted mode"):
            require_local(ModeInfo(mode=WebMode.HOSTED, source="test"), "reading the database")

    def test_refusal_names_the_capability(self) -> None:
        with pytest.raises(HostedModeRefused, match="publishing"):
            require_local(ModeInfo(mode=WebMode.HOSTED, source="test"), "publishing")

    def test_hosted_database_rule_is_its_own_refusal(self) -> None:
        with pytest.raises(HostedModeRefused, match="must not read the local database"):
            require_hosted_context_allowed(ModeInfo(mode=WebMode.HOSTED, source="test"))

    def test_hosted_database_rule_passes_locally(self) -> None:
        require_hosted_context_allowed(ModeInfo(mode=WebMode.LOCAL, source="test"))
