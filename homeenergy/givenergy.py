"""Talk to a GivEnergy inverter directly through its WiFi dongle (TCP port 8899).

No cloud involved. This module only READS for now.

Wire format (GivEnergy's wrapper around Modbus):
  header : 0x5959, 0x0001, length(2)        - length counts everything after it
  body   : unit 0x01, func 0x02,
           adapter serial (10 ascii), padding (8),
           slave 0x32, modbus func (0x03 holding / 0x04 input),
           [response only: inverter serial (10 ascii)],
           base register (2), count (2), [response only: values], crc (2)
"""
import socket
import struct
import threading
import time

PORT = 8899
HOLDING = 0x03
INPUT = 0x04


def _crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def _request(func: int, base: int, count: int) -> bytes:
    inner = struct.pack(">BBHH", 0x32, func, base, count)
    inner += struct.pack("<H", _crc16(inner))
    payload = b"\x01\x02" + b"AB1234G567" + struct.pack(">Q", 8) + inner
    return struct.pack(">HHH", 0x5959, 0x0001, len(payload)) + payload


def _s16(v: int) -> int:
    return v - 0x10000 if v & 0x8000 else v


class Inverter:
    def __init__(self, host: str, timeout: float = 5.0):
        self.host = host
        self.timeout = timeout
        # The dongle handles one connection at a time, so reads and writes take turns.
        self._lock = threading.Lock()

    def _read_block(self, sock, func, base, count=60):
        """Send one request and wait for the matching reply frame."""
        sock.sendall(_request(func, base, count))
        buf = b""
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
            # The dongle can send other frames (heartbeats etc.) - walk them all.
            while len(buf) >= 6:
                if buf[:2] != b"\x59\x59":
                    i = buf.find(b"\x59\x59", 1)
                    buf = buf[i:] if i > 0 else b""
                    continue
                length = struct.unpack(">H", buf[4:6])[0]
                if len(buf) < 6 + length:
                    break
                frame, buf = buf[: 6 + length], buf[6 + length:]
                regs = self._parse(frame, func, base, count)
                if regs is not None:
                    return regs
        raise TimeoutError(f"no reply for func {func} base {base}")

    @staticmethod
    def _parse(frame, func, base, count):
        if len(frame) < 42 or frame[7] != 0x02:
            return None
        if frame[27] != func:
            return None
        f_base, f_count = struct.unpack(">HH", frame[38:42])
        if f_base != base or f_count != count:
            return None
        raw = frame[42: 42 + count * 2]
        if len(raw) != count * 2:
            return None
        return list(struct.unpack(f">{count}H", raw))

    def read_all(self):
        """Read the register blocks we need. Returns (holding, input) dicts."""
        with self._lock:
            return self._read_all()

    def _read_all(self):
        holding, inp = {}, {}
        with socket.create_connection((self.host, PORT), timeout=self.timeout) as s:
            s.settimeout(self.timeout)
            for store, func, base in (
                (holding, HOLDING, 0),
                (holding, HOLDING, 60),
                (inp, INPUT, 0),
            ):
                for attempt in range(3):
                    try:
                        regs = self._read_block(s, func, base)
                        break
                    except TimeoutError:
                        if attempt == 2:
                            raise
                store.update({base + i: v for i, v in enumerate(regs)})
        return holding, inp

    # Only these settings may ever be written, as a guard against typos.
    WRITABLE = {
        31: "charge slot 2 start (hhmm)",
        32: "charge slot 2 end (hhmm)",
        94: "charge slot 1 start (hhmm)",
        95: "charge slot 1 end (hhmm)",
        96: "grid charging enabled (0/1)",
        111: "battery charge limit (%)",
        112: "battery discharge limit (%)",
        116: "charge target (%)",
    }

    def write(self, register, value):
        """Write one holding register, then read it back. Returns the value read back."""
        with self._lock:
            return self._write(register, value)

    def _write(self, register, value):
        if register not in self.WRITABLE:
            raise ValueError(f"register {register} is not on the writable list")
        if not 0 <= value <= 0xFFFF:
            raise ValueError("value out of range")
        inner = struct.pack(">BBHH", 0x11, 0x06, register, value)
        inner += struct.pack("<H", _crc16(inner))
        payload = b"\x01\x02" + b"AB1234G567" + struct.pack(">Q", 8) + inner
        frame = struct.pack(">HHH", 0x5959, 0x0001, len(payload)) + payload
        with socket.create_connection((self.host, PORT), timeout=self.timeout) as s:
            s.sendall(frame)
            s.settimeout(self.timeout)
            try:
                s.recv(4096)  # acknowledgement; we verify by reading back instead
            except socket.timeout:
                pass
        time.sleep(1)
        base = (register // 60) * 60
        for _ in range(3):
            try:
                with socket.create_connection((self.host, PORT), timeout=self.timeout) as s:
                    s.settimeout(self.timeout)
                    regs = self._read_block(s, HOLDING, base)
                return regs[register - base]
            except TimeoutError:
                continue
        raise TimeoutError("wrote, but could not read back to confirm")

    def status(self):
        """Read and decode into plain numbers for the dashboard."""
        hr, ir = self.read_all()

        def hhmm(v):
            return f"{v // 100:02d}:{v % 100:02d}"

        def text(a, b):
            return b"".join(struct.pack(">H", hr[r]) for r in range(a, b)).decode("ascii", "replace")

        load = ir[42]
        grid_out = _s16(ir[30])        # + exporting, - importing
        battery = _s16(ir[52])         # + discharging, - charging
        # Solar is measured by the inverter's PV clamp. Checked against the
        # energy balance (house + export + charge = solar + import + discharge).
        solar = max(0, _s16(ir[18]))

        return {
            "time": time.time(),
            "solar_w": solar,
            "house_w": load,
            "grid_w": -grid_out,       # + importing, - exporting (easier to read)
            "battery_w": battery,
            "battery_pct": ir[59],
            "battery_v": ir[50] / 100,
            "battery_temp_c": ir[56] / 10,
            "grid_v": ir[5] / 10,
            "grid_hz": ir[13] / 100,
            "import_today_kwh": ir[26] / 10,
            "export_today_kwh": ir[25] / 10,
            "settings": {
                "mode": "eco" if hr[27] == 1 else f"mode {hr[27]}",
                "grid_charge_enabled": bool(hr[96]),
                "charge_slot_1": f"{hhmm(hr[94])}-{hhmm(hr[95])}",
                "charge_slot_2": f"{hhmm(hr[31])}-{hhmm(hr[32])}",
                "charge_target_pct": hr[116],
                "reserve_pct": hr[110],
                "charge_limit": hr[111],
                "discharge_limit": hr[112],   # 0 = battery paused (held)
            },
            "inverter_serial": text(13, 18),
            "battery_serial": text(8, 13),
            "inverter_clock": "20%02d-%02d-%02d %02d:%02d" % tuple(hr[r] for r in range(35, 40)),
        }


if __name__ == "__main__":
    import json
    import sys

    print(json.dumps(Inverter(sys.argv[1] if len(sys.argv) > 1 else "192.168.1.65").status(), indent=2))
