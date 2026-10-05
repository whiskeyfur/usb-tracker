"""Per-hub power and bandwidth allocation: who behind a hub is using what.

Everything here is worked out from a Snapshot, so it costs no extra sysfs
reads and can be tested without hardware.

The supply figures are the USB specification's guarantees, not measurements.
A hub has no way to tell the host how much current its adapter can really
deliver, so "supply" means what the spec says each port must be able to
provide:

  * a root port or a self-powered hub port: 500 mA at USB 2, 900 mA at USB 3
  * a bus-powered hub port: 100 mA at USB 2, 150 mA at USB 3, and the whole
    hub shares whatever its own upstream port gives it

Bus bandwidth is set against the share of the link the host may reserve for
periodic (interrupt and isochronous) transfers: 80% at USB 2 speeds, 90% at
USB 3. Bulk transfers reserve nothing, so they do not appear in that figure.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .monitor import Node, Snapshot

SUPERSPEED_MBPS = 5000.0

PORT_MA_SELF = 500.0
PORT_MA_SELF_SS = 900.0
PORT_MA_BUS = 100.0
PORT_MA_BUS_SS = 150.0

PERIODIC_SHARE = 0.80
PERIODIC_SHARE_SS = 0.90


def superspeed(mbps: float) -> bool:
    return mbps >= SUPERSPEED_MBPS


def port_allowance(hub: Node, child_mbps: float) -> float:
    """What the spec guarantees a device on one of this hub's ports.

    The connection's own speed decides it: a USB 2 device on a USB 3 hub is
    on the hub's USB 2 half and gets the USB 2 figure.
    """
    ss = superspeed(child_mbps) if child_mbps else superspeed(hub.speed_mbps)
    if hub.self_powered or hub.busid.startswith("usb"):
        return PORT_MA_SELF_SS if ss else PORT_MA_SELF
    return PORT_MA_BUS_SS if ss else PORT_MA_BUS


def periodic_limit(hub: Node) -> float:
    """Bytes per second the host may reserve behind this hub's link."""
    share = PERIODIC_SHARE_SS if superspeed(hub.speed_mbps) else PERIODIC_SHARE
    return hub.link_bps * share


@dataclass
class AllocationRow:
    """One device behind the hub."""

    key: str
    label: str
    busid: str
    depth: int                 # 1 = on one of the hub's own ports
    present: bool
    is_hub: bool
    self_powered: bool
    budget_ma: float           # its own declared bMaxPower
    est_ma: float              # its own estimated draw
    port_budget_ma: float      # what it puts on the hub's port (depth 1 only)
    port_est_ma: float
    allowance_ma: float        # what the port guarantees (depth 1 only)
    alloc_bps: float           # reserved periodic bandwidth
    measured_bps: float
    measured: bool
    status: str
    parent_key: str | None = None

    @property
    def over_port(self) -> bool:
        return (self.depth == 1 and self.present and self.allowance_ma > 0
                and self.port_budget_ma > self.allowance_ma)


@dataclass
class HubAllocation:
    hub_key: str
    label: str
    ports: int
    self_powered: bool
    root: bool
    supply_ma: float           # what the spec says the hub can feed downstream
    declared_ma: float         # declared budgets on its ports, live devices
    est_ma: float              # estimated draw on its ports, live devices
    link_bps: float
    periodic_bps: float        # periodic bandwidth the link can reserve
    reserved_bps: float        # reserved by everything behind the hub
    measured_bps: float
    used_ports: int
    rows: list[AllocationRow] = field(default_factory=list)

    @property
    def headroom_ma(self) -> float:
        return self.supply_ma - self.declared_ma

    @property
    def overcommitted(self) -> bool:
        return self.supply_ma > 0 and self.declared_ma > self.supply_ma

    @property
    def over_ports(self) -> list[AllocationRow]:
        return [r for r in self.rows if r.over_port]

    def share(self, ma: float) -> float:
        """Fraction of the hub's supply a figure represents (0 if unknown)."""
        return ma / self.supply_ma if self.supply_ma > 0 else 0.0

    def bw_share(self, bps: float) -> float:
        return bps / self.periodic_bps if self.periodic_bps > 0 else 0.0


def _subtree(snap: Snapshot, key: str) -> list[Node]:
    out: list[Node] = []
    for child in snap.nodes[key].children:
        node = snap.nodes.get(child)
        if node is None:
            continue
        out.append(node)
        out.extend(_subtree(snap, child))
    return out


def _port_load(snap: Snapshot, node: Node) -> tuple[float, float]:
    """(declared, estimated) current this device puts on its upstream port.

    A bus-powered hub feeds everything behind it from that one port, so its
    downstream counts against the port too. A self-powered hub's downstream
    comes from its own adapter and does not.
    """
    if not node.present:
        return 0.0, 0.0
    declared, est = node.budget_ma, node.est_ma
    if node.is_hub and not node.self_powered:
        for below in _subtree(snap, node.key):
            if below.present:
                declared += below.budget_ma
                est += below.est_ma
    return declared, est


def hub_allocation(snap: Snapshot, key: str) -> HubAllocation | None:
    """The allocation table for a hub, or None if the key is not a hub."""
    hub = snap.nodes.get(key)
    if hub is None or not hub.is_hub:
        return None
    root = hub.busid.startswith("usb")

    rows: list[AllocationRow] = []

    def walk(parent: Node, depth: int) -> None:
        for child_key in parent.children:
            node = snap.nodes.get(child_key)
            if node is None:
                continue
            if depth == 1:
                port_budget, port_est = _port_load(snap, node)
                allowance = port_allowance(hub, node.speed_mbps)
            else:
                port_budget = port_est = allowance = 0.0
            rows.append(AllocationRow(
                key=node.key, label=node.label, busid=node.busid, depth=depth,
                present=node.present, is_hub=node.is_hub,
                self_powered=node.self_powered,
                budget_ma=node.budget_ma, est_ma=node.est_ma if node.present else 0.0,
                port_budget_ma=port_budget, port_est_ma=port_est,
                allowance_ma=allowance,
                alloc_bps=node.alloc_bps if node.present else 0.0,
                measured_bps=node.total_bps if node.present else 0.0,
                measured=node.measured, status=node.status,
                parent_key=parent.key))
            walk(node, depth + 1)

    walk(hub, 1)

    direct = [r for r in rows if r.depth == 1 and r.present]
    ports = hub.ports or len({r.busid for r in rows if r.depth == 1})

    if hub.self_powered or root:
        supply = sum(port_allowance(hub, 0.0) for _ in range(ports))
    else:
        # A bus-powered hub has only what its own upstream port gives it,
        # less what its own controller takes.
        upstream = PORT_MA_SELF_SS if superspeed(hub.speed_mbps) else PORT_MA_SELF
        supply = max(0.0, upstream - hub.budget_ma)

    live_below = [r for r in rows if r.present]
    return HubAllocation(
        hub_key=key, label=hub.label, ports=ports,
        self_powered=hub.self_powered, root=root,
        supply_ma=supply,
        declared_ma=sum(r.port_budget_ma for r in direct),
        est_ma=sum(r.port_est_ma for r in direct),
        link_bps=hub.link_bps,
        periodic_bps=periodic_limit(hub),
        reserved_bps=sum(r.alloc_bps for r in live_below),
        measured_bps=sum(r.measured_bps for r in live_below),
        used_ports=len(direct),
        rows=rows,
    )
