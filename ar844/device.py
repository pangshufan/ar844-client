from dataclasses import dataclass
import math
import sys
import time


@dataclass(frozen=True, slots=True)
class Mode:
    speed: str = "FAST"
    weight: str = "C"
    run: str = "NORMAL"

    @property
    def bits(self):
        return (0x40 if self.speed == "FAST" else 0) | (
            0x10 if self.weight == "C" else 0
        ) | (0x20 if self.run == "MAX" else 0)

    @classmethod
    def from_bits(cls, flags):
        return cls("FAST" if flags & 0x40 else "SLOW",
                   "C" if flags & 0x10 else "A",
                   "MAX" if flags & 0x20 else "NORMAL")

    @property
    def label(self):
        return f"{self.speed} / {self.weight} / {self.run}"


@dataclass(frozen=True, slots=True)
class Sample:
    time_ms: int
    db: float
    mode: Mode


class DeviceError(OSError):
    pass


class InvalidReading(DeviceError):
    pass


def command(opcode, flags=0, *, hid=False):
    payload = bytes([opcode, flags]) + bytes(6)
    return b"\0" + payload if hid else payload


def decode(packet):
    if len(packet) < 3 or packet[0] == 0xC4:
        raise DeviceError("未收到有效的读数报文")
    db = ((packet[0] << 8) | packet[1]) / 10
    if not 30 <= db <= 130:
        raise InvalidReading(f"无有效读数：{db:.1f} dB 超出 30–130 dB 范围")
    return Sample(time.time_ns() // 1_000_000, db, Mode.from_bits(packet[2]))


class HidDevice:
    def __init__(self, vid, pid):
        import hid

        self.handle = hid.device()
        try:
            self.handle.open(vid, pid)
            for _ in range(8):
                if not self.handle.read(64, 10):
                    break
        except Exception:
            try:
                self.close()
            except OSError:
                pass
            raise

    def _exchange(self, opcode, flags=0, timeout=1000):
        report = command(opcode, flags, hid=True)
        # Windows may pad to its longest output report and return the padded size.
        if self.handle.write(report) < len(report):
            raise DeviceError("HID 指令写入不完整")
        return self.handle.read(64, timeout)

    def read(self):
        return decode(self._exchange(0xB3))

    def configure(self, mode):
        reply = self._exchange(0x56, mode.bits, timeout=500)
        if not reply or reply[0] != 0xC4:
            raise DeviceError("设备未确认配置指令")
        return False

    def close(self):
        self.handle.close()


class UsbDevice:
    def __init__(self, vid, pid):
        import usb.core
        import usb.util

        self.util = usb.util
        self.dev = usb.core.find(idVendor=vid, idProduct=pid)
        self.detached = False
        if self.dev is None:
            raise DeviceError("等待 USB 设备")
        try:
            # The Linux reference driver resets before claiming the interface.
            try:
                self.dev.reset()
            except usb.core.USBError:
                pass
            current = usb.core.find(idVendor=vid, idProduct=pid)
            if current is None:
                raise DeviceError("等待 USB 设备重新枚举")
            usb.util.dispose_resources(self.dev)
            self.dev = current
            if self.dev.is_kernel_driver_active(0):
                self.dev.detach_kernel_driver(0)
                self.detached = True
            self.dev.set_configuration()
            usb.util.claim_interface(self.dev, 0)
            interface = self.dev.get_active_configuration()[(0, 0)]
            endpoints = {usb.util.endpoint_direction(e.bEndpointAddress): e for e in interface}
            self.out = endpoints.get(usb.util.ENDPOINT_OUT)
            self.input = endpoints.get(usb.util.ENDPOINT_IN)
            if self.out is None or self.input is None:
                raise DeviceError("找不到 AR844 的 USB 输入或输出端点")
        except Exception:
            self.close()
            raise

    def _write(self, opcode, flags=0):
        payload = command(opcode, flags)
        if self.out.write(payload, timeout=1000) != len(payload):
            raise DeviceError("USB 指令写入不完整")

    def read(self):
        self._write(0xB3)
        time.sleep(0.15)
        return decode(self.input.read(64, timeout=1000))

    def configure(self, mode):
        self._write(0x56, mode.bits)
        return True  # Reopen the Linux device, then verify with a fresh reading.

    def close(self):
        try:
            self.util.dispose_resources(self.dev)
        finally:
            if self.detached:
                try:
                    self.dev.attach_kernel_driver(0)
                finally:
                    self.util.dispose_resources(self.dev)


class DemoDevice:
    def __init__(self):
        self.mode = Mode()

    def read(self):
        now = time.time_ns() // 1_000_000
        second = now / 1000
        db = 56 + 6 * math.sin(second / 19) + 3 * math.sin(second * 0.72)
        return Sample(now, round(db, 1), self.mode)

    def configure(self, mode):
        self.mode = mode
        return False

    def close(self):
        pass


def open_device(vid, pid, demo=False):
    if demo:
        return DemoDevice()
    if sys.platform in ("win32", "darwin"):
        return HidDevice(vid, pid)
    if sys.platform == "linux":
        return UsbDevice(vid, pid)
    raise DeviceError(f"暂不支持此平台：{sys.platform}")


def verify_backend():
    if sys.platform in ("win32", "darwin"):
        import hid

        hid.enumerate(0x1234, 0x5678)
    elif sys.platform == "linux":
        from usb.backend.libusb1 import get_backend

        if get_backend() is None:
            raise DeviceError("找不到 libusb-1.0")
