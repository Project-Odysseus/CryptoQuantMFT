"""Strict, typed configuration: a TOML file or a mapping parsed into frozen dataclasses (plan 0.3).

A strategy, study or dataset is fully defined by its config plus its component code, so a config must never be
silently wrong. This parser therefore:

- rejects unknown keys (a typo cannot fall back to a default without notice);
- reports missing required fields;
- checks every value against the field's type annotation, converting only the safe cases (an int where a float is
  declared; a list where a tuple is declared);
- parses nested dataclasses, lists/tuples of them, dicts, optionals, unions and `Literal` choices;
- collects every problem and raises them together, each with its path ("regime.weights.er: expected float, got str").

`config_hash` gives a stable fingerprint of a parsed config for the experiment store.

    @dataclass(frozen=True)
    class Trend:
        fast: int = 20
        slow: int = 100

    trend = parse({"fast": 10}, Trend)            # Trend(fast=10, slow=100)
    trend = load("trend.toml", Trend, section="trend")
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import tomllib
import types
import typing
from pathlib import Path
from typing import Any, Literal, Mapping, TypeVar, Union, get_args, get_origin

T = TypeVar("T")


class ConfigError(ValueError):
    """Every problem found in a config, one per line."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        super().__init__("invalid config:\n  " + "\n  ".join(self.problems))


_MISSING = object()


def _type_name(kind: Any) -> str:
    return getattr(kind, "__name__", None) or str(kind).replace("typing.", "")


def _convert(value: Any, kind: Any, path: str, problems: list[str]) -> Any:
    """`value` checked against (and, where safe, converted to) `kind`; problems are appended, never raised."""
    if kind is Any:
        return value
    origin, args = get_origin(kind), get_args(kind)
    if origin in (Union, types.UnionType):
        if value is None and type(None) in args:
            return None
        options = [arg for arg in args if arg is not type(None)]
        for option in options:  # first option that parses cleanly wins
            trial: list[str] = []
            result = _convert(value, option, path, trial)
            if not trial:
                return result
        problems.append(f"{path}: expected {' or '.join(_type_name(arg) for arg in args)}, got {type(value).__name__} {value!r}")
        return value
    if origin is Literal:
        if value not in args:
            problems.append(f"{path}: must be one of {list(args)}, got {value!r}")
        return value
    if dataclasses.is_dataclass(kind) and isinstance(kind, type):
        if not isinstance(value, Mapping):
            problems.append(f"{path}: expected a table for {kind.__name__}, got {type(value).__name__}")
            return value
        return _build(value, kind, path, problems)
    if origin in (list, tuple) or kind in (list, tuple):
        if not isinstance(value, (list, tuple)):
            problems.append(f"{path}: expected a list, got {type(value).__name__} {value!r}")
            return value
        if origin is tuple and len(args) == 2 and args[1] is Ellipsis:
            item_kinds = [args[0]] * len(value)
        elif origin is tuple and args:
            if len(args) != len(value):
                problems.append(f"{path}: expected {len(args)} items, got {len(value)}")
                return value
            item_kinds = list(args)
        else:
            item_kinds = [args[0] if args else Any] * len(value)
        items = [_convert(item, item_kind, f"{path}[{index}]", problems) for index, (item, item_kind) in enumerate(zip(value, item_kinds))]
        return tuple(items) if (origin is tuple or kind is tuple) else items
    if origin is dict or kind is dict:
        if not isinstance(value, Mapping):
            problems.append(f"{path}: expected a table, got {type(value).__name__}")
            return value
        key_kind, value_kind = (args + (Any, Any))[:2] if args else (Any, Any)
        return {_convert(key, key_kind, f"{path}.<key>", problems): _convert(item, value_kind, f"{path}.{key}", problems) for key, item in value.items()}
    if kind is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            problems.append(f"{path}: expected float, got {type(value).__name__} {value!r}")
            return value
        return float(value)
    if kind is int:
        if isinstance(value, bool) or not isinstance(value, int):
            problems.append(f"{path}: expected int, got {type(value).__name__} {value!r}")
        return value
    if kind is bool:
        if not isinstance(value, bool):
            problems.append(f"{path}: expected true or false, got {value!r}")
        return value
    if kind is str:
        if not isinstance(value, str):
            problems.append(f"{path}: expected text, got {type(value).__name__} {value!r}")
        return value
    if isinstance(kind, type) and not isinstance(value, kind):
        problems.append(f"{path}: expected {kind.__name__}, got {type(value).__name__} {value!r}")
    return value


def _build(data: Mapping[str, Any], schema: type[T], path: str, problems: list[str]) -> T:
    hints = typing.get_type_hints(schema)
    fields = {item.name: item for item in dataclasses.fields(schema) if item.init}
    prefix = f"{path}." if path else ""
    before = len(problems)
    for key in sorted(set(data) - set(fields)):
        problems.append(f"{prefix}{key}: unknown key; allowed: {sorted(fields)}")
    values: dict[str, Any] = {}
    for name, item in fields.items():
        if name in data:
            values[name] = _convert(data[name], hints[name], f"{prefix}{name}", problems)
        elif item.default is dataclasses.MISSING and item.default_factory is dataclasses.MISSING:
            problems.append(f"{prefix}{name}: required")
    if len(problems) > before:  # don't construct from bad values: report them instead
        return typing.cast(T, None)
    try:
        return schema(**values)
    except (TypeError, ValueError) as exc:  # a dataclass's own __post_init__ validation
        problems.append(f"{path or schema.__name__}: {exc}")
        return typing.cast(T, None)


def parse(data: Mapping[str, Any], schema: type[T]) -> T:
    """`data` as an instance of the dataclass `schema`; raises ConfigError listing every problem."""
    if not (dataclasses.is_dataclass(schema) and isinstance(schema, type)):
        raise TypeError("schema must be a dataclass type")
    problems: list[str] = []
    result = _build(data, schema, "", problems)
    if problems:
        raise ConfigError(problems)
    return result


def load(path: str | Path, schema: type[T], *, section: str | None = None) -> T:
    """Parse a TOML file (or one top-level `section` of it) into `schema`."""
    data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    if section is not None:
        if section not in data:
            raise ConfigError([f"{path}: no [{section}] section"])
        data = data[section]
    try:
        return parse(data, schema)
    except ConfigError as exc:
        raise ConfigError([f"{path}: {problem}" for problem in exc.problems]) from None


def to_dict(config: Any) -> Any:
    """A parsed config as plain dicts/lists (tuples become lists), for storing and hashing."""
    if dataclasses.is_dataclass(config) and not isinstance(config, type):
        return {item.name: to_dict(getattr(config, item.name)) for item in dataclasses.fields(config)}
    if isinstance(config, Mapping):
        return {str(key): to_dict(value) for key, value in config.items()}
    if isinstance(config, (list, tuple)):
        return [to_dict(value) for value in config]
    return config


def config_hash(config: Any, *, length: int = 16) -> str:
    """A stable fingerprint of a config: SHA-256 of its canonical JSON (sorted keys), first `length` hex digits."""
    canonical = json.dumps(to_dict(config), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:length]
