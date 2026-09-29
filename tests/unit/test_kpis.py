"""The KPI board's one rule: show what a tool returned, and nothing else.

Most of these tests are about *absence*. A dashboard that renders a KPI nobody computed as `0`
is not a cosmetic bug in an alarm application — an operator reads "0 critical" as "someone
checked, and it is clear". So the cases that matter are the null KPI, the unrequested KPI, the
failed tool and the documentation-only question, and each is asserted to produce a missing tile
rather than a zero one.

The rest pins the two things a reviewer would otherwise have to take on trust: that no number is
recomputed here (`get_alarms` never overwrites a figure `get_alarm_summary` produced, because the
two count different sets), and that tile order is fixed rather than dictionary order.
"""

from __future__ import annotations

from typing import Any

from apps.backend.orchestration import kpis as kpi_board
from apps.backend.orchestration.registry import ToolOutcome


def _outcome(name: str, result: dict[str, Any] | None, *, ok: bool = True) -> ToolOutcome:
    return ToolOutcome(call_id=f"call-{name}", name=name, ok=ok, result=result)


def _summary(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "total_alarms": 42,
        "window_start": "2026-07-01T00:00:00+00:00",
        "window_end": "2026-09-29T00:00:00+00:00",
        "group_by": [],
        "overall": {
            "alarm_count": 42,
            "critical_count": 3,
            "high_or_above_count": 18,
            "avg_ack_delay": 12.5,
            "unacknowledged_rate": 0.07,
        },
        "groups": [],
        "filters_applied": {},
    }
    body.update(overrides)
    return body


def _pattern(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "alarm_name": "Suction Pressure Low",
        "alarm_type": "process",
        "asset_id": "AST-PMP-0001",
        "asset_name": "Boiler Feed Pump 101",
        "occurrences": 14,
        "max_severity": "high",
        "first_seen": "2026-07-02T00:00:00+00:00",
        "last_seen": "2026-09-28T00:00:00+00:00",
        "trend": "increasing",
        "occurrences_first_half": 5,
        "occurrences_second_half": 9,
        "chattering_share": 0.21,
    }
    body.update(overrides)
    return body


class TestFromTheSummary:
    def test_the_figures_and_the_window_come_across(self) -> None:
        board = kpi_board.extract([_outcome("get_alarm_summary", _summary())])

        assert board.total_alarms == 42
        assert board.window_days == 90
        assert board.sources == ["get_alarm_summary"]
        assert {kpi.key: kpi.value for kpi in board.metrics} == {
            "alarm_count": 42.0,
            "critical_count": 3.0,
            "high_or_above_count": 18.0,
            "avg_ack_delay": 12.5,
            "unacknowledged_rate": 0.07,
        }

    def test_every_tile_is_labelled_and_carries_a_unit(self) -> None:
        board = kpi_board.extract([_outcome("get_alarm_summary", _summary())])

        for kpi in board.metrics:
            assert kpi.label and kpi.label != kpi.key
            assert kpi.unit in {"count", "minutes", "ratio"}
            assert kpi.hint

    def test_tile_order_is_fixed_not_dictionary_order(self) -> None:
        """The same question must not rearrange its own board between two runs."""
        reversed_overall = dict(reversed(list(_summary()["overall"].items())))
        board = kpi_board.extract(
            [_outcome("get_alarm_summary", _summary(overall=reversed_overall))]
        )

        assert [kpi.key for kpi in board.metrics] == [
            "alarm_count",
            "critical_count",
            "high_or_above_count",
            "avg_ack_delay",
            "unacknowledged_rate",
        ]

    def test_a_null_kpi_is_absent_not_zero(self) -> None:
        """`avg_ack_delay: null` means nothing was acknowledged, not that it took no time."""
        board = kpi_board.extract(
            [
                _outcome(
                    "get_alarm_summary", _summary(overall={"alarm_count": 4, "avg_ack_delay": None})
                )
            ]
        )

        assert [kpi.key for kpi in board.metrics] == ["alarm_count"]

    def test_an_unknown_kpi_key_is_skipped_rather_than_rendered_raw(self) -> None:
        board = kpi_board.extract(
            [
                _outcome(
                    "get_alarm_summary", _summary(overall={"alarm_count": 4, "p95_ack_delay": 9})
                )
            ]
        )

        assert [kpi.key for kpi in board.metrics] == ["alarm_count"]

    def test_a_missing_window_leaves_the_window_unset(self) -> None:
        body = _summary()
        del body["window_end"]
        board = kpi_board.extract([_outcome("get_alarm_summary", body)])

        assert board.window_days is None
        assert board.window_start is None


class TestTone:
    """Colour only — but the thresholds still decide whether a reader looks twice."""

    def test_no_criticals_reads_good_and_any_critical_reads_bad(self) -> None:
        clear = kpi_board.extract(
            [_outcome("get_alarm_summary", _summary(overall={"critical_count": 0}))]
        )
        assert clear.metrics[0].tone == "good"

        one = kpi_board.extract(
            [_outcome("get_alarm_summary", _summary(overall={"critical_count": 1}))]
        )
        assert one.metrics[0].tone == "bad"

    def test_ack_delay_crosses_from_good_through_warn_to_bad(self) -> None:
        def tone(value: float) -> str:
            board = kpi_board.extract(
                [_outcome("get_alarm_summary", _summary(overall={"avg_ack_delay": value}))]
            )
            return board.metrics[0].tone

        assert tone(4.0) == "good"
        assert tone(12.0) == "warn"
        assert tone(40.0) == "bad"

    def test_a_plain_count_stays_neutral(self) -> None:
        board = kpi_board.extract(
            [_outcome("get_alarm_summary", _summary(overall={"alarm_count": 9999}))]
        )
        assert board.metrics[0].tone == "neutral"


class TestRecurringPatterns:
    def test_patterns_arrive_with_their_trend_and_halves(self) -> None:
        board = kpi_board.extract(
            [_outcome("get_recurring_alarms", {"patterns": [_pattern()], "total_patterns": 1})]
        )

        assert len(board.patterns) == 1
        pattern = board.patterns[0]
        assert pattern.alarm_name == "Suction Pressure Low"
        assert pattern.trend == "increasing"
        assert pattern.occurrences_first_half == 5
        assert pattern.occurrences_second_half == 9
        assert pattern.chattering_share == 0.21

    def test_patterns_are_ordered_by_occurrences(self) -> None:
        board = kpi_board.extract(
            [
                _outcome(
                    "get_recurring_alarms",
                    {
                        "patterns": [
                            _pattern(alarm_name="Bearing Temperature High", occurrences=6),
                            _pattern(alarm_name="Vibration High", occurrences=21),
                        ]
                    },
                )
            ]
        )

        assert [pattern.occurrences for pattern in board.patterns] == [21, 6]

    def test_a_malformed_pattern_is_dropped_not_rendered_half_empty(self) -> None:
        board = kpi_board.extract(
            [_outcome("get_recurring_alarms", {"patterns": ["not a pattern", {"occurrences": 3}]})]
        )

        assert board.patterns == []


class TestTheOtherTools:
    def test_recommendations_contribute_an_action_count_and_an_urgency_count(self) -> None:
        board = kpi_board.extract(
            [
                _outcome(
                    "get_operator_recommendations",
                    {
                        "asset_id": "AST-PMP-0001",
                        "asset_name": "Boiler Feed Pump 101",
                        "actions": [
                            {"rank": 1, "urgency": "immediate"},
                            {"rank": 2, "urgency": "this_shift"},
                            {"rank": 3, "urgency": "planned"},
                        ],
                    },
                )
            ]
        )

        metrics = {kpi.key: kpi for kpi in board.metrics}
        assert metrics["recommended_actions"].value == 3.0
        assert metrics["immediate_actions"].value == 1.0
        assert metrics["immediate_actions"].tone == "bad"
        assert board.asset_name == "Boiler Feed Pump 101"

    def test_search_assets_resolves_the_asset_header(self) -> None:
        board = kpi_board.extract(
            [
                _outcome(
                    "search_assets",
                    {"query": "pump", "assets": [{"asset_id": "AST-PMP-0001", "name": "BFP 101"}]},
                )
            ]
        )

        assert (board.asset_id, board.asset_name) == ("AST-PMP-0001", "BFP 101")

    def test_get_alarms_supplies_a_total_only_when_the_summary_did_not(self) -> None:
        alarms = _outcome(
            "get_alarms",
            {
                "total_matching": 7,
                "alarms": [{"asset_id": "AST-PMP-0001", "asset_name": "BFP 101"}],
            },
        )

        alone = kpi_board.extract([alarms])
        assert alone.total_alarms == 7
        assert alone.asset_name == "BFP 101"

        # The summary counts the whole window; a `get_alarms` page counts that call's filters.
        # Letting the second overwrite the first would put a number on screen that the answer's
        # prose was never written against.
        with_summary = kpi_board.extract([_outcome("get_alarm_summary", _summary()), alarms])
        assert with_summary.total_alarms == 42


class TestNothingIsInvented:
    def test_no_tools_means_an_empty_board(self) -> None:
        board = kpi_board.extract([])

        assert board.empty
        assert board.metrics == []
        assert board.patterns == []
        assert board.total_alarms is None

    def test_a_failed_tool_contributes_nothing(self) -> None:
        board = kpi_board.extract(
            [
                ToolOutcome(
                    call_id="c1",
                    name="get_alarm_summary",
                    ok=False,
                    result=None,
                    error="upstream unreachable",
                    error_kind="transport",
                )
            ]
        )

        assert board.empty
        assert board.sources == []

    def test_a_documentation_only_question_renders_no_figures(self) -> None:
        """The degraded path: retrieval ran, no alarm tool did. An empty board is correct."""
        board = kpi_board.extract([_outcome("search_procedures", {"passages": [{"quote": "…"}]})])

        assert board.empty

    def test_sources_name_only_the_tools_that_contributed(self) -> None:
        board = kpi_board.extract(
            [
                _outcome("search_assets", {"assets": [{"asset_id": "A1", "name": "Pump"}]}),
                _outcome("get_alarm_summary", _summary()),
                _outcome("get_recurring_alarms", {"patterns": []}),
            ]
        )

        assert board.sources == ["search_assets", "get_alarm_summary"]
