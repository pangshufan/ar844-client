from collections import deque
from contextlib import closing
from dataclasses import dataclass
import logging
from pathlib import Path
from threading import Event, Lock, Thread
import time

from ar844.device import InvalidReading, Mode, Sample, open_device
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


@dataclass(frozen=True, slots=True)
class Status:
    text: str
    sample: Sample | None = None
    ready: bool = False


class Monitor:
    def __init__(self, path: Path, settings: Settings, demo=False, factory=open_device):
        self.path = path
        self.demo = demo
        self.factory = factory
        self._settings = settings
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
            self._settings = settings
        self._wake.set()

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
        while not self._stop.is_set():
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with closing(connect(self.path)) as conn:
                    initialize(conn)
                    points = recent(conn, time.time_ns() // 1_000_000 - HOUR_MS, LIVE_LIMIT)
                    with self._lock:
                        self._points.clear()
                        self._points.extend(points)
                    self._publish("正在连接", ready=True)
                    self._collect(conn)
            except Exception as exc:
                self._publish(f"数据库不可用：{exc}", ready=False)
                self._stop.wait(3)

    def _collect(self, conn):
        device = None
        previous = None
        failures = 0
        next_config = 0
        try:
            while not self._stop.is_set():
                self._wake.clear()
                with self._lock:
                    settings = self._settings
                started = time.monotonic()
                delay = settings.interval
                if previous is None or (settings.vid, settings.pid) != (previous.vid, previous.pid):
                    self._close(device)
                    device = None
                    failures = 0
                    next_config = 0
                if previous is None or settings.mode != previous.mode:
                    next_config = 0
                previous = settings
                if not settings.enabled:
                    self._close(device)
                    device = None
                    self._publish("已暂停")
                    self._wake.wait(60)
                    continue

                sample = None
                message = ""
                try:
                    if device is None:
                        device = self.factory(settings.vid, settings.pid, self.demo)
                    sample = device.read()
                    failures = 0
                    message = "正在采集"
                    if sample.mode != settings.mode:
                        message = "采集中 · 等待配置回读"
                        if started >= next_config:
                            next_config = started + 5
                            if device.configure(settings.mode):
                                self._close(device)
                                device = None
                                delay = max(delay, 2)
                    else:
                        next_config = 0
                except InvalidReading as exc:
                    failures = 0
                    message = str(exc)
                except Exception as exc:
                    self._close(device)
                    device = None
                    failures = min(failures + 1, 5)
                    delay = max(delay, min(2 ** failures, 30))
                    message = f"等待重连（{exc}）"

                if sample is not None:
                    save(conn, sample)
                self._publish(message, sample)
                self._wake.wait(max(0, started + delay - time.monotonic()))
        finally:
            self._close(device)
