import json
from pathlib import Path
import subprocess

import pytest

from cloudrca_compatibility.jcode_smoke import ProviderSpec, SmokeError, configure_provider, parse_turn, run_turn


def envelope(content: object) -> str:
    return json.dumps(
        {
            "session_id": "session-1",
            "model": "qwen3-coder-next",
            "text": json.dumps(content),
            "usage": {"input_tokens": 12, "output_tokens": 8},
        }
    )


def test_structured_output_is_validated() -> None:
    result = parse_turn(envelope({"layer": "database", "evidence": ["db-1"]}), {"layer": str, "evidence": list})
    assert result.model == "qwen3-coder-next"
    assert result.usage["input_tokens"] == 12


@pytest.mark.parametrize("raw", ["not json", envelope({"layer": 3}), json.dumps({"text": "not json"})])
def test_malformed_or_schema_invalid_output_fails(raw: str) -> None:
    with pytest.raises(SmokeError):
        parse_turn(raw, {"layer": str})


def test_nonzero_jcode_exit_is_manageable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 9, "", "provider rejected model"),
    )
    with pytest.raises(SmokeError, match="provider rejected model"):
        run_turn(Path("jcode"), tmp_path, "qwen", "qwen", "prompt", {"ok": bool}, 1)


def test_remote_provider_without_credential_is_rejected(tmp_path: Path) -> None:
    spec = ProviderSpec("glm", "https://api.z.ai/api/paas/v4", "glm-5.3")
    with pytest.raises(SmokeError, match="require --api-key-env"):
        configure_provider(Path("jcode"), tmp_path, spec, 1)


def test_timeout_is_manageable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def expire(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired("jcode", 0.01)

    monkeypatch.setattr(subprocess, "run", expire)
    with pytest.raises(SmokeError, match="timed out"):
        run_turn(Path("jcode"), tmp_path, "glm", "glm-5.3", "prompt", {"ok": bool}, 0.01)


def test_handoff_prompt_and_model_are_forwarded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: list[str] = []

    def succeed(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.extend(command)
        return subprocess.CompletedProcess(command, 0, envelope({"root_cause": "storage", "confidence": 0.9, "evidence": []}), "")

    monkeypatch.setattr(subprocess, "run", succeed)
    result = run_turn(
        Path("jcode"), tmp_path, "glm", "glm-5.3", "database vm hypervisor", {"root_cause": str, "confidence": (int, float), "evidence": list}, 1
    )
    assert result.data["root_cause"] == "storage"
    assert "glm-5.3" in seen
    assert "database vm hypervisor" in seen
