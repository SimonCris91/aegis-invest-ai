"""Minimal local .env parsing that never logs or persists values."""

import re
from pathlib import Path

_KEY_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class DotenvLoadError(ValueError):
    """Raised when local .env syntax is ambiguous or unsafe."""


def load_dotenv_values(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    if not path.is_file():
        raise DotenvLoadError("local .env path is not a file")
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        raise DotenvLoadError("local .env file could not be read") from exc

    values: dict[str, str] = {}
    for line_number, line in enumerate(lines, start=1):
        parsed = _parse_line(line, line_number)
        if parsed is None:
            continue
        key, value = parsed
        if key in values:
            raise DotenvLoadError(f"duplicate key in local .env on line {line_number}")
        values[key] = value
    return values


def _parse_line(line: str, line_number: int) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith("export "):
        stripped = stripped[7:].lstrip()
    if "=" not in stripped:
        raise DotenvLoadError(f"invalid local .env entry on line {line_number}")

    raw_key, raw_value = stripped.split("=", maxsplit=1)
    key = raw_key.strip()
    if not _KEY_PATTERN.fullmatch(key):
        raise DotenvLoadError(f"invalid local .env key on line {line_number}")
    return key, _parse_value(raw_value.strip(), line_number)


def _parse_value(value: str, line_number: int) -> str:
    if not value:
        return ""
    if value[0] in {"'", '"'}:
        quote = value[0]
        end_index = value.rfind(quote)
        if end_index == 0:
            raise DotenvLoadError(f"unterminated local .env quoted value on line {line_number}")
        tail = value[end_index + 1 :].strip()
        if tail and not tail.startswith("#"):
            raise DotenvLoadError(f"invalid local .env quoted value suffix on line {line_number}")
        inner = value[1:end_index]
        if quote == '"':
            return _unescape_double_quoted(inner)
        return inner.replace("\\'", "'")
    return value.split(" #", maxsplit=1)[0].strip()


def _unescape_double_quoted(value: str) -> str:
    replacements = {
        "\\\\": "\\",
        "\\n": "\n",
        "\\r": "\r",
        "\\t": "\t",
        '\\"': '"',
    }
    for escaped, replacement in replacements.items():
        value = value.replace(escaped, replacement)
    return value
