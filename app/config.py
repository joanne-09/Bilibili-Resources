"""Local settings, read from config.local.toml (git-ignored; see config.example.toml)."""
import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Config:
    tmdb_key: str | None = None


def load_config(path: Path = ROOT / "config.local.toml") -> Config:
    """A missing, unreadable or incomplete file simply means "not configured"."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return Config()
    section = data.get("tmdb")
    key = section.get("api_key") if isinstance(section, dict) else None
    key = key.strip() if isinstance(key, str) else ""
    return Config(tmdb_key=key or None)
