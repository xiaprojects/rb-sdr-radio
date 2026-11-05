"""Configuration loading and validation.

Same two rules as rb-vor: refuse to start on an invalid config, naming the offending
path, and keep every default in the shipped `config/rb-sdr-radio.json` rather than
scattered through the code. `additionalProperties` is false throughout the schema,
so a mistyped key is an error, not a setting silently ignored.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = ["Config", "ConfigError", "load", "DEFAULT_PATHS", "SCHEMA_PATH", "SHIPPED_PATH"]

_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = _ROOT / "schema" / "rb-sdr-radio.schema.json"
SHIPPED_PATH = _ROOT / "config" / "rb-sdr-radio.json"
DEFAULT_PATHS = (Path("/etc/rb-sdr-radio/rb-sdr-radio.json"), SHIPPED_PATH)


class ConfigError(ValueError):
    """Any invalid configuration. Always names the offending path."""


@dataclass(frozen=True)
class Config:
    raw: Mapping[str, Any]
    source: Path | None = None

    def __getattr__(self, section: str) -> dict:
        try:
            return self.raw[section]
        except KeyError:
            raise AttributeError(section) from None

    def with_overrides(self, **sections: dict) -> Config:
        """A copy with keys replaced inside the named sections (bench tests)."""
        raw = copy.deepcopy(dict(self.raw))
        for name, values in sections.items():
            raw[name].update(values)
        return Config(raw, self.source)


def _validate(data: dict) -> None:
    import jsonschema
    schema = json.loads(SCHEMA_PATH.read_text())
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(data), key=lambda e: list(map(str, e.path)))
    if errors:
        e = errors[0]
        path = "".join(f"[{p!r}]" for p in e.path)
        more = f" (and {len(errors) - 1} more)" if len(errors) > 1 else ""
        raise ConfigError(f"config{path}: {e.message}{more}")

    bridge = data["bridge"]
    if bridge["ws_reconnect_max_s"] < bridge["ws_reconnect_min_s"]:
        raise ConfigError("config['bridge']['ws_reconnect_max_s']: must not be below "
                          "ws_reconnect_min_s")
    record = data["record"]
    if record["max_segment_s"] <= record["min_segment_s"]:
        raise ConfigError("config['record']['max_segment_s']: must exceed min_segment_s")
    if record["max_segment_s"] * 2 * 16_000 > record["max_total_mb"] * 1_000_000:
        raise ConfigError("config['record']['max_segment_s']: one segment would exceed "
                          "max_total_mb")


def load(path: str | Path | None = None) -> Config:
    """Load and validate. Raises `ConfigError`; the daemon must not start on one."""
    if path is not None:
        p = Path(path)
        if not p.exists():
            raise ConfigError(f"config file not found: {p}")
    else:
        p = next((c for c in DEFAULT_PATHS if c.exists()), None)
        if p is None:
            raise ConfigError("no configuration found; looked in "
                              + ", ".join(str(c) for c in DEFAULT_PATHS))
    try:
        data = json.loads(p.read_text())
    except json.JSONDecodeError as e:
        raise ConfigError(f"{p}: invalid JSON at line {e.lineno}: {e.msg}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"{p}: top level must be an object")
    _validate(data)
    return Config(data, p)
