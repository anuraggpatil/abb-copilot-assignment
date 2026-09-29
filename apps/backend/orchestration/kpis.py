"""The operator-facing KPI board, assembled from numbers a tool already returned.

The answer is prose, and prose is the wrong shape for "how bad is it right now". A control-room
reader wants the counts and rates at a glance and the reasoning underneath — so the same tool
results that go to the model are also projected into a small typed structure the GUI renders as a
header of KPI tiles.

Three rules make this honest rather than decorative:

**Nothing is computed here.** Every value is lifted from a tool result. `get_alarm_summary`
computes the KPIs in the alarm API, where the raw alarms are; recomputing them in the backend
from a paginated `get_alarms` page would produce a *different* number from the one the answer was
written against, and a dashboard that disagrees with the text beside it is worse than no
dashboard.

**A KPI a tool did not return is absent, not zero.** `extract` skips unknown keys and `None`
values. Rendering "0 critical alarms" when the KPI was never requested is the single most
dangerous thing this module could do: an operator reads a zero as "checked and clear".

**The tone is presentation only.** `_TONE` decides tile colour and nothing else — it never edits
the answer, adds a caveat or changes what the model saw. The thresholds are a reading of what a
control room cares about, not a plant standard, which is why they live in one table here instead
of being scattered through the GUI. The GUI also prints the value and unit next to the colour,
because colour alone does not survive a projector.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from apps.backend.orchestration.registry import ToolOutcome

Tone = Literal["neutral", "good", "warn", "bad"]

#: Label, unit and hover text for each KPI key the alarm API can return. Keys not in this table
#: are skipped rather than rendered raw: a tile reading `p95_ack_delay 1.0` with no unit is noise,
#: and a new KPI in the API should be a deliberate addition here. The hints are a one-line gloss
#: for a tile, not a restatement of the API's definitions in `apps/alarm_api/store.py` — that
#: text is written for the model reading a tool result, and is too long for a tooltip.
_LABELS: dict[str, tuple[str, str, str]] = {
    "alarm_count": ("Alarms", "count", "Alarms in the window after filters."),
    "critical_count": ("Critical", "count", "Severity 'critical'."),
    "high_or_above_count": ("High or above", "count", "Severity 'high' or 'critical'."),
    "avg_ack_delay": ("Avg ack delay", "minutes", "Mean raise-to-acknowledge, acked alarms only."),
    "max_ack_delay": ("Worst ack delay", "minutes", "Longest raise-to-acknowledge in the window."),
    "avg_duration": ("Avg duration", "minutes", "Mean time an alarm stayed active."),
    "recurring_rate": ("Recurring", "ratio", "Share belonging to a repeating signature."),
    "unacknowledged_rate": ("Unacknowledged", "ratio", "Share never acknowledged by an operator."),
    "suppression_candidate_rate": (
        "Suppression candidates",
        "ratio",
        "Share that chattered — a deadband problem rather than a process one.",
    ),
    "operator_response_efficiency": (
        "Acked within 10 min",
        "ratio",
        "Share of acknowledgeable alarms acknowledged inside ten minutes.",
    ),
}

#: Colour only. See the module docstring — these are a reading of control-room priorities, and
#: the one place to change them.
_TONE: dict[str, Callable[[float], Tone]] = {
    "critical_count": lambda v: "bad" if v > 0 else "good",
    "high_or_above_count": lambda v: "warn" if v > 0 else "good",
    "avg_ack_delay": lambda v: "good" if v <= 5 else "warn" if v <= 15 else "bad",
    "max_ack_delay": lambda v: "good" if v <= 15 else "warn" if v <= 60 else "bad",
    "recurring_rate": lambda v: "bad" if v >= 0.6 else "warn" if v >= 0.3 else "good",
    "unacknowledged_rate": lambda v: "bad" if v >= 0.25 else "warn" if v >= 0.1 else "good",
    "suppression_candidate_rate": lambda v: "bad" if v >= 0.4 else "warn" if v >= 0.2 else "good",
    "operator_response_efficiency": lambda v: "good" if v >= 0.8 else "warn" if v >= 0.5 else "bad",
}


class Kpi(BaseModel):
    """One tile. `value` is the raw number; the GUI formats it from `unit`."""

    model_config = ConfigDict(extra="forbid")

    key: str
    label: str
    value: float
    unit: str = ""
    tone: Tone = "neutral"
    #: The alarm API's own description of the KPI, shown on hover. Taken from the tool result
    #: where available so the definition a reader sees is the one the number was computed to.
    hint: str = ""


class PatternKpi(BaseModel):
    """One recurring signature, as the board's second row shows it."""

    model_config = ConfigDict(extra="forbid")

    alarm_name: str
    asset_name: str = ""
    occurrences: int
    max_severity: str = ""
    trend: str = ""
    occurrences_first_half: int | None = None
    occurrences_second_half: int | None = None
    chattering_share: float | None = None


class InvestigationKpis(BaseModel):
    """What the GUI renders above the answer. Empty means "no tool returned numbers"."""

    model_config = ConfigDict(extra="forbid")

    asset_name: str = ""
    asset_id: str = ""
    window_start: datetime | None = None
    window_end: datetime | None = None
    window_days: int | None = None
    total_alarms: int | None = None
    metrics: list[Kpi] = Field(default_factory=list)
    patterns: list[PatternKpi] = Field(default_factory=list)
    #: The tools these numbers came from, so a reviewer can tie a tile back to a trace row.
    sources: list[str] = Field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.metrics and not self.patterns and self.total_alarms is None


def extract(outcomes: list[ToolOutcome]) -> InvestigationKpis:
    """Project the successful tool results onto the KPI board.

    Later results win over earlier ones for the scalar fields: an investigation that narrows
    from "all alarms" to "high severity on this asset" should show the narrowed window, which is
    the one the answer is about.
    """
    board = InvestigationKpis()
    metrics: dict[str, Kpi] = {}

    for outcome in outcomes:
        if not outcome.ok or not isinstance(outcome.result, dict):
            continue
        result = outcome.result
        touched = False

        if outcome.name == "get_alarm_summary":
            _window(board, result)
            total = _int(result.get("total_alarms"))
            if total is not None:
                board.total_alarms = total
            for kpi in _metrics_of(result):
                metrics[kpi.key] = kpi
            touched = True

        elif outcome.name == "get_recurring_alarms":
            _window(board, result)
            patterns = _patterns_of(result)
            if patterns:
                board.patterns = patterns
                touched = True

        elif outcome.name == "get_alarms":
            _window(board, result)
            # Only as a fallback: `get_alarm_summary` counts over the whole window, while this
            # is the count matching *this* call's filters. Never overwrite the summary's figure.
            total = _int(result.get("total_matching"))
            if total is not None and board.total_alarms is None:
                board.total_alarms = total
                touched = True
            _name_from(board, _first_record(result.get("alarms")))

        elif outcome.name == "get_operator_recommendations":
            _name_from(board, result)
            actions = result.get("actions")
            if isinstance(actions, list) and actions:
                metrics["recommended_actions"] = Kpi(
                    key="recommended_actions",
                    label="Recommended actions",
                    value=float(len(actions)),
                    unit="count",
                    hint="Ranked actions returned by the alarm system's own rule engine.",
                )
                immediate = sum(
                    1
                    for action in actions
                    if isinstance(action, dict) and action.get("urgency") == "immediate"
                )
                metrics["immediate_actions"] = Kpi(
                    key="immediate_actions",
                    label="Immediate",
                    value=float(immediate),
                    unit="count",
                    tone="bad" if immediate else "neutral",
                    hint="Actions the rule engine marked as immediate rather than this shift.",
                )
                touched = True

        elif outcome.name == "search_assets":
            best = _first_record(result.get("assets"))
            if best:
                board.asset_id = board.asset_id or _str(best.get("asset_id"))
                board.asset_name = board.asset_name or _str(best.get("name"))
                touched = True

        if touched and outcome.name not in board.sources:
            board.sources.append(outcome.name)

    # Tile order is _LABELS' order, not the order the API happened to serialise its dict in, so
    # the board does not rearrange itself between two runs of the same question.
    ordered = [metrics[key] for key in _LABELS if key in metrics]
    ordered += [
        metrics[key] for key in ("recommended_actions", "immediate_actions") if key in metrics
    ]
    board.metrics = ordered
    return board


def _metrics_of(result: dict[str, Any]) -> list[Kpi]:
    overall = result.get("overall")
    if not isinstance(overall, dict):
        return []

    kpis: list[Kpi] = []
    for key, raw in overall.items():
        spec = _LABELS.get(key)
        if spec is None or not isinstance(raw, (int, float)) or isinstance(raw, bool):
            # `None` lands here too, and deliberately: `avg_ack_delay: null` means nothing in
            # the set was acknowledged, which is not the same claim as "0 minutes".
            continue
        label, unit, hint = spec
        value = float(raw)
        tone = _TONE.get(key)
        kpis.append(
            Kpi(
                key=key,
                label=label,
                value=value,
                unit=unit,
                tone=tone(value) if tone else "neutral",
                hint=hint,
            )
        )
    return kpis


def _patterns_of(result: dict[str, Any]) -> list[PatternKpi]:
    raw = result.get("patterns")
    if not isinstance(raw, list):
        return []
    patterns: list[PatternKpi] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        occurrences = _int(item.get("occurrences"))
        name = _str(item.get("alarm_name"))
        if occurrences is None or not name:
            continue
        chattering = item.get("chattering_share")
        patterns.append(
            PatternKpi(
                alarm_name=name,
                asset_name=_str(item.get("asset_name")),
                occurrences=occurrences,
                max_severity=_str(item.get("max_severity")),
                trend=_str(item.get("trend")),
                occurrences_first_half=_int(item.get("occurrences_first_half")),
                occurrences_second_half=_int(item.get("occurrences_second_half")),
                chattering_share=(
                    float(chattering)
                    if isinstance(chattering, (int, float)) and not isinstance(chattering, bool)
                    else None
                ),
            )
        )
    return sorted(patterns, key=lambda pattern: pattern.occurrences, reverse=True)


def _window(board: InvestigationKpis, result: dict[str, Any]) -> None:
    start = _when(result.get("window_start"))
    end = _when(result.get("window_end"))
    if start is None or end is None:
        return
    board.window_start = start
    board.window_end = end
    board.window_days = max(round((end - start).total_seconds() / 86400), 0)


def _name_from(board: InvestigationKpis, record: dict[str, Any] | None) -> None:
    if not record:
        return
    board.asset_name = board.asset_name or _str(record.get("asset_name"))
    board.asset_id = board.asset_id or _str(record.get("asset_id"))


def _first_record(value: Any) -> dict[str, Any] | None:
    if isinstance(value, list) and value and isinstance(value[0], dict):
        first: dict[str, Any] = value[0]
        return first
    return None


def _when(value: Any) -> datetime | None:
    """Parse a window boundary. Tool results arrive as JSON, so these are ISO strings."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    return int(value) if isinstance(value, (int, float)) else None


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""
