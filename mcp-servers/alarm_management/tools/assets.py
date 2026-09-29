"""Asset resolution: the first step of every investigation.

An operator asks about "Boiler Feed Pump 101"; every other tool needs an `asset_id`. This
tool is the bridge, and its description says so explicitly, because a planner that skips it
will pass a human-readable name where an id is required and get a 404 it cannot fix.

It makes two upstream calls on purpose. Resolving a name is almost never the goal in itself
— the next question is always "what is this thing and what is it connected to" — so the
metadata for the best match is folded into the same result. One MCP round trip instead of
two is a real saving when every round trip costs a model turn.
"""

from __future__ import annotations

from typing import Annotated

from mcp.server.mcpserver import Context, MCPServer
from pydantic import Field

from alarm_management.context import tool_session
from alarm_management.schemas import AssetMatch, AssetSearchResult, RelatedAsset

DESCRIPTION = """\
Resolve a plant asset from a name, tag or description into the `asset_id` that every other
alarm tool requires. Always call this first when the operator names equipment in words.

Returns the ranked matches plus, for the best match, its plant tag, criticality, live alarm
count and the equipment it is connected to — the neighbours worth checking when looking for
a shared root cause.
"""


def register(server: MCPServer) -> None:
    @server.tool(name="search_assets", title="Search plant assets", description=DESCRIPTION)
    async def search_assets(
        ctx: Context,
        query: Annotated[
            str,
            Field(
                min_length=1,
                max_length=200,
                description="Asset name, plant tag or free text, e.g. 'Boiler Feed Pump 101'",
            ),
        ],
        limit: Annotated[int, Field(ge=1, le=25, description="Maximum matches to return")] = 5,
        unit: Annotated[
            str | None, Field(description="Restrict to one process unit, e.g. 'Unit 2'")
        ] = None,
        site: Annotated[
            str | None, Field(description="Restrict to one site, e.g. 'EastRefinery'")
        ] = None,
    ) -> AssetSearchResult:
        async with tool_session(ctx, "search_assets") as session:
            found = await session.client.search_assets(
                query, limit=limit, unit=unit, site=site, trace=session.trace
            )
            matches = [AssetMatch.from_wire(asset) for asset in found.results]

            result = AssetSearchResult(
                query=found.query or query,
                total_matches=found.total_matches,
                assets=matches,
                upstream=session.upstream(),
                trace_id=session.trace_id,
            )
            if not matches:
                # No second call, and no invented fields: an empty search is a legitimate
                # answer, and the planner needs to see it as one so it can rephrase.
                return result

            metadata = await session.client.asset_metadata(matches[0].asset_id, trace=session.trace)
            result.best_match_tag = metadata.asset.tag
            result.best_match_criticality = metadata.asset.criticality
            result.best_match_related_assets = [
                RelatedAsset(
                    asset_id=related.asset_id,
                    name=related.name,
                    relationship=related.relationship,
                )
                for related in metadata.related_assets
            ]
            result.best_match_active_alarms = metadata.active_alarm_count
            result.upstream = session.upstream()
            return result
