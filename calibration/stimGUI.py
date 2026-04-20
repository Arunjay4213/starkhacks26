"""
calibration/stimGUI.py

PyQt5 desktop app for electrode placement and intensity tuning against the
Monday hardware bridge. Not part of the live demo. Operators use this tool
while the subject sits with electrodes on the hand and the Belifu unit on a
calibration mode, twitching one channel at a time to find the cleanest
placement and the correct dial intensity.

Safety posture: this GUI is a UI over the HTTP contract, nothing else.
Every safety-relevant rule lives in the firmware and the bridge. The GUI
only adds two things on top:
  1. Escape key and a big red STOP ALL button that stay enabled even when
     the connection indicator is red.
  2. A Verify Safety routine that exercises the priority-stop path end to
     end and reports pass or fail.

Threading model: HTTP calls run on short-lived daemon threads. Results
come back to the UI thread through Qt signals, which are thread safe
when connected with the default Qt.AutoConnection across threads.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

# Make the repo root importable so app.state resolves when the file is run
# directly from the calibration/ directory.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import requests  # noqa: E402
from PyQt5.QtCore import QObject, Qt, QTimer, pyqtSignal  # noqa: E402
from PyQt5.QtGui import QColor, QKeySequence, QPalette  # noqa: E402
from PyQt5.QtWidgets import (  # noqa: E402
    QApplication,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QShortcut,
    QSlider,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.state import Action, Finger  # noqa: E402


DEFAULT_BASE_URL = "http://localhost:5001"
HEALTH_POLL_MS = 2000
HTTP_TIMEOUT_S = 1.0
SEQUENCE_GAP_MS = 500
SEQUENCE_PULSE_MS = 200  # duration per finger inside the sequence test
VERIFY_STOP_LATENCY_BUDGET_S = 0.100
WATCHDOG_EXPECTED_FLOOR_MS = 2500  # after a short idle, watchdog should be near 3000


# =============================================================================
# HTTP worker
# =============================================================================


@dataclass
class HttpResult:
    method: str
    url: str
    status_code: Optional[int]
    body: Any
    elapsed_s: float
    error: Optional[str]
    tag: str  # free-form label so callers can route results


class HttpClient(QObject):
    """
    Fires HTTP requests on daemon threads and emits finished() on the UI
    thread via Qt's cross-thread signal delivery.
    """

    finished = pyqtSignal(object)  # HttpResult

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._base_url = DEFAULT_BASE_URL

    def set_base_url(self, url: str) -> None:
        self._base_url = url.rstrip("/")

    def base_url(self) -> str:
        return self._base_url

    def _spawn(self, fn: Callable[[], HttpResult]) -> None:
        t = threading.Thread(target=lambda: self.finished.emit(fn()), daemon=True)
        t.start()

    def get(self, path: str, tag: str) -> None:
        url = self._base_url + path
        def work() -> HttpResult:
            t0 = time.monotonic()
            try:
                r = requests.get(url, timeout=HTTP_TIMEOUT_S)
                return HttpResult("GET", url, r.status_code, _safe_json(r), time.monotonic() - t0, None, tag)
            except requests.RequestException as e:
                return HttpResult("GET", url, None, None, time.monotonic() - t0, str(e), tag)
        self._spawn(work)

    def post(self, path: str, payload: dict, tag: str) -> None:
        url = self._base_url + path
        def work() -> HttpResult:
            t0 = time.monotonic()
            try:
                r = requests.post(url, json=payload, timeout=HTTP_TIMEOUT_S)
                return HttpResult("POST", url, r.status_code, _safe_json(r), time.monotonic() - t0, None, tag)
            except requests.RequestException as e:
                return HttpResult("POST", url, None, None, time.monotonic() - t0, str(e), tag)
        self._spawn(work)


def _safe_json(r: "requests.Response") -> Any:
    try:
        return r.json()
    except ValueError:
        return r.text


# =============================================================================
# Main window
# =============================================================================


class CalibrationWindow(QMainWindow):
    # Signal used by background routines (Verify Safety, Sequence) to append
    # log lines from threads other than the main thread.
    log_line = pyqtSignal(str, str)  # (html_color, message)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Monday Calibration")
        self.resize(760, 640)

        self._http = HttpClient(self)
        self._http.finished.connect(self._on_http_result)
        self._http.set_base_url(os.environ.get("MONDAY_RECEIVER_URL", DEFAULT_BASE_URL))

        self._connected = False  # True when last /health poll succeeded
        self._build_ui()
        self._install_hotkeys()
        self.log_line.connect(self._append_log_colored)

        # Health polling
        self._health_timer = QTimer(self)
        self._health_timer.setInterval(HEALTH_POLL_MS)
        self._health_timer.timeout.connect(self._poll_health)
        self._health_timer.start()
        self._poll_health()

    # ------------------------ UI construction ------------------------

    def _build_ui(self) -> None:
        central = QWidget(self)
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        # STOP ALL always at the top.
        self.btn_stop = QPushButton("STOP ALL")
        self.btn_stop.setMinimumHeight(80)
        self.btn_stop.setStyleSheet(
            "QPushButton { background-color: #c0392b; color: white; "
            "font-size: 22px; font-weight: bold; border-radius: 6px; } "
            "QPushButton:pressed { background-color: #922b21; }"
        )
        self.btn_stop.clicked.connect(self._on_stop_clicked)
        root.addWidget(self.btn_stop)

        # Connection row: dot, label, URL field, apply button.
        conn_row = QHBoxLayout()
        self.conn_dot = QLabel()
        self.conn_dot.setFixedSize(20, 20)
        self._set_connection_dot(False)
        conn_row.addWidget(self.conn_dot)
        conn_row.addWidget(QLabel("Receiver:"))
        self.url_field = QLineEdit(self._http.base_url())
        self.url_field.setMinimumWidth(260)
        conn_row.addWidget(self.url_field)
        self.btn_apply_url = QPushButton("Apply")
        self.btn_apply_url.clicked.connect(self._on_apply_url)
        conn_row.addWidget(self.btn_apply_url)
        conn_row.addStretch(1)
        root.addLayout(conn_row)

        # Duration slider.
        dur_row = QHBoxLayout()
        dur_row.addWidget(QLabel("Duration:"))
        self.duration_slider = QSlider(Qt.Horizontal)
        self.duration_slider.setMinimum(50)
        self.duration_slider.setMaximum(1000)
        self.duration_slider.setSingleStep(50)
        self.duration_slider.setPageStep(50)
        self.duration_slider.setTickInterval(50)
        self.duration_slider.setTickPosition(QSlider.TicksBelow)
        self.duration_slider.setValue(200)
        self.duration_slider.valueChanged.connect(self._on_duration_changed)
        dur_row.addWidget(self.duration_slider, 1)
        self.duration_label = QLabel("200 ms")
        self.duration_label.setMinimumWidth(70)
        dur_row.addWidget(self.duration_label)
        root.addLayout(dur_row)

        # Finger buttons (Pinky, Middle, Index as specified).
        fingers_row = QGridLayout()
        self.btn_pinky = self._make_finger_button("Pinky [1]", Finger.PINKY)
        self.btn_middle = self._make_finger_button("Middle [2]", Finger.MIDDLE)
        self.btn_index = self._make_finger_button("Index [3]", Finger.INDEX)
        fingers_row.addWidget(self.btn_pinky, 0, 0)
        fingers_row.addWidget(self.btn_middle, 0, 1)
        fingers_row.addWidget(self.btn_index, 0, 2)
        root.addLayout(fingers_row)

        # Utility row: sequence + verify safety.
        util_row = QHBoxLayout()
        self.btn_sequence = QPushButton("Sequence Test")
        self.btn_sequence.setMinimumHeight(40)
        self.btn_sequence.clicked.connect(self._on_sequence_clicked)
        util_row.addWidget(self.btn_sequence)
        self.btn_verify = QPushButton("Verify Safety")
        self.btn_verify.setMinimumHeight(40)
        self.btn_verify.clicked.connect(self._on_verify_clicked)
        util_row.addWidget(self.btn_verify)
        root.addLayout(util_row)

        # Log panel.
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setLineWrapMode(QTextEdit.NoWrap)
        mono = self.log.font()
        mono.setFamily("monospace")
        self.log.setFont(mono)
        root.addWidget(self.log, 1)

        self.setCentralWidget(central)

        # Gated buttons. STOP ALL is intentionally not in this list.
        self._gated_buttons = [
            self.btn_pinky,
            self.btn_middle,
            self.btn_index,
            self.btn_sequence,
            self.btn_verify,
        ]

    def _make_finger_button(self, text: str, finger: Finger) -> QPushButton:
        btn = QPushButton(text)
        btn.setMinimumHeight(70)
        btn.setStyleSheet("QPushButton { font-size: 16px; font-weight: bold; }")
        btn.clicked.connect(lambda _=False, f=finger: self._fire_finger(f))
        return btn

    def _install_hotkeys(self) -> None:
        QShortcut(QKeySequence(Qt.Key_Escape), self, activated=self._on_stop_clicked)
        QShortcut(QKeySequence(Qt.Key_1), self, activated=lambda: self._hotkey_fire(Finger.PINKY))
        QShortcut(QKeySequence(Qt.Key_2), self, activated=lambda: self._hotkey_fire(Finger.MIDDLE))
        QShortcut(QKeySequence(Qt.Key_3), self, activated=lambda: self._hotkey_fire(Finger.INDEX))

    # ------------------------ Connection state ------------------------

    def _set_connection_dot(self, connected: bool) -> None:
        color = "#27ae60" if connected else "#c0392b"
        self.conn_dot.setStyleSheet(
            f"background-color: {color}; border-radius: 10px; border: 1px solid #222;"
        )

    def _set_connected(self, connected: bool) -> None:
        if connected == self._connected:
            return
        self._connected = connected
        self._set_connection_dot(connected)
        for b in self._gated_buttons:
            b.setEnabled(connected)
        self._append_log_colored(
            "#27ae60" if connected else "#c0392b",
            "connection UP" if connected else "connection DOWN (buttons disabled, STOP still works)",
        )

    def _poll_health(self) -> None:
        self._http.get("/health", tag="health")

    # ------------------------ Log helpers ------------------------

    def _ts(self) -> str:
        return datetime.now().strftime("%H:%M:%S.%f")[:-3]

    def _append_log_colored(self, color: str, message: str) -> None:
        """Thread-safe only when invoked via self.log_line signal or UI thread."""
        safe = _html_escape(message)
        self.log.append(f'<span style="color: #888;">[{self._ts()}]</span> '
                        f'<span style="color: {color};">{safe}</span>')
        sb = self.log.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _log_info(self, msg: str) -> None:
        self._append_log_colored("#2c3e50", msg)

    def _log_ok(self, msg: str) -> None:
        self._append_log_colored("#27ae60", msg)

    def _log_warn(self, msg: str) -> None:
        self._append_log_colored("#e67e22", msg)

    def _log_err(self, msg: str) -> None:
        self._append_log_colored("#c0392b", msg)

    # ------------------------ Button handlers ------------------------

    def _on_apply_url(self) -> None:
        new_url = self.url_field.text().strip()
        if not new_url:
            self._log_err("empty URL ignored")
            self.url_field.setText(self._http.base_url())
            return
        self._http.set_base_url(new_url)
        self._log_info(f"base URL set to {new_url}")
        self._poll_health()

    def _on_duration_changed(self, value: int) -> None:
        # Snap to the 50 ms grid.
        snapped = (value // 50) * 50
        if snapped != value:
            self.duration_slider.setValue(snapped)
            return
        self.duration_label.setText(f"{snapped} ms")

    def _on_stop_clicked(self) -> None:
        self._log_warn("STOP ALL")
        self._http.post("/stop", {}, tag="stop")

    def _hotkey_fire(self, finger: Finger) -> None:
        if not self._connected:
            self._log_warn(f"hotkey {finger.value} ignored: connection down")
            return
        self._fire_finger(finger)

    def _fire_finger(self, finger: Finger) -> None:
        duration = self.duration_slider.value()
        payload = {"finger": finger.value, "action": Action.ON.value, "duration_ms": duration}
        self._log_info(f"fire {finger.value} for {duration} ms")
        self._http.post("/stimulate", payload, tag=f"fire:{finger.value}")

    def _on_sequence_clicked(self) -> None:
        if not self._connected:
            self._log_warn("sequence ignored: connection down")
            return
        self._log_info("sequence test: PINKY -> MIDDLE -> INDEX")
        threading.Thread(target=self._run_sequence, daemon=True, name="sequence").start()

    def _on_verify_clicked(self) -> None:
        if not self._connected:
            self._log_warn("verify safety ignored: connection down")
            return
        self._log_info("=== Verify Safety begin ===")
        threading.Thread(target=self._run_verify_safety, daemon=True, name="verify").start()

    # ------------------------ Background routines ------------------------
    #
    # These run off the UI thread. They make SYNCHRONOUS HTTP calls with
    # requests.post/get directly, so the timing is accurate. Results are
    # reported to the log via the log_line signal which marshals back to
    # the UI thread.

    def _post_sync(self, path: str, payload: dict) -> tuple[Optional[int], Any, float, Optional[str]]:
        url = self._http.base_url() + path
        t0 = time.monotonic()
        try:
            r = requests.post(url, json=payload, timeout=HTTP_TIMEOUT_S)
            return r.status_code, _safe_json(r), time.monotonic() - t0, None
        except requests.RequestException as e:
            return None, None, time.monotonic() - t0, str(e)

    def _get_sync(self, path: str) -> tuple[Optional[int], Any, float, Optional[str]]:
        url = self._http.base_url() + path
        t0 = time.monotonic()
        try:
            r = requests.get(url, timeout=HTTP_TIMEOUT_S)
            return r.status_code, _safe_json(r), time.monotonic() - t0, None
        except requests.RequestException as e:
            return None, None, time.monotonic() - t0, str(e)

    def _run_sequence(self) -> None:
        for finger in (Finger.PINKY, Finger.MIDDLE, Finger.INDEX):
            payload = {"finger": finger.value, "action": Action.ON.value, "duration_ms": SEQUENCE_PULSE_MS}
            status, body, elapsed, err = self._post_sync("/stimulate", payload)
            if err is not None:
                self.log_line.emit("#c0392b", f"sequence {finger.value} ERROR: {err}")
                return
            self.log_line.emit("#2c3e50", f"sequence {finger.value} -> {status} {body} ({elapsed*1000:.0f} ms)")
            time.sleep(SEQUENCE_GAP_MS / 1000.0)
        self.log_line.emit("#27ae60", "sequence complete")

    def _run_verify_safety(self) -> None:
        # Step 1: start a 1000 ms pulse on INDEX.
        payload = {"finger": Finger.INDEX.value, "action": Action.ON.value, "duration_ms": 1000}
        status, body, elapsed, err = self._post_sync("/stimulate", payload)
        if err is not None:
            self.log_line.emit("#c0392b", f"verify FAIL step 1: /stimulate error: {err}")
            return
        if status != 200:
            self.log_line.emit("#c0392b", f"verify FAIL step 1: /stimulate status {status} body {body}")
            return
        self.log_line.emit("#2c3e50", f"step 1: /stimulate INDEX 1000ms ok, ack={body}")

        # Step 2: wait 200 ms, then /stop and time the response.
        time.sleep(0.200)
        status, body, elapsed, err = self._post_sync("/stop", {})
        if err is not None:
            self.log_line.emit("#c0392b", f"verify FAIL step 2: /stop error: {err}")
            return
        if status != 200:
            self.log_line.emit("#c0392b", f"verify FAIL step 2: /stop status {status} body {body}")
            return
        self.log_line.emit("#2c3e50", f"step 2: /stop returned in {elapsed*1000:.1f} ms")

        # Step 3: assert /stop latency under budget.
        if elapsed >= VERIFY_STOP_LATENCY_BUDGET_S:
            self.log_line.emit(
                "#c0392b",
                f"verify FAIL step 3: /stop latency {elapsed*1000:.1f} ms >= "
                f"{VERIFY_STOP_LATENCY_BUDGET_S*1000:.0f} ms budget",
            )
            return
        self.log_line.emit("#27ae60", f"step 3: /stop latency {elapsed*1000:.1f} ms under 100 ms budget")

        # Step 4: short idle, then poll /status. Watchdog remaining should be
        # near 3000 ms if the bridge has not sent anything since /stop. The
        # bridge's _last_send_ms was just updated by /stop, so remaining
        # should be very close to 3000 right now. Wait a beat to let the
        # pulse tail thread, if any, run its abort path.
        time.sleep(0.100)
        status, body, elapsed, err = self._get_sync("/status")
        if err is not None:
            self.log_line.emit("#c0392b", f"verify FAIL step 4: /status error: {err}")
            return
        if status != 200 or not isinstance(body, dict):
            self.log_line.emit("#c0392b", f"verify FAIL step 4: /status bad response: {status} {body}")
            return
        remaining = body.get("watchdog_remaining_ms", 0)
        if not isinstance(remaining, int) or remaining < WATCHDOG_EXPECTED_FLOOR_MS:
            self.log_line.emit(
                "#c0392b",
                f"verify FAIL step 4: watchdog_remaining_ms={remaining} < "
                f"{WATCHDOG_EXPECTED_FLOOR_MS} (expected near 3000, no pulse in flight)",
            )
            return
        self.log_line.emit(
            "#27ae60",
            f"step 4: watchdog_remaining_ms={remaining}, connected={body.get('connected')}",
        )

        self.log_line.emit("#27ae60", "=== Verify Safety PASS ===")

    # ------------------------ Async HTTP result sink ------------------------

    def _on_http_result(self, result: HttpResult) -> None:
        if result.tag == "health":
            self._set_connected(result.error is None and result.status_code == 200)
            return

        if result.error is not None:
            self._log_err(f"{result.method} {result.url} FAILED: {result.error}")
            return

        if result.status_code and 200 <= result.status_code < 300:
            self._log_info(
                f"{result.method} {result.url} -> {result.status_code} "
                f"{result.body} ({result.elapsed_s*1000:.0f} ms)"
            )
        else:
            self._log_err(
                f"{result.method} {result.url} -> {result.status_code} {result.body}"
            )


def _html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# =============================================================================
# Entrypoint
# =============================================================================


def main() -> int:
    app = QApplication(sys.argv)
    # Force a sensible palette so the red STOP ALL and dot colors render
    # consistently on hosts that default to a dark theme.
    pal = app.palette()
    pal.setColor(QPalette.Window, QColor("#ecf0f1"))
    app.setPalette(pal)

    win = CalibrationWindow()
    win.show()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
