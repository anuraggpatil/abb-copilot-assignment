"""Self-describing analytics metadata.

`/analytics/kpi-definitions` is generated from the same `KPI_DEFINITIONS` registry the
aggregations execute, so the documented meaning of a KPI cannot drift from the code that
computes it. The copilot calls this to discover valid `kpis` and `group_by` values instead
of guessing them, which is what keeps invalid-parameter retries rare.
"""

from __future__ import annotations

from fastapi import APIRouter

from apps.alarm_api.schemas import KpiDefinition, KpiDefinitionsResponse
from apps.alarm_api.store import GROUP_KEYS, KPI_DEFINITIONS, SORT_KEYS

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get(
    "/kpi-definitions",
    response_model=KpiDefinitionsResponse,
    summary="Available KPIs, grouping dimensions and sort fields",
)
def kpi_definitions() -> KpiDefinitionsResponse:
    return KpiDefinitionsResponse(
        kpis=[
            KpiDefinition(name=name, unit=meta["unit"], description=meta["description"])
            for name, meta in sorted(KPI_DEFINITIONS.items())
        ],
        group_by_dimensions=sorted(GROUP_KEYS),
        sort_fields=sorted(SORT_KEYS),
    )
