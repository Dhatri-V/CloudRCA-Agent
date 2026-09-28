import pytest
from cloudrca_backend.settings import Profile, SettingsError, load_settings


def valid_environment(profile: str = "development") -> dict[str, str]:
    return {
        "CLOUDRCA_PROFILE": profile,
        "CLOUDRCA_DATA_DIR": "./data",
        "CLOUDRCA_KNOWLEDGE_DIR": "./knowledge",
        "CLOUDRCA_JCODE_BINARY": "jcode",
    }


@pytest.mark.parametrize("profile", list(Profile))
def test_supported_profiles_load(profile: Profile) -> None:
    settings = load_settings(valid_environment(profile.value))
    assert settings.profile is profile
    assert settings.data_dir.name == "data"


def test_missing_required_configuration_fails_clearly() -> None:
    environment = valid_environment()
    del environment["CLOUDRCA_DATA_DIR"]
    del environment["CLOUDRCA_JCODE_BINARY"]

    with pytest.raises(SettingsError) as captured:
        load_settings(environment)

    assert "CLOUDRCA_DATA_DIR" in str(captured.value)
    assert "CLOUDRCA_JCODE_BINARY" in str(captured.value)


def test_unknown_profile_fails_clearly() -> None:
    with pytest.raises(SettingsError, match="development, test, demo"):
        load_settings(valid_environment("production"))
