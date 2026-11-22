# MarkTux application logo

This directory is for **MarkTux's own** application icon, not VNote's logo.

1. Put the master artwork here as `marktux.svg`.
2. Generate every platform size:

   ```bash
   python scripts/build_app_icons.py
   ```

The script reads `marktux.svg` and writes:

```
16x16/marktux.png … 256x256/marktux.png
marktux.png
icon.iconset/icon_*.png
marktux.ico          (if Pillow is installed)
marktux.icns         (if Pillow supports ICNS)
```

The source SVG is the only artwork that should be edited by hand. The PNG/ICO/
ICNS files are generated outputs.
