#!/usr/bin/env python3
"""Expose Linux power_supply batteries to UPower over the system D-Bus.

This is an optional bridge for Android PRoot environments where the kernel
power_supply sysfs entries are visible in the container but UPower cannot read
them directly. It does not require systemd. Start it only when no other UPower
service owns org.freedesktop.UPower on the system bus.
"""

import argparse
import sys
import time
from pathlib import Path

SYSFS_ROOT = Path("/sys/class/power_supply")
ROOT = "/org/freedesktop/UPower"
DISPLAY = ROOT + "/devices/DisplayDevice"
DEVICE_IFACE = "org.freedesktop.UPower.Device"
ROOT_IFACE = "org.freedesktop.UPower"
PROPS_IFACE = "org.freedesktop.DBus.Properties"


def read_text(path, default=""):
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return default


def read_int(path):
    value = read_text(path)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def battery_sources(root=SYSFS_ROOT):
    """Find battery supplies without assuming a vendor-specific sysfs name."""
    found = []
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return found

    for path in entries:
        if not path.is_dir():
            continue
        supply_type = read_text(path / "type").lower()
        # The type file is the standard interface. The name fallback supports
        # older Android kernels which omit it while still exposing capacity.
        if supply_type == "battery" or (
            not supply_type and read_int(path / "capacity") is not None
        ):
            found.append(path)
    return found


def _ratio(source, names):
    for current_name, full_name in names:
        current = read_int(source / current_name)
        full = read_int(source / full_name)
        if current is not None and full is not None and full > 0:
            return max(0.0, min(1.0, current / full))
    capacity = read_int(source / "capacity")
    if capacity is not None:
        return max(0.0, min(1.0, capacity / 100.0))
    return None


def _supply_status(sources):
    statuses = {read_text(source / "status").lower() for source in sources}
    statuses.discard("")
    if "charging" in statuses:
        return "charging"
    if statuses and statuses <= {"full"}:
        return "full"
    if "discharging" in statuses:
        return "discharging"
    if "not charging" in statuses:
        return "not charging"
    if "empty" in statuses:
        return "empty"
    return "unknown"


def _aggregate(sources, current_names, full_names):
    current_total = 0
    full_total = 0
    found = False
    for source in sources:
        current = next((read_int(source / name) for name in current_names
                        if read_int(source / name) is not None), None)
        full = next((read_int(source / name) for name in full_names
                     if read_int(source / name) is not None), None)
        if current is not None and full is not None and full > 0:
            found = True
            current_total += current
            full_total += full
    return (current_total, full_total) if found and full_total > 0 else (None, None)


def collect_battery_data(sources):
    """Return plain Python values for tests and the D-Bus adapter."""
    sources = list(sources)
    if not sources:
        return None

    energy_now, energy_full = _aggregate(
        sources, ("energy_now",), ("energy_full",)
    )
    if energy_now is None:
        charge_now, charge_full = _aggregate(
            sources, ("charge_now",), ("charge_full", "charge_full_design")
        )
        if charge_now is not None:
            # Linux sysfs charge is in µAh and voltage in µV; their product
            # divided by 1e6 is µWh. If voltage is absent, use charge ratios.
            weighted = []
            for source in sources:
                voltage = read_int(source / "voltage_now")
                current = read_int(source / "charge_now")
                full = read_int(source / "charge_full") or read_int(
                    source / "charge_full_design"
                )
                if voltage and current is not None and full:
                    weighted.append((current * voltage, full * voltage))
            if weighted:
                energy_now = sum(item[0] for item in weighted) / 1_000_000
                energy_full = sum(item[1] for item in weighted) / 1_000_000
            else:
                energy_now, energy_full = None, None
            percentage = 100.0 * charge_now / charge_full
        else:
            ratios = [ratio for source in sources
                      if (ratio := _ratio(source, ())) is not None]
            percentage = (sum(ratios) / len(ratios) * 100) if ratios else 0.0
    else:
        percentage = 100.0 * energy_now / energy_full

    percentage = max(0.0, min(100.0, percentage))
    status = _supply_status(sources)
    state = {
        "charging": 1,
        "discharging": 2,
        "empty": 3,
        "full": 4,
        "not charging": 5,
    }.get(status, 0)

    energy_rate, _unused = _aggregate(
        sources, ("power_now",), ("power_now",)
    )
    if energy_rate is None:
        current_values = [read_int(source / "current_now") for source in sources]
        voltage_values = [read_int(source / "voltage_now") for source in sources]
        products = [abs(current) * voltage for current, voltage
                    in zip(current_values, voltage_values)
                    if current is not None and voltage is not None]
        energy_rate = sum(products) / 1_000_000 if products else 0.0

    temperatures = [read_int(source / "temp") for source in sources]
    temperatures = [value for value in temperatures if value is not None]
    # power_supply temp is conventionally tenths of a degree Celsius.
    temperature = (sum(temperatures) / len(temperatures) / 10
                   if temperatures else 0.0)
    voltages = [read_int(source / "voltage_now") for source in sources]
    voltages = [value for value in voltages if value is not None]
    voltage = (sum(voltages) / len(voltages) / 1_000_000
               if voltages else 0.0)

    models = [read_text(source / "model_name") or read_text(source / "model")
              for source in sources]
    vendors = [read_text(source / "manufacturer") for source in sources]
    models = [value for value in models if value]
    vendors = [value for value in vendors if value]
    icon = "battery-full-symbolic" if percentage >= 95 else (
        "battery-good-symbolic" if percentage >= 50 else
        "battery-low-symbolic"
    )
    if state == 1:
        icon = "battery-good-charging-symbolic"

    battery_level = (6 if percentage >= 100 else
                     3 if percentage <= 5 else
                     2 if percentage <= 10 else 4)
    warning_level = (5 if percentage <= 2 else
                     4 if percentage <= 5 else
                     3 if percentage <= 10 else 1)

    return {
        "NativePath": ",".join(str(source) for source in sources),
        "Vendor": ", ".join(dict.fromkeys(vendors)),
        "Model": ", ".join(dict.fromkeys(models)) or "Android battery",
        "Serial": "",
        "Type": 2,
        "State": state,
        "Technology": 1,
        "Percentage": percentage,
        "IsPresent": True,
        "IsRechargeable": True,
        "PowerSupply": True,
        "Online": False,
        "Capacity": 100.0,
        "BatteryLevel": battery_level,
        "Energy": (energy_now or 0.0) / 1_000_000,
        "EnergyEmpty": 0.0,
        "EnergyFull": (energy_full or 0.0) / 1_000_000,
        "EnergyFullDesign": (energy_full or 0.0) / 1_000_000,
        "EnergyRate": energy_rate / 1_000_000,
        "Voltage": voltage,
        "Temperature": temperature,
        "TimeToEmpty": 0,
        "TimeToFull": 0,
        "UpdateTime": int(time.time()),
        "WarningLevel": warning_level,
        "IconName": icon,
        "HasHistory": False,
        "HasStatistics": False,
        "ChargeCycles": 0,
        "ChargeStartThreshold": 0,
        "ChargeEndThreshold": 0,
        "ChargeThresholdEnabled": False,
        "ChargeThresholdSupported": False,
        "Luminosity": 0.0,
    }


DEVICE_PROPERTY_SIGNATURES = {
    "NativePath": "s", "Vendor": "s", "Model": "s", "Serial": "s",
    "Type": "u", "State": "u", "Technology": "u", "Percentage": "d",
    "IsPresent": "b", "IsRechargeable": "b", "PowerSupply": "b",
    "Online": "b", "Capacity": "d", "BatteryLevel": "u", "Energy": "d",
    "EnergyEmpty": "d", "EnergyFull": "d", "EnergyFullDesign": "d",
    "EnergyRate": "d", "Voltage": "d", "Temperature": "d",
    "TimeToEmpty": "x", "TimeToFull": "x", "UpdateTime": "t",
    "WarningLevel": "u", "IconName": "s", "HasHistory": "b",
    "HasStatistics": "b", "ChargeCycles": "i", "ChargeStartThreshold": "u",
    "ChargeEndThreshold": "u", "ChargeThresholdEnabled": "b",
    "ChargeThresholdSupported": "b", "Luminosity": "d",
}


def run_bridge(root):
    # Keep optional runtime dependencies out of the import path for --check.
    import dbus
    import dbus.service
    from dbus.mainloop.glib import DBusGMainLoop
    from gi.repository import GLib

    battery_path = ROOT + "/devices/AndroidBattery"

    def dbus_values(data):
        converted = {}
        for name, value in data.items():
            signature = DEVICE_PROPERTY_SIGNATURES[name]
            if signature == "s":
                converted[name] = dbus.String(value)
            elif signature == "b":
                converted[name] = dbus.Boolean(value)
            elif signature == "d":
                converted[name] = dbus.Double(value)
            elif signature == "u":
                converted[name] = dbus.UInt32(value)
            elif signature == "x":
                converted[name] = dbus.Int64(value)
            elif signature == "t":
                converted[name] = dbus.UInt64(value)
            elif signature == "i":
                converted[name] = dbus.Int32(value)
        return converted

    class Properties(dbus.service.Object):
        def __init__(self, bus, path):
            super().__init__(bus, path)
            self.path = path

        @dbus.service.method("org.freedesktop.DBus.Introspectable",
                             in_signature="", out_signature="s")
        def Introspect(self):
            from xml.etree import ElementTree as ET
            xml = dbus.service.Object.Introspect(
                self, self.path, self._connection
            )
            tree = ET.fromstring(xml)
            interface_name = (DEVICE_IFACE if self.path != ROOT else ROOT_IFACE)
            interface = next((node for node in tree.findall("interface")
                              if node.get("name") == interface_name), None)
            if interface is None:
                interface = ET.SubElement(tree, "interface", name=interface_name)
            definitions = (DEVICE_PROPERTY_SIGNATURES if self.path != ROOT else {
                "DaemonVersion": "s", "OnBattery": "b",
                "LidIsClosed": "b", "LidIsPresent": "b",
            })
            existing = {node.get("name") for node in interface.findall("property")}
            for name, signature in definitions.items():
                if name not in existing:
                    ET.SubElement(interface, "property", name=name,
                                  type=signature, access="read")
            return ET.tostring(tree, encoding="unicode")

        def values(self, interface):
            data = collect_battery_data(battery_sources(root))
            if interface == DEVICE_IFACE and data is not None:
                return dbus_values(data)
            if interface == ROOT_IFACE and self.path == ROOT:
                status = _supply_status(battery_sources(root))
                return {"DaemonVersion": dbus.String("1.90.9"),
                        "OnBattery": dbus.Boolean(status == "discharging"),
                        "LidIsClosed": dbus.Boolean(False),
                        "LidIsPresent": dbus.Boolean(False)}
            raise dbus.exceptions.DBusException(
                "No readable battery supply", name="org.freedesktop.UPower.Error.NotFound"
            )

        @dbus.service.method(PROPS_IFACE, in_signature="ss", out_signature="v")
        def Get(self, interface, name):
            values = self.values(interface)
            if name not in values:
                raise dbus.exceptions.DBusException(
                    "Unknown property", name="org.freedesktop.DBus.Error.UnknownProperty"
                )
            return values[name]

        @dbus.service.method(PROPS_IFACE, in_signature="s", out_signature="a{sv}")
        def GetAll(self, interface):
            return self.values(interface)

        @dbus.service.method(PROPS_IFACE, in_signature="ssv", out_signature="")
        def Set(self, interface, name, value):
            raise dbus.exceptions.DBusException(
                "Read-only property", name="org.freedesktop.DBus.Error.PropertyReadOnly"
            )

        @dbus.service.signal(PROPS_IFACE, signature="sa{sv}as")
        def PropertiesChanged(self, interface, changed, invalidated):
            pass

    class Device(Properties):
        @dbus.service.method(DEVICE_IFACE, in_signature="", out_signature="")
        def Refresh(self):
            data = collect_battery_data(battery_sources(root))
            if data:
                self.PropertiesChanged(DEVICE_IFACE, dbus_values(data), [])

        @dbus.service.method(DEVICE_IFACE, in_signature="suu", out_signature="a(udu)")
        def GetHistory(self, kind, timespan, resolution):
            return dbus.Array([], signature="(udu)")

        @dbus.service.method(DEVICE_IFACE, in_signature="s", out_signature="a(dd)")
        def GetStatistics(self, kind):
            return dbus.Array([], signature="(dd)")

        @dbus.service.method(DEVICE_IFACE, in_signature="b", out_signature="")
        def EnableChargeThreshold(self, enabled):
            raise dbus.exceptions.DBusException(
                "Charge thresholds unsupported",
                name="org.freedesktop.UPower.Error.NotSupported"
            )

    class UPower(Properties):
        @dbus.service.method(ROOT_IFACE, in_signature="", out_signature="ao")
        def EnumerateDevices(self):
            return dbus.Array([dbus.ObjectPath(battery_path)], signature="o")

        @dbus.service.method(ROOT_IFACE, in_signature="", out_signature="o")
        def GetDisplayDevice(self):
            return dbus.ObjectPath(DISPLAY)

        @dbus.service.method(ROOT_IFACE, in_signature="", out_signature="s")
        def GetCriticalAction(self):
            return "PowerOff"

        @dbus.service.signal(ROOT_IFACE, signature="o")
        def DeviceAdded(self, path):
            pass

    DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    # Keep the BusName object alive for the lifetime of the service; dropping
    # it can release org.freedesktop.UPower immediately in dbus-python.
    bus_name = dbus.service.BusName("org.freedesktop.UPower", bus=bus,
                                   allow_replacement=False,
                                   replace_existing=False,
                                   do_not_queue=True)
    root_object = UPower(bus, ROOT)
    battery = Device(bus, battery_path)
    display = Device(bus, DISPLAY)
    previous = collect_battery_data(battery_sources(root)) or {}

    def update():
        nonlocal previous
        current = collect_battery_data(battery_sources(root)) or {}
        changed = {key: dbus_values({key: value})[key]
                   for key, value in current.items()
                   if key != "UpdateTime" and value != previous.get(key)}
        if changed:
            changed["UpdateTime"] = dbus.UInt64(current["UpdateTime"])
            battery.PropertiesChanged(DEVICE_IFACE, changed, [])
            display.PropertiesChanged(DEVICE_IFACE, changed, [])
        previous = current
        return True

    if previous:
        root_object.DeviceAdded(dbus.ObjectPath(battery_path))
    else:
        print("No readable battery source found; bridge will keep watching.",
              file=sys.stderr, flush=True)
    GLib.timeout_add_seconds(5, update)
    print("Anland UPower bridge active on the system D-Bus.", flush=True)
    GLib.MainLoop().run()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sysfs-root", type=Path, default=SYSFS_ROOT,
                        help="power_supply directory (default: %(default)s)")
    parser.add_argument("--check", action="store_true",
                        help="print detected supplies and battery data, then exit")
    args = parser.parse_args()
    sources = battery_sources(args.sysfs_root)
    if args.check:
        if not sources:
            print(f"No readable battery supplies under {args.sysfs_root}")
            return 1
        print("Detected sources:")
        for source in sources:
            print(f"  {source}")
        data = collect_battery_data(sources)
        for key, value in data.items():
            print(f"{key}: {value}")
        return 0
    if not sources:
        print(f"No readable battery supplies under {args.sysfs_root}; "
              "check whether the PRoot can read Android sysfs.",
              file=sys.stderr)
        return 1
    run_bridge(args.sysfs_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
