"""The website's input validation and per-session limits.

These are the checks that stand between a visitor's typing and a network call, so
they are tested directly rather than through a page. No Streamlit and no network
are involved: ``web.limits`` and ``web.session`` are pure Python.
"""

from __future__ import annotations

import pytest
from web import limits
from web.limits import (
    InputError,
    SessionLimits,
    cap_text,
    validate_full_name,
    validate_issue_number,
)


class TestValidateFullName:
    def test_accepts_a_normal_reference(self) -> None:
        assert validate_full_name("pydantic/pydantic") == "pydantic/pydantic"

    def test_trims_surrounding_whitespace(self) -> None:
        assert validate_full_name("  owner/name  ") == "owner/name"

    def test_accepts_dots_dashes_and_underscores(self) -> None:
        assert validate_full_name("my-org/my_repo.py") == "my-org/my_repo.py"

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "   ",
            "noslash",
            "a/b/c",
            "owner/../etc",
            "owner name/repo",
            "owner/repo name",
            "-leading/repo",
            "owner/-leading",
            "owner/" + "x" * 101,
            "x" * 40 + "/repo",
        ],
    )
    def test_rejects_bad_references(self, value: str) -> None:
        with pytest.raises(InputError):
            validate_full_name(value)

    def test_error_message_does_not_echo_the_input(self) -> None:
        """A hostile string must not be reflected back into the page."""
        hostile = "<script>alert(1)</script>"
        with pytest.raises(InputError) as excinfo:
            validate_full_name(hostile)
        assert "<script>" not in str(excinfo.value)


class TestValidateIssueNumber:
    def test_accepts_positive_integers(self) -> None:
        assert validate_issue_number(1) == 1
        assert validate_issue_number("42") == 42

    @pytest.mark.parametrize("value", [0, -1, "abc", "", None, "1.5"])
    def test_rejects_non_positive_or_non_numeric(self, value: object) -> None:
        with pytest.raises(InputError):
            validate_issue_number(value)


class TestCapText:
    def test_short_text_is_returned_unchanged(self) -> None:
        text, cut = cap_text("hello")
        assert text == "hello"
        assert cut is False

    def test_long_text_is_cut_and_reports_it(self) -> None:
        text, cut = cap_text("a" * 100, limit=10)
        assert text == "a" * 10
        assert cut is True

    def test_cap_counts_bytes_not_characters(self) -> None:
        """A multi-byte string must not exceed the byte cap."""
        # Each 'e' with an accent is two bytes in UTF-8.
        text, cut = cap_text("\u00e9" * 100, limit=11)
        assert cut is True
        assert len(text.encode("utf-8")) <= 11

    def test_cutting_mid_character_does_not_raise(self) -> None:
        text, cut = cap_text("\u00e9" * 100, limit=3)
        assert cut is True
        assert isinstance(text, str)


class TestSessionLimits:
    def test_defaults_match_the_documented_caps(self) -> None:
        session = SessionLimits()
        assert session.max_model_calls == 8
        assert session.max_cost_usd == 0.25
        assert session.max_github_requests == 30

    def test_model_call_limit_is_enforced(self) -> None:
        session = SessionLimits(max_model_calls=1)
        session.check_model_call()
        session.record_model_call(cost_usd=0.0, label="m")
        with pytest.raises(InputError, match="model-call limit"):
            session.check_model_call()

    def test_cost_limit_is_enforced(self) -> None:
        session = SessionLimits(max_cost_usd=0.10)
        session.check_model_call()
        session.record_model_call(cost_usd=0.10, label="m")
        with pytest.raises(InputError, match="cost limit"):
            session.check_model_call()

    def test_a_refused_call_costs_nothing(self) -> None:
        """The cap is checked before spending, so a refusal adds no cost."""
        session = SessionLimits(max_model_calls=0)
        with pytest.raises(InputError):
            session.check_model_call()
        assert session.model_calls == 0
        assert session.cost_usd == 0.0

    def test_github_request_limit_is_enforced(self) -> None:
        session = SessionLimits(max_github_requests=2)
        session.record_github_request()
        session.record_github_request()
        with pytest.raises(InputError, match="GitHub request limit"):
            session.check_github_request()

    def test_tokens_and_cost_accumulate(self) -> None:
        session = SessionLimits()
        session.record_model_call(
            cost_usd=0.01, label="triage", prompt_tokens=100, completion_tokens=20
        )
        session.record_model_call(
            cost_usd=0.02, label="analysis", prompt_tokens=200, completion_tokens=40
        )
        assert session.model_calls == 2
        assert session.prompt_tokens == 300
        assert session.completion_tokens == 60
        assert session.cost_usd == pytest.approx(0.03)
        assert session.cost_breakdown == [("triage", 0.01), ("analysis", 0.02)]

    def test_negative_costs_are_clamped_to_zero(self) -> None:
        session = SessionLimits()
        session.record_model_call(cost_usd=-5.0, label="m", prompt_tokens=-1)
        assert session.cost_usd == 0.0
        assert session.prompt_tokens == 0

    def test_remaining_cost_is_never_negative(self) -> None:
        session = SessionLimits(max_cost_usd=0.05)
        session.record_model_call(cost_usd=0.10, label="m")
        assert session.remaining_cost_usd == 0.0


def test_module_constants_are_sane() -> None:
    assert limits.MAX_FILES_FETCHED >= 1
    assert limits.MAX_FILE_BYTES >= 1024
    assert limits.MAX_PASTED_TEXT_BYTES >= 1024
