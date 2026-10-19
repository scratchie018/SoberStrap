#!/usr/bin/env bash
# Build a single standalone binary: dist/soberstrap
set -euo pipefail
cd "$(dirname "$0")"

[[ -d .venv ]] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt pyinstaller

# Only QtCore/QtGui/QtWidgets are used; drop the rest of Qt to keep the binary small.
EXCLUDES=()
for m in QtNetwork QtQml QtQuick QtQuick3D QtSql QtSvg QtSvgWidgets QtMultimedia QtMultimediaWidgets \
         QtWebEngineCore QtWebEngineWidgets QtWebChannel QtWebSockets QtPdf QtPdfWidgets QtOpenGL \
         QtOpenGLWidgets QtDesigner QtHelp QtTest QtBluetooth QtNfc QtPositioning QtSensors \
         QtSerialPort QtSpatialAudio QtTextToSpeech QtRemoteObjects QtDBus QtPrintSupport QtXml; do
    EXCLUDES+=(--exclude-module "PyQt6.$m")
done

.venv/bin/pyinstaller --noconfirm --clean --onefile --windowed \
    --name soberstrap \
    --add-data "assets:assets" \
    "${EXCLUDES[@]}" \
    soberstrap.py

echo
echo "built: $(pwd)/dist/soberstrap ($(du -h dist/soberstrap | cut -f1))"
