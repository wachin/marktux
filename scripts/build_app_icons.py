#!/usr/bin/env python3
"""Build MarkTux application icons from a single SVG source.

Usage:
    python scripts/build_app_icons.py
    python scripts/build_app_icons.py --svg path/to/marktux.svg --out src/marktux/core/logo

The script renders PNGs with Qt's SVG renderer. Pillow is optional and is used
only for the .ico and .icns containers when it is available.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QRectF
from PyQt6.QtGui import QGuiApplication, QImage, QPainter
from PyQt6.QtSvg import QSvgRenderer

PNG_SIZES = (16, 32, 48, 64, 128, 256, 512, 1024)
ICONSET = (
    (16, "icon_16x16.png"),
    (32, "icon_16x16@2x.png"),
    (32, "icon_32x32.png"),
    (64, "icon_32x32@2x.png"),
    (128, "icon_128x128.png"),
    (256, "icon_128x128@2x.png"),
    (256, "icon_256x256.png"),
    (512, "icon_256x256@2x.png"),
    (512, "icon_512x512.png"),
    (1024, "icon_512x512@2x.png"),
)


def render(svg: Path, size: int) -> QImage:
    renderer = QSvgRenderer(str(svg))
    if not renderer.isValid():
        raise SystemExit(f"not a valid SVG: {svg}")
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    try:
        renderer.render(painter, QRectF(0, 0, size, size))
    finally:
        painter.end()
    return image


def save_png(image: QImage, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not image.save(str(path), "PNG"):
        raise SystemExit(f"could not write {path}")


def save_with_pillow(images: dict[int, QImage], out: Path) -> None:
    try:
        from PIL import Image
    except ImportError:
        print("Pillow not installed; skipping .ico and .icns", file=sys.stderr)
        return

    def pil_image(size: int) -> Image.Image:
        image = images[size]
        ptr = image.constBits()
        ptr.setsize(image.sizeInBytes())
        return Image.frombuffer(
            "RGBA", (size, size), bytes(ptr), "raw", "BGRA", image.bytesPerLine(), 1
        )

    ico = out / "marktux.ico"
    pil_image(256).save(ico, sizes=[(s, s) for s in (16, 32, 48, 64, 128, 256)])
    try:
        pil_image(1024).save(out / "marktux.icns")
    except Exception as error:  # noqa: BLE001 - Pillow may lack ICNS support.
        print(f"could not write marktux.icns: {error}", file=sys.stderr)
        return
    print(f"wrote {ico} and {out / 'marktux.icns'}")


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--svg",
        type=Path,
        default=root / "src" / "marktux" / "core" / "logo" / "marktux.svg",
        help="source SVG (default: src/marktux/core/logo/marktux.svg)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=root / "src" / "marktux" / "core" / "logo",
        help="output directory",
    )
    args = parser.parse_args()

    if not args.svg.is_file():
        print(f"missing source SVG: {args.svg}", file=sys.stderr)
        print("Add the MarkTux logo as marktux.svg, then rerun this script.", file=sys.stderr)
        return 2

    app = QGuiApplication.instance()
    if app is None:
        app = QGuiApplication([])
    args.out.mkdir(parents=True, exist_ok=True)
    images = {size: render(args.svg, size) for size in PNG_SIZES}

    for size in (16, 32, 48, 64, 128, 256):
        save_png(images[size], args.out / f"{size}x{size}" / "marktux.png")
    save_png(images[256], args.out / "marktux.png")
    for size, name in ICONSET:
        save_png(images[size], args.out / "icon.iconset" / name)
    save_with_pillow(images, args.out)
    print(f"wrote MarkTux icons from {args.svg} to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
