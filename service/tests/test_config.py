"""Settings a start must refuse: a typo here reaches every customer's rows, or every key's shape."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from kernel_turns.config import Settings


@pytest.mark.parametrize("days", [0, -1])
def test_a_retention_under_a_day_refuses_to_start(days: int) -> None:
    # 0 would be a TTL that removes every turn and ledger row in the database on the next pass
    with pytest.raises(ValidationError):
        Settings(bonfire_id="b", retention_days=days)


def test_a_retention_of_zero_in_the_environment_refuses_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KT_RETENTION_DAYS", "0")
    with pytest.raises(ValidationError):
        Settings(bonfire_id="b")


def test_retention_is_unset_or_at_least_a_day() -> None:
    assert Settings(bonfire_id="b").retention_days is None
    assert Settings(bonfire_id="b", retention_days=1).retention_days == 1


@pytest.mark.parametrize("bonfire", ["a\x1fb", "\x1fa", "a\x1f", "a\x00b", "a\nb", "ab\n", "a\tb", "a\x7fb", "a\x85b"])
def test_a_bonfire_id_with_a_control_character_refuses_to_start(bonfire: str) -> None:
    # ("A", "B\x1fm") and ("A\x1fB", "m") would be one message key
    with pytest.raises(ValidationError):
        Settings(bonfire_id=bonfire)


@pytest.mark.parametrize("bonfire", ["hermes", "65f0c0ffee0000000000abcd", "bonfire-a", "b_1.x", "fogata-niño"])
def test_an_ordinary_bonfire_id_is_taken(bonfire: str) -> None:
    assert Settings(bonfire_id=bonfire).bonfire_id == bonfire
