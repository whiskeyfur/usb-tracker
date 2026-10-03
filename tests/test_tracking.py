"""Tests for the parts that decide what counts as an event.

The sysfs scan is injected, so a plug, a drop and a flap can be replayed
without touching real hardware.
"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from usbtracker import power, store, sysfs               # noqa: E402
from usbtracker.app import fmt_bps, fmt_ma, nice_ceiling  # noqa: E402
from usbtracker.monitor import Monitor                   # noqa: E402
from usbtracker.store import Store                       # noqa: E402
from usbtracker.sysfs import Endpoint, Interface, UsbDevice   # noqa: E402


def endpoint(kind="Interrupt", packet=64, mult=1, interval_us=1000.0,
             direction="in") -> Endpoint:
    return Endpoint(name="ep_81", address=0x81, kind=kind, direction=direction,
                    max_packet=packet, mult=mult, interval_us=interval_us)


def device(busid, *, vid="abcd", pid="1234", serial="", power=100,
           status="active", active_ms=0, susp_ms=0, product="Widget",
           dev_class=0x03, endpoints=(), counters=None) -> UsbDevice:
    dev = UsbDevice(busid=busid)
    dev.is_root_hub = busid.startswith("usb")
    dev.parent_busid = sysfs._parent_busid(busid)
    dev.vid, dev.pid, dev.serial, dev.product = vid, pid, serial, product
    dev.dev_class = dev_class
    dev.speed_mbps = 480.0
    dev.max_power_ma = power
    dev.runtime_status = status
    dev.active_time_ms, dev.suspended_time_ms = active_ms, susp_ms
    if endpoints:
        dev.interfaces = [Interface(name=f"{busid}:1.0", number=0, cls=dev_class,
                                    subclass=0, protocol=0, driver="",
                                    endpoints=list(endpoints))]
    if counters is not None:
        dev.counter_source, dev.rx_bytes, dev.tx_bytes = counters
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


class BandwidthTests(unittest.TestCase):
    def test_reserved_bandwidth_from_endpoint_descriptor(self):
        # 64 bytes every 1 ms is 64 kB/s of reserved bus time
        dev = device("3-1", endpoints=[endpoint(packet=64, interval_us=1000.0)])
        self.assertAlmostEqual(dev.reserved_bps, 64000.0)

    def test_high_speed_multiplier_counts(self):
        dev = device("3-1", endpoints=[endpoint(packet=1024, mult=3,
                                                interval_us=125.0)])
        self.assertAlmostEqual(dev.reserved_bps, 1024 * 3 / 125e-6)

    def test_bulk_endpoints_reserve_nothing(self):
        """Bulk is best-effort: the controller sets no bandwidth aside."""
        dev = device("3-1", endpoints=[endpoint(kind="Bulk", packet=512,
                                                interval_us=0.0)])
        self.assertEqual(dev.reserved_bps, 0.0)

    def test_idle_isochronous_alt_setting_reserves_nothing(self):
        """A camera that is not streaming sits on a zero-bandwidth setting."""
        dev = device("3-1", endpoints=[endpoint(kind="Isoc", packet=0,
                                                interval_us=1000.0)])
        self.assertEqual(dev.reserved_bps, 0.0)

    def test_interval_parsing(self):
        self.assertEqual(sysfs._interval_us("125us"), 125.0)
        self.assertEqual(sysfs._interval_us("10ms"), 10000.0)
        self.assertEqual(sysfs._interval_us("0ms"), 0.0)

    def test_throughput_from_counter_deltas(self):
        before = device("3-1", counters=("block:sdb", 1000, 500))
        after = device("3-1", counters=("block:sdb", 3000, 1500))
        rx, tx = after.throughput(before, 2.0)
        self.assertAlmostEqual(rx, 1000.0)
        self.assertAlmostEqual(tx, 500.0)

    def test_no_counter_means_no_measurement(self):
        before, after = device("3-1"), device("3-1")
        self.assertEqual(after.throughput(before, 2.0), (0.0, 0.0))

    def test_counter_reset_is_not_a_spike(self):
        before = device("3-1", counters=("block:sdb", 9_000_000, 0))
        after = device("3-1", counters=("block:sdb", 4096, 0))
        self.assertEqual(after.throughput(before, 2.0), (0.0, 0.0))

    def test_changed_backing_device_is_not_a_spike(self):
        before = device("3-1", counters=("block:sdb", 9_000_000, 0))
        after = device("3-1", counters=("block:sdc", 10, 0))
        self.assertEqual(after.throughput(before, 2.0), (0.0, 0.0))


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

    def test_bandwidth_reservation_change_is_logged(self):
        """A camera starting to stream shows up as a reservation appearing."""
        self.world = {"3-1": device("3-1", serial="SN1",
                                    endpoints=[endpoint(kind="Isoc", packet=0,
                                                        interval_us=1000.0)])}
        self.mon.tick()
        self.world = {"3-1": device("3-1", serial="SN1",
                                    endpoints=[endpoint(kind="Isoc", packet=3072,
                                                        mult=2,
                                                        interval_us=125.0)])}
        snap = self.mon.tick()
        self.assertIn(store.KIND_BANDWIDTH, self.kinds("abcd:1234:SN1"))
        self.assertGreater(snap.nodes["abcd:1234:SN1"].alloc_bps, 1e6)

    def test_subtree_bandwidth_sums_downstream_traffic(self):
        hub = device("usb3", power=0, product="Root hub", dev_class=0x09)
        inner = device("3-1", serial="HUB", dev_class=0x09)
        leaf = device("3-1.1", serial="LEAF", counters=("block:sdb", 0, 0))
        self.world = {"usb3": hub, "3-1": inner, "3-1.1": leaf}
        self.mon.tick()
        self.world["3-1.1"] = device("3-1.1", serial="LEAF",
                                     counters=("block:sdb", 2048, 1024))
        snap = self.mon.tick()
        leaf_node = snap.nodes["abcd:1234:LEAF"]
        self.assertGreater(leaf_node.total_bps, 0)
        self.assertAlmostEqual(snap.nodes["abcd:1234:HUB"].sub_bps,
                               leaf_node.total_bps)

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


class OutageTests(unittest.TestCase):
    """The case this app exists for: a hub taking its whole tree down."""

    def setUp(self):
        self.world: dict[str, UsbDevice] = {}
        self.mon = Monitor(db_path=":memory:", interval=1.0, use_kmsg=False,
                           scan_fn=lambda: dict(self.world))
        self.mon._store = Store(":memory:")
        self.mon._rows = {}
        self.store = self.mon._store

    def tree(self):
        """A root hub, a hub on port 4, a second hub below it, four leaves."""
        world = {
            "usb3": device("usb3", serial="PCI3", product="Root hub",
                           dev_class=0x09, power=0),
            "3-4": device("3-4", serial="HUBA", product="USB2.1 Hub",
                          dev_class=0x09),
            "3-4.1": device("3-4.1", serial="HUBB", product="USB2.1 Hub",
                            dev_class=0x09),
        }
        for n in range(1, 5):
            world[f"3-4.1.{n}"] = device(f"3-4.1.{n}", serial=f"LEAF{n}",
                                         product=f"Device {n}")
        world["3-9"] = device("3-9", serial="OTHER", product="Unrelated device")
        return world

    def outage_details(self):
        return [e[3] for e in self.store.outages()]

    def test_hub_dropping_is_reported_once_and_attributed_to_the_hub(self):
        self.world = self.tree()
        self.mon.tick()

        # The hub and everything behind it vanish in the same poll.
        for busid in ("3-4", "3-4.1", "3-4.1.1", "3-4.1.2", "3-4.1.3", "3-4.1.4"):
            self.world.pop(busid, None)
        self.mon.tick()

        details = self.outage_details()
        self.assertEqual(len(details), 1, "one outage, not six disconnects")
        self.assertIn("hub at 3-4", details[0])
        self.assertIn("6 devices", details[0])

    def test_unrelated_device_is_not_swept_into_the_outage(self):
        self.world = self.tree()
        self.mon.tick()
        for busid in ("3-4", "3-4.1", "3-4.1.1", "3-4.1.2"):
            self.world.pop(busid)
        self.mon.tick()
        self.assertIn("3-4", self.outage_details()[0])
        kinds = [e[2] for e in self.store.events("abcd:1234:OTHER")]
        self.assertEqual(kinds, [store.KIND_ATTACH],
                         "a device that stayed up never dropped")

    def test_recovery_is_logged_with_how_long_it_took(self):
        self.world = self.tree()
        self.mon.tick()
        gone = {b: self.world[b] for b in
                ("3-4", "3-4.1", "3-4.1.1", "3-4.1.2", "3-4.1.3", "3-4.1.4")}
        for busid in gone:
            del self.world[busid]
        self.mon.tick()
        self.world.update(gone)
        self.mon.tick()
        details = self.outage_details()
        self.assertEqual(len(details), 2)
        self.assertIn("back after", details[0])
        self.assertIn("6 of 6 devices returned", details[0])
        self.assertIsNone(self.mon._outage)

    def test_outage_records_what_the_hub_was_carrying(self):
        """The brown-out question: how much load was on it when it went?"""
        self.world = self.tree()
        self.mon.tick()
        self.mon.tick()                       # a sample to look back at
        for busid in ("3-4", "3-4.1", "3-4.1.1", "3-4.1.2", "3-4.1.3", "3-4.1.4"):
            self.world.pop(busid)
        self.mon.tick()
        detail = self.outage_details()[0]
        self.assertIn("carrying", detail)
        self.assertIn("mA downstream at the time", detail)
        # the hub plus four leaves, at the 100 mA each the helper declares
        self.assertIn("600 mA", detail)

    def test_outage_notes_a_peak_higher_than_the_last_sample(self):
        self.world = self.tree()
        self.mon.tick()
        for n in range(1, 5):                  # a heavy moment, then calm
            self.world[f"3-4.1.{n}"] = device(f"3-4.1.{n}", serial=f"LEAF{n}",
                                              power=500)
        self.mon.tick()
        for n in range(1, 5):
            self.world[f"3-4.1.{n}"] = device(f"3-4.1.{n}", serial=f"LEAF{n}",
                                              power=100)
        self.mon.tick()
        for busid in ("3-4", "3-4.1", "3-4.1.1", "3-4.1.2", "3-4.1.3", "3-4.1.4"):
            self.world.pop(busid)
        self.mon.tick()
        detail = self.outage_details()[0]
        self.assertIn("peaking at", detail)
        self.assertIn("2200 mA", detail)

    def test_outage_without_samples_omits_the_load(self):
        self.world = self.tree()
        self.mon.tick()
        for busid in ("3-4.1.1", "3-4.1.2", "3-4.1.3"):
            self.world.pop(busid)
        # no sample exists for the ancestor that survived? it does -- but a
        # scope with no recorded samples must not invent a figure
        self.mon._key_for_busid = lambda _b: "nonexistent"
        self.mon.tick()
        self.assertNotIn("carrying", self.outage_details()[0])

    def test_a_single_unplug_is_not_an_outage(self):
        self.world = self.tree()
        self.mon.tick()
        del self.world["3-9"]
        self.mon.tick()
        self.assertEqual(self.outage_details(), [])

    def test_drops_spanning_controllers_read_as_system_wide(self):
        self.world = self.tree()
        self.world["usb1"] = device("usb1", serial="PCI1", dev_class=0x09, power=0)
        self.world["1-1"] = device("1-1", serial="OTHERBUS")
        self.world["4-1"] = device("4-1", serial="THIRDBUS")
        self.mon.tick()
        for busid in ("3-4.1.1", "1-1", "4-1"):
            del self.world[busid]
        self.mon.tick()
        self.assertIn("system-wide", self.outage_details()[0])

    def test_devices_that_never_return_are_not_called_an_outage(self):
        self.world = self.tree()
        self.mon.tick()
        for busid in ("3-4.1.1", "3-4.1.2", "3-4.1.3"):
            del self.world[busid]
        self.mon.tick()
        self.mon._outage["start"] -= 1000      # pretend a long time passed
        self.mon.tick()
        self.assertIn("never came back", self.outage_details()[0])

    def test_a_starved_poll_loop_is_recorded(self):
        self.world = self.tree()
        self.mon.tick()
        self.mon._prev_ts -= 30                # the tick arrived 30s late
        self.mon._skip_stall = False
        self.mon.tick()
        kinds = [e[2] for e in self.store.events("")]
        self.assertIn(store.KIND_STALL, kinds)

    def test_pausing_is_not_mistaken_for_a_stall(self):
        self.world = self.tree()
        self.mon.tick()
        self.mon.set_paused(True)
        self.mon.set_paused(False)
        self.mon._prev_ts -= 30
        self.mon.tick()
        kinds = [e[2] for e in self.store.events("")]
        self.assertNotIn(store.KIND_STALL, kinds)


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


class PowerControlTests(unittest.TestCase):
    """The bus id reaches a command line, so validation is the whole game."""

    def test_rejects_anything_that_is_not_a_bus_id(self):
        for bad in ("3-4; rm -rf /", "../../etc/passwd", "usb1$(id)",
                    "3-4 4-5", "", "/sys/bus/usb/devices/3-4", "3-4\n4-5"):
            with self.assertRaises(ValueError, msg=bad):
                power.control_path(bad)

    def test_accepts_real_bus_ids(self):
        for good in ("usb1", "3-4", "3-4.1.2.3", "12-1.1"):
            self.assertTrue(power.control_path(good).endswith("power/control"))

    def test_command_targets_the_helper_when_installed(self):
        real = power.helper_installed
        try:
            power.helper_installed = lambda: True
            cmd = power.pkexec_command("3-4.1", False)
            self.assertEqual(cmd[1:], [power.HELPER, "3-4.1", "on"])
        finally:
            power.helper_installed = real

    def test_command_falls_back_to_a_one_off_write(self):
        real = power.helper_installed
        try:
            power.helper_installed = lambda: False
            cmd = power.pkexec_command("3-4.1", True)
            self.assertEqual(cmd[1], "/bin/sh")
            self.assertIn("auto", cmd[-1])
            self.assertIn("/sys/bus/usb/devices/3-4.1/power/control", cmd[-1])
        finally:
            power.helper_installed = real

    def test_udev_rule_matches_on_vendor_and_product(self):
        rule = power.udev_rule("1b1c", "2b19", False, "Keyboard")
        self.assertIn('ATTR{idVendor}=="1b1c"', rule)
        self.assertIn('ATTR{idProduct}=="2b19"', rule)
        self.assertIn('ATTR{power/control}="on"', rule)
        self.assertIn("# Keyboard", rule)

    def test_udev_rule_for_allowing_autosuspend(self):
        self.assertIn('ATTR{power/control}="auto"',
                      power.udev_rule("046d", "0892", True))


class FormattingTests(unittest.TestCase):
    def test_axis_ceilings_are_round_numbers(self):
        self.assertEqual(nice_ceiling(224), 250)
        self.assertEqual(nice_ceiling(2.4), 2.5)
        self.assertEqual(nice_ceiling(0), 10)

    def test_milliamp_formatting(self):
        self.assertEqual(fmt_ma(0), "–")
        self.assertEqual(fmt_ma(2.5), "2.5 mA")
        self.assertEqual(fmt_ma(480), "480 mA")

    def test_byte_rate_formatting(self):
        self.assertEqual(fmt_bps(0), "–")
        self.assertEqual(fmt_bps(512), "512 B/s")
        self.assertEqual(fmt_bps(64000), "62.5 kB/s")
        self.assertEqual(fmt_bps(12 * 1024 * 1024), "12.0 MB/s")
        self.assertEqual(fmt_bps(12 * 1024 * 1024, "axis"), "12.0 MB")
        self.assertEqual(fmt_bps(12 * 1024 * 1024, "compact"), "12.0M")
        self.assertEqual(fmt_bps(392192, "compact"), "383k")


if __name__ == "__main__":
    unittest.main(verbosity=2)
