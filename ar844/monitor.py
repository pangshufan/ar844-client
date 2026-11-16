from collections import deque
from contextlib import closing
from dataclasses import dataclass, replace
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys
from threading import Event, Lock, Thread
import time

from ar844.device import (
    ConfigUnconfirmed, HidDevice, HidReadTimeout, InvalidReading, Mode, Sample, UsbDevice, open_device,
)
from ar844.storage import connect, initialize, recent, save

HOUR_MS = 3_600_000
LIVE_LIMIT = 15_000  # At least one hour at the fastest supported interval (250 ms).
LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Settings:
    mode: Mode = Mode()
    interval: float = 1.0
    vid: int = 0x1234
    pid: int = 0x5678
    enabled: bool = True
    debug: bool = False


@dataclass(frozen=True, slots=True)
class Status:
    text: str
    sample: Sample | None = None
    ready: bool = False


class Monitor:
    def __init__(self, path: Path, settings: Settings, demo=False, factory=open_device):
        self.path = path
        self.demo = demo
        self.windows_hardware = sys.platform == "win32" and not demo
        self.debug_path = path.with_name(path.name + ".debug.log")
        self._debug = logging.Logger("ar844.debug", logging.DEBUG)
        self._debug_handler = None
        self._debug_enabled = False
        self.factory = factory
        self._settings = self._normalize(settings)
        self._status = Status("正在打开数据库")
        self._points = deque(maxlen=LIVE_LIMIT)
        self._lock = Lock()
        self._wake = Event()
        self._stop = Event()
        self._thread = Thread(target=self._run, name="ar844-monitor")

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def join(self, timeout=None):
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def update(self, settings):
        with self._lock:
            self._settings = self._normalize(settings)
        self._wake.set()

    def _normalize(self, settings):
        minimum = self.minimum_interval(settings.debug)
        if settings.interval < minimum:
            return replace(settings, interval=minimum)
        return settings

    def minimum_interval(self, debug):
        return 1.0 if self.windows_hardware and debug else 0.25

    def _set_debug(self, enabled):
        if enabled == self._debug_enabled:
            return
        self._debug_enabled = enabled
        if enabled:
            try:
                handler = RotatingFileHandler(self.debug_path, maxBytes=2_000_000,
                                              backupCount=3, encoding="utf-8")
            except OSError as exc:
                LOG.warning("Could not open AR844 debug log: %s", exc)
                return
            handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
            self._debug.addHandler(handler)
            self._debug_handler = handler
            self._debug.debug("debug enabled platform=%s demo=%s", sys.platform, self.demo)
        elif self._debug_handler is not None:
            self._debug.debug("debug disabled")
            self._debug.removeHandler(self._debug_handler)
            self._debug_handler.close()
            self._debug_handler = None

    def snapshot(self, *, include_points=False):
        with self._lock:
            if self._thread.is_alive():
                self._prune(time.time_ns() // 1_000_000 - HOUR_MS)
            return self._status, self._settings, list(self._points) if include_points else []

    def _prune(self, cutoff):
        while self._points and self._points[0].time_ms < cutoff:
            self._points.popleft()

    def _publish(self, text, sample=None, *, ready=True):
        with self._lock:
            previous = self._status.sample
            if sample is not None:
                self._points.append(sample)
                self._prune(sample.time_ms - HOUR_MS)
            self._status = Status(text, sample if sample is not None else previous, ready)

    @staticmethod
    def _close(device):
        if device is not None:
            try:
                device.close()
            except Exception as exc:
                LOG.warning("Could not close AR844 device: %s", exc)

    def _run(self):
        try:
            while not self._stop.is_set():
                try:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    with self._lock:
                        debug_requested = self._settings.debug
                    self._set_debug(debug_requested)
                    with closing(connect(self.path)) as conn:
                        initialize(conn, self._stop.is_set)
                        points = recent(conn, time.time_ns() // 1_000_000 - HOUR_MS, LIVE_LIMIT)
                        with self._lock:
                            self._points.clear()
                            self._points.extend(points)
                        self._publish("正在连接", ready=True)
                        self._collect(conn)
                except Exception as exc:
                    self._debug.debug("database_error type=%s error=%s", type(exc).__name__, exc)
                    self._publish(f"数据库不可用：{exc}", ready=False)
                    self._stop.wait(3)
        finally:
            self._set_debug(False)

    def _collect(self, conn):
        device = None
        previous = None
        failures = 0
        read_timeouts = 0
        next_read = 0.0
        next_config = 0
        config_message = ""
        try:
            while not self._stop.is_set():
                self._wake.clear()
                if self._stop.is_set():
                    break
                with self._lock:
                    settings = self._settings
                self._set_debug(settings.debug)
                debug = self._debug_handler is not None
                windows_debug = self.windows_hardware and settings.debug
                started = time.monotonic()
                delay = settings.interval
                if settings != previous and debug:
                    self._debug.debug("settings vid=%04x pid=%04x mode=%s interval=%g enabled=%s",
                                      settings.vid, settings.pid, settings.mode.label,
                                      settings.interval, settings.enabled)
                if previous is not None and settings.debug != previous.debug:
                    next_read = 0
                    read_timeouts = 0
                if previous is None or (settings.vid, settings.pid) != (previous.vid, previous.pid):
                    self._close(device)
                    device = None
                    failures = 0
                    next_config = 0
                    config_message = ""
                if previous is None or settings.mode != previous.mode:
                    next_config = 0
                    config_message = ""
                previous = settings
                if not settings.enabled:
                    self._close(device)
                    device = None
                    next_config = 0
                    config_message = ""
                    self._publish("已暂停")
                    self._wake.wait(60)
                    continue

                sample = None
                message = ""
                try:
                    if device is None:
                        device = self.factory(settings.vid, settings.pid, self.demo)
                        read_timeouts = 0
                        if debug:
                            self._debug.debug("connected backend=%s open_ms=%.1f",
                                              type(device).__name__, (time.monotonic() - started) * 1000)
                        if windows_debug:
                            next_read = time.monotonic() + 1
                            self._publish("设备已连接 · 等待稳定")
                    if isinstance(device, (HidDevice, UsbDevice)):
                        device.trace = self._debug.debug if debug else None
                    if windows_debug:
                        remaining = next_read - time.monotonic()
                        if remaining > 0:
                            self._wake.wait(remaining)
                            continue
                    read_started = time.monotonic()
                    sample = device.read()
                    if debug:
                        self._debug.debug("sample ts_ms=%d db=%.1f mode=%s read_ms=%.1f",
                                          sample.time_ms, sample.db, sample.mode.label,
                                          (time.monotonic() - read_started) * 1000)
                    failures = 0
                    read_timeouts = 0
                    message = "正在采集"
                    if sample.mode != settings.mode:
                        if started >= next_config:
                            next_config = time.monotonic() + 5
                            try:
                                reconnect = device.configure(settings.mode)
                                config_message = ""
                                if debug:
                                    self._debug.debug("config desired=%s actual=%s reopen=%s",
                                                      settings.mode.label, sample.mode.label, reconnect)
                            except ConfigUnconfirmed as exc:
                                reconnect = False
                                config_message = str(exc)
                                if debug:
                                    self._debug.debug("config_unconfirmed desired=%s error=%s",
                                                      settings.mode.label, exc)
                            if reconnect:
                                self._close(device)
                                device = None
                                delay = max(delay, 2)
                        message = f"采集中 · {config_message or '等待配置回读'}"
                    else:
                        next_config = 0
                        config_message = ""
                except InvalidReading as exc:
                    failures = 0
                    read_timeouts = 0
                    message = str(exc)
                    if debug:
                        self._debug.debug("invalid_reading %s", exc)
                except Exception as exc:
                    if settings.debug and isinstance(exc, HidReadTimeout) and read_timeouts == 0:
                        read_timeouts += 1
                        message = f"读数超时 · 下轮重试（{exc}）"
                    else:
                        self._close(device)
                        device = None
                        failures = min(failures + 1, 5)
                        delay = max(delay, min(2 ** failures, 30))
                        message = f"等待重连（{exc}）"
                    if debug:
                        self._debug.debug("acquisition_error type=%s outcome=%s", type(exc).__name__, message)

                if sample is not None:
                    save(conn, sample)
                self._publish(message, sample)
                if windows_debug:
                    # Match the reference driver: wait after the exchange, including timeouts.
                    next_read = time.monotonic() + delay
                    self._wake.wait(delay)
                else:
                    self._wake.wait(max(0, started + delay - time.monotonic()))
        finally:
            self._close(device)
