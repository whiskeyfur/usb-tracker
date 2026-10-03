"""Tests for the parts that decide what counts as an event.

The sysfs scan is injected, so a plug, a drop and a flap can be replayed
without touching real hardware.
"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from usbtracker import store, sysfs                      # noqa: E402
from usbtracker.app import fmt_ma, nice_ceiling          # noqa: E402
from usbtracker.monitor import Monitor                   # noqa: E402
from usbtracker.store import Store                       # noqa: E402
from usbtracker.sysfs import UsbDevice                   # noqa: E402


def device(busid, *, vid="abcd", pid="1234", serial="", power=100,
           status="active", active_ms=0, susp_ms=0, product="Widget",
           dev_class=0x03) -> UsbDevice:
    dev = UsbDevice(busid=busid)
    dev.is_root_hub = busid.startswith("usb")
    dev.parent_busid = sysfs._parent_busid(busid)
    dev.vid, dev.pid, dev.serial, dev.product = vid, pid, serial, product
    dev.dev_class = dev_class
    dev.speed_mbps = 480.0
    dev.max_power_ma = power
    dev.runtime_status = status
    dev.active_time_ms, dev.suspended_time_ms = active_ms, susp_ms
    dev.key = sysfs.identity_key(dev)
    return dev


class PowerEstimateTests(unittest.TestCase):
    def test_duty_cycle_scales_the_budget(self):
        before = device("3-1", active_ms=1000, susp_ms=1000)
        after = device("3-1", active_ms=1500, susp_ms=1500)   # half the interval
        self.assertAlmostEqual(after.active_fraction(before), 0.5)
        self.assertAlmostEqual(after.estimate_ma(before), 100 * 0.5 + 2.5 * 0.5)

    def test_suspended_device_floors_at_spec_minimum(self):
        before = device("3-1", status="suspended", active_ms=1000, susp_ms=1000)
        after = device("3-1", status="suspended", active_ms=1000, susp_ms=3000)
        self.assertEqual(after.active_fraction(before), 0.0)
        self.assertAlmostEqual(after.estimate_ma(before), sysfs.SUSPEND_MA)

    def test_zero_budget_draws_nothing(self):
        dev = device("usb3", power=0)
        self.assertEqual(dev.estimate_ma(None), 0.0)

    def test_counter_rollback_falls_back_to_status(self):
        """A device that re-enumerates restarts its counters at zero."""
        before = device("3-1", active_ms=90000, susp_ms=0)
        after = device("3-1", active_ms=120, susp_ms=0)
        self.assertEqual(after.active_fraction(before), 1.0)


class IdentityTests(unittest.TestCase):
    def test_serial_survives_a_move_between_ports(self):
        a = device("3-1", serial="SN123")
        b = device("3-4.2", serial="SN123")
        self.assertEqual(a.key, b.key)

    def test_without_a_serial_the_port_is_the_identity(self):
        a, b = device("3-1"), device("3-2")
        self.assertNotEqual(a.key, b.key)

    def test_parent_resolution(self):
        self.assertEqual(sysfs._parent_busid("3-4.1.2"), "3-4.1")
        self.assertEqual(sysfs._parent_busid("3-4"), "usb3")
        self.assertIsNone(sysfs._parent_busid("usb3"))

    def test_sibling_ordering_is_numeric(self):
        order = sorted(["3-10", "3-2", "3-1"], key=sysfs.sort_key)
        self.assertEqual(order, ["3-1", "3-2", "3-10"])


class TrackingTests(unittest.TestCase):
    """Replay a session: plug in, unplug, plug back, and check the log."""

    def setUp(self):
        self.world: dict[str, UsbDevice] = {}
        self.mon = Monitor(db_path=":memory:", interval=1.0, use_kmsg=False,
                           scan_fn=lambda: dict(self.world))
        self.mon._store = Store(":memory:")
        self.mon._rows = {}
        self.store = self.mon._store

    def kinds(self, key):
        return [e[2] for e in self.store.events(key, limit=50)]

    def test_attach_disconnect_reconnect(self):
        hub = device("usb3", power=0, product="Root hub", dev_class=0x09)
        stick = device("3-1", serial="SN1", product="Stick")
        self.world = {"usb3": hub, "3-1": stick}
        snap = self.mon.tick()
        self.assertIn(stick.key, snap.nodes)
        self.assertTrue(snap.nodes[stick.key].present)
        self.assertEqual(self.kinds(stick.key), [store.KIND_ATTACH])

        del self.world["3-1"]
        snap = self.mon.tick()
        node = snap.nodes[stick.key]
        self.assertFalse(node.present)            # kept as a shadow
        self.assertEqual(node.disconnects, 1)
        self.assertEqual(self.kinds(stick.key)[0], store.KIND_DISCONNECT)

        self.world["3-1"] = device("3-1", serial="SN1", product="Stick")
        snap = self.mon.tick()
        self.assertTrue(snap.nodes[stick.key].present)
        # A quick return is flagged as a flap, not a deliberate replug.
        self.assertEqual(self.kinds(stick.key)[:2],
                         [store.KIND_FLAP, store.KIND_CONNECT])

    def test_shadow_keeps_its_place_in_the_tree(self):
        hub = device("usb3", power=0, product="Root hub", dev_class=0x09)
        inner = device("3-1", serial="HUB", product="Hub", dev_class=0x09)
        leaf = device("3-1.1", serial="LEAF", product="Leaf")
        self.world = {"usb3": hub, "3-1": inner, "3-1.1": leaf}
        self.mon.tick()
        del self.world["3-1.1"]
        snap = self.mon.tick()
        self.assertIn(leaf.key, snap.nodes[inner.key].children)
        self.assertFalse(snap.nodes[leaf.key].present)

    def test_subtree_total_sums_downstream_draw(self):
        hub = device("usb3", power=0, product="Root hub", dev_class=0x09)
        inner = device("3-1", serial="HUB", power=100, dev_class=0x09)
        leaf = device("3-1.1", serial="LEAF", power=500)
        self.world = {"usb3": hub, "3-1": inner, "3-1.1": leaf}
        snap = self.mon.tick()
        self.assertAlmostEqual(snap.nodes[inner.key].subtree_ma, 600.0)
        self.assertAlmostEqual(snap.nodes[leaf.key].subtree_ma, 500.0)
        self.assertAlmostEqual(snap.nodes[hub.key].subtree_ma, 600.0)

    def test_config_and_power_changes_are_logged(self):
        self.world = {"3-1": device("3-1", serial="SN1", power=100)}
        self.mon.tick()
        self.world = {"3-1": device("3-1", serial="SN1", power=500)}
        self.mon.tick()
        self.assertIn(store.KIND_CONFIG, self.kinds("abcd:1234:SN1"))

    def test_suspend_and_resume_are_logged(self):
        self.world = {"3-1": device("3-1", serial="SN1")}
        self.mon.tick()
        self.world = {"3-1": device("3-1", serial="SN1", status="suspended")}
        self.mon.tick()
        self.assertEqual(self.kinds("abcd:1234:SN1")[0], store.KIND_SUSPEND)

    def test_samples_are_recorded_per_tick(self):
        self.world = {"3-1": device("3-1", serial="SN1")}
        self.mon.tick()
        self.mon.tick()
        self.assertEqual(len(self.store.samples("abcd:1234:SN1")), 2)


class StoreTests(unittest.TestCase):
    def test_bucketing_averages_long_windows(self):
        db = Store(":memory:")
        t0 = time.time()
        for i in range(100):
            db.add_sample(t0 + i, "k", float(i), 500, float(i), "active", 0)
        db.commit()
        buckets = db.samples_bucketed("k", t0, 10.0)
        self.assertEqual(len(buckets), 10)
        self.assertAlmostEqual(buckets[0][1], 4.5)

    def test_forget_absent_clears_history(self):
        db = Store(":memory:")
        now = time.time()
        db.upsert_device(key="k", busid="3-1", parent_key=None, vid="a", pid="b",
                         serial="c", product="p", manufacturer="m",
                         class_hint="HID", speed_mbps=480, max_power_ma=100,
                         version="2.00", is_hub=False, self_powered=False,
                         ts=now, present=False)
        db.add_event(now, "k", store.KIND_DISCONNECT, "")
        db.add_sample(now, "k", 1, 2, 3, "active", 0)
        db.commit()
        self.assertEqual(db.forget_absent(), 1)
        self.assertEqual(db.devices(), [])
        self.assertEqual(db.events("k"), [])
        self.assertEqual(db.samples("k"), [])


class FormattingTests(unittest.TestCase):
    def test_axis_ceilings_are_round_numbers(self):
        self.assertEqual(nice_ceiling(224), 250)
        self.assertEqual(nice_ceiling(2.4), 2.5)
        self.assertEqual(nice_ceiling(0), 10)

    def test_milliamp_formatting(self):
        self.assertEqual(fmt_ma(0), "–")
        self.assertEqual(fmt_ma(2.5), "2.5 mA")
        self.assertEqual(fmt_ma(480), "480 mA")


if __name__ == "__main__":
    unittest.main(verbosity=2)
