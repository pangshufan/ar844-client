from bisect import bisect_left
from collections import defaultdict
from datetime import datetime
from math import hypot

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from ar844.device import Mode


COLORS = ("#61d9c8", "#85aafa", "#f1bd6b", "#ed9db4",
          "#5ad0fc", "#b6a5ff", "#c0db80", "#efa584")


def format_time(ms, pattern="%Y-%m-%d %H:%M:%S"):
    return datetime.fromtimestamp(ms / 1000).strftime(pattern)


class NoiseChart(QWidget):
    def __init__(self):
        super().__init__()
        self.plot = pg.PlotWidget(axisItems={"bottom": pg.DateAxisItem()}, background="#111c2b")
        self.plot.showGrid(x=True, y=True, alpha=0.19)
        self.plot.setLabel("left", "声级", units="dB")
        self.plot.getPlotItem().hideButtons()
        self.plot.setMinimumHeight(220)
        self.legend = self.plot.addLegend(offset=(12, 8), labelTextColor="#d8e4f4")
        self.detail = QLabel("移动鼠标查看读数")
        self.detail.setWordWrap(True)
        self.detail.setTextFormat(Qt.TextFormat.PlainText)
        self.detail.setObjectName("chartDetail")
        self.plot.scene().sigMouseMoved.connect(self._hover)
        self.crosshair = pg.InfiniteLine(angle=90, pen=pg.mkPen("#7186a4", width=1))
        self.plot.addItem(self.crosshair, ignoreBounds=True)
        self.crosshair.hide()
        self.series = {}
        self.visible_points = {}
        self.hover_position = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.plot, 1)
        layout.addWidget(self.detail)

    def set_points(self, points, *, gap_ms=None):
        groups = defaultdict(list)
        for ts, db, mode, detail in points:
            groups[mode].append((ts / 1000, db, detail))
        self.visible_points.clear()
        self.crosshair.hide()
        self.detail.setText("移动鼠标查看读数" if groups else "此时间范围暂无数据")
        for mode, line in self.series.items():
            if mode not in groups and line.isVisible():
                line.setData([], [])
                line.hide()
                self.legend.removeItem(line)

        for mode, group in groups.items():
            group.sort(key=lambda point: point[0])
            x, y, details = zip(*group)
            self.visible_points[mode] = (x, y, details)
            line = self.series.get(mode)
            if line is None:
                color = COLORS[mode.bits >> 4]
                line = self.plot.plot(pen=pg.mkPen(color, width=1),
                                      symbolPen=None, symbolBrush=color, symbolSize=6)
                self.series[mode] = line
                self.legend.addItem(line, mode.label)
            elif not line.isVisible():
                line.show()
                self.legend.addItem(line, mode.label)
            x_values = np.asarray(x)
            connect = "all"
            if gap_ms is not None:
                connect = np.r_[np.diff(x_values) <= gap_ms / 1000, False]
            line.setData(x_values, np.asarray(y), connect=connect,
                         symbol="o" if len(x) <= 50 else None)
        if self.hover_position is not None:
            self._hover(self.hover_position)

    def set_range(self, start, end):
        self.plot.setXRange(start / 1000, end / 1000, padding=0)

    def _hover(self, position):
        box = self.plot.getViewBox()
        if not self.visible_points or not box.sceneBoundingRect().contains(position):
            self.hover_position = None
            self.crosshair.hide()
            return
        self.hover_position = position
        cursor = box.mapSceneToView(position)
        closest = None
        for mode, (x, y, details) in self.visible_points.items():
            index = bisect_left(x, cursor.x())
            for i in (index - 1, index):
                if 0 <= i < len(x):
                    dot = box.mapViewToScene(pg.Point(x[i], y[i]))
                    distance = hypot(dot.x() - position.x(), dot.y() - position.y())
                    if closest is None or distance < closest[0]:
                        closest = distance, mode, x[i], y[i], details[i]
        if closest is None or closest[0] > 40:
            self.crosshair.hide()
            self.detail.setText("移动鼠标查看读数")
            return
        _, mode, ts, db, detail = closest
        self.crosshair.setPos(ts)
        self.crosshair.show()
        self.detail.setText(detail or f"{format_time(ts * 1000)}   {db:.1f} dB   {mode.label}")


def history_points(rows):
    for row in rows:
        mode = Mode(row["speed"], row["weight"], row["run"])
        detail = None
        if row["end_ms"] is not None:
            rise = format_db(row["max_rise"])
            detail = (f"{format_time(row['ts'])} 至 {format_time(row['end_ms'])}   "
                      f"峰值 {row['db']:.1f} dB @ {format_time(row['peak_ms'])}   "
                      f"{row['sample_count']} 条   {mode.label}\n"
                      f"最小 {row['min_db']:.1f}   平均 {row['avg_db']:.1f}   "
                      f"最大单步涨幅 {rise}   P10 {row['p10']:.1f}   P90 {row['p90']:.1f}   "
                      f"峰值高出底噪 {row['above_floor']:.1f} dB")
        yield row["ts"], row["plot_db"], mode, detail


def format_db(value):
    return "—" if value is None else f"{value:.1f}"
