"""CRC checked FIG 0/15 signalling, ETSI TS 104 089 Annex E.

The installed third-party decoder's summary ignores OE and P/D. Never use that
summary alone to authorize transmission. This reader uses its raw /fic stream.
"""

import binascii
import math


def location_code(latitude, longitude):
    if (
        not math.isfinite(latitude)
        or not math.isfinite(longitude)
        or not -90 <= latitude <= 90
        or not -180 <= longitude <= 180
    ):
        raise ValueError("Invalid ASA receiver coordinates")
    se, ee = 90 - latitude, longitude % 360
    if se < 18 or se >= 162:
        north = se < 18
        zone = 0 if north else 41
        cap = se < 9 if north else se >= 171
        first = int(ee / 72) + 11 if cap else int(ee / 36) + 1
        base = 0 if north and cap else 9 if north else 171 if cap else 162
        sc = min(1023, int((se - base) / 9 * 1024))
        ec = min(1023, int((ee / (72 if cap else 36) % 1) * 1024))
        code, bits = first, 10
    else:
        zone = 10 * int((se - 18) / 36) + int(ee / 36) + 1
        sc = int(((se - 18) / 36 % 1) * 4096)
        ec = int((ee / 36 % 1) * 4096)
        code, bits = 0, 12
    for shift in range(bits - 2, -1, -2):
        code = (code << 4) | (((sc >> shift) & 3) << 2) | ((ec >> shift) & 3)
    return zone, code


def matches_location(locations, latitude, longitude):
    if not locations:
        return True  # no restriction: entire tuned ensemble coverage
    zone, code = location_code(latitude, longitude)
    for area in locations:
        n = area["digits"]
        if area["zone"] != zone:
            continue
        if zone in (0, 41) and area["code"] == 0 and n == 1:
            return True
        if code >> (4 * (6 - n)) != area["code"]:
            continue
        if area["subcodes"] is None:
            return True
        digit = (code >> (4 * (5 - n))) & 15
        if area["subcodes"] & (1 << digit):
            return True
    return False


def parse_fig(fig):
    if len(fig) < 2 or fig[0] >> 5 or len(fig) != (fig[0] & 31) + 1 or fig[1] & 31 != 15:
        return None
    control, payload = fig[1], fig[2:]
    if control & 0x20:  # P/D = Discard; not a test-stage discriminator.
        return {"kind": "discard"}
    other = bool(control & 0x40)
    if not payload:
        return {"kind": "heartbeat"} if control & 0x80 and not other else None
    if other:
        if len(payload) < 3:
            return None
        phase, target, offset = 1, "ensemble:" + payload[:2].hex(), 2
    else:
        phase, target, offset = payload[0] >> 6, "subchannel:" + str(payload[0] & 63), 1
    if phase in (2, 3):
        if len(payload) != offset:
            return None
        return {"kind": "sustain" if phase == 2 else "end", "target_id": target}
    if phase == 0:
        offset += 1
    if len(payload) <= offset:
        return None
    status = payload[offset]
    stage, iid = (status >> 4) & 7, status & 15
    offset += 1
    locations = []
    remaining = 0
    while offset < len(payload):
        if len(payload) - offset < 2:
            return None
        a, b = payload[offset : offset + 2]
        zone, subcodes, digits = a & 63, bool(b & 128), (b >> 4) & 7
        if zone > 41 or digits > (4 if subcodes else 5):
            return None
        end = offset + 2 + (digits + 1) // 2 + (2 if subcodes else 0)
        if end > len(payload):
            return None
        if not locations:
            remaining = a >> 6
        raw = int.from_bytes(payload[offset + 2 : offset + 2 + (digits + 1) // 2], "big")
        if digits % 2:
            if raw & 15:
                return None
            raw >>= 4
        prefix = ((b & 15) << (digits * 4)) | raw
        bitmap = int.from_bytes(payload[end - 2 : end], "big") if subcodes else None
        locations.append({"zone": zone, "digits": digits + 1, "code": prefix, "subcodes": bitmap})
        offset = end
    return {
        "kind": "pretrigger" if phase == 0 else "trigger",
        "target_id": target,
        "iid": iid,
        "stage": stage,
        "is_test": stage == 7,
        "level": 1 if stage < 4 else 2 if stage < 7 else 0,
        "other_ensemble": other,
        "locations": locations,
        "continuation": bool(control & 128),
        "remaining": remaining,
    }


class FicState:
    def __init__(self):
        self.signals = {}
        self.last_fib = self.last_fig = 0
        self.heartbeat = False
        self.ews = False
        self.pending = {}

    def feed(self, fib, now):
        if len(fib) != 32 or (binascii.crc_hqx(fib[:30], 0xFFFF) ^ 0xFFFF) != int.from_bytes(fib[30:], "big"):
            return
        self.last_fib = now
        offset = 0
        while offset < 30 and fib[offset] != 255:
            size = (fib[offset] & 31) + 1
            if size < 2 or offset + size > 30:
                return
            event = parse_fig(fib[offset : offset + size])
            offset += size
            if not event:
                continue
            self.last_fig = now
            if event["kind"] == "discard":
                continue
            self.ews = True
            if event["kind"] == "heartbeat":
                self.heartbeat = True
                self.pending.clear()
                for signal in self.signals.values():
                    signal["active"] = False
            elif event["kind"] == "trigger":
                self.heartbeat = False
                key = event["target_id"] + ":" + str(event["iid"])
                if not event["continuation"]:
                    if key not in self.pending and len(self.pending) >= 64:
                        self.pending.pop(next(iter(self.pending)))
                    self.pending[key] = {**event, "seen_at": now}
                else:
                    previous = self.pending.get(key)
                    if (
                        not previous
                        or now - previous["seen_at"] > 2
                        or event["remaining"] != previous["remaining"] - 1
                        or event["stage"] != previous["stage"]
                    ):
                        self.pending.pop(key, None)
                        continue
                    self.pending[key] = {
                        **event,
                        "locations": previous["locations"] + event["locations"],
                        "seen_at": now,
                    }
                if event["remaining"]:
                    continue
                event = self.pending.pop(key)
                if key not in self.signals and len(self.signals) >= 64:
                    self.signals.pop(next(iter(self.signals)))
                self.signals[key] = {**event, "active": True, "seen_at": now}
            elif event["kind"] in {"end", "sustain"}:
                for signal in self.signals.values():
                    if signal["target_id"] == event["target_id"]:
                        signal["active"] = event["kind"] == "sustain" and signal["active"]
                        signal["seen_at"] = now

    def snapshot(self, now):
        fresh = (
            0 <= now - self.last_fib < 5
            and 0 <= now - self.last_fig < 10
            and (self.heartbeat or bool(self.signals))
        )
        signals = []
        for event in self.signals.values():
            signals.append(
                {
                    **event,
                    "active": event["active"],
                    "status": "test" if event["is_test"] else "actual",
                    "validation_version": 1,
                    "validated_at": self.last_fig,
                }
            )
        actual = [s for s in signals if s["active"] and not s["is_test"]]
        tests = [s for s in signals if s["active"] and s["is_test"]]
        return {
            "validated": fresh,
            "validated_at": self.last_fig,
            "heartbeat": self.heartbeat,
            "ews_ensemble": self.ews,
            "active": bool(actual or tests),
            "is_test": bool(tests and not actual),
            "signals": signals,
        }
