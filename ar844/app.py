import argparse
import logging
from pathlib import Path
import sys
import time

from PySide6.QtCore import QEvent, QLockFile, QSettings, QStandardPaths, QTimer, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox, QPushButton,
    QSystemTrayIcon, QTabWidget, QVBoxLayout, QWidget,
)

from ar844.chart import NoiseChart, format_time
from ar844.device import Mode, verify_backend
from ar844.history_view import HistoryView
from ar844.monitor import HOUR_MS, Monitor, Settings

STYLE = """
QMainWindow, QWidget { background: #0c1523; color: #d8e4f4; }
QLabel#title { font-size: 24px; font-weight: 700; color: #eff5ff; }
QLabel#reading { font-size: 42px; font-weight: 700; color: #61d9c8; }
QLabel#status, QLabel#chartDetail { color: #acbdd5; padding: 4px; }
QTabWidget::pane { border: 1px solid #26374c; }
QTabBar::tab { background: #111c2b; padding: 9px 18px; color: #acbdd5; }
QTabBar::tab:selected { background: #203449; color: #ecf5ff; }
QComboBox, QAbstractSpinBox, QLineEdit, QPushButton {
    background: #18263a; border: 1px solid #37506a; border-radius: 4px;
    padding: 5px 9px; min-height: 20px;
}
QPushButton:hover { background: #27415b; }
QPushButton#primary { background: #195949; color: #eefefa; }
QPushButton:disabled, QPushButton#primary:disabled { color: #91a0b2; background: #172234; }
QTableWidget { background: #111c2b; alternate-background-color: #18263a; gridline-color: #213349; }
QHeaderView::section { background: #1b2d43; color: #d8e4f4; padding: 5px; border: 0; }
"""


def app_icon():
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor("#132338"))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(1, 1, 62, 62, 14, 14)
    painter.setPen(QPen(QColor("#61d9c8"), 4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    for x, height in [(12, 11), (20, 22), (28, 35), (36, 25), (44, 17), (52, 9)]:
        painter.drawLine(x, 32 - height // 2, x, 32 + height // 2)
    painter.end()
    return QIcon(pixmap)


class SettingsView(QWidget):
    def __init__(self, prefs, initial, path, monitor, fixed_path, tray_available):
        super().__init__()
        self.prefs = prefs
        self.monitor = monitor
        self.speed = self._options(("FAST", "SLOW"), initial.mode.speed)
        self.weight = self._options(("A", "C"), initial.mode.weight)
        self.run = self._options(("NORMAL", "MAX"), initial.mode.run)
        self.debug = QCheckBox("启用采集 Debug")
        self.debug.setChecked(initial.debug)
        self.debug.setToolTip(f"记录采集诊断信息：{monitor.debug_path}")
        self.interval = QDoubleSpinBox()
        self.interval.setRange(monitor.minimum_interval(initial.debug), 60)
        self.debug.toggled.connect(lambda enabled: self.interval.setMinimum(monitor.minimum_interval(enabled)))
        self.interval.setSingleStep(0.25)
        self.interval.setSuffix(" 秒")
        self.interval.setValue(initial.interval)
        if monitor.windows_hardware:
            self.interval.setToolTip("启用 Debug 后，Windows 在每轮采样完成后等待这个间隔。")
        self.vid = QLineEdit(f"{initial.vid:04X}")
        self.pid = QLineEdit(f"{initial.pid:04X}")
        self.enabled = QCheckBox("运行时采集")
        self.enabled.setChecked(initial.enabled)
        self.close_to_tray = QCheckBox("关闭窗口时继续在托盘运行")
        self.close_to_tray.setChecked(prefs.value("close_to_tray", True, type=bool))
        if not tray_available:
            self.close_to_tray.setText("关闭时最小化（当前桌面未提供托盘）")
        self.path = QLineEdit(str(path))
        self.path.setReadOnly(True)
        browse = QPushButton("选择…")
        browse.setEnabled(not fixed_path)
        browse.clicked.connect(self._browse)
        path_row = QHBoxLayout()
        path_row.addWidget(self.path, 1)
        path_row.addWidget(browse)
        self.notice = QLabel()
        self.notice.setWordWrap(True)
        self.notice.setTextFormat(Qt.TextFormat.PlainText)
        apply = QPushButton("保存并应用")
        apply.setObjectName("primary")
        apply.clicked.connect(self._save)
        layout = QFormLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        for label, field in (("响应速度", self.speed), ("频率计权", self.weight),
                             ("设备模式", self.run), ("采样间隔", self.interval),
                             ("USB VID（十六进制）", self.vid), ("USB PID（十六进制）", self.pid),
                             ("数据库", path_row)):
            layout.addRow(label, field)
        layout.addRow(self.enabled)
        layout.addRow(self.close_to_tray)
        layout.addRow(self.debug)
        layout.addRow(apply)
        layout.addRow(self.notice)
        self.fixed_path = fixed_path
        self.active_path = path

    @staticmethod
    def _options(values, current):
        box = QComboBox()
        box.addItems(values)
        box.setCurrentText(current)
        return box

    def _browse(self):
        path, _ = QFileDialog.getSaveFileName(self, "选择数据库位置", self.path.text(),
                                               "SQLite 数据库 (*.sqlite3 *.db)",
                                               options=QFileDialog.Option.DontConfirmOverwrite)
        if path:
            self.path.setText(path)

    def _save(self):
        try:
            ids = [int(field.text().strip(), 16) for field in (self.vid, self.pid)]
            if not all(0 < value <= 0xFFFF for value in ids):
                raise ValueError
        except ValueError:
            self.notice.setText("VID 和 PID 必须是 0001 至 FFFF 的十六进制数。")
            return
        mode = Mode(self.speed.currentText(), self.weight.currentText(), self.run.currentText())
        settings = Settings(mode, self.interval.value(), *ids, self.enabled.isChecked(), self.debug.isChecked())
        values = {"speed": mode.speed, "weight": mode.weight, "run": mode.run,
                  "interval": settings.interval, "vid": settings.vid, "pid": settings.pid,
                  "enabled": settings.enabled, "debug": settings.debug,
                  "close_to_tray": self.close_to_tray.isChecked()}
        if not self.fixed_path:
            values["database"] = self.path.text()
        for key, value in values.items():
            self.prefs.setValue(key, value)
        self.prefs.sync()
        self.monitor.update(settings)
        if self.prefs.status() != QSettings.Status.NoError:
            self.notice.setText("本次运行已应用参数，系统设置存储写入失败。")
        elif Path(self.path.text()) != self.active_path:
            self.notice.setText("监测参数已应用。重启后使用所选数据库，已有记录继续保存在原文件中。")
        elif settings.debug:
            self.notice.setText(f"采集 Debug 已开启。诊断日志：{self.monitor.debug_path}")
        else:
            self.notice.setText("设置已保存。请在实时监测页查看设备回读状态。")


class MainWindow(QMainWindow):
    def __init__(self, prefs, path, settings, demo, fixed_path):
        super().__init__()
        self.prefs = prefs
        self.monitor = Monitor(path, settings, demo)
        self.demo = demo
        self.quitting = False
        self.cleaned_up = False
        self.last_display = None
        self.setWindowTitle("AR844 噪声监测")
        self.resize(1010, 640)
        self.setMinimumSize(850, 470)
        self.tray = self._make_tray()

        self.reading = QLabel("— dB")
        self.reading.setObjectName("reading")
        self.connection = QLabel("正在打开数据库")
        self.actual = QLabel("设备回读 —")
        self.desired = QLabel("期望配置 —")
        for label in (self.connection, self.actual, self.desired):
            label.setObjectName("status")
            label.setTextFormat(Qt.TextFormat.PlainText)
        self.connection.setWordWrap(True)
        self.live_chart = NoiseChart()
        live = QWidget()
        live_layout = QVBoxLayout(live)
        overview = QHBoxLayout()
        overview.addWidget(self.reading)
        overview.addSpacing(16)
        status = QVBoxLayout()
        for label in (self.connection, self.actual, self.desired):
            status.addWidget(label)
        overview.addLayout(status, 1)
        live_layout.addLayout(overview)
        chart_controls = QHBoxLayout()
        chart_controls.addWidget(QLabel("近一小时"))
        chart_controls.addStretch()
        self.follow = QCheckBox("跟随最新数据")
        self.follow.setChecked(True)
        chart_controls.addWidget(self.follow)
        live_layout.addLayout(chart_controls)
        live_layout.addWidget(self.live_chart, 1)
        self.live = live

        self.history = HistoryView(path)
        self.tabs = QTabWidget()
        self.tabs.addTab(live, "实时监测")
        self.tabs.addTab(self.history, "历史查询")
        self.tabs.addTab(SettingsView(prefs, settings, path, self.monitor, fixed_path,
                                      self.tray is not None), "设置")
        self.tabs.currentChanged.connect(self.refresh)

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(18, 14, 18, 14)
        top = QHBoxLayout()
        title = QLabel("AR844 噪声监测")
        title.setObjectName("title")
        top.addWidget(title)
        top.addStretch()
        if demo:
            top.addWidget(QLabel("模拟设备"))
        exit_button = QPushButton("退出")
        exit_button.clicked.connect(self.quit)
        top.addWidget(exit_button)
        layout.addLayout(top)
        layout.addWidget(self.tabs, 1)
        self.setCentralWidget(content)

        self.refresh_timer = QTimer(self, interval=500)
        self.refresh_timer.timeout.connect(self.refresh)
        self.monitor.start()

    def _make_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return None
        tray = QSystemTrayIcon(QApplication.windowIcon(), self)
        tray.setToolTip("AR844 噪声监测")
        self.tray_menu = QMenu(self)
        self.tray_menu.addAction("显示主窗口", self.restore)
        self.tray_menu.addAction("退出", self.quit)
        tray.setContextMenu(self.tray_menu)
        tray.activated.connect(self._tray_activated)
        tray.show()
        return tray

    def _tray_activated(self, reason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                      QSystemTrayIcon.ActivationReason.DoubleClick):
            self.restore()

    def restore(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def refresh(self, *_):
        if self.quitting or not self.isVisible() or self.isMinimized():
            return
        live = self.tabs.currentWidget() is self.live
        status, settings, points = self.monitor.snapshot(include_points=live)
        self.history.set_ready(status.ready)
        if not live:
            return
        now = time.time_ns() // 1_000_000
        self.desired.setText(f"期望配置  {settings.mode.label}  ·  {settings.interval:g} 秒")
        self.connection.setText(status.text)
        if status.sample:
            sample = status.sample
            fresh = status.text.startswith("正在采集") or status.text.startswith("采集中")
            self.reading.setText(f"{sample.db:.1f} dB{sample.mode.weight}" if fresh else "— dB")
            self.actual.setText(f"最近回读  {sample.mode.label}  ·  {format_time(sample.time_ms, '%H:%M:%S')}")
        chart_key = (len(points), points[-1] if points else None, settings.interval)
        if chart_key != self.last_display:
            self.last_display = chart_key
            self.live_chart.set_points(((p.time_ms, p.db, p.mode, None) for p in points),
                                       gap_ms=max(1000, settings.interval * 1800))
        if self.follow.isChecked():
            self.live_chart.set_range(now - HOUR_MS, now)

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh_timer.start()
        self.refresh()

    def hideEvent(self, event):
        self.refresh_timer.stop()
        super().hideEvent(event)

    def changeEvent(self, event):
        super().changeEvent(event)
        if hasattr(self, "refresh_timer") and event.type() == QEvent.Type.WindowStateChange:
            if self.isMinimized():
                self.refresh_timer.stop()
            elif self.isVisible():
                self.refresh_timer.start()
                self.refresh()

    def closeEvent(self, event):
        if not self.quitting and self.prefs.value("close_to_tray", True, type=bool):
            event.ignore()
            if self.tray is not None and QSystemTrayIcon.isSystemTrayAvailable():
                self.hide()
            else:
                self.showMinimized()
        else:
            event.accept()
            self.quit()

    def eventFilter(self, watched, event):
        if watched is QApplication.instance() and event.type() == QEvent.Type.Quit:
            self.quitting = True
        return super().eventFilter(watched, event)

    def quit(self):
        if not self.quitting:
            self.quitting = True
            self.shutdown()
            QApplication.quit()

    def shutdown(self):
        if self.cleaned_up:
            return
        self.cleaned_up = True
        self.refresh_timer.stop()
        self.monitor.stop()
        self.history.shutdown()
        self.monitor.join()
        if self.tray is not None:
            self.tray.hide()


def main(argv=None):
    parser = argparse.ArgumentParser(description="AR844 噪声监测")
    parser.add_argument("--demo", action="store_true", help="使用模拟设备和独立数据库")
    parser.add_argument("--database", type=Path, help="覆盖本次运行使用的数据库路径")
    parser.add_argument("--smoke-test", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.smoke_test:
        verify_backend()
    logging.basicConfig(level=logging.WARNING)
    app = QApplication([sys.argv[0]])
    app.setOrganizationName("AR844")
    app.setApplicationName("AR844 Monitor")
    app.setQuitOnLastWindowClosed(False)
    app.setWindowIcon(app_icon())
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    demo = args.demo or args.smoke_test
    prefs = QSettings("AR844", "AR844 Demo" if demo else "AR844 Monitor")
    app_dir = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation))
    stored = prefs.value("database", str(app_dir / ("demo.sqlite3" if demo else "noise.sqlite3")))
    path = (args.database or Path(stored)).expanduser().resolve()
    if args.smoke_test:
        prefs = QSettings(str(path) + ".smoke.ini", QSettings.Format.IniFormat)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        QMessageBox.critical(None, "无法打开数据库目录", str(exc))
        return 1

    lock = QLockFile(str(path) + ".lock")
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        if args.smoke_test:
            return 1
        QMessageBox.critical(None, "无法锁定数据库", f"请检查目录权限，或关闭正在使用此数据库的客户端。\n{path}")
        return 1

    mode = Mode(prefs.value("speed", "FAST"), prefs.value("weight", "C"), prefs.value("run", "NORMAL"))
    settings = Settings(mode, prefs.value("interval", 1.0, type=float),
                        prefs.value("vid", 0x1234, type=int), prefs.value("pid", 0x5678, type=int),
                        prefs.value("enabled", True, type=bool), prefs.value("debug", False, type=bool))
    if args.smoke_test:
        settings = Settings()
    window = MainWindow(prefs, path, settings, demo, args.database is not None)
    app.installEventFilter(window)
    app.aboutToQuit.connect(window.shutdown)
    window.show()

    if args.smoke_test:
        deadline = time.monotonic() + 15
        timer = QTimer(window, interval=100)
        hidden_at = None
        queried = False

        def check():
            nonlocal hidden_at, queried
            status, _, _ = window.monitor.snapshot()
            if status.sample is not None:
                if hidden_at is None:
                    window.refresh()
                    if window.live_chart.visible_points:
                        hidden_at = status.sample.time_ms
                        window.close()
                        if window.refresh_timer.isActive():
                            app.exit(1)
                            return
                elif status.sample.time_ms > hidden_at and not queried:
                    window.restore()
                    window.tabs.setCurrentWidget(window.history)
                    window.history.end.setDateTime(window.history.end.dateTime().addSecs(60))
                    window.history.period.setCurrentIndex(1)
                    window.history._search()
                    queried = True
                elif queried and window.history.count > 0 and window.history.table.rowCount() > 0:
                    timer.stop()
                    QTimer.singleShot(2000, lambda: app.exit(1))
                    app.quit()
                    return
            if time.monotonic() > deadline:
                timer.stop()
                sys.stderr.write(f"Smoke test failed. {status.text}. {window.history.message.text()}\n")
                app.exit(1)
        timer.timeout.connect(check)
        timer.start()
    return app.exec()
