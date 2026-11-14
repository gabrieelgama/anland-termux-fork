import importlib.util
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "anland-upower-bridge.py"
SPEC = importlib.util.spec_from_file_location("anland_upower_bridge", SCRIPT)
BRIDGE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BRIDGE)


def add_supply(root, name, values):
    source = root / name
    source.mkdir()
    for key, value in values.items():
        (source / key).write_text(f"{value}\n", encoding="utf-8")
    return source


class BatteryDiscoveryTests(unittest.TestCase):
    def test_discovers_multiple_batteries_and_ignores_usb(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            primary = add_supply(root, "battery_primary", {
                "type": "Battery", "capacity": "82",
            })
            add_supply(root, "battery_cover", {
                "type": "Battery", "capacity": "81",
            })
            add_supply(root, "legacy_pack", {"capacity": "50"})
            add_supply(root, "usb", {"type": "USB", "capacity": "100"})

            self.assertEqual(
                BRIDGE.battery_sources(root),
                [root / "battery_cover", primary, root / "legacy_pack"],
            )

    def test_returns_no_sources_when_root_is_missing(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.assertEqual(BRIDGE.battery_sources(Path(temporary) / "missing"), [])


class BatteryAggregationTests(unittest.TestCase):
    def test_aggregates_energy_supplies_and_converts_units(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            add_supply(root, "cell_a", {
                "type": "Battery", "energy_now": "4000000",
                "energy_full": "8000000", "power_now": "1600000",
                "voltage_now": "3800000", "temp": "284",
                "status": "Discharging", "model_name": "Primary",
            })
            add_supply(root, "cell_b", {
                "type": "Battery", "energy_now": "2000000",
                "energy_full": "4000000", "power_now": "400000",
                "voltage_now": "3800000", "temp": "286",
                "status": "Discharging", "model_name": "Auxiliary",
            })

            data = BRIDGE.collect_battery_data(BRIDGE.battery_sources(root))

            self.assertEqual(data["Percentage"], 50.0)
            self.assertEqual(data["Energy"], 6.0)
            self.assertEqual(data["EnergyFull"], 12.0)
            self.assertEqual(data["EnergyRate"], 2.0)
            self.assertEqual(data["Voltage"], 3.8)
            self.assertEqual(data["Temperature"], 28.5)
            self.assertEqual(data["State"], 2)
            self.assertEqual(data["Model"], "Primary, Auxiliary")

    def test_falls_back_to_charge_and_capacity_attributes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            add_supply(root, "pack_a", {
                "type": "Battery", "charge_now": "500000",
                "charge_full": "1000000", "voltage_now": "3800000",
                "status": "Charging",
            })
            add_supply(root, "pack_b", {
                "type": "Battery", "charge_now": "250000",
                "charge_full": "500000", "voltage_now": "3800000",
                "status": "Charging",
            })

            data = BRIDGE.collect_battery_data(BRIDGE.battery_sources(root))

            self.assertEqual(data["Percentage"], 50.0)
            self.assertEqual(data["Energy"], 2.85)
            self.assertEqual(data["EnergyFull"], 5.7)
            self.assertEqual(data["State"], 1)
            self.assertEqual(data["IconName"], "battery-good-charging-symbolic")

    def test_assigns_upower_battery_warning_levels(self):
        for percentage, battery_level, warning_level in (
            (100, 6, 1), (50, 4, 1), (10, 2, 3), (5, 3, 4), (2, 3, 5),
        ):
            with self.subTest(percentage=percentage), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                add_supply(root, "battery", {
                    "type": "Battery", "capacity": str(percentage),
                    "status": "Discharging",
                })
                data = BRIDGE.collect_battery_data(BRIDGE.battery_sources(root))
                self.assertEqual(data["BatteryLevel"], battery_level)
                self.assertEqual(data["WarningLevel"], warning_level)

    def test_returns_none_for_empty_source_list(self):
        self.assertIsNone(BRIDGE.collect_battery_data([]))


if __name__ == "__main__":
    unittest.main()
