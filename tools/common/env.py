"""Typed environment readers for the tools (#263). Stdlib only.

An unset or empty variable gives the default. A non-integer for env_int raises
ConfigError naming the variable, so a typo fails at start-up instead of later.
"""
import os

TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


class ConfigError(ValueError):
    pass


def _raw(name):
    """The variable's value, or None when it is unset or empty."""
    return os.environ.get(name) or None


def env_str(name, default=""):
    value = _raw(name)
    return default if value is None else value


def env_int(name, default):
    value = _raw(name)
    if value is None:
        return default
    try:
        return int(value.strip())
    except ValueError:
        raise ConfigError(f"{name} must be an integer") from None


def env_bool(name, default=False):
    value = _raw(name)
    if value is None:
        return default
    return value.strip().lower() in TRUE_VALUES
