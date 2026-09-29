"""Replay the assignment's own Postman collections against the simulator.

`test_alarm_api.py` already asserts the wrapper keys, the trace echo and the chaining flow —
but it asserts them from *our* transcription of the contract, and a transcription cannot catch
the case where we and the collection have drifted apart. This file reads the collection JSON
committed under `postman/` and sends what it says, so the expectations come from the artefact
a grader will actually run.

That distinction is not academic. Rename a query parameter, and the hand-written test gets
updated in the same commit and stays green; this one goes red, because the collection still
sends the old spelling.

Three things make a faithful replay possible without a JavaScript runtime:

* **Variables are substituted from the collection's own declared defaults**, including the
  empty ones. Postman does the same — an uncaptured variable interpolates to `""` — so a
  request that depends on a step which did not run is sent in the state Postman would send it,
  rather than being skipped and quietly reported as fine.
* **The captures its test scripts perform are expressed as data** (`CAPTURES`), as paths into
  the response body. This doubles as the pinned-wrapper-key assertion: `results[0].asset_id`
  and `data[0].alarm_id` are exactly what the collection's JavaScript reads, so a renamed
  wrapper breaks the capture and then the whole chain.
* **The collection is walked once, in order, and every exchange recorded**, so the assertions
  below read the result rather than each re-running the chain. Ordering is preserved (the
  chain needs it) without giving up per-assertion failure locality.

Seven of the fifteen endpoints are not built — the flex phase of this project's plan, listed
in `NOT_IMPLEMENTED`. They are asserted to be *absent*, in both directions: an unbuilt
endpoint must report itself missing, and an endpoint that reports itself missing must be one
of the declared seven. So this file states the scope gap precisely instead of hiding it, and
it fails the day one of them lands without this list being updated.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import TEST_TOKEN

REPO_ROOT = Path(__file__).resolve().parents[2]
POSTMAN = REPO_ROOT / "postman"
SIMULATOR_COLLECTION = POSTMAN / "Alarm-API-Simulator.postman_collection.json"
CHAINING_COLLECTION = POSTMAN / "chaining" / "Alarm-API-Chaining.postman_collection.json"

#: Endpoints the collections exercise that this build does not implement. The plan's flex phase
#: (P7) broadens the simulator to all fifteen; until then the gap is declared here rather than
#: discovered by a grader, and the assertions below hold this list to exactly the truth.
NOT_IMPLEMENTED = frozenset(
    {
        "/alarms/trends",
        "/alarms/correlation",
        "/alarms/flood-analysis",
        "/alarms/rationalization-candidates",
        "/alarms/priority-score",
        "/calculation-code/generate",
        "/calculation-code/execute",
    }
)

#: 405 and not only 404 because `GET /alarms/{alarm_id}` shadows the path: `POST /alarms/trends`
#: matches that route with `alarm_id="trends"` and is rejected on the method. `/calculation-code/*`
#: matches no router at all and is a plain 404. Both mean "not built"; neither means "broken".
ABSENT = frozenset({404, 405})

#: What each collection's `pm.collectionVariables.set(...)` calls do, as paths into the response
#: body. Written as data because executing the JavaScript is out of scope — and because a path
#: is the assertion: these are the wrapper keys the assignment pins.
Captures = dict[str, tuple[str, tuple[str | int, ...]]]

SIMULATOR_CAPTURES: Captures = {
    "01 - Search Asset (sets asset_id)": ("asset_id", ("results", 0, "asset_id")),
    "03 - Get Alarms (sets alarm_id)": ("alarm_id", ("data", 0, "alarm_id")),
    "12 - Generate Calculation Code (sets calculation_id)": (
        "calculation_id",
        ("calculation_id",),
    ),
}

#: Keyed separately because the chaining collection names its steps by position, so a shared
#: table would silently apply one collection's capture to the other's step.
CHAIN_05_CAPTURES: Captures = {
    "1) Search Boiler Feed Pump 102": ("asset_id", ("results", 0, "asset_id")),
    "2) Get Active Alarms": ("alarm_id", ("data", 0, "alarm_id")),
}

#: Requests the collection sends the three trace headers on, and that this build serves.
#: (`/alarms/correlation` and `/calculation-code/execute` also send them, and are unbuilt.)
TRACED_ITEMS = [
    "05 - Alarm Summary (trace headers)",
    "11 - Operator Recommendations (trace headers)",
]

_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


@dataclass(frozen=True)
class Exchange:
    """One replayed request and what came back."""

    name: str
    method: str
    path: str
    status: int
    body: Any
    headers: dict[str, str]

    @property
    def endpoint(self) -> str:
        return self.path.split("?")[0]


# --- the replay ---------------------------------------------------------------------------


def _load(path: Path) -> dict[str, Any]:
    assert path.exists(), (
        f"{path.relative_to(REPO_ROOT)} is missing. The collections are committed under "
        "postman/ on purpose, so this test replays the assignment's own artefact instead of a "
        "copy of it that can drift."
    )
    return json.loads(path.read_text(encoding="utf-8"))


def _variables(collection: dict[str, Any]) -> dict[str, str]:
    """The collection's declared defaults, with the token swapped for the suite's own.

    Everything else is left exactly as published — including the static
    `2026-05-01 → 2026-07-01` window, which is the point of
    `test_the_collections_static_window_still_has_data`.
    """
    values = {
        str(variable["key"]): str(variable.get("value", ""))
        for variable in collection.get("variable", [])
    }
    values["auth_token"] = TEST_TOKEN
    return values


def _substitute(text: str, variables: dict[str, str]) -> str:
    """Interpolate `{{name}}`. Applied twice: the chaining collection defines one in terms
    of another (`window_start={{start_time}}`), and one pass would leave the inner one raw."""

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        assert name in variables, (
            f"{{{{{name}}}}} is used by the collection but not declared in its variables — "
            "the replay cannot guess what it should be"
        )
        return variables[name]

    return _PLACEHOLDER.sub(replace, _PLACEHOLDER.sub(replace, text))


def _dig(body: Any, path: tuple[str | int, ...]) -> Any:
    for step in path:
        try:
            body = body[step]
        except (KeyError, IndexError, TypeError):
            return None
    return body


def _send(client: TestClient, item: dict[str, Any], variables: dict[str, str]) -> Exchange:
    request = item["request"]
    url = request["url"]
    raw = url if isinstance(url, str) else url["raw"]
    path = _substitute(raw, variables).removeprefix(variables["baseUrl"])

    headers = {
        str(header["key"]): _substitute(str(header.get("value", "")), variables)
        for header in request.get("header", [])
    }
    # Collection-level bearer auth, applied to every request including /health.
    headers["Authorization"] = f"Bearer {variables['auth_token']}"

    raw_body = (request.get("body") or {}).get("raw")
    content = _substitute(raw_body, variables) if raw_body else None

    response = client.request(request["method"], path, headers=headers, content=content)
    try:
        body = response.json()
    except ValueError:
        body = None

    return Exchange(
        name=item["name"],
        method=request["method"],
        path=path,
        status=response.status_code,
        body=body,
        headers=dict(response.headers),
    )


def _run(
    client: TestClient,
    items: list[dict[str, Any]],
    variables: dict[str, str],
    captures: Captures,
) -> dict[str, Exchange]:
    """Send each item in order, applying the captures the collection's scripts perform."""
    exchanges: dict[str, Exchange] = {}

    for item in items:
        exchange = _send(client, item, variables)
        exchanges[exchange.name] = exchange

        capture = captures.get(exchange.name)
        if capture is None or exchange.status != 200:
            continue
        name, path = capture
        value = _dig(exchange.body, path)
        assert isinstance(value, str) and value, (
            f"{exchange.name!r} is supposed to set {name!r} from "
            f"{'.'.join(str(step) for step in path)}, and that path held {value!r}. The "
            "collection's own test script reads exactly this path, so every later request in "
            "the chain would be sent with an empty id."
        )
        variables[name] = value

    return exchanges


@pytest.fixture(scope="module")
def replay(client: TestClient) -> dict[str, Exchange]:
    """The simulator collection, walked once in order."""
    collection = _load(SIMULATOR_COLLECTION)
    return _run(client, collection["item"], _variables(collection), SIMULATOR_CAPTURES)


@pytest.fixture(scope="module")
def chain_05(client: TestClient) -> dict[str, Exchange]:
    """CHAIN-05, the one chain that lies entirely within the implemented endpoints."""
    collection = _load(CHAINING_COLLECTION)
    folders = [item for item in collection["item"] if item["name"].startswith("CHAIN-05")]
    assert len(folders) == 1, f"expected exactly one CHAIN-05 folder, found {len(folders)}"
    return _run(client, folders[0]["item"], _variables(collection), CHAIN_05_CAPTURES)


# --- what the replay proves ---------------------------------------------------------------


class TestTheCollectionRuns:
    def test_every_request_either_succeeds_or_is_a_declared_gap(
        self, replay: dict[str, Exchange]
    ) -> None:
        unexpected = {
            name: exchange.status
            for name, exchange in replay.items()
            if exchange.status != 200 and exchange.status not in ABSENT
        }

        # A 401 would mean the collection's auth shape is not what we accept; a 422 would mean
        # our schema rejects a body the assignment publishes. Both are contract failures that a
        # test written from our own understanding of the contract cannot find.
        assert unexpected == {}, f"requests that neither worked nor are missing: {unexpected}"

    def test_the_missing_endpoints_are_exactly_the_declared_ones(
        self, replay: dict[str, Exchange]
    ) -> None:
        reported_missing = {name for name, ex in replay.items() if ex.status in ABSENT}
        declared_missing = {name for name, ex in replay.items() if ex.endpoint in NOT_IMPLEMENTED}

        # Both directions. Left to right catches an implemented endpoint that 404s because the
        # chain broke; right to left catches NOT_IMPLEMENTED going stale once P7 builds one.
        assert reported_missing == declared_missing

    def test_all_fifteen_collection_requests_were_replayed(
        self, replay: dict[str, Exchange]
    ) -> None:
        # Guards the walk itself: a substitution assertion that silently skipped items would
        # make every test above pass on a subset.
        assert len(replay) == 15

    def test_the_token_the_collection_hands_out_is_the_one_we_document(self) -> None:
        collection = _load(SIMULATOR_COLLECTION)
        declared = {variable["key"]: variable["value"] for variable in collection["variable"]}
        env_example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")

        # Whoever grades this runs the collection unmodified against `make api`, so the
        # simulator's documented default token has to be the one the collection sends. The
        # replay itself substitutes the suite's token instead — see `_variables`.
        assert declared["auth_token"] == "demo-token"
        assert "ALARM_API_TOKEN=demo-token" in env_example
        assert _variables(collection)["auth_token"] == TEST_TOKEN


class TestThePinnedShapes:
    @pytest.mark.parametrize(
        ("item", "path"),
        [
            ("01 - Search Asset (sets asset_id)", ("results", 0, "asset_id")),
            ("03 - Get Alarms (sets alarm_id)", ("data", 0, "alarm_id")),
            ("02 - Asset Metadata", ("asset", "asset_id")),
            ("04 - Get Alarm by ID", ("alarm", "alarm_id")),
            ("14 - KPI Definitions", ("kpis", 0, "name")),
        ],
    )
    def test_the_response_holds_what_the_collection_reads(
        self, replay: dict[str, Exchange], item: str, path: tuple[str | int, ...]
    ) -> None:
        assert _dig(replay[item].body, path), f"{item}: {'.'.join(map(str, path))} was empty"

    def test_the_collections_static_window_still_has_data(
        self, replay: dict[str, Exchange]
    ) -> None:
        summary = replay["05 - Alarm Summary (trace headers)"].body

        # The collection's window is hard-coded to 2026-05-01 → 2026-07-01, which is in the past
        # and stays there. The seeder generates relative to *now* over a 400-day horizon
        # specifically so this request keeps returning rows; a shorter horizon would make it
        # return an empty summary and every assertion here would still pass.
        assert summary["total_alarms"] > 0
        assert summary["groups"], "grouped by alarm_name, so a non-empty window has groups"


class TestTraceHeaders:
    @pytest.mark.parametrize("item", TRACED_ITEMS)
    def test_the_three_headers_come_back(self, replay: dict[str, Exchange], item: str) -> None:
        headers = replay[item].headers

        assert headers["trace_id"] == "trace-postman-001"
        assert headers["x-client-id"] == "postman-client"
        assert headers["x-metadata-tag"] == "manual-test"

    @pytest.mark.parametrize("item", TRACED_ITEMS)
    def test_they_are_in_the_body_too(self, replay: dict[str, Exchange], item: str) -> None:
        # The copilot's trace panel correlates by body when a response is replayed from a log,
        # so the echo cannot live in the headers alone.
        trace = replay[item].body["trace"]

        assert trace == {
            "trace_id": "trace-postman-001",
            "client_id": "postman-client",
            "metadata_tag": "manual-test",
        }

    def test_an_untraced_request_still_gets_a_generated_id(
        self, replay: dict[str, Exchange]
    ) -> None:
        headers = replay["03 - Get Alarms (sets alarm_id)"].headers

        # The collection sends no trace headers here. Generating one rather than leaving it
        # blank is what makes a chain traceable when a caller forgets.
        assert headers["trace_id"].startswith("trc-")
        assert headers["x-trace-id-generated"] == "true"


class TestTheChain:
    def test_the_asset_id_from_search_was_accepted_downstream(
        self, replay: dict[str, Exchange]
    ) -> None:
        search = replay["01 - Search Asset (sets asset_id)"]
        asset_id = _dig(search.body, ("results", 0, "asset_id"))

        # Not a re-assertion of the ids: the point is that the *replayed* requests carried them,
        # so the chain was exercised rather than described.
        assert asset_id in replay["02 - Asset Metadata"].path
        assert asset_id in replay["03 - Get Alarms (sets alarm_id)"].path
        assert replay["02 - Asset Metadata"].body["asset"]["asset_id"] == asset_id

    def test_the_alarm_id_from_the_list_was_accepted_by_recommendations(
        self, replay: dict[str, Exchange]
    ) -> None:
        alarm_id = _dig(replay["03 - Get Alarms (sets alarm_id)"].body, ("data", 0, "alarm_id"))
        recommendations = replay["11 - Operator Recommendations (trace headers)"].body

        assert alarm_id in replay["04 - Get Alarm by ID"].path
        assert recommendations["alarm_id"] == alarm_id
        assert recommendations["recommendations"], "an alarm must yield at least one action"

    def test_the_recommendations_cite_procedures_for_rag_to_retrieve(
        self, replay: dict[str, Exchange]
    ) -> None:
        references = replay["11 - Operator Recommendations (trace headers)"].body[
            "procedure_references"
        ]

        # This field is the seam between the two halves of the copilot: the API names a section,
        # retrieval fetches it. An empty list here would make the combined workflow untestable.
        assert references
        assert all("§" in reference for reference in references)

    def test_chain_05_replays_end_to_end(self, chain_05: dict[str, Exchange]) -> None:
        statuses = {name: exchange.status for name, exchange in chain_05.items()}

        assert statuses == {
            "1) Search Boiler Feed Pump 102": 200,
            "2) Get Active Alarms": 200,
            "3) Recommendations": 200,
        }

    def test_chain_05_carried_its_own_ids_through(self, chain_05: dict[str, Exchange]) -> None:
        asset_id = _dig(chain_05["1) Search Boiler Feed Pump 102"].body, ("results", 0, "asset_id"))
        alarm_id = _dig(chain_05["2) Get Active Alarms"].body, ("data", 0, "alarm_id"))

        # `?status=active` is the filter this chain turns on, and BFP-102 having a live alarm is
        # therefore a seed property the assignment's own chain depends on.
        assert asset_id in chain_05["2) Get Active Alarms"].path
        assert alarm_id, "BFP-102 must have at least one active alarm for this chain to run"
        assert chain_05["3) Recommendations"].body["alarm_id"] == alarm_id
        assert chain_05["3) Recommendations"].body["status"] == "active"

    def test_chain_05_is_the_only_chain_fully_within_the_built_surface(self) -> None:
        collection = _load(CHAINING_COLLECTION)
        runnable = []
        for folder in collection["item"]:
            endpoints = [
                (
                    step["request"]["url"]
                    if isinstance(step["request"]["url"], str)
                    else step["request"]["url"]["raw"]
                )
                .removeprefix("{{baseUrl}}")
                .split("?")[0]
                for step in folder["item"]
            ]
            if not any(endpoint in NOT_IMPLEMENTED for endpoint in endpoints):
                runnable.append(folder["name"])

        # Says out loud which of the ten chains this build can serve, and flips the moment P7
        # implements an endpoint — at which point another chain becomes replayable and should be.
        assert runnable == ["CHAIN-05 Asset Active -> Recommendation"]
