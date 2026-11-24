Masar logo source.

logo.html       : the design (SVG mark + Tajawal wordmark).
export_logo.py  : renders it with headless Edge into transparent PNGs + favicon.ico

  venv\Scripts\python.exe scripts\brand\export_logo.py static\img

Outputs: logo-on-dark.png (dark UI), logo.png (white background / print),
         logo-mark.png (icon only), app-icon.png + favicon.ico (browser tab).
