"""Regression: real Qwen (qwen2.5-coder:7b via JCode) wraps valid JSON replies in a Markdown code fence."""

from __future__ import annotations

import json
from typing import Any

import pytest
from cloudrca_backend.api import ArtifactKind, JobStatus
from cloudrca_backend.pipeline import RcaPipeline
from cloudrca_backend.routing import load_routing_config
from cloudrca_backend.specialist_runtime import unwrap_code_fence

# Verbatim reply captured from a live A1 call on the AIOps2025 fixture.
OBSERVED_QWEN_REPLY = '```json\n{\n  "findings": []\n}\n```'


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        (OBSERVED_QWEN_REPLY, '{\n  "findings": []\n}'),
        ('```\n{"ok": true}\n```', '{"ok": true}'),
        ('  ```JSON\r\n{"ok": true}\r\n```  \n', '{"ok": true}'),
        ('{"ok": true}', '{"ok": true}'),
    ],
)
def test_whole_reply_fence_is_removed(reply: str, expected: str) -> None:
    assert unwrap_code_fence(reply) == expected


@pytest.mark.parametrize(
    "reply",
    [
        'Here is the JSON:\n```json\n{"ok": true}\n```',
        '```json\n{"ok": true}\n```\nLet me know if you need more.',
        '```json\n{"ok": true}',
        '{"ok": true}\n```',
    ],
)
def test_partial_or_prose_wrapped_replies_are_left_to_fail_validation(reply: str) -> None:
    assert unwrap_code_fence(reply) == reply


def fenced(text: str) -> str:
    return f"```json\n{text}\n```"


def test_fenced_specialist_and_rca_replies_complete_the_pipeline(aiops_jsonl: bytes, scripted_jcode: Any, agents_for: Any) -> None:
    class FencedJCode(scripted_jcode):  # type: ignore[misc, valid-type]
        def respond(self, prompt: str) -> str:
            return fenced(super().respond(prompt))

    transport = FencedJCode()
    routing = load_routing_config("config/routing/aiops2025.yaml")
    result = RcaPipeline(lambda _: ("aiops.jsonl", aiops_jsonl), lambda: agents_for(transport), routing)({"dataset_id": "d"})
    assert result.status is JobStatus.COMPLETED
    reports = [payload for kind, payload in result.artifacts if kind is ArtifactKind.REPORT]
    assert len(reports) == 2
    # No repair rounds were needed: one call per specialist layer and per reported incident.
    assert len(transport.prompts["main_rca"]) == 2


def test_prose_wrapped_reply_still_fails_closed(aiops_jsonl: bytes, scripted_jcode: Any, agents_for: Any) -> None:
    prose = "Here is the analysis:\n" + fenced(json.dumps({"findings": []}))
    transport = scripted_jcode({"database": [prose] * 4, "vm": [prose] * 2})
    routing = load_routing_config("config/routing/aiops2025.yaml")
    result = RcaPipeline(lambda _: ("aiops.jsonl", aiops_jsonl), lambda: agents_for(transport), routing)({"dataset_id": "d"})
    assert result.status is JobStatus.FAILED
    outcomes = {payload["analysis_outcome"] for kind, payload in result.artifacts if kind is ArtifactKind.INCIDENT}
    assert outcomes == {"specialist_failed"}
