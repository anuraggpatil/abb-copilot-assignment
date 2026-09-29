"""The documentation deliverables, asserted where they can drift away from the code.

Docs are a graded deliverable, and the ways they rot are predictable: a document named in the
guidelines is never written, a tool is added and the catalogue is not, a box the diagram must
show is deleted during a refactor. Each of those is mechanically checkable, and checking it is
worth more than a review pass that has to remember the requirement list.

What is deliberately *not* asserted is prose quality or completeness of explanation. A test
that greps for sentences teaches the next person to write sentences that pass the grep. These
tests check existence, coverage and the one geometric invariant that actually broke — arrows
drawn between boxes of different widths came out diagonal, which is invisible in the source
and obvious in the PNG.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

from tests.integration.test_mcp_server import EXPECTED_TOOLS

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"

#: The documents the submission guidelines name. `architecture-diagram.png` is listed with
#: them because §9 requires a diagram, not a description of one.
REQUIRED_DOCS = (
    "architecture.md",
    "architecture-diagram.png",
    "mcp-tool-catalog.md",
    "rag-design.md",
    "api-integration.md",
    "design-decisions.md",
    "known-limitations.md",
)

#: §9's eleven required elements, mapped to the `BOXES` keys that carry them. Written as a
#: mapping rather than a list of keys so a failure names the requirement, not just an
#: identifier someone renamed.
REQUIRED_DIAGRAM_ELEMENTS = {
    "GUI / chat interface": "gui",
    "copilot orchestration layer": "orchestration",
    "LLM planner and tool selection": "planner",
    "MCP client": "mcp_client",
    "MCP server": "mcp_server",
    "source-system API": "alarm_api",
    "optional secondary source": "secondary",
    "document store": "documents",
    "RAG ingestion pipeline": "ingestion",
    "retrieval index": "index",
    "observability and tracing": "observability",
}


def _load_renderer() -> ModuleType:
    """Import the diagram script by path — `scripts/` is not a package, by design."""
    path = ROOT / "scripts" / "render_architecture_diagram.py"
    spec = importlib.util.spec_from_file_location("render_architecture_diagram", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before exec so the module is importable from within itself if it ever grows
    # a dataclass or a typing construct that needs it.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def renderer() -> ModuleType:
    return _load_renderer()


class TestTheRequiredDocumentsExist:
    @pytest.mark.parametrize("name", REQUIRED_DOCS)
    def test_the_document_is_present_and_not_a_stub(self, name: str) -> None:
        path = DOCS / name
        assert path.exists(), f"docs/{name} is a named deliverable and is missing"
        # 2 KB rules out a placeholder without pretending to measure quality. The PNG is
        # binary and much larger; the same floor covers both.
        assert path.stat().st_size > 2048, f"docs/{name} is too small to be the real thing"

    def test_the_readme_links_every_one_of_them(self) -> None:
        """A document nothing points at is a document nobody reads."""
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        for name in REQUIRED_DOCS:
            assert f"docs/{name}" in readme, f"README does not link docs/{name}"


class TestTheToolCatalogueCoversTheServer:
    """§5 requires an entry per tool. Adding a tool and forgetting the catalogue is the drift."""

    def test_every_advertised_tool_has_a_catalogue_section(self) -> None:
        catalogue = (DOCS / "mcp-tool-catalog.md").read_text(encoding="utf-8")
        headings = set(re.findall(r"^#+\s+`?([a-z_]+)`?", catalogue, flags=re.MULTILINE))

        missing = EXPECTED_TOOLS - headings
        assert not missing, f"undocumented tools: {sorted(missing)}"

    def test_the_local_retrieval_tool_is_in_the_same_catalogue(self) -> None:
        """It is not an MCP tool, but the planner sees one catalogue, so a reader should too."""
        catalogue = (DOCS / "mcp-tool-catalog.md").read_text(encoding="utf-8")

        assert "search_procedures" in catalogue

    def test_it_says_how_to_start_the_server_independently(self) -> None:
        """Named explicitly in §5, and the one instruction a reviewer needs first."""
        catalogue = (DOCS / "mcp-tool-catalog.md").read_text(encoding="utf-8")

        assert "python -m alarm_management" in catalogue
        assert "--transport stdio" in catalogue


class TestTheDiagramShowsWhatItMust:
    @pytest.mark.parametrize(
        ("requirement", "key"), sorted(REQUIRED_DIAGRAM_ELEMENTS.items()), ids=lambda value: value
    )
    def test_the_required_element_has_a_box(
        self, renderer: ModuleType, requirement: str, key: str
    ) -> None:
        assert key in renderer.BY_KEY, f"the diagram no longer shows {requirement} (box {key!r})"

    def test_the_committed_png_matches_the_renderer(self, renderer: ModuleType) -> None:
        """Catches a committed diagram that predates a change to the canvas."""
        from PIL import Image

        with Image.open(DOCS / "architecture-diagram.png") as image:
            assert image.size == (renderer.WIDTH, renderer.HEIGHT), (
                "docs/architecture-diagram.png is stale — re-run "
                "`uv run python scripts/render_architecture_diagram.py`"
            )


class TestTheDiagramGeometryIsSane:
    """The bug this class exists for: arrows that rendered diagonally.

    Arrows were originally drawn centre-to-centre. Between two boxes of different widths that
    is a diagonal, which reads as "this connects to something over there" in a picture whose
    whole point is two straight columns. The fix was to give every arrow an explicit x — and
    an x outside either box reintroduces the problem silently, because nothing about the
    source looks wrong.
    """

    def test_every_arrow_connects_boxes_that_exist(self, renderer: ModuleType) -> None:
        for _, source, target, _ in renderer.ARROWS:
            assert source in renderer.BY_KEY, f"arrow from unknown box {source!r}"
            assert target in renderer.BY_KEY, f"arrow to unknown box {target!r}"

    def test_every_arrow_is_vertical_inside_both_boxes(self, renderer: ModuleType) -> None:
        for x, source, target, label in renderer.ARROWS:
            for key in (source, target):
                left, _, right, _ = renderer.BY_KEY[key].rect
                assert left < x < right, (
                    f"the {label!r} arrow is drawn at x={x}, outside {key!r} "
                    f"({left}..{right}) — it will render as a diagonal"
                )

    def test_every_arrow_points_downwards(self, renderer: ModuleType) -> None:
        """The layout's one convention: the request path runs down the page."""
        for _, source, target, label in renderer.ARROWS:
            assert renderer.BY_KEY[source].rect[3] <= renderer.BY_KEY[target].rect[1], (
                f"the {label!r} arrow runs upwards or overlaps; the layout reads top-down"
            )

    def test_the_boundary_lines_sit_between_boxes_rather_than_through_one(
        self, renderer: ModuleType
    ) -> None:
        """A trust boundary drawn across a box says the boundary runs through that component."""
        span_start, span_end = renderer.BOUNDARY_SPAN
        for y, caption in renderer.BOUNDARIES:
            for box in renderer.BOXES:
                left, top, right, bottom = box.rect
                if right <= span_start or left >= span_end:
                    continue  # the offline RAG column; the line stops before it
                assert not (top < y < bottom), (
                    f"boundary {caption[:40]!r} at y={y} crosses box {box.key!r} ({top}..{bottom})"
                )
