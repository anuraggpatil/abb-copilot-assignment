"""The acceptance scenario, defined once.

The assignment names one question that must work, and two test layers exercise it: the
orchestration tests in `tests/integration/test_orchestrator.py`, which drive `CopilotService`
directly, and the end-to-end test in `tests/e2e/`, which drives the HTTP surface the GUI uses.
Both need the same scripted investigation, so it lives here rather than in two copies that can
drift apart — a second copy is how an "end-to-end" test ends up asserting a different scenario
from the one it shares a name with.

The scripted turns read the ids and references they use **out of the conversation** rather than
from a fixture. That is the point of the script: a plan built from `bfp101_id` would pass even if
the orchestrator never fed a tool result back to the model, which is the one thing a chaining
test has to prove.
"""

from __future__ import annotations

import re

from apps.backend.llm.provider import LLMMessage, LLMResponse
from apps.backend.llm.scripted import Turn, answer_turn, finish_turn, plan_turn

#: The assignment's own wording. Kept verbatim, because the thing being demonstrated is that this
#: sentence works — not a convenient paraphrase of it.
QUESTION = (
    "Investigate recurring high-severity alarms for Boiler Feed Pump 101 over the last 90 days, "
    "identify likely contributing factors, retrieve the relevant operating procedure, and "
    "provide recommended actions with source evidence."
)

ASSET_ID = re.compile(r"AST-[A-Z0-9-]+")
# Tool results reach the conversation as JSON, so a reference is whatever sits between quotes.
# Matching it unquoted would run past the end of the value and into the rest of the payload.
REFERENCE = re.compile(r"\"([A-Z][A-Z0-9-]{2,} §[^\"]+)\"")
# `reference="OP-BFP-101 §4.2 Low suction pressure response"` in the synthesis prompt, reduced
# to the `DOC §N` form an answer's citation marker uses.
OFFERED_SECTION = re.compile(r"reference=\"([A-Z][A-Z0-9-]{2,} §[\d.]+)")


def conversation_text(messages: list[LLMMessage]) -> str:
    return "\n".join(message.content for message in messages if message.role == "tool")


def asset_id_from(messages: list[LLMMessage]) -> str:
    found = ASSET_ID.search(conversation_text(messages))
    assert found, "no asset id came back from search_assets — the chain is broken"
    return found.group(0)


def sections_offered(messages: list[LLMMessage]) -> list[str]:
    """`DOC §N` for each passage the synthesis prompt presented, in order, deduplicated."""
    prompt = "\n".join(message.content for message in messages)
    seen: list[str] = []
    for reference in OFFERED_SECTION.findall(prompt):
        if reference not in seen:
            seen.append(reference)
    return seen


def acceptance_script() -> list[Turn]:
    """Resolve the asset, characterise the pattern, get recommendations, retrieve the procedure."""

    def investigate(messages: list[LLMMessage]) -> LLMResponse:
        asset_id = asset_id_from(messages)
        return plan_turn(
            (
                "get_recurring_alarms",
                {"asset_id": asset_id, "min_severity": "high", "lookback_days": 90},
            ),
            ("get_operator_recommendations", {"asset_id": asset_id}),
            thought="characterise the pattern and see what the API recommends",
        )

    def retrieve(messages: list[LLMMessage]) -> LLMResponse:
        references = REFERENCE.findall(conversation_text(messages))
        assert references, "the recommendations carried no procedure references to follow"
        return plan_turn(
            (
                "search_procedures",
                {
                    "query": (
                        "low suction pressure response and bearing lubrication for a boiler "
                        "feed pump with recurring cavitation alarms"
                    ),
                    "references": references[:10],
                    "top_k": 8,
                },
            ),
            thought="retrieve the sections the recommendations cited",
        )

    def write_answer(messages: list[LLMMessage]) -> LLMResponse:
        # Cites what it was shown, the way an instructed model is meant to: the synthesis prompt
        # carries `reference="…"` for each passage, and the answer quotes those back. Hardcoding
        # a section here would make the citation check assert against the test's opinion of what
        # was retrieved rather than against what actually was.
        cited = sections_offered(messages)[:2]
        assert cited, "synthesis was given no passages to cite"
        return answer_turn(
            "Recurring suction-pressure and vibration alarms on this pump point to cavitation. "
            + " ".join(f"Follow the documented response [{section}]." for section in cited)
        )

    return [
        plan_turn(
            ("search_assets", {"query": "Boiler Feed Pump 101"}),
            thought="resolve the asset name to an id",
        ),
        investigate,
        retrieve,
        finish_turn(),
        write_answer,
    ]
