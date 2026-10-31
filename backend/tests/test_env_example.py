"""Regression: .env.example must not ship JCode provider profile names that collide with JCode built-ins."""

from __future__ import annotations

from pathlib import Path

ENV_EXAMPLE = Path(__file__).resolve().parents[2] / ".env.example"
PROFILE_SETTINGS = ("CLOUDRCA_QWEN_PROVIDER_PROFILE", "CLOUDRCA_GLM_PROVIDER_PROFILE")


def example_settings() -> dict[str, str]:
    lines = ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
    return dict(line.split("=", 1) for line in lines if line.strip() and not line.lstrip().startswith("#"))


def test_provider_profiles_use_the_cloudrca_namespace() -> None:
    # JCode v0.88 rejects `provider add` for built-in ids/aliases such as "zai", "z.ai", "zhipu", and "ollama"
    # ("'zai' is a built-in provider id or alias"). Project-prefixed names can never collide with them.
    settings = example_settings()
    for name in PROFILE_SETTINGS:
        assert settings[name].startswith("cloudrca-"), f"{name}={settings[name]!r} may collide with a JCode built-in"
    assert settings["CLOUDRCA_GLM_PROVIDER_PROFILE"] != settings["CLOUDRCA_QWEN_PROVIDER_PROFILE"]


def test_default_openrouter_main_rca_model_is_free() -> None:
    # The default must never point at a paid OpenRouter model; ":free" variants cost $0.
    settings = example_settings()
    assert settings["CLOUDRCA_GLM_BASE_URL"] == "https://openrouter.ai/api/v1"
    assert settings["CLOUDRCA_GLM_MODEL"].endswith(":free")
    assert settings["CLOUDRCA_GLM_API_KEY_ENV"] == "OPENROUTER_API_KEY"
    assert "OPENROUTER_API_KEY" not in settings, "keys belong in .env, never in .env.example"
