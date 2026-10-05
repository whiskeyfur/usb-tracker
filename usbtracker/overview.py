"""Whole-system overview: power or bandwidth flowing from controllers to devices.

Drawn as a Sankey diagram. Each controller is a column-0 node, every hub and
device sits in the column for its depth, and a ribbon runs from each parent
to each child as wide as what that child (and everything behind it) takes.
Colour carries health, so a sick branch shows up without reading a number.

The flow and the geometry are both worked out here, without GTK, so they can
be tested; the window only paints the rectangles and ribbons it is handed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .allocation import hub_allocation
from .monitor import Snapshot

METRIC_POWER = "power"
METRIC_BANDWIDTH = "bandwidth"

HEALTH_OK = "ok"
HEALTH_SUSPENDED = "suspended"
HEALTH_STRAINED = "strained"     # over a port's allowance, or a hub over-committed
HEALTH_FAULT = "fault"           # over-current trips or bus errors
HEALTH_LOST = "lost"

# Worst first: a node shows the worst thing that is true of it.
HEALTH_ORDER = (HEALTH_FAULT, HEALTH_STRAINED, HEALTH_LOST, HEALTH_SUSPENDED,
                HEALTH_OK)


@dataclass
class FlowNode:
    key: str
    label: str
    depth: int
    own: float                 # what the device itself takes
    value: float               # own + everything behind it
    health: str
    reasons: list[str] = field(default_factory=list)
    present: bool = True
    is_hub: bool = False
    children: list[str] = field(default_factory=list)
    parent: str | None = None


@dataclass
class Flow:
    metric: str
    nodes: dict[str, FlowNode]
    roots: list[str]
    total: float
    counts: dict[str, int]     # health -> how many devices

    def order(self) -> list[str]:
        """Depth-first, so children stay in their parent's order and
        ribbons never cross."""
        out: list[str] = []

        def walk(key: str) -> None:
            out.append(key)
            for child in self.nodes[key].children:
                walk(child)

        for root in self.roots:
            walk(root)
        return out


def build(snap: Snapshot, metric: str = METRIC_POWER,
          include_lost: bool = True) -> Flow:
    """Turn a snapshot into a flow of declared power or reserved bandwidth."""
    strained: dict[str, list[str]] = {}
    for key, node in snap.nodes.items():
        if not (node.is_hub and node.present):
            continue
        alloc = hub_allocation(snap, key)
        if alloc is None:
            continue
        if alloc.overcommitted:
            strained.setdefault(key, []).append(
                f"over-committed: {alloc.declared_ma:.0f} of "
                f"{alloc.supply_ma:.0f} mA declared")
        if alloc.periodic_bps and alloc.reserved_bps > alloc.periodic_bps:
            strained.setdefault(key, []).append("periodic bandwidth over-reserved")
        for row in alloc.over_ports:
            strained.setdefault(row.key, []).append(
                f"{row.port_budget_ma:.0f} mA on a {row.allowance_ma:.0f} mA port")

    nodes: dict[str, FlowNode] = {}

    def add(key: str, depth: int, parent: str | None) -> FlowNode | None:
        node = snap.nodes.get(key)
        if node is None or (node.lost and not include_lost):
            return None
        reasons: list[str] = []
        if not node.present:
            health = HEALTH_LOST
            reasons.append("not connected")
        elif node.over_current or node.errors:
            health = HEALTH_FAULT
            if node.over_current:
                reasons.append(f"over-current count {node.over_current}")
            if node.errors:
                reasons.append(f"{node.errors} errors logged")
        elif key in strained:
            health = HEALTH_STRAINED
        elif node.status == "suspended":
            health = HEALTH_SUSPENDED
            reasons.append("suspended")
        else:
            health = HEALTH_OK
        reasons.extend(strained.get(key, []))
        if node.present and node.disconnects:
            reasons.append(f"{node.disconnects} drops")

        if not node.present:
            own = 0.0
        elif metric == METRIC_BANDWIDTH:
            own = node.alloc_bps
        else:
            own = node.budget_ma
        flow = FlowNode(key=key, label=node.label, depth=depth, own=own,
                        value=own, health=health, reasons=reasons,
                        present=node.present, is_hub=node.is_hub, parent=parent)
        nodes[key] = flow
        for child in node.children:
            sub = add(child, depth + 1, key)
            if sub is not None:
                flow.children.append(child)
                flow.value += sub.value
        return flow

    roots = [r for r in snap.roots if add(r, 0, None) is not None]

    counts = {h: 0 for h in HEALTH_ORDER}
    for flow in nodes.values():
        counts[flow.health] += 1
    return Flow(metric=metric, nodes=nodes, roots=roots,
                total=sum(nodes[r].value for r in roots), counts=counts)


@dataclass
class Box:
    key: str
    x: float
    y: float
    w: float
    h: float


@dataclass
class Ribbon:
    parent: str
    child: str
    x0: float                  # parent's right edge
    y0: float                  # top of the ribbon at the parent
    x1: float                  # child's left edge
    y1: float                  # top of the ribbon at the child
    width: float


@dataclass
class Layout:
    boxes: dict[str, Box]
    ribbons: list[Ribbon]
    columns: int


def layout(flow: Flow, width: float, height: float, *, margin: float = 12.0,
           node_w: float = 10.0, label_w: float = 130.0, min_h: float = 9.0,
           pad: float = 6.0) -> Layout:
    """Place every node and ribbon inside width x height.

    A node is at least min_h tall so a device taking nothing still shows,
    and always at least as tall as the ribbons it feeds, so the ribbons
    leave a parent stacked edge to edge.
    """
    order = flow.order()
    if not order:
        return Layout({}, [], 0)
    columns = max(flow.nodes[k].depth for k in order) + 1
    by_col: list[list[str]] = [[] for _ in range(columns)]
    for key in order:
        by_col[flow.nodes[key].depth].append(key)

    usable = max(20.0, height - 2 * margin)
    busiest = max(len(col) for col in by_col)
    # With hundreds of devices the minimum sizes alone would not fit.
    if busiest * (min_h + pad) > usable:
        per = usable / busiest
        pad = min(pad, per * 0.25)
        min_h = per - pad

    def heights(scale: float) -> dict[str, float]:
        out: dict[str, float] = {}
        for key in reversed(order):           # children before parents
            node = flow.nodes[key]
            below = sum(out[c] for c in node.children)
            out[key] = max(min_h, node.own * scale + below)
        return out

    def overflow(h: dict[str, float]) -> float:
        return max(sum(h[k] for k in col) + pad * (len(col) - 1)
                   for col in by_col if col)

    peak = max((flow.nodes[r].value for r in flow.roots), default=0.0)
    scale = usable / flow.total if flow.total > 0 else 0.0
    h = heights(scale)
    for _ in range(40):
        extent = overflow(h)
        if extent <= usable + 0.5 or scale <= 0 or peak <= 0:
            break
        scale *= max(0.5, min(0.98, usable / extent))
        h = heights(scale)

    span = max(1.0, width - 2 * margin - node_w - label_w)
    step = span / (columns - 1) if columns > 1 else 0.0
    boxes: dict[str, Box] = {}
    for col, keys in enumerate(by_col):
        total = sum(h[k] for k in keys) + pad * max(0, len(keys) - 1)
        y = margin + max(0.0, (usable - total) / 2)
        x = margin + col * step
        for key in keys:
            boxes[key] = Box(key, x, y, node_w, h[key])
            y += h[key] + pad

    ribbons: list[Ribbon] = []
    for key in order:
        node = flow.nodes[key]
        parent = boxes[key]
        y0 = parent.y
        for child in node.children:
            box = boxes[child]
            ribbons.append(Ribbon(key, child, parent.x + parent.w, y0, box.x,
                                  box.y, box.h))
            y0 += box.h
    return Layout(boxes, ribbons, columns)


def hit(lay: Layout, x: float, y: float, slop: float = 3.0) -> str | None:
    for box in lay.boxes.values():
        if (box.x - slop <= x <= box.x + box.w + slop
                and box.y - 1 <= y <= box.y + box.h + 1):
            return box.key
    return None
