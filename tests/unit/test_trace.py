"""The trace: redaction, bounds, ordering.

Redaction gets the most attention here because it is a security property and because it is the
kind of property that silently stops holding. Nothing about a payload's shape tells you a
credential is in it, so these tests assert the rule (any key that looks like a secret) rather
than the instances (this field in that tool's result), and they assert it at depth and inside
lists — which is exactly where a rule implemented with a shallow loop passes the easy test and
publishes the token anyway.
"""

from __future__ import annotations

from typing import Any

import pytest

from apps.backend.tracing.trace import (
    MAX_FIELD_CHARS,
    MAX_LIST_ITEMS,
    REDACTED,
    TraceRecorder,
    TraceStore,
    redact,
)

#: Shaped like a credential but matching no real provider's format on purpose, so
#: `scripts/check_secrets.sh` does not have to special-case this file.
SECRET = "not-a-real-key-0123456789abcdef"


class TestRedaction:
    @pytest.mark.parametrize(
        "key",
        [
            "token",
            "GEMINI_API_KEY",
            "alarm_api_token",
            "Authorization",
            "api_key",
            "apiKey",
            "api-key",
            # The header the Gemini provider actually sends the key in.
            "x-goog-api-key",
            "password",
            "passwd",
            "client_secret",
            "credentials",
            "bearer",
            "Cookie",
        ],
    )
    def test_anything_that_looks_like_a_credential_is_removed(self, key: str) -> None:
        assert redact({key: SECRET})[key] == REDACTED

    def test_ordinary_fields_are_untouched(self) -> None:
        payload = {"asset_id": "AST-PMP-0001", "severity": "high", "occurrences": 14}

        assert redact(payload) == payload

    def test_a_credential_nested_in_a_tool_result_is_removed(self) -> None:
        # The realistic shape: a tool echoes the request it made, headers and all.
        payload = {
            "upstream": [
                {
                    "method": "GET",
                    "path": "/alarms",
                    "headers": {"Authorization": f"Bearer {SECRET}", "x-client-id": "copilot"},
                }
            ]
        }

        redacted = redact(payload)

        assert redacted["upstream"][0]["headers"]["Authorization"] == REDACTED
        assert redacted["upstream"][0]["headers"]["x-client-id"] == "copilot"
        assert SECRET not in str(redacted)

    def test_a_credential_inside_a_list_of_dicts_is_removed(self) -> None:
        payload = {"calls": [{"api_key": SECRET}, {"api_key": SECRET}]}

        assert SECRET not in str(redact(payload))

    def test_long_strings_are_truncated_so_a_trace_cannot_carry_a_document(self) -> None:
        body = "x" * (MAX_FIELD_CHARS * 3)

        redacted = redact({"text": body})["text"]

        assert len(redacted) < len(body)
        assert "chars omitted" in redacted

    def test_long_lists_are_capped(self) -> None:
        redacted = redact({"chunks": list(range(MAX_LIST_ITEMS * 2))})["chunks"]

        assert len(redacted) == MAX_LIST_ITEMS + 1
        assert "more items omitted" in redacted[-1]

    def test_pathological_nesting_terminates(self) -> None:
        payload: dict[str, object] = {"level": 0}
        deepest = payload
        for level in range(1, 30):
            child: dict[str, object] = {"level": level}
            deepest["child"] = child
            deepest = child

        # The assertion is simply that this returns. A recursive walk with no depth limit is a
        # stack overflow in a request handler, served as a 500 with no explanation.
        assert "truncated" in str(redact(payload))


class TestStore:
    async def test_events_are_grouped_by_conversation(self) -> None:
        store = TraceStore()
        recorder_a = TraceRecorder(store, conversation_id="conv-a")
        recorder_b = TraceRecorder(store, conversation_id="conv-b")

        await recorder_a.record("plan", "step 1")
        await recorder_b.record("plan", "step 1")
        await recorder_a.record("plan", "step 2")

        trace_a = store.get("conv-a")
        assert trace_a is not None
        assert [event.name for event in trace_a.events] == ["step 1", "step 2"]

    async def test_the_oldest_conversation_is_evicted(self) -> None:
        store = TraceStore(max_conversations=2)

        for index in range(3):
            await TraceRecorder(store, conversation_id=f"conv-{index}").record("plan", "step")

        # Bounded because this store lives in a long-running process. Unbounded it is a leak
        # that presents as the backend slowly getting killed by the OS.
        assert store.conversation_ids() == ["conv-1", "conv-2"]

    def test_an_unknown_conversation_is_absent_rather_than_empty(self) -> None:
        # The API returns 404 for this, which is only correct if "no such conversation" and
        # "a conversation with no events" are distinguishable here.
        assert TraceStore().get("conv-nope") is None


class TestRecorder:
    async def test_sequence_numbers_are_assigned_in_order(self) -> None:
        store = TraceStore()
        recorder = TraceRecorder(store, conversation_id="conv-1")

        for name in ("a", "b", "c"):
            await recorder.record("plan", name)

        trace = store.get("conv-1")
        assert trace is not None
        assert [event.seq for event in trace.events] == [1, 2, 3]

    async def test_every_event_carries_the_ids_that_tie_it_to_the_api_log(self) -> None:
        store = TraceStore()
        recorder = TraceRecorder(store, conversation_id="conv-1", trace_id="trc-fixed")

        event = await recorder.record("mcp_tool_call", "get_alarms")

        # `trace_id` is propagated to the MCP server and echoed by the alarm API, so this is the
        # value that makes one row in the panel findable in the API's own log.
        assert event.trace_id == "trc-fixed"
        assert event.request_id.startswith("req-")
        assert event.conversation_id == "conv-1"

    async def test_the_sink_sees_events_as_they_happen(self) -> None:
        seen: list[str] = []
        recorder = TraceRecorder(
            TraceStore(),
            conversation_id="conv-1",
            sink=lambda event: _append(seen, event.name),
        )

        await recorder.record("plan", "first")
        await recorder.record("plan", "second")

        # Awaited rather than fired-and-forgotten: an out-of-order trace panel is worse than a
        # slow one, since the order *is* the information.
        assert seen == ["first", "second"]

    async def test_the_sink_receives_redacted_events(self) -> None:
        captured: list[dict[str, object]] = []
        recorder = TraceRecorder(
            TraceStore(),
            conversation_id="conv-1",
            sink=lambda event: _append(captured, event.detail),
        )

        await recorder.record("mcp_tool_call", "get_alarms", detail={"token": SECRET})

        # The SSE stream is a second exit for trace data. Redacting on the way *in* is what
        # makes both exits safe with one rule.
        assert SECRET not in str(captured)


async def _append(target: list[Any], value: object) -> None:
    target.append(value)
