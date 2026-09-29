"""Render `docs/architecture-diagram.png` from this file.

The diagram is a required deliverable and it has to stay true as the code moves, so it is
drawn from a declaration in source rather than exported from a drawing tool. Pillow only —
no graphviz, no mermaid CLI, no network — because the machine this was built on has none of
those and a diagram that cannot be regenerated goes stale on the first refactor.

    uv run python scripts/render_architecture_diagram.py

Every box the submission guidelines require is listed in `BOXES` below, and `test_docs.py`
asserts the list against those requirements, so a box cannot be dropped by accident.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from PIL import Image, ImageDraw, ImageFont

OUTPUT = Path(__file__).resolve().parents[1] / "docs" / "architecture-diagram.png"

#: Drawn at 2x and downsampled, which is the cheapest way to get non-jagged text and lines
#: out of Pillow without a font-hinting rabbit hole.
SCALE = 2
WIDTH, HEIGHT = 1760, 1180

INK = (26, 32, 44)
MUTED = (100, 116, 139)
PAPER = (251, 251, 253)
GRID = (226, 232, 240)

#: One colour per concern, so the eye groups the request path, the MCP hop, the source
#: system, the offline RAG path and observability without reading the labels first.
FILLS = {
    "gui": ((234, 242, 255), (59, 130, 246)),
    "backend": ((240, 253, 244), (34, 150, 94)),
    "mcp": ((255, 247, 237), (234, 130, 40)),
    "source": ((254, 242, 242), (220, 80, 80)),
    "rag": ((245, 243, 255), (124, 100, 235)),
    "obs": ((248, 250, 252), (100, 116, 139)),
}

Kind = Literal["gui", "backend", "mcp", "source", "rag", "obs"]


class Box:
    def __init__(
        self,
        key: str,
        kind: Kind,
        rect: tuple[int, int, int, int],
        title: str,
        lines: tuple[str, ...] = (),
        *,
        dashed: bool = False,
    ) -> None:
        self.key = key
        self.kind = kind
        self.rect = rect
        self.title = title
        self.lines = lines
        self.dashed = dashed

    @property
    def top(self) -> tuple[int, int]:
        x1, y1, x2, _ = self.rect
        return (x1 + x2) // 2, y1

    @property
    def bottom(self) -> tuple[int, int]:
        x1, _, x2, y2 = self.rect
        return (x1 + x2) // 2, y2

    @property
    def left(self) -> tuple[int, int]:
        x1, y1, _, y2 = self.rect
        return x1, (y1 + y2) // 2

    @property
    def right(self) -> tuple[int, int]:
        _, y1, x2, y2 = self.rect
        return x2, (y1 + y2) // 2


#: The eleven elements §9 of the submission guidelines requires, plus the internals that
#: make the request path legible. Keys are asserted by `tests/unit/test_docs.py`.
BOXES: tuple[Box, ...] = (
    Box(
        "gui",
        "gui",
        (96, 96, 700, 214),
        "GUI — React + Vite + TypeScript",
        (
            "question box · answer with inline [DOC §N] citations",
            "evidence list · expandable MCP execution trace · health badge",
        ),
    ),
    Box(
        "orchestration",
        "backend",
        (96, 300, 1040, 560),
        "Copilot orchestration — apps/backend (FastAPI :8080)",
        (),
    ),
    Box(
        "planner",
        "backend",
        (124, 372, 400, 470),
        "Planner + Orchestrator",
        ("bounded step loop", "native tools or JSON plan"),
    ),
    Box(
        "registry",
        "backend",
        (420, 372, 700, 470),
        "Tool registry",
        ("one catalogue:", "MCP tools + local RAG tool"),
    ),
    Box(
        "synthesis",
        "backend",
        (720, 372, 1012, 470),
        "Synthesis + citation guard",
        ("grounded answer, invented", "references stripped"),
    ),
    Box(
        "mcp_client",
        "backend",
        (124, 492, 400, 542),
        "MCP client (session per request)",
    ),
    Box(
        "local_tool",
        "rag",
        (420, 492, 1012, 542),
        "local tool: search_procedures  →  retrieval index",
    ),
    Box(
        "mcp_server",
        "mcp",
        (96, 660, 700, 822),
        "MCP server — alarm-management (:9100/mcp, or stdio)",
        (
            "5 tools · typed in/out contracts · input + output validation",
            "pagination · timeout · retry · API error → MCP error mapping",
        ),
    ),
    Box(
        "connector",
        "mcp",
        (124, 762, 672, 806),
        "connectors/alarm_api — httpx + tenacity, bearer auth, trace headers",
    ),
    Box(
        "alarm_api",
        "source",
        (96, 916, 620, 1034),
        "Alarm Management API (:8000)",
        (
            "assets · alarms · summary · recurring · operator actions",
            "simulator in apps/alarm_api, seeded relative to now",
        ),
    ),
    Box(
        "secondary",
        "source",
        (660, 916, 1040, 1034),
        "Optional secondary source",
        ("historian / ticketing —", "not implemented (out of scope)"),
        dashed=True,
    ),
    Box(
        "documents",
        "rag",
        (1128, 96, 1664, 214),
        "Document store — rag/documents",
        (
            "4 authored procedures, markdown + YAML frontmatter",
            "OP-BFP-101 · TS-BFP · MM-CP-MAINT · SAF-PUMP-LOTO",
        ),
    ),
    Box(
        "ingestion",
        "rag",
        (1128, 286, 1664, 466),
        "RAG ingestion pipeline — make ingest",
        (
            "loader (frontmatter → typed metadata)",
            "sanitizer (strips model-directed instructions)",
            "chunker (one chunk = one numbered section)",
            "embedder (bge-small-en-v1.5, 384-d)",
        ),
    ),
    Box(
        "index",
        "rag",
        (1128, 538, 1664, 700),
        "Retrieval index",
        (
            "embedded Qdrant (dense cosine) + BM25 (lexical)",
            "reciprocal-rank fusion · reference pinning",
            "relevance floor → low-confidence path",
        ),
    ),
    Box(
        "observability",
        "obs",
        (1128, 800, 1664, 1034),
        "Observability",
        (
            "TraceEvent per step: plan · tool_discovery",
            "mcp_tool_call · rag_retrieval · llm_call",
            "",
            "streamed over SSE, stored per conversation,",
            "replayable at GET /trace/{id}",
            "",
            "structured JSON logs · secrets redacted",
            "chunk ids and scores only, never bodies",
        ),
    ),
)

BY_KEY = {box.key: box for box in BOXES}

#: (x, from-box, to-box, label) — every hop is vertical, drawn at an x inside both boxes so
#: the picture reads as two straight columns rather than a web of diagonals.
ARROWS: tuple[tuple[int, str, str, str], ...] = (
    (300, "gui", "orchestration", "POST /chat  ·  SSE stream back"),
    (262, "mcp_client", "mcp_server", "MCP: tools/list, tools/call"),
    (300, "connector", "alarm_api", "HTTPS + Bearer + trace_id"),
    (1396, "documents", "ingestion", "offline, on demand"),
    (1396, "ingestion", "index", "embed + upsert"),
)

#: The observability spine: a dashed line every layer feeds, ending at the trace store.
#: `x` is the lane between the two columns; the feeds are (y, x-start).
TRACE_LANE = 1076
TRACE_FEEDS: tuple[tuple[int, int], ...] = ((545, 1040), (741, 708), (1060, 358))
TRACE_EXIT_Y = 770

#: Horizontal extent of the boundary lines: the request-path column only, stopping short of
#: the observability lane. The offline RAG column to its right is not on the request path, so a
#: boundary drawn across it would claim something untrue. `tests/unit/test_docs.py` reads this
#: to check which boxes a boundary may legitimately pass between.
BOUNDARY_SPAN = (80, 1072)

#: Dashed lines across the request path, each naming what does and does not cross it.
BOUNDARIES: tuple[tuple[int, str], ...] = (
    (262, "trust boundary — browser holds no credential; operator text is never logged as a URL"),
    (
        610,
        "process boundary — the copilot holds no alarm-API token and cannot reach the API directly",
    ),
    (
        872,
        "auth boundary — ALARM_API_TOKEN lives only here; sent as Bearer, never returned or traced",
    ),
)


def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = (
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
        if bold
        else "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans%s.ttf" % ("-Bold" if bold else ""),
    )
    for path in candidates:
        try:
            return ImageFont.truetype(path, size * SCALE)
        except OSError:
            continue
    raise SystemExit("no TrueType font found; install DejaVu or run on macOS")


def _dashed(
    draw: ImageDraw.ImageDraw,
    start: tuple[int, int],
    end: tuple[int, int],
    colour: tuple[int, int, int],
    width: int = 2,
    dash: int = 9,
) -> None:
    """Draw a dashed line in diagram coordinates (scaled here, like every other helper)."""
    (x1, y1) = (start[0] * SCALE, start[1] * SCALE)
    (x2, y2) = (end[0] * SCALE, end[1] * SCALE)
    dash *= SCALE
    span = max(abs(x2 - x1), abs(y2 - y1))
    if span == 0:
        return
    steps = span // dash
    for step in range(0, steps, 2):
        a = step / steps
        b = min((step + 1) / steps, 1.0)
        draw.line(
            [
                (x1 + (x2 - x1) * a, y1 + (y2 - y1) * a),
                (x1 + (x2 - x1) * b, y1 + (y2 - y1) * b),
            ],
            fill=colour,
            width=width * SCALE,
        )


def _dashed_rect(
    draw: ImageDraw.ImageDraw, rect: tuple[int, int, int, int], colour: tuple[int, int, int]
) -> None:
    x1, y1, x2, y2 = rect
    for start, end in (
        ((x1, y1), (x2, y1)),
        ((x2, y1), (x2, y2)),
        ((x2, y2), (x1, y2)),
        ((x1, y2), (x1, y1)),
    ):
        _dashed(draw, start, end, colour)


def _arrowhead(
    draw: ImageDraw.ImageDraw,
    tip: tuple[int, int],
    direction: str,
    colour: tuple[int, int, int],
) -> None:
    x, y = tip[0] * SCALE, tip[1] * SCALE
    size = 9 * SCALE
    if direction == "down":
        points = [(x, y), (x - size // 2, y - size), (x + size // 2, y - size)]
    elif direction == "up":
        points = [(x, y), (x - size // 2, y + size), (x + size // 2, y + size)]
    else:  # left
        points = [(x, y), (x + size, y - size // 2), (x + size, y + size // 2)]
    draw.polygon(points, fill=colour)


def render() -> Path:
    image = Image.new("RGB", (WIDTH * SCALE, HEIGHT * SCALE), PAPER)
    draw = ImageDraw.Draw(image)

    title = _font(26, bold=True)
    subtitle = _font(14)
    box_title = _font(15, bold=True)
    body = _font(13)
    edge = _font(12)
    small = _font(11)

    draw.text(
        (96 * SCALE, 34 * SCALE), "Alarm Investigation & Procedure Guidance Copilot", INK, title
    )
    draw.text(
        (96 * SCALE, 68 * SCALE),
        "Request path on the left, offline retrieval path on the right. "
        "The alarm API is reachable only through the MCP server.",
        MUTED,
        subtitle,
    )

    # Boundaries first, so boxes sit on top of them.
    span_start, span_end = BOUNDARY_SPAN
    for y, label in BOUNDARIES:
        _dashed(draw, (span_start, y), (span_end, y), (200, 90, 90), width=2, dash=11)
        draw.text(((span_start + 4) * SCALE, (y + 8) * SCALE), label, (185, 80, 80), small)

    for box in BOXES:
        fill, outline = FILLS[box.kind]
        x1, y1, x2, y2 = (value * SCALE for value in box.rect)
        radius = 12 * SCALE
        if box.dashed:
            draw.rounded_rectangle((x1, y1, x2, y2), radius, fill=PAPER, outline=None)
            _dashed_rect(draw, box.rect, outline)
        else:
            draw.rounded_rectangle(
                (x1, y1, x2, y2), radius, fill=fill, outline=outline, width=2 * SCALE
            )

        text_x = box.rect[0] + 16
        text_y = box.rect[1] + 13
        draw.text((text_x * SCALE, text_y * SCALE), box.title, INK, box_title)
        text_y += 26
        for line in box.lines:
            if line:
                draw.text((text_x * SCALE, text_y * SCALE), line, MUTED, body)
            text_y += 21

    for x, source_key, target_key, label in ARROWS:
        source, target = BY_KEY[source_key], BY_KEY[target_key]
        y1, y2 = source.rect[3], target.rect[1]
        colour = FILLS[source.kind][1]
        draw.line([(x * SCALE, y1 * SCALE), (x * SCALE, y2 * SCALE)], fill=colour, width=2 * SCALE)
        _arrowhead(draw, (x, y2), "down", colour)
        # Hugging the source box rather than centred on the edge: the gaps between rows are
        # narrow and a centred label collides with the trust-boundary line drawn across them.
        draw.text(((x + 14) * SCALE, (y1 + 6) * SCALE), label, MUTED, edge)

    # Retrieval into the local tool: out of the index, up the lane, into the registry's
    # non-MCP tool. The one edge that crosses the columns during a request.
    index, local_tool = BY_KEY["index"], BY_KEY["local_tool"]
    rag_lane = 1104
    draw.line(
        [
            (index.left[0] * SCALE, index.left[1] * SCALE),
            (rag_lane * SCALE, index.left[1] * SCALE),
            (rag_lane * SCALE, local_tool.right[1] * SCALE),
            (local_tool.right[0] * SCALE, local_tool.right[1] * SCALE),
        ],
        fill=FILLS["rag"][1],
        width=2 * SCALE,
    )
    _arrowhead(draw, local_tool.right, "left", FILLS["rag"][1])

    # The observability spine. Dashed, because nothing on it is part of the request path:
    # every layer writes one trace event per step into the same store as a side effect.
    observability = BY_KEY["observability"]
    _dashed(draw, (TRACE_LANE, TRACE_FEEDS[0][0]), (TRACE_LANE, TRACE_FEEDS[-1][0]), GRID, dash=10)
    for y, x_start in TRACE_FEEDS:
        _dashed(draw, (x_start, y), (TRACE_LANE, y), GRID, dash=10)
    # The API's feed leaves from underneath it, to stay clear of the secondary-source box.
    _dashed(draw, BY_KEY["alarm_api"].bottom, (BY_KEY["alarm_api"].bottom[0], 1060), GRID, dash=10)
    _dashed(draw, (TRACE_LANE, TRACE_EXIT_Y), (observability.top[0], TRACE_EXIT_Y), GRID, dash=10)
    _dashed(
        draw,
        (observability.top[0], TRACE_EXIT_Y),
        (observability.top[0], observability.top[1]),
        GRID,
        dash=10,
    )
    _arrowhead(draw, observability.top, "down", MUTED)
    draw.text((716 * SCALE, 750 * SCALE), "every step emits one trace event", MUTED, edge)

    # `Image.Resampling.LANCZOS`, not the `Image.LANCZOS` alias: the alias works at runtime but
    # Pillow's type stubs do not carry it, so mypy rejects it.
    image = image.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    image.save(OUTPUT, optimize=True)
    return OUTPUT


if __name__ == "__main__":
    print(f"wrote {render()}")
