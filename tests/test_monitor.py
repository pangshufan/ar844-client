from dataclasses import replace
import time

from ar844.device import HidDevice, Mode, Sample, command, decode
from ar844.monitor import LIVE_LIMIT, Monitor, Settings


def wait_for(condition):
    end = time.monotonic() + 6
    while time.monotonic() < end:
        if condition():
            return
        time.sleep(0.02)
    assert condition()


def test_command_and_report_framing():
    mode = Mode("FAST", "C", "MAX")
    assert command(0x56, mode.bits) == bytes([0x56, 0x70] + [0] * 6)
    assert command(0xB3, hid=True) == bytes([0, 0xB3] + [0] * 7)
    assert decode(bytes([0x02, 0x58, 0x70])).db == 60
    assert decode(bytes([0x02, 0x58, 0x70])).mode == mode


def test_hid_transport_accepts_windows_report_padding():
    class Handle:
        def write(self, report):
            assert report[0] == 0 and len(report) == 9
            self.opcode = report[1]
            return 65

        def read(self, size, timeout):
            return [0xC4] if self.opcode == 0x56 else [0x02, 0x58, 0x50]

    device = HidDevice.__new__(HidDevice)
    device.handle = Handle()
    assert device.configure(Mode()) is False
    assert device.read().db == 60


def test_reconnect_pause_and_shutdown_release_devices(tmp_path):
    events = []

    class Device:
        def __init__(self, index):
            self.index = index
            self.mode = Mode()
            events.append(("open", index))

        def read(self):
            if self.index == 1:
                raise OSError("disconnected")
            return Sample(time.time_ns() // 1_000_000, 64.1, self.mode)

        def configure(self, mode):
            self.mode = mode
            return False

        def close(self):
            events.append(("close", self.index))

    def factory(*_):
        return Device(1 + sum(event == "open" for event, _ in events))

    settings = Settings(interval=0.25)
    monitor = Monitor(tmp_path / "test.sqlite3", settings, factory=factory)
    monitor.start()
    try:
        wait_for(lambda: monitor.snapshot()[0].sample is not None)
        assert events[:3] == [("open", 1), ("close", 1), ("open", 2)]
        new = replace(settings, mode=Mode("SLOW", "A"))
        monitor.update(new)
        wait_for(lambda: monitor.snapshot()[0].sample.mode == new.mode)
        monitor.update(replace(new, enabled=False))
        wait_for(lambda: monitor.snapshot()[0].text == "已暂停")
        assert events[-1] == ("close", 2)
        monitor.update(new)
        wait_for(lambda: ("open", 3) in events)
    finally:
        monitor.stop()
        assert monitor.join(5)
    assert events[-1] == ("close", 3)


def test_live_cache_does_not_grow_with_run_time(tmp_path):
    monitor = Monitor(tmp_path / "test.sqlite3", Settings())
    now = time.time_ns() // 1_000_000
    # Simulate a sustained input burst without real-time sleeps or writing test data to disk.
    for i in range(3 * LIVE_LIMIT):
        monitor._publish("test", Sample(now + i, 60, Mode()))
    _, _, values = monitor.snapshot(include_points=True)
    assert len(values) == LIVE_LIMIT
    assert values[-1].time_ms == now + 3 * LIVE_LIMIT - 1
