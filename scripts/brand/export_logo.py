"""Render the logo variants from logo.html with headless Edge (transparent background,
2x scale) and crop each to its content. Writes PNG/ICO files into OUT."""
import os
import subprocess
import sys

from PIL import Image

D = os.path.dirname(os.path.abspath(__file__))
OUT = sys.argv[1]
EDGE = r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'
src = open(os.path.join(D, 'logo.html'), encoding='utf-8').read()

VARIANTS = {
    # name: (element id to keep, extra css)
    'logo-on-dark': ('v-dark', ''),
    'logo': ('v-light', ''),
    'logo-mark': ('v-mark', ''),
}


def wait_file(path, timeout=30):
    import time
    end = time.time() + timeout
    last = -1
    while time.time() < end:
        if os.path.exists(path):
            size = os.path.getsize(path)
            if size > 0 and size == last:
                return
            last = size
        time.sleep(0.5)
    raise RuntimeError('screenshot not produced: ' + path)


def render(name, keep_id, scale=2, size=(1200, 700)):
    html = src
    for vid in ('v-dark', 'v-light', 'v-mark'):
        if vid != keep_id:
            html = html.replace(f'id="{vid}"', f'id="{vid}" style="display:none"', 1)
    html = html.replace('<br>', '')
    page = os.path.join(D, f'_{name}.html')
    open(page, 'w', encoding='utf-8').write(html)
    shot = os.path.join(D, f'_{name}.png')
    if os.path.exists(shot):
        os.remove(shot)
    subprocess.run([EDGE, '--headless', '--disable-gpu', '--hide-scrollbars', '--user-data-dir=' + os.path.join(D, '_edge_' + name),
                    f'--force-device-scale-factor={scale}', f'--window-size={size[0]},{size[1]}',
                    '--default-background-color=00000000', '--virtual-time-budget=6000',
                    f'--screenshot={shot}', 'file:///' + page.replace('\\', '/')],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    wait_file(shot)
    img = Image.open(shot).convert('RGBA')
    bbox = img.getchannel('A').getbbox()
    img = img.crop(bbox)
    pad = 8 * scale
    out = Image.new('RGBA', (img.width + 2 * pad, img.height + 2 * pad), (0, 0, 0, 0))
    out.paste(img, (pad, pad))
    return out


for name, (vid, _) in VARIANTS.items():
    img = render(name, vid)
    if name == 'logo-mark':
        img.thumbnail((512, 512), Image.LANCZOS)
    path = os.path.join(OUT, f'{name}.png')
    img.save(path, optimize=True)
    print(name, img.size, os.path.getsize(path), 'bytes')

# App icon: white-ink mark on a dark rounded square (browser tab, home screen)
mark_html = src.replace('id="v-dark"', 'id="v-dark" style="display:none"', 1) \
               .replace('id="v-light"', 'id="v-light" style="display:none"', 1)
mark_html = mark_html.replace('<div id="v-mark" class="stage"><svg width="512" height="448"><use href="#mark"/></svg></div>',
                              '<div id="v-mark" class="stage" style="width:512px;height:512px;background:#0b1220;border-radius:112px;'
                              'display:flex;align-items:center;justify-content:center;">'
                              '<svg width="380" height="332" style="--ink:#ffffff"><use href="#mark"/></svg></div>')
open(os.path.join(D, '_icon.html'), 'w', encoding='utf-8').write(mark_html.replace('<br>', ''))
shot = os.path.join(D, '_icon.png')
if os.path.exists(shot):
    os.remove(shot)
subprocess.run([EDGE, '--headless', '--disable-gpu', '--hide-scrollbars', '--user-data-dir=' + os.path.join(D, '_edge_icon'), '--force-device-scale-factor=1',
                '--window-size=600,600', '--default-background-color=00000000', '--virtual-time-budget=4000',
                f'--screenshot={shot}', 'file:///' + os.path.join(D, '_icon.html').replace('\\', '/')],
               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
wait_file(shot)
icon = Image.open(shot).convert('RGBA')
icon = icon.crop(icon.getchannel('A').getbbox())
icon = icon.resize((512, 512), Image.LANCZOS)
icon.save(os.path.join(OUT, 'app-icon.png'), optimize=True)
icon.save(os.path.join(OUT, 'favicon.ico'), sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])
print('app-icon', icon.size, '| favicon.ico written')
