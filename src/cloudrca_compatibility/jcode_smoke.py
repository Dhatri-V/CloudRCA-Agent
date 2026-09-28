"""Small, credential-safe JCode compatibility smoke runner.

The runner invokes the real JCode CLI. It does not implement CloudRCA agents.
Provider URLs, model IDs, and API-key environment variable names are supplied
at runtime so credentials never enter source control.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

ExpectedType = type[Any] | tuple[type[Any], ...]


class SmokeError(RuntimeError):
    """Raised when JCode cannot complete or validate a smoke request."""


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    base_url: str
    model: str
    api_key_env: str | None = None
    context_window: int | None = None


@dataclass(frozen=True)
class TurnResult:
    session_id: str
    model: str
    data: dict[str, Any]
    usage: dict[str, int | None]


SPECIALISTS = (
    {"layer": "database", "finding": "connection latency rose", "evidence": ["db-1"]},
    {"layer": "vm", "finding": "iowait rose first", "evidence": ["vm-1"]},
    {"layer": "hypervisor", "finding": "storage queue saturated", "evidence": ["host-1"]},
)


def _run(command: Sequence[str], env: Mapping[str, str], timeout_seconds: float) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            check=False,
            env=dict(env),
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as error:
        raise SmokeError(f"JCode timed out after {timeout_seconds:g}s") from error
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise SmokeError(f"JCode exited {result.returncode}: {detail}")
    return result


def configure_provider(
    binary: Path,
    home: Path,
    spec: ProviderSpec,
    timeout_seconds: float,
) -> None:
    command = [
        str(binary),
        "--no-update",
        "provider",
        "add",
        spec.name,
        "--base-url",
        spec.base_url,
        "--model",
        spec.model,
        "--overwrite",
        "--json",
    ]
    if spec.context_window:
        command.extend(("--context-window", str(spec.context_window)))
    if spec.api_key_env:
        if not os.environ.get(spec.api_key_env):
            raise SmokeError(f"required credential environment variable is absent: {spec.api_key_env}")
        command.extend(("--api-key-env", spec.api_key_env))
    elif spec.base_url.startswith(("http://localhost", "http://127.0.0.1")):
        command.append("--no-api-key")
    else:
        raise SmokeError("remote providers require --api-key-env; credentials are never embedded")
    _run(command, _environment(home), timeout_seconds)


def _environment(home: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update({"JCODE_HOME": str(home), "JCODE_NO_TELEMETRY": "1"})
    return environment


def parse_turn(raw: str, required: Mapping[str, ExpectedType]) -> TurnResult:
    try:
        envelope = json.loads(raw)
        content = json.loads(envelope["text"])
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise SmokeError("JCode returned malformed structured output") from error
    errors = [key for key, kind in required.items() if key not in content or not isinstance(content[key], kind)]
    if errors:
        raise SmokeError(f"structured output failed validation: {', '.join(errors)}")
    usage = envelope.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    return TurnResult(
        session_id=str(envelope.get("session_id", "")),
        model=str(envelope.get("model", "")),
        data=content,
        usage=usage,
    )


def run_turn(
    binary: Path,
    home: Path,
    profile: str,
    model: str,
    prompt: str,
    required: Mapping[str, ExpectedType],
    timeout_seconds: float,
) -> TurnResult:
    command = [
        str(binary),
        "--no-update",
        "--socket",
        str(home / "jcode.sock"),
        "--provider-profile",
        profile,
        "--model",
        model,
        "--tool-profile",
        "none",
        "run",
        "--json",
        prompt,
    ]
    return parse_turn(_run(command, _environment(home), timeout_seconds).stdout, required)


def run_smoke(
    binary: Path,
    qwen: ProviderSpec,
    glm: ProviderSpec,
    timeout_seconds: float = 60,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="cloudrca-jcode-") as temporary:
        home = Path(temporary)
        try:
            configure_provider(binary, home, qwen, timeout_seconds)
            configure_provider(binary, home, glm, timeout_seconds)
            qwen_result = run_turn(
                binary,
                home,
                qwen.name,
                qwen.model,
                "Return only JSON: {\"layer\":\"database\",\"finding\":\"...\",\"evidence\":[]}",
                {"layer": str, "finding": str, "evidence": list},
                timeout_seconds,
            )
            handoff = json.dumps(SPECIALISTS, separators=(",", ":"))
            glm_result = run_turn(
                binary,
                home,
                glm.name,
                glm.model,
                f"Correlate these specialist findings: {handoff}. Return only JSON with root_cause, confidence, evidence.",
                {"root_cause": str, "confidence": (int, float), "evidence": list},
                timeout_seconds,
            )
            return {
                "status": "VERIFIED",
                "qwen": asdict(qwen_result),
                "glm": asdict(glm_result),
                "handoff": list(SPECIALISTS),
            }
        finally:
            try:
                subprocess.run(
                    [str(binary), "--socket", str(home / "jcode.sock"), "server", "stop"],
                    capture_output=True,
                    check=False,
                    env=_environment(home),
                    text=True,
                    timeout=min(timeout_seconds, 5),
                )
            except subprocess.TimeoutExpired:
                pass
