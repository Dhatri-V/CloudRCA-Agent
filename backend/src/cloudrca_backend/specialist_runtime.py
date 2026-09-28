"""Shared, isolated JCode/Qwen runtime for future specialist agents."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Protocol
from uuid import uuid4

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from cloudrca_compatibility.jcode_smoke import ProviderSpec, SmokeError, configure_provider

from .contracts import Finding, Layer
from .routing import RoutedEvent, RoutingLayer
from .settings import Settings, SettingsError


class SpecialistFailureCode(StrEnum):
    CONFIGURATION = "configuration_failure"
    BATCH_LIMIT = "batch_limit_failure"
    CONTEXT_LIMIT = "context_limit_failure"
    TIMEOUT = "timeout"
    PROVIDER = "provider_or_jcode_failure"
    INVALID_OUTPUT = "invalid_structured_output"


class TokenUsage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    input_tokens: Annotated[int, Field(ge=0)] | None = None
    output_tokens: Annotated[int, Field(ge=0)] | None = None

    def plus(self, other: TokenUsage) -> TokenUsage:
        """Add available usage fields without turning unavailable values into zero."""

        def total(left: int | None, right: int | None) -> int | None:
            return None if left is None and right is None else (left or 0) + (right or 0)

        return TokenUsage(
            input_tokens=total(self.input_tokens, other.input_tokens),
            output_tokens=total(self.output_tokens, other.output_tokens),
        )


class SpecialistRuntimeConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    jcode_binary: str = Field(min_length=1)
    provider_profile: str = Field(min_length=1)
    provider_base_url: str = Field(min_length=1)
    model: str = Field(min_length=1)
    api_key_env: str | None = None
    timeout_seconds: Annotated[float, Field(gt=0)]
    max_events: Annotated[int, Field(gt=0)]
    max_prompt_characters: Annotated[int, Field(gt=0)]
    max_repair_attempts: Annotated[int, Field(ge=0, le=3)]
    prompt_version: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def remote_provider_requires_credential_name(self) -> SpecialistRuntimeConfig:
        local = self.provider_base_url.startswith(("http://localhost", "http://127.0.0.1"))
        if not local and not self.api_key_env:
            raise ValueError("remote Qwen provider requires CLOUDRCA_QWEN_API_KEY_ENV")
        return self


class SpecialistRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    layer: Layer
    events: tuple[RoutedEvent, ...] = Field(min_length=1)
    correlation_id: str | None = Field(default=None, min_length=1, max_length=256)


class JCodeResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    text: str
    session_id: str
    model: str
    usage: TokenUsage = Field(default_factory=TokenUsage)


class SpecialistExecutionMetadata(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    correlation_id: str
    specialist_layer: Layer
    provider_profile: str
    model: str
    prompt_version: str
    duration_ms: Annotated[float, Field(ge=0)]
    repair_count: Annotated[int, Field(ge=0)]
    success: bool
    session_id: str | None = None
    usage: TokenUsage = Field(default_factory=TokenUsage)
    error_code: SpecialistFailureCode | None = None
    error_message: str | None = None


class SpecialistRunResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    finding: Finding
    metadata: SpecialistExecutionMetadata


class SpecialistRuntimeError(RuntimeError):
    """Typed specialist failure with complete, safe execution metadata."""

    def __init__(self, code: SpecialistFailureCode, message: str, metadata: SpecialistExecutionMetadata) -> None:
        super().__init__(message)
        self.code = code
        self.metadata = metadata


class JCodeTimeoutError(TimeoutError):
    """Raised by a JCode session when a configured deadline expires."""


class JCodeProviderError(RuntimeError):
    """Raised when JCode or its configured provider fails."""


class JCodeConfigurationError(RuntimeError):
    """Raised when an isolated JCode provider cannot be configured."""


class JCodeSession(Protocol):
    def complete(self, prompt: str, timeout_seconds: float) -> JCodeResponse:
        """Run one turn inside this isolated specialist session."""
        ...


class JCodeTransport(Protocol):
    def session(
        self, config: SpecialistRuntimeConfig, correlation_id: str
    ) -> AbstractContextManager[JCodeSession]:
        """Create one isolated JCode session lifecycle."""
        ...


class EventLogger(Protocol):
    def info(self, event: str, **values: Any) -> Any:
        """Record a structured, secret-safe runtime event."""
        ...


def _required(values: Mapping[str, str], name: str) -> str:
    value = values.get(name, "").strip()
    if not value:
        raise SettingsError(f"Missing required specialist configuration: {name}")
    return value


def _integer(values: Mapping[str, str], name: str, default: int) -> int:
    raw = values.get(name, str(default)).strip()
    try:
        return int(raw)
    except ValueError as exc:
        raise SettingsError(f"Invalid integer specialist configuration: {name}={raw!r}") from exc


def _number(values: Mapping[str, str], name: str, default: float) -> float:
    raw = values.get(name, str(default)).strip()
    try:
        return float(raw)
    except ValueError as exc:
        raise SettingsError(f"Invalid numeric specialist configuration: {name}={raw!r}") from exc


def load_specialist_runtime_config(
    settings: Settings,
    environ: Mapping[str, str] | None = None,
) -> SpecialistRuntimeConfig:
    """Load typed Qwen/JCode settings without retaining credential values."""
    values = os.environ if environ is None else environ
    api_key_env = values.get("CLOUDRCA_QWEN_API_KEY_ENV", "").strip() or None
    try:
        config = SpecialistRuntimeConfig(
            jcode_binary=settings.jcode_binary,
            provider_profile=_required(values, "CLOUDRCA_QWEN_PROVIDER_PROFILE"),
            provider_base_url=_required(values, "CLOUDRCA_QWEN_BASE_URL"),
            model=_required(values, "CLOUDRCA_QWEN_MODEL"),
            api_key_env=api_key_env,
            timeout_seconds=_number(values, "CLOUDRCA_SPECIALIST_TIMEOUT_SECONDS", 60.0),
            max_events=_integer(values, "CLOUDRCA_SPECIALIST_MAX_EVENTS", 100),
            max_prompt_characters=_integer(values, "CLOUDRCA_SPECIALIST_MAX_PROMPT_CHARACTERS", 50_000),
            max_repair_attempts=_integer(values, "CLOUDRCA_SPECIALIST_MAX_REPAIR_ATTEMPTS", 1),
            prompt_version=values.get("CLOUDRCA_SPECIALIST_PROMPT_VERSION", "specialist-v1").strip(),
        )
    except ValidationError as exc:
        raise SettingsError(f"Invalid specialist configuration: {exc}") from exc
    if config.api_key_env and not values.get(config.api_key_env, "").strip():
        raise SettingsError(f"Missing provider credential environment variable: {config.api_key_env}")
    return config


def _event_context(routed: RoutedEvent) -> dict[str, Any]:
    event = routed.event
    return {
        "event_id": event.event_id,
        "timestamp": event.timestamp.isoformat().replace("+00:00", "Z"),
        "source": event.source,
        "service": event.service,
        "severity": event.severity.value,
        "message": event.message,
        "component_ids": {
            "host_id": event.host_id,
            "vm_id": event.vm_id,
            "pod_id": event.pod_id,
            "database_id": event.database_id,
        },
        "provenance": {
            "kind": event.provenance.kind.value,
            "source": event.provenance.source,
            "record_id": event.provenance.record_id,
        },
        "routing": {
            "layer": routed.routing.layer.value,
            "confidence": routed.routing.confidence,
            "explanation": routed.routing.explanation,
        },
        "topology": {
            "relationships": [item.model_dump(mode="json") for item in routed.topology.relationships],
            "unavailable": [item.model_dump(mode="json") for item in routed.topology.unavailable],
        },
    }


def build_specialist_prompt(request: SpecialistRequest, prompt_version: str) -> str:
    """Build a stable prompt without including raw source records or secrets."""
    payload = {
        "specialist_layer": request.layer.value,
        "events": [_event_context(item) for item in request.events],
    }
    schema = Finding.model_json_schema(mode="validation")
    sections = (
        f"CLOUDRCA_SPECIALIST_PROMPT_VERSION={prompt_version}",
        "Analyze only the supplied specialist layer. Do not invent evidence.",
        "Return exactly one JSON object that validates against FINDING_SCHEMA.",
        f"FINDING_SCHEMA={json.dumps(schema, sort_keys=True, separators=(',', ':'))}",
        f"INPUT={json.dumps(payload, sort_keys=True, separators=(',', ':'))}",
    )
    return "\n".join(sections)


def _repair_prompt(
    original_prompt: str,
    invalid_response: str,
    error: str,
    prompt_version: str,
) -> str:
    return "\n".join(
        (
            f"CLOUDRCA_SPECIALIST_PROMPT_VERSION={prompt_version}",
            "Repair the previous response. Return only a corrected JSON object.",
            f"VALIDATION_ERROR={error}",
            f"INVALID_RESPONSE={invalid_response}",
            original_prompt,
        )
    )


def _allowed_evidence(request: SpecialistRequest) -> set[str]:
    allowed = {item.event.event_id for item in request.events}
    for item in request.events:
        allowed.update(relationship.relationship_id for relationship in item.topology.relationships)
    return allowed


def _validate_finding(raw: str, request: SpecialistRequest) -> Finding:
    try:
        finding = Finding.model_validate_json(raw)
    except ValidationError as exc:
        safe_errors = exc.errors(include_input=False, include_url=False)
        raise ValueError(json.dumps(safe_errors, sort_keys=True, separators=(",", ":"))) from exc
    if finding.layer is not request.layer:
        raise ValueError(f"finding layer {finding.layer.value} does not match specialist {request.layer.value}")
    event_ids = {item.event.event_id for item in request.events}
    if not set(finding.event_ids).issubset(event_ids):
        raise ValueError("finding references event IDs outside the specialist input")
    allowed_evidence = _allowed_evidence(request)
    if any(item.reference_id not in allowed_evidence for item in finding.evidence):
        raise ValueError("finding references evidence outside the specialist input")
    return finding


def _routing_layer(layer: Layer) -> RoutingLayer:
    return RoutingLayer(layer.value)


def _validate_request_routes(request: SpecialistRequest) -> None:
    expected = _routing_layer(request.layer)
    invalid = [
        item.event.event_id
        for item in request.events
        if item.routing.layer is not expected
        and not (
            item.routing.layer is RoutingLayer.AMBIGUOUS
            and request.layer in item.routing.candidate_layers
        )
    ]
    if invalid:
        raise ValueError(f"events are not routed to {request.layer.value}: {', '.join(invalid)}")


class _JCodeCliSession:
    def __init__(self, config: SpecialistRuntimeConfig, home: Path) -> None:
        self._config = config
        self._home = home

    def complete(self, prompt: str, timeout_seconds: float) -> JCodeResponse:
        command = [
            self._config.jcode_binary,
            "--no-update",
            "--socket",
            str(self._home / "jcode.sock"),
            "--provider-profile",
            self._config.provider_profile,
            "--model",
            self._config.model,
            "--tool-profile",
            "none",
            "run",
            "--json",
            prompt,
        ]
        environment = os.environ.copy()
        environment.update({"JCODE_HOME": str(self._home), "JCODE_NO_TELEMETRY": "1"})
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                check=False,
                env=environment,
                text=True,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise JCodeTimeoutError(f"JCode timed out after {timeout_seconds:g}s") from exc
        except OSError as exc:
            raise JCodeConfigurationError(f"JCode executable could not start: {exc}") from exc
        if completed.returncode:
            detail = (completed.stderr or completed.stdout).strip()
            raise JCodeProviderError(f"JCode exited {completed.returncode}: {detail}")
        try:
            envelope = json.loads(completed.stdout)
            text = envelope["text"]
            if not isinstance(text, str):
                raise TypeError("JCode text field must be a string")
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise JCodeProviderError("JCode returned a malformed response envelope") from exc
        usage = envelope.get("usage", {})
        if not isinstance(usage, dict):
            usage = {}
        return JCodeResponse(
            text=text,
            session_id=str(envelope.get("session_id", "")),
            model=str(envelope.get("model", self._config.model)),
            usage=TokenUsage(
                input_tokens=usage.get("input_tokens") if type(usage.get("input_tokens")) is int else None,
                output_tokens=usage.get("output_tokens") if type(usage.get("output_tokens")) is int else None,
            ),
        )


class JCodeCliTransport:
    """Real JCode v0.88-compatible CLI transport with one temporary home per run."""

    @contextmanager
    def session(self, config: SpecialistRuntimeConfig, correlation_id: str) -> Iterator[JCodeSession]:
        del correlation_id  # correlation is logged by the runtime, never added to provider configuration
        with tempfile.TemporaryDirectory(prefix="cloudrca-specialist-") as temporary:
            home = Path(temporary)
            spec = ProviderSpec(
                name=config.provider_profile,
                base_url=config.provider_base_url,
                model=config.model,
                api_key_env=config.api_key_env,
            )
            try:
                configure_provider(Path(config.jcode_binary), home, spec, config.timeout_seconds)
            except SmokeError as exc:
                message = str(exc)
                if "timed out" in message:
                    raise JCodeTimeoutError(message) from exc
                if "credential" in message or "remote providers require" in message:
                    raise JCodeConfigurationError(message) from exc
                raise JCodeProviderError(message) from exc
            except OSError as exc:
                raise JCodeConfigurationError(f"JCode executable could not start: {exc}") from exc
            try:
                yield _JCodeCliSession(config, home)
            finally:
                environment = os.environ.copy()
                environment.update({"JCODE_HOME": str(home), "JCODE_NO_TELEMETRY": "1"})
                try:
                    subprocess.run(
                        [config.jcode_binary, "--socket", str(home / "jcode.sock"), "server", "stop"],
                        capture_output=True,
                        check=False,
                        env=environment,
                        text=True,
                        timeout=min(config.timeout_seconds, 5),
                    )
                except (OSError, subprocess.TimeoutExpired):
                    pass


class SpecialistRuntime:
    """Execute any one specialist layer through an isolated Qwen/JCode session."""

    def __init__(
        self,
        config: SpecialistRuntimeConfig,
        transport: JCodeTransport,
        *,
        logger: EventLogger | None = None,
        clock: Callable[[], float] = time.perf_counter,
        correlation_id_factory: Callable[[], str] = lambda: str(uuid4()),
    ) -> None:
        self._config = config
        self._transport = transport
        self._logger = logger or structlog.get_logger("cloudrca.specialist")
        self._clock = clock
        self._correlation_id_factory = correlation_id_factory

    def _metadata(
        self,
        request: SpecialistRequest,
        correlation_id: str,
        start: float,
        *,
        success: bool,
        repair_count: int,
        usage: TokenUsage,
        session_id: str | None,
        error_code: SpecialistFailureCode | None = None,
        error_message: str | None = None,
    ) -> SpecialistExecutionMetadata:
        return SpecialistExecutionMetadata(
            correlation_id=correlation_id,
            specialist_layer=request.layer,
            provider_profile=self._config.provider_profile,
            model=self._config.model,
            prompt_version=self._config.prompt_version,
            duration_ms=max(0.0, (self._clock() - start) * 1000),
            repair_count=repair_count,
            success=success,
            session_id=session_id,
            usage=usage,
            error_code=error_code,
            error_message=error_message,
        )

    def _fail(
        self,
        request: SpecialistRequest,
        correlation_id: str,
        start: float,
        code: SpecialistFailureCode,
        message: str,
        *,
        repair_count: int = 0,
        usage: TokenUsage | None = None,
        session_id: str | None = None,
    ) -> SpecialistRuntimeError:
        metadata = self._metadata(
            request,
            correlation_id,
            start,
            success=False,
            repair_count=repair_count,
            usage=usage or TokenUsage(),
            session_id=session_id,
            error_code=code,
            error_message=message,
        )
        self._logger.info(
            "specialist_run_failed",
            **metadata.model_dump(mode="json"),
        )
        return SpecialistRuntimeError(code, message, metadata)

    def run(self, request: SpecialistRequest) -> SpecialistRunResult:
        """Run one specialist request, bounded repair, and Finding validation."""
        correlation_id = request.correlation_id or self._correlation_id_factory()
        start = self._clock()
        usage = TokenUsage()
        repair_count = 0
        session_id: str | None = None
        self._logger.info(
            "specialist_run_started",
            correlation_id=correlation_id,
            specialist_layer=request.layer.value,
            provider_profile=self._config.provider_profile,
            model=self._config.model,
            prompt_version=self._config.prompt_version,
            event_count=len(request.events),
        )
        if len(request.events) > self._config.max_events:
            raise self._fail(
                request,
                correlation_id,
                start,
                SpecialistFailureCode.BATCH_LIMIT,
                f"event batch has {len(request.events)} events; maximum is {self._config.max_events}",
            )
        try:
            _validate_request_routes(request)
        except ValueError as exc:
            raise self._fail(
                request, correlation_id, start, SpecialistFailureCode.CONFIGURATION, str(exc)
            ) from exc
        prompt = build_specialist_prompt(request, self._config.prompt_version)
        if len(prompt) > self._config.max_prompt_characters:
            raise self._fail(
                request,
                correlation_id,
                start,
                SpecialistFailureCode.CONTEXT_LIMIT,
                f"prompt has {len(prompt)} characters; maximum is {self._config.max_prompt_characters}",
            )
        try:
            with self._transport.session(self._config, correlation_id) as session:
                current_prompt = prompt
                while True:
                    response = session.complete(current_prompt, self._config.timeout_seconds)
                    session_id = response.session_id
                    usage = usage.plus(response.usage)
                    try:
                        finding = _validate_finding(response.text, request)
                        break
                    except (ValueError, json.JSONDecodeError) as exc:
                        if repair_count >= self._config.max_repair_attempts:
                            raise self._fail(
                                request,
                                correlation_id,
                                start,
                                SpecialistFailureCode.INVALID_OUTPUT,
                                f"structured Finding remained invalid after {repair_count} repairs: {exc}",
                                repair_count=repair_count,
                                usage=usage,
                                session_id=session_id,
                            ) from exc
                        repair_count += 1
                        current_prompt = _repair_prompt(
                            prompt,
                            response.text,
                            str(exc),
                            self._config.prompt_version,
                        )
                        if len(current_prompt) > self._config.max_prompt_characters:
                            raise self._fail(
                                request,
                                correlation_id,
                                start,
                                SpecialistFailureCode.CONTEXT_LIMIT,
                                "repair prompt exceeds configured context limit",
                                repair_count=repair_count,
                                usage=usage,
                                session_id=session_id,
                            )
        except SpecialistRuntimeError:
            raise
        except JCodeTimeoutError as exc:
            raise self._fail(
                request,
                correlation_id,
                start,
                SpecialistFailureCode.TIMEOUT,
                str(exc),
                repair_count=repair_count,
                usage=usage,
                session_id=session_id,
            ) from exc
        except JCodeConfigurationError as exc:
            raise self._fail(
                request,
                correlation_id,
                start,
                SpecialistFailureCode.CONFIGURATION,
                str(exc),
                repair_count=repair_count,
                usage=usage,
                session_id=session_id,
            ) from exc
        except JCodeProviderError as exc:
            raise self._fail(
                request,
                correlation_id,
                start,
                SpecialistFailureCode.PROVIDER,
                str(exc),
                repair_count=repair_count,
                usage=usage,
                session_id=session_id,
            ) from exc
        metadata = self._metadata(
            request,
            correlation_id,
            start,
            success=True,
            repair_count=repair_count,
            usage=usage,
            session_id=session_id,
        )
        self._logger.info("specialist_run_completed", **metadata.model_dump(mode="json"))
        return SpecialistRunResult(finding=finding, metadata=metadata)
