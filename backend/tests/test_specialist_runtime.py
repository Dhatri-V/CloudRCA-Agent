"""Tests for the shared JCode/Qwen specialist execution runtime."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cloudrca_backend.specialist_runtime as specialist_module
import pytest
from cloudrca_backend.contracts import Layer, NormalizedEvent, Provenance
from cloudrca_backend.routing import RoutedEvent, load_routing_config, route_and_resolve
from cloudrca_backend.settings import Profile, Settings, SettingsError
from cloudrca_backend.specialist_runtime import (
    JCodeCliTransport,
    JCodeConfigurationError,
    JCodeProviderError,
    JCodeResponse,
    JCodeSession,
    JCodeTimeoutError,
    SpecialistFailureCode,
    SpecialistRequest,
    SpecialistRuntime,
    SpecialistRuntimeConfig,
    SpecialistRuntimeError,
    TokenUsage,
    build_specialist_prompt,
    load_specialist_runtime_config,
)

ROOT = Path(__file__).parents[2]
ROUTING_CONFIG = load_routing_config(ROOT / "config" / "routing" / "aiops2025.yaml")
UTC_TIME = datetime(2025, 4, 29, 6, 3, 8, tzinfo=timezone.utc)


def config(**changes: object) -> SpecialistRuntimeConfig:
    values: dict[str, object] = {
        "jcode_binary": "jcode",
        "provider_profile": "cloudrca-qwen",
        "provider_base_url": "http://localhost:11434/v1",
        "model": "qwen-test",
        "timeout_seconds": 10,
        "max_events": 10,
        "max_prompt_characters": 100_000,
        "max_repair_attempts": 1,
        "prompt_version": "specialist-v1",
    }
    values.update(changes)
    return SpecialistRuntimeConfig.model_validate(values)


def routed(layer: Layer, event_id: str = "event-1", message: str = "component failure") -> RoutedEvent:
    identifiers: dict[str, str] = {}
    if layer is Layer.DATABASE:
        identifiers = {"service": "redis", "database_id": "redis-cart-0"}
    elif layer is Layer.VM:
        identifiers = {"service": "node", "vm_id": "vm-worker-03"}
    else:
        identifiers = {"service": "libvirt", "host_id": "host-01"}
    event = NormalizedEvent(
        event_id=event_id,
        timestamp=UTC_TIME,
        source="aiops2025.test",
        layer=layer,
        severity="error",
        message=message,
        provenance=Provenance(kind="observed", source="AIOps2025", record_id=f"row-{event_id}"),
        **identifiers,
    )
    return route_and_resolve(event, ROUTING_CONFIG)


def finding_json(layer: Layer, event_id: str = "event-1") -> str:
    return json.dumps(
        {
            "finding_id": f"finding-{layer.value}",
            "layer": layer.value,
            "summary": "A component failed",
            "suspected_problem": "Resource pressure",
            "confidence": {"score": 0.8, "level": "high"},
            "evidence": [
                {
                    "evidence_id": f"evidence-{event_id}",
                    "kind": "event",
                    "reference_id": event_id,
                }
            ],
            "event_ids": [event_id],
            "component_ids": [],
        }
    )


class FakeSession:
    def __init__(self, session_id: str, actions: list[str | Exception], prompts: list[tuple[str, str]]) -> None:
        self.session_id = session_id
        self.actions = actions
        self.prompts = prompts

    def complete(self, prompt: str, timeout_seconds: float) -> JCodeResponse:
        self.prompts.append((self.session_id, prompt))
        action = self.actions.pop(0)
        if isinstance(action, Exception):
            raise action
        return JCodeResponse(
            text=action,
            session_id=self.session_id,
            model="qwen-test",
            usage=TokenUsage(input_tokens=10, output_tokens=5),
        )


class FakeTransport:
    def __init__(self, actions: list[str | Exception]) -> None:
        self.actions = actions
        self.prompts: list[tuple[str, str]] = []
        self.sessions: list[str] = []
        self.correlations: list[str] = []

    @contextmanager
    def session(self, runtime_config: SpecialistRuntimeConfig, correlation_id: str) -> Iterator[JCodeSession]:
        del runtime_config
        session_id = f"session-{len(self.sessions) + 1}"
        self.sessions.append(session_id)
        self.correlations.append(correlation_id)
        yield FakeSession(session_id, self.actions, self.prompts)


class FailingTransport:
    def __init__(self, error: Exception) -> None:
        self.error = error

    @contextmanager
    def session(self, runtime_config: SpecialistRuntimeConfig, correlation_id: str) -> Iterator[JCodeSession]:
        del runtime_config, correlation_id
        raise self.error
        yield  # pragma: no cover


class RecordingLogger:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def info(self, event: str, **values: Any) -> None:
        self.events.append((event, values))


@pytest.mark.parametrize("layer", list(Layer))
def test_runtime_succeeds_for_each_specialist_layer(layer: Layer) -> None:
    transport = FakeTransport([finding_json(layer)])
    result = SpecialistRuntime(config(), transport).run(SpecialistRequest(layer=layer, events=(routed(layer),)))
    assert result.finding.layer is layer
    assert result.metadata.success is True


def test_each_execution_uses_an_isolated_session() -> None:
    transport = FakeTransport(
        [
            finding_json(Layer.DATABASE),
            finding_json(Layer.VM, "event-2"),
            finding_json(Layer.HYPERVISOR, "event-3"),
        ]
    )
    runtime = SpecialistRuntime(
        config(), transport, correlation_id_factory=iter(("corr-1", "corr-2", "corr-3")).__next__
    )
    runtime.run(SpecialistRequest(layer=Layer.DATABASE, events=(routed(Layer.DATABASE),)))
    runtime.run(SpecialistRequest(layer=Layer.VM, events=(routed(Layer.VM, "event-2"),)))
    runtime.run(SpecialistRequest(layer=Layer.HYPERVISOR, events=(routed(Layer.HYPERVISOR, "event-3"),)))
    assert transport.sessions == ["session-1", "session-2", "session-3"]
    assert transport.correlations == ["corr-1", "corr-2", "corr-3"]


def test_prompt_assembly_is_deterministic_and_versioned() -> None:
    request = SpecialistRequest(layer=Layer.DATABASE, events=(routed(Layer.DATABASE),))
    first = build_specialist_prompt(request, "specialist-v7")
    second = build_specialist_prompt(request, "specialist-v7")
    assert first == second
    assert first.startswith("CLOUDRCA_SPECIALIST_PROMPT_VERSION=specialist-v7")
    assert "FINDING_SCHEMA=" in first


def test_prompt_excludes_raw_provenance_details() -> None:
    candidate = routed(Layer.DATABASE)
    changed_event = candidate.event.model_copy(
        update={
            "provenance": Provenance(
                kind="observed",
                source="AIOps2025",
                record_id="row-1",
                details={"raw_record": {"secret_field": "do-not-copy"}},
            )
        }
    )
    request = SpecialistRequest(layer=Layer.DATABASE, events=(candidate.model_copy(update={"event": changed_event}),))
    assert "do-not-copy" not in build_specialist_prompt(request, "v1")


def test_malformed_json_reaches_typed_failure_without_repairs() -> None:
    runtime = SpecialistRuntime(config(max_repair_attempts=0), FakeTransport(["not-json"]))
    with pytest.raises(SpecialistRuntimeError) as raised:
        runtime.run(SpecialistRequest(layer=Layer.DATABASE, events=(routed(Layer.DATABASE),)))
    assert raised.value.code is SpecialistFailureCode.INVALID_OUTPUT


def test_invalid_finding_is_repaired_in_same_session() -> None:
    transport = FakeTransport(["{\"summary\":\"missing fields\"}", finding_json(Layer.DATABASE)])
    result = SpecialistRuntime(config(), transport).run(
        SpecialistRequest(layer=Layer.DATABASE, events=(routed(Layer.DATABASE),))
    )
    assert result.metadata.repair_count == 1
    assert len(transport.sessions) == 1
    assert len(transport.prompts) == 2
    assert "Repair the previous response" in transport.prompts[1][1]


def test_repair_limit_produces_typed_failure() -> None:
    transport = FakeTransport(["bad", "still bad"])
    with pytest.raises(SpecialistRuntimeError) as raised:
        SpecialistRuntime(config(max_repair_attempts=1), transport).run(
            SpecialistRequest(layer=Layer.DATABASE, events=(routed(Layer.DATABASE),))
        )
    assert raised.value.code is SpecialistFailureCode.INVALID_OUTPUT
    assert raised.value.metadata.repair_count == 1


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (JCodeTimeoutError("deadline"), SpecialistFailureCode.TIMEOUT),
        (JCodeProviderError("provider failed"), SpecialistFailureCode.PROVIDER),
        (JCodeConfigurationError("bad provider config"), SpecialistFailureCode.CONFIGURATION),
    ],
)
def test_jcode_failures_remain_typed(error: Exception, code: SpecialistFailureCode) -> None:
    with pytest.raises(SpecialistRuntimeError) as raised:
        SpecialistRuntime(config(), FailingTransport(error)).run(
            SpecialistRequest(layer=Layer.DATABASE, events=(routed(Layer.DATABASE),))
        )
    assert raised.value.code is code
    assert raised.value.metadata.success is False


def test_missing_and_invalid_provider_configuration() -> None:
    settings = Settings(Profile.TEST, Path("data"), Path("knowledge"), "jcode")
    with pytest.raises(SettingsError, match="CLOUDRCA_QWEN_PROVIDER_PROFILE"):
        load_specialist_runtime_config(settings, {})
    with pytest.raises(SettingsError, match="API_KEY_ENV"):
        load_specialist_runtime_config(
            settings,
            {
                "CLOUDRCA_QWEN_PROVIDER_PROFILE": "qwen",
                "CLOUDRCA_QWEN_BASE_URL": "https://provider.example/v1",
                "CLOUDRCA_QWEN_MODEL": "qwen-model",
            },
        )


def test_credentials_are_checked_but_not_stored() -> None:
    settings = Settings(Profile.TEST, Path("data"), Path("knowledge"), "jcode")
    runtime_config = load_specialist_runtime_config(
        settings,
        {
            "CLOUDRCA_QWEN_PROVIDER_PROFILE": "qwen",
            "CLOUDRCA_QWEN_BASE_URL": "https://provider.example/v1",
            "CLOUDRCA_QWEN_MODEL": "qwen-model",
            "CLOUDRCA_QWEN_API_KEY_ENV": "QWEN_SECRET",
            "QWEN_SECRET": "secret-value",
        },
    )
    assert runtime_config.api_key_env == "QWEN_SECRET"
    assert "secret-value" not in runtime_config.model_dump_json()


def test_correlation_provider_model_version_usage_and_duration_are_recorded() -> None:
    times = iter((5.0, 5.25))
    result = SpecialistRuntime(config(), FakeTransport([finding_json(Layer.DATABASE)]), clock=times.__next__).run(
        SpecialistRequest(correlation_id="correlation-7", layer=Layer.DATABASE, events=(routed(Layer.DATABASE),))
    )
    assert result.metadata.correlation_id == "correlation-7"
    assert result.metadata.provider_profile == "cloudrca-qwen"
    assert result.metadata.model == "qwen-test"
    assert result.metadata.prompt_version == "specialist-v1"
    assert result.metadata.usage == TokenUsage(input_tokens=10, output_tokens=5)
    assert result.metadata.duration_ms == 250


def test_repair_token_usage_is_accumulated() -> None:
    transport = FakeTransport(["bad", finding_json(Layer.DATABASE)])
    result = SpecialistRuntime(config(), transport).run(
        SpecialistRequest(layer=Layer.DATABASE, events=(routed(Layer.DATABASE),))
    )
    assert result.metadata.usage == TokenUsage(input_tokens=20, output_tokens=10)


def test_logs_are_structured_and_do_not_include_prompt_or_credentials() -> None:
    logger = RecordingLogger()
    SpecialistRuntime(config(), FakeTransport([finding_json(Layer.DATABASE)]), logger=logger).run(
        SpecialistRequest(layer=Layer.DATABASE, events=(routed(Layer.DATABASE),))
    )
    assert [name for name, _ in logger.events] == ["specialist_run_started", "specialist_run_completed"]
    serialized = json.dumps(logger.events)
    assert "FINDING_SCHEMA" not in serialized
    assert "component failure" not in serialized


def test_event_batch_limit_is_explicit() -> None:
    request = SpecialistRequest(
        layer=Layer.DATABASE,
        events=(routed(Layer.DATABASE, "event-1"), routed(Layer.DATABASE, "event-2")),
    )
    with pytest.raises(SpecialistRuntimeError) as raised:
        SpecialistRuntime(config(max_events=1), FakeTransport([])).run(request)
    assert raised.value.code is SpecialistFailureCode.BATCH_LIMIT


def test_context_limit_is_explicit_and_does_not_call_jcode() -> None:
    transport = FakeTransport([])
    with pytest.raises(SpecialistRuntimeError) as raised:
        SpecialistRuntime(config(max_prompt_characters=20), transport).run(
            SpecialistRequest(layer=Layer.DATABASE, events=(routed(Layer.DATABASE),))
        )
    assert raised.value.code is SpecialistFailureCode.CONTEXT_LIMIT
    assert transport.sessions == []


def test_mismatched_route_is_rejected_before_jcode() -> None:
    with pytest.raises(SpecialistRuntimeError) as raised:
        SpecialistRuntime(config(), FakeTransport([])).run(
            SpecialistRequest(layer=Layer.DATABASE, events=(routed(Layer.VM),))
        )
    assert raised.value.code is SpecialistFailureCode.CONFIGURATION


def test_finding_cannot_reference_evidence_outside_input() -> None:
    bad = json.loads(finding_json(Layer.DATABASE))
    bad["evidence"][0]["reference_id"] = "invented-event"
    with pytest.raises(SpecialistRuntimeError) as raised:
        SpecialistRuntime(config(max_repair_attempts=0), FakeTransport([json.dumps(bad)])).run(
            SpecialistRequest(layer=Layer.DATABASE, events=(routed(Layer.DATABASE),))
        )
    assert raised.value.code is SpecialistFailureCode.INVALID_OUTPUT


def test_real_cli_adapter_uses_validated_jcode_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    configured_homes: list[Path] = []
    commands: list[list[str]] = []

    def configure(binary: Path, home: Path, spec: object, timeout: float) -> None:
        del binary, spec, timeout
        configured_homes.append(home)

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if "run" in command:
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps(
                    {
                        "session_id": "real-session",
                        "model": "qwen-test",
                        "text": finding_json(Layer.DATABASE),
                        "usage": {"input_tokens": 3, "output_tokens": 2},
                    }
                ),
                "",
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(specialist_module, "configure_provider", configure)
    monkeypatch.setattr(subprocess, "run", run)
    transport = JCodeCliTransport()
    with transport.session(config(), "correlation-1") as session:
        response = session.complete("prompt", 10)
    with transport.session(config(), "correlation-2"):
        pass
    assert response.session_id == "real-session"
    run_command = next(command for command in commands if "run" in command)
    assert "--provider-profile" in run_command
    assert "--tool-profile" in run_command
    assert run_command[-3:] == ["run", "--json", "prompt"]
    assert configured_homes[0].name.startswith("cloudrca-specialist-")
    assert configured_homes[0] != configured_homes[1]
