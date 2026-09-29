"""Ranked operator actions — the advanced operation, and the handoff to the procedure corpus.

This is the tool the whole workflow converges on. It is "advanced" in the sense the
assignment means: the alarm API derives the ranking from recurrence, severity trend, ack
behaviour and neighbouring equipment, so the result is analysis rather than a record lookup.

It is also where MCP stops and RAG starts. Every action names a `procedure_reference` such
as `OP-BFP-101 §4.2 Low suction pressure response`, and the de-duplicated
`procedure_references` list is what the copilot feeds to `search_procedures`. That is the
seam that makes this one workflow instead of two demonstrations: the retrieval query is
*derived from* the MCP result, and cannot be constructed without it.

Accepting either an `alarm_id` or an `asset_id` is deliberate. "What should I do about this
alarm" and "what should I do about this pump" are both real questions, and the API resolves
the asset form to its most significant open alarm.
"""

from __future__ import annotations

from typing import Annotated

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from alarm_management.context import tool_session
from alarm_management.schemas import (
    Escalation,
    OperatorAction,
    OperatorRecommendationsResult,
)

DESCRIPTION = """\
Get ranked, actionable recommendations for an alarm, derived from its history on the asset:
what to do first, why, how urgent it is, whether the plant must be isolated, and whether the
alarm needs escalating.

Supply either `alarm_id` (from get_alarms) or `asset_id` (from search_assets); with an asset
the most significant open alarm is used. Every action cites a procedure section, and the
`procedure_references` list should be passed to search_procedures so the written procedure
can be quoted alongside the recommendation rather than paraphrased from memory.
"""


def register(server: MCPServer) -> None:
    @server.tool(
        name="get_operator_recommendations",
        title="Recommend operator actions",
        description=DESCRIPTION,
    )
    async def get_operator_recommendations(
        ctx: Context,
        alarm_id: Annotated[
            str | None, Field(description="A specific alarm, from get_alarms")
        ] = None,
        asset_id: Annotated[
            str | None,
            Field(description="An asset, from search_assets; its top open alarm is used"),
        ] = None,
        lookback_days: Annotated[
            int,
            Field(ge=1, le=400, description="History window the ranking is derived from"),
        ] = 90,
        max_recommendations: Annotated[
            int, Field(ge=1, le=20, description="How many ranked actions to return")
        ] = 6,
    ) -> OperatorRecommendationsResult:
        if not alarm_id and not asset_id:
            # Caught here rather than upstream so the message names the tool's own
            # parameters instead of the API's request body.
            raise ToolError(
                "Supply either alarm_id or asset_id. Use get_alarms for an alarm_id, or "
                "search_assets for an asset_id."
            )

        async with tool_session(ctx, "get_operator_recommendations") as session:
            actions = await session.client.operator_actions(
                {
                    **({"alarm_id": alarm_id} if alarm_id else {}),
                    **({"asset_id": asset_id} if asset_id else {}),
                    "lookback_days": lookback_days,
                    "max_recommendations": max_recommendations,
                },
                trace=session.trace,
            )
            return OperatorRecommendationsResult(
                alarm_id=actions.alarm_id,
                alarm_name=actions.alarm_name,
                asset_id=actions.asset_id,
                asset_name=actions.asset_name,
                severity=actions.severity,
                status=actions.status,
                unit=actions.unit,
                context=actions.context,
                actions=[OperatorAction.from_wire(rec) for rec in actions.recommendations],
                escalation=Escalation(
                    required=actions.escalation.required,
                    reason=actions.escalation.reason,
                    escalate_to=actions.escalation.escalate_to,
                    asset_criticality=actions.escalation.asset_criticality,
                ),
                safety_notes=actions.safety_notes,
                procedure_references=actions.procedure_references,
                disclaimer=actions.disclaimer,
                upstream=session.upstream(),
                trace_id=session.trace_id,
            )
