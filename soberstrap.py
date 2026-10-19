"""Sober Bootstrapper — a Bloxstrap-style launcher for Sober (Roblox on Linux).

GUI:  sober-strap
CLI:  sober-strap --launch | --fleasion | --no-proxy
"""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

APP = "org.vinegarhq.Sober"
HOME = Path.home()
APPDIR = HOME / ".var/app" / APP
CONFIG = APPDIR / "config/sober/config.json"
LOGDIR = APPDIR / "data/sober/sober_logs"
APK = APPDIR / "data/sober/packages/x86_64/com.roblox.client/split_config.x86_64.apk"
FLEASION = HOME / ".local/bin/fleasion-launch"
FLEASION_BIN = HOME / ".local/share/Fleasion/Fleasion"
PROXY_VARS = ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy",
              "all_proxy", "NO_PROXY", "no_proxy"]

# Performance-only preset (laptop-safe; every name checked against a current flag dump).
PERF_FLAGS = {
    "DFIntTaskSchedulerTargetFps": 60,
    "FFlagHandleAltEnterFullscreenManually": False,
    "DFIntMaxFrameBufferSize": 4,
    "FIntDebugForceMSAASamples": 1,
    "FFlagDisablePostFx": True,
    "FIntRenderShadowIntensity": 0,
    "FIntRobloxGuiBlurIntensity": 0,
    "DFIntDebugFRMQualityLevelOverride": 1,
    "DFFlagTextureQualityOverrideEnabled": True,
    "DFIntTextureQualityOverride": 0,
    "FIntDebugTextureManagerSkipMips": 2,
    "FIntRenderLocalLightUpdatesMax": 1,
    "FIntRenderLocalLightUpdatesMin": 1,
    "FIntRenderLocalLightFadeInMs": 0,
    "FIntGrassMovementReducedMotionFactor": 0,
    "FIntFRMMinGrassDistance": 0,
    "FIntFRMMaxGrassDistance": 0,
    "FIntRenderGrassDetailStrands": 0,
    "DFIntCSGLevelOfDetailSwitchingDistance": 0,
    "DFIntCSGLevelOfDetailSwitchingDistanceL12": 0,
    "DFIntCSGLevelOfDetailSwitchingDistanceL23": 0,
    "DFIntCSGLevelOfDetailSwitchingDistanceL34": 0,
}

VALID_NAME = re.compile(r"^[DS]?F(Flag|Int|String|Log)")
# Flags known to freeze/crash/break the client when forced; stripped on import.
BAD_FLAG = re.compile(
    r"Crash|Throw|Assert|DebugDraw|Debugger|DrawSkeleton|MicroProfiler|Imgui|OOM"
    r"|_IXP$|_PlaceFilter$|_UniverseFilter$|^DFIntRuntimeConcurrency$"
    r"|^DFIntConnectionMTUSize$|^DFFlagAlwaysSkipDiskCache$|CheckEngineContextOnInstanceAccess")


# ------------------------------------------------------------------ backend
def sober_running():
    """Is something holding Sober's single-instance lock on the APK?"""
    try:
        ino = APK.stat().st_ino
        locks = Path("/proc/locks").read_text()
    except OSError:
        return False
    return re.search(rf"FLOCK +ADVISORY +WRITE +\d+ +[0-9a-f]+:[0-9a-f]+:{ino} ", locks) is not None


_proxy_cache = (0.0, None)


def proxy_port(max_age=10):
    """Port of the proxy baked into Sober's flatpak override (Fleasion env mode), if any."""
    global _proxy_cache
    if time.monotonic() - _proxy_cache[0] < max_age:
        return _proxy_cache[1]
    _proxy_cache = (time.monotonic(), _read_proxy_port())
    return _proxy_cache[1]


def _read_proxy_port():
    try:
        out = subprocess.run(["flatpak", "override", "--user", "--show", APP],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"^(?:HTTPS?_PROXY|https?_proxy)=http://(?:127\.0\.0\.1|localhost):(\d+)", out, re.M)
    return int(m.group(1)) if m else None


def port_up(port):
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def fleasion_running():
    return subprocess.run(["pgrep", "-f", str(FLEASION_BIN)], capture_output=True).returncode == 0


def sober_version():
    try:
        out = subprocess.run(["flatpak", "info", APP], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return "?"
    m = re.search(r"^\s*Version:\s*(\S+)", out, re.M)
    return m.group(1) if m else "?"


def read_config():
    """Sober's config is JSON with // comment lines on top. Returns (header, data)."""
    lines = CONFIG.read_text().splitlines(keepends=True)
    header = [l for l in lines if l.lstrip().startswith("//")]
    body = "".join(l for l in lines if not l.lstrip().startswith("//"))
    return header, json.loads(body)


def get_flags():
    try:
        return read_config()[1].get("fflags", {})
    except (OSError, ValueError):
        return {}


def convert_value(v):
    """Bloxstrap writes everything as strings; Sober wants real JSON types."""
    if not isinstance(v, str):
        return v
    if v.lower() == "true":
        return True
    if v.lower() == "false":
        return False
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    return v


def write_flags(flags):
    if not CONFIG.exists():
        raise RuntimeError("Sober's config doesn't exist yet. Open Sober once first.")
    if sober_running():
        raise RuntimeError("Close Roblox first — Sober rewrites its config on exit.")
    header, data = read_config()
    data["fflags"] = {k: convert_value(v) for k, v in flags.items()}
    shutil.copy2(CONFIG, CONFIG.with_suffix(".json.bak"))
    tmp = CONFIG.with_suffix(".json.tmp")
    tmp.write_text("".join(header) + json.dumps(data, indent=4) + "\n")
    tmp.replace(CONFIG)


def clean_import(flags):
    """Drop invalid names, empty values and known crash/debug flags."""
    return {k: v for k, v in flags.items()
            if VALID_NAME.match(k) and not BAD_FLAG.search(k) and v != ""}


def launch(mode):
    """mode: auto | fleasion | noproxy. Returns a status message. Fleasion waiting is the caller's job."""
    if sober_running():
        return "Roblox is already running."
    port = proxy_port(max_age=0)
    cmd = ["flatpak", "run"]
    msg = "Launching Roblox…"
    if port and (mode == "noproxy" or (mode == "auto" and not port_up(port))):
        # Proxy configured but dead (or not wanted): strip it so Roblox can connect.
        cmd += [f"--unset-env={v}" for v in PROXY_VARS]
        if mode == "auto":
            msg = "Fleasion isn't running — launching without its proxy."
    subprocess.Popen(cmd + [APP], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    return msg


def start_fleasion():
    if not fleasion_running():
        subprocess.Popen([str(FLEASION)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)


def kill_sober():
    subprocess.run(["flatpak", "kill", APP], capture_output=True)


def tidy(line):
    """'info: Roblox: 2026-..Z,6.51,fb97,12 [DFLog::X] msg' -> '[X] msg'."""
    line = re.sub(r"^info: Roblox: \S+?,[\d.]+,[0-9a-f]+,\d+(?:,\w+)?\s*", "", line.rstrip())
    return re.sub(r"\[D?F?Log::", "[", line)


def latest_errors():
    logs = sorted((p for p in LOGDIR.glob("*.log") if not p.is_symlink()),
                  key=lambda p: p.stat().st_mtime) if LOGDIR.exists() else []
    if not logs:
        return None, []
    pat = re.compile(r"FATAL|ERROR|Error \[|HttpTraceError|tunnel failed")
    with open(logs[-1], errors="replace") as f:
        hits = [tidy(l) for l in f if pat.search(l) and "FlagCache" not in l]
    return logs[-1], hits[-60:]


def clear_cache():
    if sober_running():
        raise RuntimeError("Close Roblox first.")
    app_data = APPDIR / "data/sober/appData"
    for p in [app_data / "rbx-storage", APPDIR / "cache/sober"]:
        shutil.rmtree(p, ignore_errors=True)
    for name in ["rbx-storage.db", "rbx-storage.db-wal", "rbx-storage.db-shm"]:
        (app_data / name).unlink(missing_ok=True)




# ---------------------------------------------------------------------- CLI
CLI_MODES = {"--launch": "auto", "--fleasion": "fleasion", "--no-proxy": "noproxy"}
if len(sys.argv) > 1 and sys.argv[1] in CLI_MODES and sys.argv[1] != "--fleasion":
    print(launch(CLI_MODES[sys.argv[1]]))
    sys.exit(0)

# ---------------------------------------------------------------------- GUI
import math  # noqa: E402

from PyQt6.QtCore import (  # noqa: E402
    QEasingCurve, QParallelAnimationGroup, QPoint, QPointF, QProcess, QPropertyAnimation, QRect,
    QRectF, QSettings, QSize, Qt, QTimer, QVariantAnimation, pyqtProperty)
from PyQt6.QtGui import (  # noqa: E402
    QAction, QBrush, QColor, QFont, QIcon, QKeySequence, QLinearGradient, QPainter, QPainterPath,
    QPen, QPixmap, QRadialGradient, QShortcut)
from PyQt6.QtWidgets import (  # noqa: E402
    QAbstractButton, QAbstractItemView, QApplication, QButtonGroup, QFileDialog, QFrame,
    QGraphicsDropShadowEffect, QGraphicsOpacityEffect, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMenu, QMessageBox, QPlainTextEdit, QPushButton, QStackedWidget, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget)

# Bundled assets live next to the script, or in PyInstaller's unpack dir when frozen.
ASSETS = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "assets"
DATA_DIR = HOME / ".local/share/sober-strap"
LOGO = DATA_DIR / "logo.png"   # user's logo (Settings → Logo); falls back to the bundled one
DESKTOP = HOME / ".local/share/applications/sober-strap.desktop"

BG, PANEL, CARD, LINE = "#0d0b24", "#151345", "#1c1a52", "#2c2970"
ACCENT, ACCENT2, TEXT, MUTED = "#7c5cff", "#b14cff", "#ece9ff", "#9a96c8"
OK, WARN, BAD = "#3ddc97", "#ffb547", "#ff5c7a"
FPS_CHOICES = [("60", 60), ("120", 120), ("144", 144), ("240", 240), ("Uncapped", 9999)]
EASE = QEasingCurve.Type.OutCubic


def make_qss(r):
    """r = outer corner radius (0 when maximized)."""
    return f"""
* {{ font-family: 'Inter', 'Cantarell', 'Noto Sans', sans-serif; color: {TEXT}; }}
QWidget#root {{ background: transparent; }}
QFrame#shell {{ background: {BG}; border: 1px solid {LINE}; border-radius: {r}px; }}
QFrame#titlebar {{ background: {PANEL}; border-top-left-radius: {r}px; border-top-right-radius: {r}px;
    border-bottom: 1px solid {LINE}; }}
QLabel#titleText {{ font-size: 13px; font-weight: 800; letter-spacing: 2px; }}
QLabel#chip {{ background: {CARD}; border: 1px solid {LINE}; border-radius: 8px; padding: 2px 8px;
    color: {MUTED}; font-size: 11px; font-weight: 600; }}
QFrame#sidebar {{ background: {PANEL}; border-right: 1px solid {LINE}; border-bottom-left-radius: {r}px; }}
QFrame#navPill {{ border-radius: 10px;
    background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 {ACCENT}, stop:1 {ACCENT2}); }}
QLabel#brandSub {{ color: {MUTED}; font-size: 11px; }}
QPushButton#nav {{
    text-align: left; padding: 11px 16px; border: none; border-radius: 10px;
    background: transparent; color: {MUTED}; font-size: 14px; font-weight: 600; }}
QPushButton#nav:hover {{ color: {TEXT}; }}
QPushButton#nav:checked {{ color: white; }}
QLabel#navBadge {{ background: {WARN}; border-radius: 4px; }}
QLabel#h1 {{ font-size: 26px; font-weight: 800; }}
QLabel#sub {{ color: {MUTED}; font-size: 13px; }}
QLabel#section {{ color: {MUTED}; font-size: 11px; font-weight: 800; letter-spacing: 1.5px; }}
QFrame#card {{ background: {CARD}; border: 1px solid {LINE}; border-radius: 14px; }}
QFrame#card:hover {{ border-color: #3d3891; }}
QLabel#cardTitle {{ color: {MUTED}; font-size: 11px; font-weight: 700; letter-spacing: 1px; }}
QLabel#cardValue {{ font-size: 16px; font-weight: 700; }}
QFrame#banner {{ background: #2b2210; border: 1px solid #6b5420; border-radius: 12px; }}
QLabel#heroTitle {{ font-size: 36px; font-weight: 900; background: transparent; }}
QLabel#heroSub {{ color: #e2dcff; font-size: 13px; background: transparent; }}
QLabel#fpsLabel {{ color: #d9d2ff; font-size: 12px; font-weight: 700; background: transparent; }}
QPushButton#play {{
    font-size: 20px; font-weight: 900; letter-spacing: 3px; padding: 16px 52px; border-radius: 14px;
    border: none; color: #1a0f4d; background: white; }}
QPushButton#play:hover {{ background: #f3efff; }}
QPushButton#play:pressed {{ background: #d8ceff; }}
QPushButton#play:disabled {{ background: #8f86c9; color: #3a3270; }}
QPushButton#stop {{
    font-size: 13px; font-weight: 800; padding: 10px 18px; border-radius: 12px;
    border: 1px solid rgba(255,92,122,0.6); background: rgba(40,8,24,0.55); color: {BAD}; }}
QPushButton#stop:hover {{ background: rgba(80,12,40,0.7); }}
QPushButton#seg {{
    padding: 8px 15px; border: 1px solid rgba(255,255,255,0.22); background: rgba(0,0,0,0.20);
    color: #e6e0ff; font-weight: 600; border-radius: 9px; }}
QPushButton#seg:hover {{ border-color: rgba(255,255,255,0.55); }}
QPushButton#seg:checked {{ background: rgba(255,255,255,0.93); color: #2a1a7a; border-color: white; }}
QPushButton#chipBtn {{
    padding: 5px 11px; border: 1px solid rgba(255,255,255,0.18); background: rgba(0,0,0,0.18);
    color: #e6e0ff; font-weight: 600; font-size: 12px; border-radius: 8px; }}
QPushButton#chipBtn:hover {{ border-color: rgba(255,255,255,0.5); }}
QPushButton#chipBtn:checked {{ background: rgba(255,255,255,0.9); color: #2a1a7a; }}
QPushButton#btn {{
    padding: 9px 16px; border-radius: 10px; border: 1px solid {LINE}; background: {CARD}; font-weight: 600; }}
QPushButton#btn:hover {{ border-color: {ACCENT}; background: #221f60; }}
QPushButton#primary {{
    padding: 9px 18px; border-radius: 10px; border: none; font-weight: 700; color: white;
    background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 {ACCENT}, stop:1 {ACCENT2}); }}
QPushButton#primary:hover {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #8e72ff, stop:1 #c06aff); }}
QPushButton#primary:disabled {{ background: {LINE}; color: {MUTED}; }}
QPushButton#danger {{
    padding: 9px 16px; border-radius: 10px; border: 1px solid #5a2342; background: #2a1330; color: {BAD}; font-weight: 600; }}
QPushButton#danger:hover {{ border-color: {BAD}; }}
QLineEdit {{
    padding: 9px 12px; border-radius: 10px; border: 1px solid {LINE}; background: {PANEL};
    selection-background-color: {ACCENT}; }}
QLineEdit:focus {{ border-color: {ACCENT}; }}
QTableWidget {{
    background: {PANEL}; border: 1px solid {LINE}; border-radius: 12px; gridline-color: {LINE};
    selection-background-color: #3a2f8f; alternate-background-color: #181650; }}
QHeaderView::section {{
    background: {CARD}; color: {MUTED}; border: none; border-bottom: 1px solid {LINE};
    padding: 8px; font-weight: 700; font-size: 11px; letter-spacing: 1px; }}
QTableWidget QLineEdit {{ padding: 2px 6px; border-radius: 4px; }}
QPlainTextEdit {{
    background: #090719; border: 1px solid {LINE}; border-radius: 12px; padding: 10px;
    font-family: 'JetBrains Mono', 'Fira Code', 'Noto Sans Mono', monospace; font-size: 12px; color: #cfc8ff; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 4px; }}
QScrollBar::handle:vertical {{ background: {LINE}; border-radius: 4px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {ACCENT}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QScrollBar:horizontal {{ height: 0; }}
QLabel#toast {{
    background: {CARD}; border: 1px solid {ACCENT}; border-radius: 12px; padding: 12px 18px; font-weight: 600; }}
QMenu {{ background: {PANEL}; border: 1px solid {LINE}; border-radius: 10px; padding: 6px; }}
QMenu::item {{ padding: 7px 18px; border-radius: 6px; }}
QMenu::item:selected {{ background: {ACCENT}; }}
QMenu::separator {{ height: 1px; background: {LINE}; margin: 4px 8px; }}
QMessageBox, QFileDialog {{ background: {PANEL}; }}
QMessageBox QPushButton {{ padding: 7px 18px; border-radius: 8px; border: 1px solid {LINE}; background: {CARD}; }}
QToolTip {{ background: {PANEL}; color: {TEXT}; border: 1px solid {LINE}; padding: 6px; border-radius: 6px; }}
"""


def logo_pixmap(size):
    """User logo, else the bundled logo, else a gradient placeholder."""
    for src in (LOGO, ASSETS / "logo.png"):
        if not src.exists():
            continue
        pm = QPixmap(str(src))
        if not pm.isNull():
            return pm.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    g = QLinearGradient(0, 0, size, size)
    g.setColorAt(0, QColor(ACCENT))
    g.setColorAt(1, QColor(ACCENT2))
    p.setBrush(g)
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(QRectF(0, 0, size, size), size * 0.28, size * 0.28)
    f = QFont()
    f.setBold(True)
    f.setPixelSize(int(size * 0.62))
    p.setFont(f)
    p.setPen(QColor("white"))
    p.drawText(QRect(0, 0, size, size), Qt.AlignmentFlag.AlignCenter, "S")
    p.end()
    return pm


def sync_desktop_icon():
    """Point the app-menu entry at logo.png once it exists."""
    if not (LOGO.exists() and DESKTOP.exists()):
        return
    text = DESKTOP.read_text()
    new = re.sub(r"^Icon=.*$", f"Icon={LOGO}", text, count=1, flags=re.M)
    if new != text:
        DESKTOP.write_text(new)


def button(text, kind="btn", slot=None, tip=None):
    b = QPushButton(text, objectName=kind)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    if slot:
        b.clicked.connect(slot)
    if tip:
        b.setToolTip(tip)
    return b


def page_header(title, sub):
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(2)
    lay.addWidget(QLabel(title, objectName="h1"))
    lay.addWidget(QLabel(sub, objectName="sub"))
    return w


# ----------------------------------------------------------------- widgets
class TitleButton(QAbstractButton):
    """Painted min / max / close glyphs (no font dependency)."""

    def __init__(self, kind, parent=None):
        super().__init__(parent)
        self.kind = kind
        self.maximized = False
        self.setFixedSize(40, 30)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._hover = 0.0
        self._anim = QVariantAnimation(self, duration=140, startValue=0.0, endValue=1.0)
        self._anim.valueChanged.connect(self._set_hover)

    def _set_hover(self, v):
        self._hover = v
        self.update()

    def enterEvent(self, e):
        self._anim.setDirection(QVariantAnimation.Direction.Forward)
        self._anim.start()

    def leaveEvent(self, e):
        self._anim.setDirection(QVariantAnimation.Direction.Backward)
        self._anim.start()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        bg = QColor(BAD if self.kind == "close" else "#ffffff")
        bg.setAlphaF((0.9 if self.kind == "close" else 0.10) * self._hover)
        p.setBrush(bg)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(QRectF(self.rect()).adjusted(3, 3, -3, -3), 7, 7)
        pen = QPen(QColor("white" if self.kind == "close" and self._hover > 0.5 else TEXT), 1.4)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        c = QPointF(self.width() / 2, self.height() / 2)
        s = 5.0
        if self.kind == "min":
            p.drawLine(QPointF(c.x() - s, c.y()), QPointF(c.x() + s, c.y()))
        elif self.kind == "max":
            if self.maximized:
                p.drawRect(QRectF(c.x() - s + 2, c.y() - s, 2 * s - 2, 2 * s - 2))
                p.drawRect(QRectF(c.x() - s, c.y() - s + 2, 2 * s - 2, 2 * s - 2))
            else:
                p.drawRoundedRect(QRectF(c.x() - s, c.y() - s, 2 * s, 2 * s), 1.5, 1.5)
        else:
            p.drawLine(QPointF(c.x() - s, c.y() - s), QPointF(c.x() + s, c.y() + s))
            p.drawLine(QPointF(c.x() + s, c.y() - s), QPointF(c.x() - s, c.y() + s))


class TitleBar(QFrame):
    def __init__(self, win):
        super().__init__(objectName="titlebar")
        self.win = win
        self.setFixedHeight(44)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 0, 8, 0)
        lay.setSpacing(10)
        self.logo = QLabel()
        self.set_logo()
        lay.addWidget(self.logo)
        lay.addWidget(QLabel("SOBERSTRAP", objectName="titleText"))
        self.chip = QLabel("", objectName="chip")
        lay.addWidget(self.chip, 0, Qt.AlignmentFlag.AlignVCenter)
        lay.addStretch(1)
        self.btn_min = TitleButton("min")
        self.btn_max = TitleButton("max")
        self.btn_close = TitleButton("close")
        self.btn_min.clicked.connect(win.showMinimized)
        self.btn_max.clicked.connect(win.toggle_max)
        self.btn_close.clicked.connect(win.close)
        for b in (self.btn_min, self.btn_max, self.btn_close):
            lay.addWidget(b)

    def set_logo(self):
        dpr = self.devicePixelRatioF() or 1
        pm = logo_pixmap(int(22 * dpr))
        pm.setDevicePixelRatio(dpr)
        self.logo.setPixmap(pm)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.win.windowHandle().startSystemMove()

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.win.toggle_max()


class Switch(QAbstractButton):
    """Animated toggle switch."""

    def __init__(self, checked=False):
        super().__init__()
        self.setCheckable(True)
        self.setChecked(checked)
        self.setFixedSize(46, 26)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._pos = 1.0 if checked else 0.0
        self._anim = QPropertyAnimation(self, b"knob", duration=180, easingCurve=EASE)
        self.toggled.connect(self._animate)

    def _animate(self, on):
        self._anim.stop()
        self._anim.setStartValue(self._pos)
        self._anim.setEndValue(1.0 if on else 0.0)
        self._anim.start()

    def get_knob(self):
        return self._pos

    def set_knob(self, v):
        self._pos = v
        self.update()

    knob = pyqtProperty(float, get_knob, set_knob)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        off, on = QColor(LINE), QColor(ACCENT)
        track = QColor(int(off.red() + (on.red() - off.red()) * self._pos),
                       int(off.green() + (on.green() - off.green()) * self._pos),
                       int(off.blue() + (on.blue() - off.blue()) * self._pos))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(track)
        p.drawRoundedRect(QRectF(0, 0, self.width(), self.height()), 13, 13)
        x = 3 + self._pos * (self.width() - 26)
        p.setBrush(QColor("white"))
        p.drawEllipse(QRectF(x, 3, 20, 20))


class PulseDot(QWidget):
    """Status dot that breathes while active."""

    def __init__(self):
        super().__init__()
        self.setFixedSize(16, 16)
        self.color = QColor(MUTED)
        self.active = False
        self._t = 0.0
        self._anim = QVariantAnimation(self, startValue=0.0, endValue=1.0, duration=1600, loopCount=-1)
        self._anim.valueChanged.connect(self._tick)

    def _tick(self, v):
        self._t = v
        self.update()

    def set_state(self, active, color):
        self.color = QColor(color)
        if active != self.active:
            self.active = active
            self._anim.start() if active else self._anim.stop()
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = QPointF(8, 8)
        if self.active:
            ring = QColor(self.color)
            ring.setAlphaF(0.45 * (1 - self._t))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(ring)
            r = 3.5 + 4.5 * self._t
            p.drawEllipse(c, r, r)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self.color)
        p.drawEllipse(c, 3.5, 3.5)


class Hero(QFrame):
    """Play banner with a slowly drifting gradient + glow blobs."""

    def __init__(self):
        super().__init__()
        self.phase = 0.0
        self.timer = QTimer(self, interval=33, timeout=self._tick)
        self.timer.start()

    def _tick(self):
        if self.isVisible():
            self.phase += 0.012
            self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(r, 18, 18)
        p.setClipPath(path)
        w, h, t = r.width(), r.height(), self.phase
        g = QLinearGradient(0, 0, w, h)
        g.setColorAt(0, QColor("#22156b"))
        g.setColorAt(0.5 + 0.18 * math.sin(t), QColor("#3a1fa0"))
        g.setColorAt(1, QColor("#6a2bc4"))
        p.fillRect(r, g)
        for cx, cy, rad, col, sp in [(0.78, 0.25, 0.55, QColor(177, 76, 255, 110), 1.0),
                                     (0.20, 0.95, 0.45, QColor(90, 120, 255, 80), -0.7),
                                     (0.55, 0.60, 0.35, QColor(255, 120, 220, 50), 1.6)]:
            x = w * (cx + 0.08 * math.cos(t * sp))
            y = h * (cy + 0.10 * math.sin(t * sp * 1.3))
            rg = QRadialGradient(QPointF(x, y), w * rad)
            rg.setColorAt(0, col)
            transparent = QColor(col)
            transparent.setAlpha(0)
            rg.setColorAt(1, transparent)
            p.fillRect(r, rg)
        p.setClipping(False)
        p.setPen(QPen(QColor(255, 255, 255, 40), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)


class GlowButton(QPushButton):
    """PLAY button: breathes when idle, glows up on hover."""

    def __init__(self, text):
        super().__init__(text, objectName="play")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.fx = QGraphicsDropShadowEffect(self, offset=QPointF(0, 0), color=QColor(214, 196, 255, 200),
                                            blurRadius=12)
        self.setGraphicsEffect(self.fx)
        self.pulse = QPropertyAnimation(self.fx, b"blurRadius", duration=2200, loopCount=-1)
        self.pulse.setKeyValueAt(0, 10.0)
        self.pulse.setKeyValueAt(0.5, 30.0)
        self.pulse.setKeyValueAt(1, 10.0)
        self.hover = QPropertyAnimation(self.fx, b"blurRadius", duration=220, easingCurve=EASE)
        self.pulse.start()

    def enterEvent(self, e):
        if self.isEnabled():
            self.pulse.stop()
            self.hover.stop()
            self.hover.setStartValue(self.fx.blurRadius())
            self.hover.setEndValue(55.0)
            self.hover.start()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self.hover.stop()
        if self.isEnabled():
            self.pulse.start()
        super().leaveEvent(e)

    def changeEvent(self, e):
        if e.type() == e.Type.EnabledChange:
            if self.isEnabled():
                self.pulse.start()
            else:
                self.pulse.stop()
                self.fx.setBlurRadius(0)
        super().changeEvent(e)


def card(title, value=""):
    f = QFrame(objectName="card")
    lay = QVBoxLayout(f)
    lay.setContentsMargins(16, 14, 16, 14)
    lay.setSpacing(4)
    top = QHBoxLayout()
    top.setSpacing(6)
    top.addWidget(QLabel(title.upper(), objectName="cardTitle"))
    top.addStretch(1)
    dot = PulseDot()
    top.addWidget(dot)
    v = QLabel(value, objectName="cardValue")
    v.setWordWrap(True)
    lay.addLayout(top)
    lay.addWidget(v)
    return f, v, dot


# ------------------------------------------------------------------ window
class Window(QWidget):
    RESIZE_MARGIN = 7

    def __init__(self):
        super().__init__(objectName="root")
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self.setAcceptDrops(True)
        self.setWindowTitle("Sober Bootstrapper")
        self.setWindowIcon(QIcon(logo_pixmap(256)))
        self.settings = QSettings("sober-strap", "sober-strap")
        self.resize(self.settings.value("size", QSize(1080, 700)))
        self.setMinimumSize(900, 600)

        self.version = sober_version()
        self.mode = self.settings.value("mode", "auto")
        self.dirty = False
        self.waiting_port = None
        self.was_running = False
        self.session_start = None
        self.update_available = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(*(self.RESIZE_MARGIN,) * 4)
        self.shell = QFrame(objectName="shell")
        outer.addWidget(self.shell)
        sl = QVBoxLayout(self.shell)
        sl.setContentsMargins(1, 1, 1, 1)
        sl.setSpacing(0)
        self.titlebar = TitleBar(self)
        self.titlebar.chip.setText(f"Sober {self.version}")
        sl.addWidget(self.titlebar)

        body = QHBoxLayout()
        body.setSpacing(0)
        body.addWidget(self.build_sidebar())
        self.stack = QStackedWidget()
        for p in (self.build_play(), self.build_flags(), self.build_tools(), self.build_logs(),
                  self.build_settings()):
            self.stack.addWidget(p)
        content = QWidget()
        cl = QVBoxLayout(content)
        cl.setContentsMargins(30, 24, 30, 26)
        cl.addWidget(self.stack)
        body.addWidget(content, 1)
        sl.addLayout(body, 1)

        self.toast_label = QLabel(self.shell, objectName="toast")
        self.toast_label.hide()
        self.toast_fx = QGraphicsOpacityEffect(self.toast_label)
        self.toast_label.setGraphicsEffect(self.toast_fx)
        self.toast_timer = QTimer(self, singleShot=True, timeout=self.hide_toast)

        self.status_timer = QTimer(self, interval=1000, timeout=self.refresh_status)
        self.status_timer.start()
        self.wait_timer = QTimer(self, interval=1000, timeout=self.poll_fleasion)
        self.load_flags()
        self.refresh_status()
        self.install_shortcuts()
        sync_desktop_icon()
        if self.settings.value("check_updates", True, type=bool):
            QTimer.singleShot(1200, self.check_updates)

    # ----------------------------------------------------- frameless chrome
    def toggle_max(self):
        self.showNormal() if self.isMaximized() else self.showMaximized()

    def changeEvent(self, e):
        if e.type() == e.Type.WindowStateChange:
            m = self.isMaximized()
            self.layout().setContentsMargins(*((0,) * 4 if m else (self.RESIZE_MARGIN,) * 4))
            QApplication.instance().setStyleSheet(make_qss(0 if m else 14))
            self.titlebar.btn_max.maximized = m
            self.titlebar.btn_max.update()
        super().changeEvent(e)

    def edges_at(self, pos):
        if self.isMaximized():
            return Qt.Edge(0)
        m, r = self.RESIZE_MARGIN + 2, self.rect()
        edges = Qt.Edge(0)
        if pos.x() <= m:
            edges |= Qt.Edge.LeftEdge
        if pos.x() >= r.width() - m:
            edges |= Qt.Edge.RightEdge
        if pos.y() <= m:
            edges |= Qt.Edge.TopEdge
        if pos.y() >= r.height() - m:
            edges |= Qt.Edge.BottomEdge
        return edges

    def mouseMoveEvent(self, e):
        edges = self.edges_at(e.position().toPoint())
        L, R, T, B = Qt.Edge.LeftEdge, Qt.Edge.RightEdge, Qt.Edge.TopEdge, Qt.Edge.BottomEdge
        if edges in (L | T, R | B):
            self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        elif edges in (R | T, L | B):
            self.setCursor(Qt.CursorShape.SizeBDiagCursor)
        elif edges in (L, R):
            self.setCursor(Qt.CursorShape.SizeHorCursor)
        elif edges in (T, B):
            self.setCursor(Qt.CursorShape.SizeVerCursor)
        else:
            self.unsetCursor()

    def mousePressEvent(self, e):
        edges = self.edges_at(e.position().toPoint())
        if e.button() == Qt.MouseButton.LeftButton and edges != Qt.Edge(0):
            self.windowHandle().startSystemResize(edges)

    def install_shortcuts(self):
        for i in range(5):
            QShortcut(QKeySequence(f"Ctrl+{i + 1}"), self, activated=lambda i=i: self.go(i))
        QShortcut(QKeySequence("Ctrl+Return"), self, activated=self.on_play)
        QShortcut(QKeySequence.StandardKey.Save, self, activated=self.save_flags)
        QShortcut(QKeySequence.StandardKey.Find, self, activated=self.focus_search)
        QShortcut(QKeySequence("F5"), self, activated=self.refresh_all)

    def refresh_all(self):
        proxy_port(max_age=0)
        self.refresh_status()
        if self.stack.currentIndex() == 3:
            self.load_logs()
        self.toast("Refreshed.")

    # ------------------------------------------------------------ sidebar
    def build_sidebar(self):
        side = QFrame(objectName="sidebar")
        side.setFixedWidth(210)
        self.sidebar = side
        lay = QVBoxLayout(side)
        lay.setContentsMargins(14, 18, 14, 18)
        lay.setSpacing(4)
        self.nav_pill = QFrame(side, objectName="navPill")
        self.nav_pill.lower()
        self.nav = QButtonGroup(self, exclusive=True)
        names = ["▶   Play", "⚑   FastFlags", "◆   Tools", "☰   Logs", "⚙   Settings"]
        for i, name in enumerate(names):
            b = button(name, "nav", tip=f"Ctrl+{i + 1}")
            b.setCheckable(True)
            self.nav.addButton(b, i)
            lay.addWidget(b)
        self.tools_badge = QLabel(self.nav.button(2), objectName="navBadge")
        self.tools_badge.setFixedSize(8, 8)
        self.tools_badge.move(150, 18)
        self.tools_badge.hide()
        self.nav.idClicked.connect(self.go)
        lay.addStretch(1)
        row = QHBoxLayout()
        row.setSpacing(6)
        self.side_dot = PulseDot()
        row.addWidget(self.side_dot)
        self.side_status = QLabel(objectName="brandSub")
        row.addWidget(self.side_status, 1)
        lay.addLayout(row)
        hint = QLabel("Ctrl+Enter to play · drop a .json to import", objectName="brandSub")
        hint.setWordWrap(True)
        hint.setStyleSheet("font-size: 10px; color: #6f6ba3;")
        lay.addWidget(hint)
        return side

    def showEvent(self, e):
        super().showEvent(e)
        start = int(self.settings.value("page", 0))
        QTimer.singleShot(0, lambda: self.go(start, animate=False))
        QTimer.singleShot(0, self.fade_in)

    def fade_in(self):
        fx = QGraphicsOpacityEffect(self.shell)
        self.shell.setGraphicsEffect(fx)
        a = QPropertyAnimation(fx, b"opacity", self, duration=280, startValue=0.0, endValue=1.0, easingCurve=EASE)
        a.finished.connect(lambda: self.shell.setGraphicsEffect(None))
        a.start()
        self._fade = a

    def go(self, i, animate=True):
        self.nav.button(i).setChecked(True)
        target = self.nav.button(i).geometry()
        if animate and self.nav_pill.isVisible():
            a = QPropertyAnimation(self.nav_pill, b"geometry", self, duration=320,
                                   easingCurve=QEasingCurve.Type.OutBack)
            a.setStartValue(self.nav_pill.geometry())
            a.setEndValue(target)
            a.start()
            self._pill_anim = a
        else:
            self.nav_pill.setGeometry(target)
        self.nav_pill.show()
        if i == self.stack.currentIndex() and animate:
            return
        self.stack.setCurrentIndex(i)
        self.settings.setValue("page", i)
        if i == 3:
            self.load_logs()
        if animate:
            self.animate_page(self.stack.currentWidget())

    def animate_page(self, w):
        fx = QGraphicsOpacityEffect(w)
        w.setGraphicsEffect(fx)
        end = w.pos()
        fade = QPropertyAnimation(fx, b"opacity", duration=260, startValue=0.0, endValue=1.0, easingCurve=EASE)
        slide = QPropertyAnimation(w, b"pos", duration=320, startValue=end + QPoint(0, 22), endValue=end,
                                   easingCurve=EASE)
        g = QParallelAnimationGroup(self)
        g.addAnimation(fade)
        g.addAnimation(slide)
        g.finished.connect(lambda: w.setGraphicsEffect(None))
        g.start()
        self._page_anim = g

    # --------------------------------------------------------------- play
    def build_play(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(16)

        self.banner = QFrame(objectName="banner")
        bl = QHBoxLayout(self.banner)
        bl.setContentsMargins(16, 10, 10, 10)
        bt = QLabel("A Sober update is available — outdated Sober refuses to launch.")
        bt.setStyleSheet(f"color: {WARN}; font-weight: 600;")
        bl.addWidget(bt, 1)
        bl.addWidget(button("Update now", "primary", lambda: (self.go(2), self.update_sober())))
        self.banner.hide()
        lay.addWidget(self.banner)

        hero = Hero()
        hl = QVBoxLayout(hero)
        hl.setContentsMargins(36, 30, 36, 30)
        hl.setSpacing(8)
        hl.addWidget(QLabel("Roblox", objectName="heroTitle"))
        self.hero_sub = QLabel("", objectName="heroSub")
        hl.addWidget(self.hero_sub)
        hl.addSpacing(16)

        row = QHBoxLayout()
        row.setSpacing(14)
        self.play_btn = GlowButton("PLAY")
        self.play_btn.clicked.connect(self.on_play)
        self.play_btn.setToolTip("Ctrl+Enter")
        row.addWidget(self.play_btn)
        self.stop_btn = button("■  Force close", "stop", self.on_stop, "Kill a running or frozen Roblox")
        self.stop_btn.hide()
        row.addWidget(self.stop_btn)
        row.addSpacing(6)
        self.seg_group = QButtonGroup(self, exclusive=True)
        self.seg_buttons = {}
        for key, label, tip in [("auto", "Auto", "Use Fleasion's proxy if it's running, otherwise launch without it"),
                                ("fleasion", "With Fleasion", "Start Fleasion, wait for its proxy, then launch"),
                                ("noproxy", "No proxy", "Plain Roblox, proxy stripped")]:
            b = button(label, "seg", tip=tip)
            b.setCheckable(True)
            b.setChecked(key == self.mode)
            b.clicked.connect(lambda _, k=key: self.set_mode(k))
            self.seg_group.addButton(b)
            self.seg_buttons[key] = b
            row.addWidget(b)
        row.addStretch(1)
        hl.addLayout(row)
        hl.addSpacing(10)

        fps = QHBoxLayout()
        fps.setSpacing(6)
        fps.addWidget(QLabel("FPS CAP", objectName="fpsLabel"))
        fps.addSpacing(4)
        self.fps_group = QButtonGroup(self, exclusive=True)
        self.fps_buttons = {}
        for label, val in FPS_CHOICES:
            b = button(label, "chipBtn", tip="Sets DFIntTaskSchedulerTargetFps" +
                       (" — uncapped runs hot on laptops" if val == 9999 else ""))
            b.setCheckable(True)
            b.clicked.connect(lambda _, v=val: self.set_fps(v))
            self.fps_group.addButton(b)
            self.fps_buttons[val] = b
            fps.addWidget(b)
        fps.addStretch(1)
        hl.addLayout(fps)
        lay.addWidget(hero)

        grid = QHBoxLayout()
        grid.setSpacing(12)
        c1, self.v_sober, self.d_sober = card("Sober")
        c2, self.v_roblox, self.d_roblox = card("Roblox")
        c3, self.v_proxy, self.d_proxy = card("Fleasion proxy")
        c4, self.v_flags, self.d_flags = card("FastFlags")
        for c in (c1, c2, c3, c4):
            grid.addWidget(c)
        lay.addLayout(grid)

        quick = QHBoxLayout()
        quick.setSpacing(8)
        quick.addWidget(button("Open FastFlags", "btn", lambda: self.go(1)))
        quick.addWidget(button("Clear cache", "btn", self.clear_cache))
        quick.addWidget(button("Latest errors", "btn", lambda: self.go(3)))
        if FLEASION.exists():
            quick.addWidget(button("Open Fleasion", "btn", start_fleasion, "Start Fleasion without launching Roblox"))
        quick.addStretch(1)
        lay.addLayout(quick)
        lay.addStretch(1)
        return page

    def set_mode(self, mode):
        self.mode = mode
        self.settings.setValue("mode", mode)
        self.refresh_status()

    def set_fps(self, val):
        if self.dirty:
            self.set_table_flag("DFIntTaskSchedulerTargetFps", str(val))
            return self.toast(f"FPS cap {val} added to your unsaved FastFlags.")
        flags = get_flags()
        flags["DFIntTaskSchedulerTargetFps"] = val
        try:
            write_flags(flags)
        except (RuntimeError, OSError, ValueError) as e:
            self.sync_fps()
            return self.toast(str(e), BAD)
        self.load_flags()
        self.toast("FPS uncapped — watch your temps." if val == 9999 else f"FPS cap set to {val}.",
                   WARN if val == 9999 else OK)

    def sync_fps(self):
        v = self.table_flags().get("DFIntTaskSchedulerTargetFps")
        try:
            v = int(v)
        except (TypeError, ValueError):
            v = None
        self.fps_group.setExclusive(False)
        for val, b in self.fps_buttons.items():
            b.setChecked(val == v)
        self.fps_group.setExclusive(True)

    def on_play(self):
        if sober_running():
            return self.toast("Roblox is already running.")
        if self.dirty:
            r = QMessageBox.question(self, "Unsaved FastFlags", "Save your FastFlag changes before launching?",
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No |
                                     QMessageBox.StandardButton.Cancel)
            if r == QMessageBox.StandardButton.Cancel or (r == QMessageBox.StandardButton.Yes and not self.save_flags()):
                return
        if self.settings.value("clear_cache_on_launch", False, type=bool):
            try:
                clear_cache()
            except RuntimeError:
                pass
        if self.mode == "fleasion":
            port = proxy_port(max_age=0)
            if not FLEASION.exists():
                return self.toast("Fleasion isn't installed.", BAD)
            if not port:
                return self.toast("Fleasion's proxy isn't set up for Sober. Enable it in Fleasion first.", BAD)
            if not port_up(port):
                start_fleasion()
                self.waiting_port, self.wait_left = port, 60
                self.play_btn.setEnabled(False)
                self.play_btn.setText("WAITING…")
                self.wait_timer.start()
                return self.toast("Starting Fleasion — launching as soon as its proxy is up…")
        self.after_launch(launch(self.mode))

    def after_launch(self, msg):
        self.toast(msg, OK)
        self.play_btn.setEnabled(False)
        self.play_btn.setText("STARTING…")
        self.refresh_status()
        if self.settings.value("close_after_launch", False, type=bool):
            QTimer.singleShot(1800, QApplication.instance().quit)

    def poll_fleasion(self):
        self.wait_left -= 1
        if port_up(self.waiting_port) or self.wait_left <= 0:
            self.wait_timer.stop()
            self.play_btn.setText("PLAY")
            self.play_btn.setEnabled(True)
            if port_up(self.waiting_port):
                self.after_launch(launch("fleasion"))
            else:
                self.toast(f"Fleasion's proxy didn't come up on port {self.waiting_port}.", BAD)
            self.waiting_port = None
        else:
            self.play_btn.setText(f"WAITING {self.wait_left}s")

    def on_stop(self):
        if QMessageBox.question(self, "Force close", "Kill Roblox now? Unsaved in-game progress may be lost.") \
                == QMessageBox.StandardButton.Yes:
            kill_sober()
            self.toast("Roblox closed.", WARN)
            QTimer.singleShot(600, self.refresh_status)

    def refresh_status(self):
        running = sober_running()
        if running and not self.was_running:
            self.session_start = time.monotonic()
        if not running:
            self.session_start = None
        self.was_running = running
        port = proxy_port()
        up = port is not None and port_up(port)
        n = len(self.table_flags()) if self.dirty else len(get_flags())

        self.v_sober.setText(f"v{self.version}" + ("  · update!" if self.update_available else ""))
        self.d_sober.set_state(self.update_available, WARN if self.update_available else OK)
        if running:
            secs = int(time.monotonic() - self.session_start)
            self.set_value(self.v_roblox, f"Running · {secs // 3600:d}:{secs // 60 % 60:02d}:{secs % 60:02d}"
                           if secs >= 3600 else f"Running · {secs // 60:d}:{secs % 60:02d}", OK)
        else:
            self.set_value(self.v_roblox, "Closed", MUTED)
        self.d_roblox.set_state(running, OK if running else MUTED)
        if port is None:
            self.set_value(self.v_proxy, "Not set up", MUTED)
            self.d_proxy.set_state(False, MUTED)
        else:
            self.set_value(self.v_proxy, f"Running · :{port}" if up else f"Off · :{port}", OK if up else WARN)
            self.d_proxy.set_state(up, OK if up else WARN)
        self.v_flags.setText(f"{n} set" + (" · unsaved" if self.dirty else ""))
        self.d_flags.set_state(self.dirty, WARN if self.dirty else ACCENT)

        if not self.wait_timer.isActive():
            self.play_btn.setEnabled(not running)
            self.play_btn.setText("RUNNING" if running else "PLAY")
        self.stop_btn.setVisible(running)
        for b in self.seg_buttons.values():
            b.setVisible(not running)
        hints = {
            "auto": "Fleasion is up — launching through it." if up else
                    ("Fleasion is off — Roblox will launch without its proxy." if port else "Launching normally."),
            "fleasion": "Fleasion is up and ready." if up else "Fleasion will be started first, then Roblox.",
            "noproxy": "Proxy stripped — Fleasion swaps won't apply.",
        }
        self.hero_sub.setText("Have fun! Close Roblox to change launch settings." if running else hints[self.mode])
        self.side_dot.set_state(running, OK if running else MUTED)
        self.side_status.setText("Roblox running" if running else "Roblox closed")

    @staticmethod
    def set_value(label, text, color):
        label.setText(text)
        label.setStyleSheet(f"color: {color};")

    # -------------------------------------------------------------- flags
    def build_flags(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(14)
        lay.addWidget(page_header("FastFlags", "Double-click to edit · right-click for more · Del removes · Ctrl+S saves"))

        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.search = QLineEdit(placeholderText="Search flags…  (Ctrl+F)")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.filter_flags)
        bar.addWidget(self.search, 1)
        bar.addWidget(button("+ Add", "btn", self.add_flag))
        bar.addWidget(button("Performance preset", "btn", self.apply_preset, "Add 22 laptop-safe performance flags"))
        bar.addWidget(button("Import", "btn", lambda: self.import_flags(), "Or drag a .json onto the window"))
        bar.addWidget(button("Export", "btn", self.export_flags, "Save as Bloxstrap-style JSON to share"))
        bar.addWidget(button("Clear all", "danger", self.clear_flags))
        self.save_btn = button("Save", "primary", self.save_flags)
        bar.addWidget(self.save_btn)
        lay.addLayout(bar)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["FLAG", "VALUE"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(1, 200)
        self.table.verticalHeader().hide()
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.table_menu)
        self.table.itemChanged.connect(self.on_item_changed)
        QShortcut(QKeySequence.StandardKey.Delete, self.table, activated=self.delete_selected)
        lay.addWidget(self.table, 1)
        self.flag_count = QLabel("", objectName="sub")
        lay.addWidget(self.flag_count)
        return page

    def focus_search(self):
        self.go(1)
        self.search.setFocus()
        self.search.selectAll()

    def load_flags(self, flags=None):
        flags = get_flags() if flags is None else flags
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        for k, v in sorted(flags.items(), key=lambda kv: kv[0].lower()):
            self.append_row(k, v)
        self.table.blockSignals(False)
        self.filter_flags()
        self.sync_fps()

    def append_row(self, k, v):
        r = self.table.rowCount()
        self.table.insertRow(r)
        if isinstance(v, bool):
            v = "True" if v else "False"
        name = QTableWidgetItem(k)
        name.setFont(QFont("monospace"))
        self.table.setItem(r, 0, name)
        self.table.setItem(r, 1, QTableWidgetItem(str(v)))
        self.validate_row(r)
        return r

    def validate_row(self, r):
        """Red value = wrong type for the flag's prefix; red name = not a real flag name."""
        k, v = self.table.item(r, 0), self.table.item(r, 1)
        if not k or not v:
            return
        name, val = k.text().strip(), v.text().strip()
        bad_name = not VALID_NAME.match(name)
        if "FFlag" in name[:6]:
            bad_val = val.lower() not in ("true", "false")
        elif "FInt" in name[:5]:
            bad_val = not re.fullmatch(r"-?\d+", val)
        else:
            bad_val = False
        k.setForeground(QColor(BAD if bad_name else TEXT))
        k.setToolTip("Not a valid flag name (needs FFlag / FInt / FString / FLog prefix)" if bad_name else "")
        v.setForeground(QColor(BAD if bad_val else TEXT))
        v.setToolTip("Wrong type: FFlags take True/False, FInts take whole numbers" if bad_val else "")

    def on_item_changed(self, item):
        self.table.blockSignals(True)
        self.validate_row(item.row())
        self.table.blockSignals(False)
        self.mark_dirty()

    def table_flags(self):
        out = {}
        for r in range(self.table.rowCount()):
            k, v = self.table.item(r, 0), self.table.item(r, 1)
            if k and k.text().strip():
                out[k.text().strip()] = v.text().strip() if v else ""
        return out

    def set_table_flag(self, name, value):
        for r in range(self.table.rowCount()):
            if self.table.item(r, 0).text().strip() == name:
                self.table.item(r, 1).setText(value)
                return
        self.append_row(name, value)
        self.mark_dirty()

    def mark_dirty(self, dirty=True):
        self.dirty = dirty
        self.save_btn.setText("Save •" if dirty else "Save")
        self.refresh_status()
        self.update_count()

    def update_count(self):
        n = self.table.rowCount()
        shown = sum(not self.table.isRowHidden(r) for r in range(n))
        self.flag_count.setText(f"{n} flags" + (f" · {shown} shown" if shown != n else "") +
                                (" · unsaved changes" if self.dirty else ""))

    def filter_flags(self):
        q = self.search.text().lower()
        for r in range(self.table.rowCount()):
            item = self.table.item(r, 0)
            self.table.setRowHidden(r, bool(q) and q not in (item.text().lower() if item else ""))
        self.update_count()

    def table_menu(self, pos):
        item = self.table.itemAt(pos)
        if not item:
            return
        r = item.row()
        name, val = self.table.item(r, 0).text(), self.table.item(r, 1).text()
        m = QMenu(self)
        if val.lower() in ("true", "false"):
            m.addAction(f"Toggle → {'False' if val.lower() == 'true' else 'True'}",
                        lambda: self.table.item(r, 1).setText("False" if val.lower() == "true" else "True"))
        m.addAction("Copy name", lambda: QApplication.clipboard().setText(name))
        m.addAction("Copy as JSON", lambda: QApplication.clipboard().setText(f'"{name}": "{val}"'))
        m.addAction("Duplicate", lambda: (self.append_row(name + "_copy", val), self.mark_dirty()))
        m.addSeparator()
        m.addAction("Delete", self.delete_selected)
        m.exec(self.table.viewport().mapToGlobal(pos))

    def add_flag(self):
        self.search.clear()
        r = self.append_row("FFlagNewFlag", "True")
        self.table.scrollToItem(self.table.item(r, 0))
        self.table.selectRow(r)
        self.table.editItem(self.table.item(r, 0))
        self.mark_dirty()

    def delete_selected(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        for r in rows:
            self.table.removeRow(r)
        if rows:
            self.mark_dirty()
            self.toast(f"Removed {len(rows)} flag{'s' * (len(rows) != 1)}.")

    def apply_preset(self):
        flags = self.table_flags()
        flags.update(PERF_FLAGS)
        self.load_flags(flags)
        self.mark_dirty()
        self.toast(f"Performance preset added ({len(PERF_FLAGS)} flags). Hit Save to apply.")

    def import_flags(self, path=None):
        if not path:
            path, _ = QFileDialog.getOpenFileName(self, "Import FastFlags (Bloxstrap / ClientAppSettings.json)",
                                                  str(HOME / "Downloads"), "JSON (*.json)")
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
            assert isinstance(data, dict)
        except (OSError, ValueError, AssertionError):
            return self.toast("That file isn't a JSON object of flags.", BAD)
        cleaned = clean_import(data)
        removed = len(data) - len(cleaned)
        if len(data) > 300 and QMessageBox.warning(
                self, "That's a full flag dump",
                f"This file has {len(data):,} flags — a full dump, not a tweak list. Forcing thousands of flags "
                "is a common cause of crashes, and Roblox ignores most of them anyway.\n\nImport anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        r = QMessageBox.question(
            self, "Import FastFlags",
            f"{Path(path).name}: {len(cleaned):,} flags ({removed:,} crash/debug/invalid stripped).\n\n"
            "Yes = merge with your current flags · No = replace them",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel)
        if r == QMessageBox.StandardButton.Cancel:
            return
        flags = self.table_flags() if r == QMessageBox.StandardButton.Yes else {}
        flags.update(cleaned)
        self.go(1)
        self.load_flags(flags)
        self.mark_dirty()
        self.toast(f"Imported {len(cleaned):,} flags. Hit Save to apply.")

    def export_flags(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export FastFlags", str(HOME / "Downloads/ClientAppSettings.json"),
                                              "JSON (*.json)")
        if not path:
            return
        flags = {k: str(v) for k, v in self.table_flags().items()}
        Path(path).write_text(json.dumps(flags, indent=2) + "\n")
        self.toast(f"Exported {len(flags)} flags to {Path(path).name}.", OK)

    def clear_flags(self):
        if QMessageBox.question(self, "Clear all", "Remove every FastFlag?") == QMessageBox.StandardButton.Yes:
            self.load_flags({})
            self.mark_dirty()

    def save_flags(self):
        try:
            write_flags(self.table_flags())
        except (RuntimeError, OSError, ValueError) as e:
            self.toast(str(e), BAD)
            return False
        self.mark_dirty(False)
        self.load_flags()
        self.toast(f"Saved {len(get_flags())} FastFlags.", OK)
        return True

    # -------------------------------------------------------------- tools
    def build_tools(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        lay.addWidget(page_header("Tools", "Maintenance for Sober and Roblox"))
        self.update_btn = None
        for title, desc, label, kind, slot in [
            ("Update Sober", "Pull the latest Sober from Flathub. Outdated Sober refuses to start.",
             "Update", "primary", self.update_sober),
            ("Clear cache", "Delete downloaded Roblox assets. Login and settings are kept.",
             "Clear", "danger", self.clear_cache),
            ("Restore config backup", "Undo the last FastFlag save (config.json.bak).",
             "Restore", "btn", self.restore_backup),
            ("Sober config", "Open Sober's config.json in your text editor.",
             "Open", "btn", lambda: self.open_path(CONFIG)),
            ("Logs folder", "Open the folder with Sober's launch logs.",
             "Open", "btn", lambda: self.open_path(LOGDIR)),
        ]:
            f = QFrame(objectName="card")
            row = QHBoxLayout(f)
            row.setContentsMargins(18, 12, 18, 12)
            col = QVBoxLayout()
            col.setSpacing(2)
            t = QLabel(title)
            t.setStyleSheet("font-size: 15px; font-weight: 700;")
            col.addWidget(t)
            col.addWidget(QLabel(desc, objectName="sub"))
            row.addLayout(col, 1)
            b = button(label, kind, slot)
            b.setMinimumWidth(100)
            row.addWidget(b)
            if self.update_btn is None:
                self.update_btn = b
            lay.addWidget(f)
        self.tool_out = QPlainTextEdit(readOnly=True)
        self.tool_out.setPlaceholderText("Output appears here.")
        lay.addWidget(self.tool_out, 1)
        return page

    def check_updates(self):
        self.upd_proc = QProcess(self)
        self.upd_proc.finished.connect(self.check_updates_done)
        self.upd_proc.start("flatpak", ["remote-ls", "--updates", "--columns=application"])

    def check_updates_done(self, code, _status):
        out = bytes(self.upd_proc.readAllStandardOutput()).decode(errors="replace")
        self.update_available = code == 0 and APP in out
        self.banner.setVisible(self.update_available)
        self.tools_badge.setVisible(self.update_available)
        self.refresh_status()

    def update_sober(self):
        self.update_btn.setEnabled(False)
        self.update_btn.setText("Updating…")
        self.tool_out.setPlainText(f"$ flatpak update -y {APP}\n")
        self.proc = QProcess(self)
        self.proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.proc.readyReadStandardOutput.connect(
            lambda: self.tool_out.appendPlainText(bytes(self.proc.readAllStandardOutput()).decode(errors="replace").rstrip()))
        self.proc.finished.connect(self.update_done)
        self.proc.start("flatpak", ["update", "-y", "--noninteractive", APP])

    def update_done(self, code, _status):
        self.version = sober_version()
        self.titlebar.chip.setText(f"Sober {self.version}")
        self.update_btn.setEnabled(True)
        self.update_btn.setText("Update")
        self.tool_out.appendPlainText(f"\nDone — Sober {self.version}" if code == 0 else f"\nFailed (exit {code})")
        if code == 0:
            self.update_available = False
            self.banner.hide()
            self.tools_badge.hide()
        self.toast(f"Sober is up to date (v{self.version})." if code == 0 else "Update failed — see output.",
                   OK if code == 0 else BAD)
        self.refresh_status()

    def clear_cache(self):
        if QMessageBox.question(self, "Clear cache", "Delete Roblox's downloaded assets?") != QMessageBox.StandardButton.Yes:
            return
        try:
            clear_cache()
        except RuntimeError as e:
            return self.toast(str(e), BAD)
        self.toast("Cache cleared.", OK)

    def restore_backup(self):
        bak = CONFIG.with_suffix(".json.bak")
        if not bak.exists():
            return self.toast("No backup yet — one is made every time you save flags.", WARN)
        if sober_running():
            return self.toast("Close Roblox first.", BAD)
        if QMessageBox.question(self, "Restore backup", "Replace Sober's config with the backup from your last save?") \
                != QMessageBox.StandardButton.Yes:
            return
        shutil.copy2(bak, CONFIG)
        self.mark_dirty(False)
        self.load_flags()
        self.toast("Config restored from backup.", OK)

    def open_path(self, p):
        subprocess.Popen(["xdg-open", str(p)], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # --------------------------------------------------------------- logs
    def build_logs(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(14)
        top = QHBoxLayout()
        top.addWidget(page_header("Logs", "Errors from the latest Sober launch"), 1)
        top.addWidget(button("Copy", "btn", self.copy_logs, "Copy to clipboard (for support)"))
        top.addWidget(button("Full log", "btn", self.open_full_log))
        top.addWidget(button("Refresh", "btn", self.load_logs, "F5"))
        lay.addLayout(top)
        self.log_view = QPlainTextEdit(readOnly=True)
        self.log_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        lay.addWidget(self.log_view, 1)
        return page

    def load_logs(self):
        log, hits = latest_errors()
        self.current_log = log
        if log is None:
            return self.log_view.setPlainText("No Sober logs yet.")
        body = "\n".join(hits) if hits else "No errors in the latest log."
        self.log_view.setPlainText(f"{log.name}\n\n{body}")
        self.log_view.verticalScrollBar().setValue(self.log_view.verticalScrollBar().maximum())

    def copy_logs(self):
        QApplication.clipboard().setText(self.log_view.toPlainText())
        self.toast("Logs copied.", OK)

    def open_full_log(self):
        if getattr(self, "current_log", None):
            self.open_path(self.current_log)

    # ----------------------------------------------------------- settings
    def build_settings(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        lay.addWidget(page_header("Settings", "Saved automatically"))
        lay.addSpacing(4)
        lay.addWidget(QLabel("LAUNCHING", objectName="section"))
        for key, title, desc, default in [
            ("close_after_launch", "Close after launching", "Quit the bootstrapper once Roblox starts.", False),
            ("clear_cache_on_launch", "Clear cache before every launch",
             "Fixes stale assets / Fleasion swaps not showing. Slower loads.", False),
        ]:
            lay.addWidget(self.setting_row(key, title, desc, default))
        lay.addSpacing(6)
        lay.addWidget(QLabel("UPDATES", objectName="section"))
        lay.addWidget(self.setting_row("check_updates", "Check for Sober updates on startup",
                                       "Shows a banner when Flathub has a newer Sober.", True))
        lay.addSpacing(6)
        lay.addWidget(QLabel("APPEARANCE", objectName="section"))
        f = QFrame(objectName="card")
        row = QHBoxLayout(f)
        row.setContentsMargins(18, 12, 18, 12)
        col = QVBoxLayout()
        col.setSpacing(2)
        t = QLabel("Logo")
        t.setStyleSheet("font-size: 15px; font-weight: 700;")
        col.addWidget(t)
        self.logo_hint = QLabel(f"Drop a square PNG at {LOGO}", objectName="sub")
        self.logo_hint.setWordWrap(True)
        col.addWidget(self.logo_hint)
        row.addLayout(col, 1)
        row.addWidget(button("Choose…", "btn", self.choose_logo))
        lay.addWidget(f)
        lay.addStretch(1)
        about = QLabel("Soberstrap · Ctrl+1–5 pages · Ctrl+Enter play · Ctrl+F search · Ctrl+S save · F5 refresh",
                       objectName="brandSub")
        lay.addWidget(about)
        return page

    def setting_row(self, key, title, desc, default):
        f = QFrame(objectName="card")
        row = QHBoxLayout(f)
        row.setContentsMargins(18, 12, 18, 12)
        col = QVBoxLayout()
        col.setSpacing(2)
        t = QLabel(title)
        t.setStyleSheet("font-size: 15px; font-weight: 700;")
        col.addWidget(t)
        col.addWidget(QLabel(desc, objectName="sub"))
        row.addLayout(col, 1)
        sw = Switch(self.settings.value(key, default, type=bool))
        sw.toggled.connect(lambda on: self.settings.setValue(key, on))
        row.addWidget(sw)
        return f

    def choose_logo(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose logo", str(HOME / "Downloads"),
                                              "Images (*.png *.jpg *.jpeg *.webp)")
        if not path:
            return
        pm = QPixmap(path)
        if pm.isNull():
            return self.toast("Couldn't read that image.", BAD)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        pm.save(str(LOGO), "PNG")
        self.titlebar.set_logo()
        self.setWindowIcon(QIcon(logo_pixmap(256)))
        sync_desktop_icon()
        self.toast("Logo updated — titlebar, window and app menu.", OK)

    # -------------------------------------------------------- drag & drop
    def dragEnterEvent(self, e):
        if any(u.toLocalFile().lower().endswith(".json") for u in e.mimeData().urls()):
            e.acceptProposedAction()

    def dropEvent(self, e):
        for u in e.mimeData().urls():
            if u.toLocalFile().lower().endswith(".json"):
                return self.import_flags(u.toLocalFile())

    # -------------------------------------------------------------- toast
    def toast(self, text, color=ACCENT):
        lbl = self.toast_label
        lbl.setText(text)
        lbl.setStyleSheet(f"border-color: {color};")
        lbl.adjustSize()
        end = QPoint(self.shell.width() - lbl.width() - 22, self.shell.height() - lbl.height() - 22)
        lbl.move(end + QPoint(0, 24))
        lbl.raise_()
        lbl.show()
        fade = QPropertyAnimation(self.toast_fx, b"opacity", duration=220, startValue=0.0, endValue=1.0)
        slide = QPropertyAnimation(lbl, b"pos", duration=320, startValue=end + QPoint(0, 24), endValue=end,
                                   easingCurve=QEasingCurve.Type.OutBack)
        g = QParallelAnimationGroup(self)
        g.addAnimation(fade)
        g.addAnimation(slide)
        g.start()
        self._toast_anim = g
        self.toast_timer.start(3800)

    def hide_toast(self):
        a = QPropertyAnimation(self.toast_fx, b"opacity", self, duration=260, startValue=1.0, endValue=0.0)
        a.finished.connect(self.toast_label.hide)
        a.start()
        self._toast_anim = a

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self.toast_label.isVisible():
            self.toast_label.move(self.shell.width() - self.toast_label.width() - 22,
                                  self.shell.height() - self.toast_label.height() - 22)
        if self.nav_pill.isVisible():
            self.nav_pill.setGeometry(self.nav.checkedButton().geometry())

    def closeEvent(self, e):
        if self.dirty and QMessageBox.question(
                self, "Unsaved FastFlags", "Discard unsaved FastFlag changes?") != QMessageBox.StandardButton.Yes:
            return e.ignore()
        if not self.isMaximized():
            self.settings.setValue("size", self.size())
        e.accept()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Sober Bootstrapper")
    app.setDesktopFileName("sober-strap")
    app.setStyle("Fusion")
    app.setStyleSheet(make_qss(14))
    w = Window()
    w.show()
    if len(sys.argv) > 1 and sys.argv[1] == "--fleasion":
        w.seg_buttons["fleasion"].click()
        w.on_play()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
