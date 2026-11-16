from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event

from PySide6.QtCore import QDateTime, QTimer, Qt
from PySide6.QtWidgets import (
    QComboBox, QDateTimeEdit, QHeaderView, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from ar844.chart import NoiseChart, format_db, format_time, history_points
from ar844.storage import CHART_BINS, METRICS, HistoryRequest, PAGE_SIZE, history


class HistoryView(QWidget):
    def __init__(self, path):
        super().__init__()
        self.path = path
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ar844-history")
        self.cancelled = Event()
        self.future = None
        self.request = None
        self.count = 0
        self.ready = False
        self.timer = QTimer(self, interval=120)
        self.timer.timeout.connect(self._poll)

        now = QDateTime.currentDateTime()
        self.start = QDateTimeEdit(now.addSecs(-3600))
        self.end = QDateTimeEdit(now)
        for edit in (self.start, self.end):
            edit.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
            edit.setCalendarPopup(True)
        self.period = QComboBox()
        for label, minutes in (("原始数据", 0), ("5 分钟统计", 5), ("15 分钟统计", 15),
                               ("30 分钟统计", 30), ("1 小时统计", 60)):
            self.period.addItem(label, minutes)
        self.metric = QComboBox()
        for key, (label, _) in METRICS.items():
            self.metric.addItem(label, key)
        self.metric.setEnabled(False)
        self.period.currentIndexChanged.connect(
            lambda: self.metric.setEnabled(bool(self.period.currentData()))
        )
        self.weight = QComboBox()
        for label, value in (("全部计权", ""), ("A 计权", "A"), ("C 计权", "C")):
            self.weight.addItem(label, value)
        self.search = QPushButton("查询")
        self.search.setObjectName("primary")
        self.search.clicked.connect(self._search)
        self.search.setEnabled(False)
        self.cancel = QPushButton("取消")
        self.cancel.clicked.connect(self.cancelled.set)
        self.latest = QPushButton("最近一小时")
        self.latest.clicked.connect(self._latest)

        filters = QHBoxLayout()
        for widget in (QLabel("从"), self.start, QLabel("到"), self.end,
                       self.period, self.weight, self.latest, self.search, self.cancel):
            filters.addWidget(widget)
        self.message = QLabel("按时间和粒度查询本机记录")
        self.message.setWordWrap(True)
        self.message.setTextFormat(Qt.TextFormat.PlainText)

        self.chart = NoiseChart()
        self.table = QTableWidget()
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.prev = QPushButton("上一页")
        self.next = QPushButton("下一页")
        self.prev.clicked.connect(lambda: self._page(-1))
        self.next.clicked.connect(lambda: self._page(1))
        self.page_info = QLabel()
        table_page = QWidget()
        table_layout = QVBoxLayout(table_page)
        table_layout.addWidget(self.table)
        paging = QHBoxLayout()
        paging.addStretch()
        for widget in (self.prev, self.page_info, self.next):
            paging.addWidget(widget)
        table_layout.addLayout(paging)
        tabs = QTabWidget()
        tabs.addTab(self.chart, "曲线")
        tabs.addTab(table_page, "数据表")
        layout = QVBoxLayout(self)
        layout.addLayout(filters)
        summary = QHBoxLayout()
        summary.addWidget(QLabel("曲线指标"))
        summary.addWidget(self.metric)
        summary.addWidget(self.message, 1)
        layout.addLayout(summary)
        layout.addWidget(tabs, 1)
        self._buttons()

    def _buttons(self):
        idle = self.future is None
        self.search.setEnabled(idle and self.ready)
        self.latest.setEnabled(idle and self.ready)
        self.cancel.setVisible(not idle)
        self.prev.setEnabled(idle and self.request is not None and self.request.page > 0)
        self.next.setEnabled(idle and self.request is not None
                             and (self.request.page + 1) * PAGE_SIZE < self.count)

    def set_ready(self, ready):
        if self.ready != ready:
            self.ready = ready
            self._buttons()

    def _search(self):
        request = HistoryRequest(self.start.dateTime().toMSecsSinceEpoch(),
                                 self.end.dateTime().toMSecsSinceEpoch(),
                                 self.period.currentData(), self.weight.currentData(),
                                 metric=self.metric.currentData())
        if request.end <= request.start:
            self.message.setText("结束时间必须晚于开始时间。")
            return
        self._submit(request, include_chart=True)

    def _latest(self):
        now = QDateTime.currentDateTime()
        self.start.setDateTime(now.addSecs(-3600))
        self.end.setDateTime(now)
        self._search()

    def _page(self, direction):
        if self.request is not None:
            self._submit(replace(self.request, page=self.request.page + direction),
                         include_chart=False)

    def _submit(self, request, *, include_chart):
        if self.future is not None:
            return
        self.cancelled.clear()
        self.pending = request, include_chart
        self.message.setText("正在查询…")
        self.future = self.pool.submit(history, self.path, request, self.cancelled.is_set,
                                       include_chart=include_chart)
        self._buttons()
        self.timer.start()

    def _poll(self):
        if not self.future.done():
            return
        self.timer.stop()
        future, self.future = self.future, None
        request, include_chart = self.pending
        try:
            result = future.result()
        except Exception as exc:
            self.message.setText("查询已取消" if self.cancelled.is_set() else f"查询失败：{exc}")
            self._buttons()
            return
        if self.cancelled.is_set():
            self.message.setText("查询已取消")
            self._buttons()
            return
        self.request = request
        self.count = result.count
        total_pages = max(1, (self.count + PAGE_SIZE - 1) // PAGE_SIZE)
        self.page_info.setText(f"{request.page + 1} / {total_pages}")
        note = " · 统计显示与查询时间重叠的完整窗口" if request.minutes else ""
        if include_chart:
            self.sampled_chart = result.count > 2 * CHART_BINS and len(result.chart) < result.count
            gap = None
            if request.minutes:
                gap = request.minutes * 60_000
                if self.sampled_chart:
                    gap = max(gap, 2 * (request.end - request.start + gap) // CHART_BINS)
            label = METRICS[request.metric][0] if request.minutes else "声级"
            self.chart.plot.setLabel("left", label, units="dB")
            self.chart.set_points(history_points(result.chart), gap_ms=gap)
            begin = request.start
            if request.minutes:
                duration = request.minutes * 60_000
                begin = begin // duration * duration
            self.chart.set_range(begin, request.end)
        if self.sampled_chart:
            note += " · 曲线保留峰谷，表格显示全部结果"
        self.message.setText(f"{result.count:,} 条记录{note}")
        self._set_table(result.rows, request.minutes)
        self._buttons()

    def _set_table(self, rows, period):
        columns = (["窗口开始", "窗口结束", "峰值 dB", "最小值 dB", "平均值 dB",
                    "最大单步涨幅 dB", "P10 dB", "P90 dB", "峰值高出底噪 dB", "峰值时间", "样本数"]
                   if period else ["采样时间", "读数 dB"])
        columns += ["计权", "快慢", "设备模式"]
        self.table.clearContents()
        self.table.setColumnCount(len(columns))
        self.table.setHorizontalHeaderLabels(columns)
        self.table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            values = [format_time(row["ts"])]
            if period:
                values.append(format_time(row["end_ms"]))
                values.extend(format_db(row[key]) for key in (
                    "db", "min_db", "avg_db", "max_rise", "p10", "p90", "above_floor"
                ))
                values += [format_time(row["peak_ms"]), str(row["sample_count"])]
            else:
                values.append(f"{row['db']:.1f}")
            values += [row["weight"], row["speed"], row["run"]]
            for j, value in enumerate(values):
                self.table.setItem(i, j, QTableWidgetItem(value))

    def shutdown(self):
        self.timer.stop()
        self.cancelled.set()
        self.pool.shutdown(wait=True, cancel_futures=True)
