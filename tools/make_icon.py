"""Draw the app icon (resources/icon.png). Run once: python tools/make_icon.py"""

import math
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QPointF, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QLinearGradient, QPainter, QPainterPath, QPen  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication(sys.argv)
S = 1024
img = QImage(S, S, QImage.Format.Format_ARGB32)
img.fill(Qt.GlobalColor.transparent)
p = QPainter(img)
p.setRenderHint(QPainter.RenderHint.Antialiasing)
g = QLinearGradient(0, 0, 0, S)
g.setColorAt(0, QColor("#2b3a55"))
g.setColorAt(1, QColor("#111827"))
p.setBrush(g)
p.setPen(Qt.PenStyle.NoPen)
p.drawRoundedRect(QRectF(64, 64, S - 128, S - 128), 200, 200)
# decaying impulse response
path = QPainterPath()
x0, x1, mid = 170, S - 170, S * 0.55
path.moveTo(x0, mid)
for i in range(600):
    t = i / 599
    x = x0 + (x1 - x0) * t
    y = mid - 330 * math.exp(-5.5 * t) * math.cos(2 * math.pi * 9 * t) * (1 if t > 0.01 else t / 0.01)
    path.lineTo(QPointF(x, y))
pen = QPen(QColor("#f5a524"), 34, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
p.setPen(pen)
p.setBrush(Qt.BrushStyle.NoBrush)
p.drawPath(path)
p.end()
out = Path(__file__).resolve().parents[1] / "src" / "irene_capture" / "resources" / "icon.png"
img.save(str(out))
print(out)
