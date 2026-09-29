"""Core entities and enums for the Alarm Management API simulator.

These are the *internal* domain types. The HTTP request/response contract lives in
`schemas.py`; the MCP server exposes a third, deliberately narrower set of models. Keeping
the three separate is what lets the tool contracts stay stable when the API shape drifts.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

# --- Sites and units -------------------------------------------------------------------
# Fixed vocabulary. The Postman collections filter on these exact strings, so they are
# part of the contract rather than free text.


class Site(StrEnum):
    EAST_REFINERY = "EastRefinery"
    NORTH_PLANT = "NorthPlant"
    SOUTH_PLANT = "SouthPlant"


UNITS: tuple[str, ...] = ("Unit 1", "Unit 2", "Unit 3", "Unit 4", "Unit 5")


class AssetType(StrEnum):
    CENTRIFUGAL_PUMP = "centrifugal_pump"
    RECIPROCATING_COMPRESSOR = "reciprocating_compressor"
    CENTRIFUGAL_COMPRESSOR = "centrifugal_compressor"
    MOTOR = "motor"
    HEAT_EXCHANGER = "heat_exchanger"
    CONTROL_VALVE = "control_valve"
    VESSEL = "vessel"
    FAN = "fan"


class AlarmType(StrEnum):
    PROCESS = "process"
    DEVICE = "device"
    SAFETY = "safety"
    SYSTEM = "system"
    INSTRUMENT = "instrument"


class Severity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


# Ordered weakest -> strongest, for `severity_threshold` comparisons.
SEVERITY_ORDER: dict[Severity, int] = {
    Severity.LOW: 0,
    Severity.MEDIUM: 1,
    Severity.HIGH: 2,
    Severity.CRITICAL: 3,
}


class AlarmStatus(StrEnum):
    ACTIVE = "active"
    ACKNOWLEDGED = "acknowledged"
    CLEARED = "cleared"
    SUPPRESSED = "suppressed"
    SHELVED = "shelved"


# --- Entities --------------------------------------------------------------------------


class Asset(BaseModel):
    asset_id: str = Field(description="Stable identifier, e.g. AST-PMP-0001")
    name: str
    asset_type: AssetType
    site: Site
    unit: str
    tag: str = Field(description="Plant instrument tag, e.g. 2-BFP-101")
    criticality: int = Field(ge=1, le=5, description="5 is most critical")
    parent_asset_id: str | None = None
    related_asset_ids: list[str] = Field(default_factory=list)
    manufacturer: str
    model: str
    commissioned_on: datetime
    maintenance_strategy: str
    design_params: dict[str, float | str] = Field(default_factory=dict)


class Alarm(BaseModel):
    alarm_id: str = Field(description="Stable identifier, e.g. ALM-20260912-004311")
    alarm_name: str
    alarm_type: AlarmType
    asset_id: str
    asset_name: str
    site: Site
    unit: str
    severity: Severity
    status: AlarmStatus
    start_time: datetime
    ack_time: datetime | None = None
    clear_time: datetime | None = None
    ack_delay_minutes: float | None = Field(
        default=None, description="Minutes from start_time to ack_time"
    )
    duration_minutes: float | None = Field(
        default=None, description="Minutes from start_time to clear_time"
    )
    value: float | None = None
    setpoint: float | None = None
    unit_of_measure: str | None = None
    priority_hint: int = Field(ge=1, le=5)
    operator_id: str | None = None
    suppressed: bool = False
    chattering: bool = Field(
        default=False, description="Repeatedly cycles; a rationalization candidate"
    )
    source_system: str = "DCS"


class AlarmDefinition(BaseModel):
    """Template from which concrete alarms are generated."""

    alarm_name: str
    alarm_type: AlarmType
    default_severity: Severity
    applies_to: tuple[AssetType, ...]
    setpoint: float
    unit_of_measure: str
    description: str
