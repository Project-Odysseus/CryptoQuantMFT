"""The strict config loader (quantlab.config)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import pytest

from quantlab.config import ConfigError, config_hash, load, parse, to_dict


@dataclass(frozen=True)
class Speeds:
    fast: int = 20
    slow: int = 100

    def __post_init__(self) -> None:
        if not self.fast < self.slow:
            raise ValueError("fast must be below slow")


@dataclass(frozen=True)
class Study:
    name: str
    target_vol: float = 0.10
    execution: Literal["next_close", "next_open"] = "next_close"
    speeds: Speeds = field(default_factory=Speeds)
    pairs: tuple[Speeds, ...] = ()
    weights: dict[str, float] = field(default_factory=dict)
    note: str | None = None


def test_a_valid_config_parses_with_conversions_and_defaults() -> None:
    study = parse({"name": "h", "target_vol": 1, "speeds": {"fast": 10}, "pairs": [{"fast": 5, "slow": 10}], "weights": {"er": 1}}, Study)
    assert study.target_vol == 1.0 and isinstance(study.target_vol, float)
    assert study.speeds == Speeds(10, 100) and study.pairs == (Speeds(5, 10),) and study.weights == {"er": 1.0}
    assert study.note is None


def test_every_problem_is_reported_at_once_with_its_path() -> None:
    with pytest.raises(ConfigError) as error:
        parse({"targt_vol": 0.1, "execution": "at_open", "speeds": {"fast": "ten"}, "weights": {"er": "high"}}, Study)
    text = str(error.value)
    for expected in ("targt_vol: unknown key", "name: required", "execution: must be one of", "speeds.fast: expected int", "weights.er: expected float"):
        assert expected in text, text


def test_booleans_are_not_numbers_and_numbers_are_not_text() -> None:
    with pytest.raises(ConfigError, match="target_vol: expected float"):
        parse({"name": "h", "target_vol": True}, Study)
    with pytest.raises(ConfigError, match="name: expected text"):
        parse({"name": 3}, Study)


def test_a_dataclass_s_own_validation_is_reported() -> None:
    with pytest.raises(ConfigError, match="fast must be below slow"):
        parse({"name": "h", "speeds": {"fast": 100, "slow": 10}}, Study)


def test_load_reads_a_toml_section_and_names_the_file(tmp_path) -> None:
    path = tmp_path / "study.toml"
    path.write_text('[study]\nname = "h"\n[study.speeds]\nfast = 10\n')
    assert load(path, Study, section="study").speeds.fast == 10
    with pytest.raises(ConfigError, match="no \\[other\\] section"):
        load(path, Study, section="other")


def test_the_hash_is_stable_and_changes_with_any_value() -> None:
    first, again = parse({"name": "h", "weights": {"a": 1, "b": 2}}, Study), parse({"weights": {"b": 2, "a": 1}, "name": "h"}, Study)
    assert config_hash(first) == config_hash(again)
    assert config_hash(first) != config_hash(parse({"name": "h", "weights": {"a": 1, "b": 3}}, Study))
    assert to_dict(first)["speeds"] == {"fast": 20, "slow": 100}
