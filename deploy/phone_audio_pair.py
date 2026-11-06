"""Pair only the USB-selected Motorola; run with system Python (python3-dbus/gi)."""


def main():
    import json
    import os
    import sys
    from pathlib import Path

    import dbus
    import dbus.service
    from dbus.mainloop.glib import DBusGMainLoop
    from gi.repository import GLib

    root = Path("/var/lib/meshcore-gateway/phone-remote")
    DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    loop = GLib.MainLoop()
    object_manager = dbus.Interface(bus.get_object("org.bluez", "/"), "org.freedesktop.DBus.ObjectManager")
    objects = object_manager.GetManagedObjects()
    adapter_path = next(path for path, interfaces in objects.items() if "org.bluez.Adapter1" in interfaces)
    adapter = dbus.Interface(bus.get_object("org.bluez", adapter_path), "org.bluez.Adapter1")
    adapter.SetDiscoveryFilter({"Transport": "bredr"})
    adapter.StartDiscovery()
    __import__("time").sleep(8)
    objects = object_manager.GetManagedObjects()
    sys.path.insert(0, str(Path(__file__).parent / "local-console"))
    from phone_remote import PhoneRemote

    phone = PhoneRemote(root)
    profile = {"serial_hash": __import__("hashlib").sha256(phone.device().encode()).hexdigest()}
    phone_name = (
        phone.adb("shell", "-T", "sh", script="settings get global device_name\n")
        .decode(errors="replace")
        .strip()
    )
    candidates = [
        (str(path), interfaces["org.bluez.Device1"])
        for path, interfaces in objects.items()
        if "org.bluez.Device1" in interfaces
        and str(interfaces["org.bluez.Device1"].get("Name", "")).casefold() == phone_name.casefold()
    ]
    print("Classic Bluetooth Motorola candidates:", len(candidates), flush=True)
    if len(candidates) != 1:
        adapter.StopDiscovery()
        sys.exit(2)
    address = str(candidates[0][1]["Address"])
    profile["address"] = address
    profile["device_path"] = candidates[0][0]
    fd = os.open(root / "audio-device.pending.json", os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(profile, stream)
    adapter.StopDiscovery()
    target = next(
        str(path)
        for path, interfaces in objects.items()
        if str(interfaces.get("org.bluez.Device1", {}).get("Address", "")).lower() == address.lower()
    )

    class Rejected(dbus.DBusException):
        _dbus_error_name = "org.bluez.Error.Rejected"

    class Agent(dbus.service.Object):
        def check(self, device):
            if str(device) != target:
                raise Rejected("Unexpected device")

        @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
        def Release(self):
            pass

        @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="s")
        def RequestPinCode(self, device):
            raise Rejected("Numeric comparison required")

        @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="u")
        def RequestPasskey(self, device):
            raise Rejected("Numeric comparison required")

        @dbus.service.method("org.bluez.Agent1", in_signature="ou", out_signature="")
        def RequestConfirmation(self, device, passkey):
            self.check(device)
            print(f"Confirm Bluetooth pairing on Motorola. Comparison number: {passkey:06d}", flush=True)

        @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="")
        def RequestAuthorization(self, device):
            self.check(device)

        @dbus.service.method("org.bluez.Agent1", in_signature="os", out_signature="")
        def AuthorizeService(self, device, uuid):
            self.check(device)
            if str(uuid).lower() not in {
                "0000111f-0000-1000-8000-00805f9b34fb",
                "0000110a-0000-1000-8000-00805f9b34fb",
                "0000110b-0000-1000-8000-00805f9b34fb",
            }:
                raise Rejected("Audio service only")

        @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
        def Cancel(self):
            print("Pairing cancelled.", flush=True)

    agent = Agent(bus, "/org/ideenwerft/PhoneAudioAgent")
    manager = dbus.Interface(bus.get_object("org.bluez", "/org/bluez"), "org.bluez.AgentManager1")
    manager.RegisterAgent("/org/ideenwerft/PhoneAudioAgent", "DisplayYesNo")
    manager.RequestDefaultAgent("/org/ideenwerft/PhoneAudioAgent")
    result = {"ok": False}

    def success():
        props = dbus.Interface(bus.get_object("org.bluez", target), "org.freedesktop.DBus.Properties")
        props.Set("org.bluez.Device1", "Trusted", True)
        os.replace(root / "audio-device.pending.json", root / "audio-device.json")
        uuids = props.Get("org.bluez.Device1", "UUIDs")
        print(
            "Motorola paired. HFP Audio Gateway:",
            "0000111f-0000-1000-8000-00805f9b34fb" in [str(x).lower() for x in uuids],
            flush=True,
        )
        result["ok"] = True
        loop.quit()

    def error(err):
        print("Pairing not completed:", str(err).split(":")[0], flush=True)
        loop.quit()

    def timeout():
        error("Timeout")
        return False

    properties = dbus.Interface(bus.get_object("org.bluez", target), "org.freedesktop.DBus.Properties")
    if bool(properties.Get("org.bluez.Device1", "Paired")):
        success()
    else:
        dbus.Interface(bus.get_object("org.bluez", target), "org.bluez.Device1").Pair(
            reply_handler=success, error_handler=error, timeout=115
        )
    GLib.timeout_add_seconds(120, timeout)
    print("Waiting for Motorola Bluetooth confirmation.", flush=True)
    try:
        if not result["ok"]:
            loop.run()
    finally:
        manager.UnregisterAgent("/org/ideenwerft/PhoneAudioAgent")
        agent.remove_from_connection()
    if not result["ok"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
