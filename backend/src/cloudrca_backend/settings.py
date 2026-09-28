"""Typed application settings loaded from the process environment."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class Profile(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    DEMO = "demo"


class SettingsError(ValueError):
    """Raised when application configuration is missing or invalid."""


@dataclass(frozen=True, slots=True)
class Settings:
    profile: Profile
    data_dir: Path
    knowledge_dir: Path
    jcode_binary: str


_REQUIRED = {
    "CLOUDRCA_PROFILE": "profile",
    "CLOUDRCA_DATA_DIR": "data directory",
    "CLOUDRCA_KNOWLEDGE_DIR": "knowledge directory",
    "CLOUDRCA_JCODE_BINARY": "JCode executable",
}


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    """Load settings or fail with all missing and invalid values named."""
    values = os.environ if environ is None else environ
    missing = [name for name in _REQUIRED if not values.get(name, "").strip()]
    if missing:
        raise SettingsError(f"Missing required configuration: {', '.join(missing)}")

    profile_value = values["CLOUDRCA_PROFILE"].strip().lower()
    try:
        profile = Profile(profile_value)
    except ValueError as error:
        supported = ", ".join(item.value for item in Profile)
        raise SettingsError(f"Invalid CLOUDRCA_PROFILE '{profile_value}'. Expected one of: {supported}") from error

    return Settings(
        profile=profile,
        data_dir=Path(values["CLOUDRCA_DATA_DIR"]).expanduser(),
        knowledge_dir=Path(values["CLOUDRCA_KNOWLEDGE_DIR"]).expanduser(),
        jcode_binary=values["CLOUDRCA_JCODE_BINARY"].strip(),
    )
