"""Turn the driver's white & black backdrop grabs into transparent PNGs.

usage: matte.py <raw dir> <out dir>
       matte.py --outline <raw dir> <flat raw dir> <out dir>

The first form combines each NAME@white.png / NAME@black.png pair into a transparent NAME.png, trimmed
to its shadow. Where the driver left a NAME.composite.json, the window's grab is combined with a
separately grabbed menu bar layer (see capture_menu in shots.py). The second form keeps only the window's
outline: the shadowed grab within a pixel of the flat (shadowless) grab's window, since macos draws a
window's border as part of its shadow.
"""
import sys, os, glob, json
import numpy as np
from PIL import Image
from scipy.ndimage import grey_dilation

def matte(white, black):
    w = np.asarray(Image.open(white).convert('RGB')).astype(float)
    b = np.asarray(Image.open(black).convert('RGB')).astype(float)
    if w.shape != b.shape:
        raise ValueError('size mismatch %r %r' % (w.shape, b.shape))

    # a pixel's alpha is how little the backdrop shows through it
    a = (1 - (w - b).mean(axis=2) / 255).clip(0, 1)
    rgb = np.where(a[..., None] > 0, b / np.maximum(a[..., None], 1e-6), 0).clip(0, 255)
    return rgb, a

def save(rgb, a, path):
    img = Image.fromarray(np.dstack([rgb, a * 255]).round().astype(np.uint8), 'RGBA')
    bbox = Image.fromarray(((a * 255) > 2).astype(np.uint8) * 255).getbbox() # trim to the faintest visible shadow
    if bbox:
        img = img.crop(bbox)
    img.save(path, optimize=True)
    return img.size

def composite(raw, name):
    """Lay a menu bar layer over a window's grab, with the bar starting at the window's left edge"""
    with open(os.path.join(raw, name + '.composite.json')) as f:
        g = json.load(f)
    s = g['scale']
    margin, bar, width = round(g['margin'] * s), round(g['menubar'] * s), round(g['window'][0] * s)
    rgb, a = matte(*(os.path.join(raw, '%s@%s.png' % (name, c)) for c in ('white', 'black')))
    mrgb, ma = matte(*(os.path.join(raw, '%s@%s.png' % (g['layer'], c)) for c in ('white', 'black')))

    # the bar runs from the window's left edge to a margin past its right, and its bottom meets the window's top
    mrgb, ma = mrgb[:, :width + margin], ma[:, :width + margin]
    ox, oy = margin, margin - bar
    h, w = max(a.shape[0], oy + ma.shape[0]), max(a.shape[1], ox + ma.shape[1])
    out_rgb, out_a = np.zeros((h, w, 3)), np.zeros((h, w))
    out_rgb[:a.shape[0], :a.shape[1]], out_a[:a.shape[0], :a.shape[1]] = rgb, a

    # the menu layer goes on top (its shadow falls across the window)
    ys, xs = slice(oy, oy + ma.shape[0]), slice(ox, ox + ma.shape[1])
    under_rgb, under_a = out_rgb[ys, xs], out_a[ys, xs]
    top = ma + under_a * (1 - ma)
    out_rgb[ys, xs] = (mrgb * ma[..., None] + under_rgb * (under_a * (1 - ma))[..., None]) / np.maximum(top, 1e-6)[..., None]
    out_a[ys, xs] = top
    return out_rgb, out_a

def pairs(raw):
    for white in sorted(glob.glob(os.path.join(raw, '*@white.png'))):
        name = os.path.basename(white).rsplit('@', 1)[0]
        if '~' in name:
            continue # a layer of some other shot's composite
        black = white.replace('@white.png', '@black.png')
        if os.path.exists(black):
            yield name, white, black
        else:
            print('no black pair for', name, flush=True)

if sys.argv[1] == '--outline':
    raw, flat, out = sys.argv[2:5]
    os.makedirs(out, exist_ok=True)
    disk = np.array([[0, 1, 0], [1, 1, 1], [0, 1, 0]], bool)
    for name, white, black in pairs(flat):
        shadowed = (os.path.join(raw, name + '@white.png'), os.path.join(raw, name + '@black.png'))
        if not all(map(os.path.exists, shadowed)):
            continue
        rgb, a = matte(*shadowed)
        _, a_flat = matte(white, black)

        # grow the flat window's (soft) alpha by a pixel so the antialiased corners survive
        a = a * grey_dilation(a_flat, footprint=disk)
        print('outlined', name, save(rgb, a, os.path.join(out, name + '.png')), flush=True)
else:
    raw, out = sys.argv[1:3]
    os.makedirs(out, exist_ok=True)
    for name, white, black in pairs(raw):
        try:
            if os.path.exists(os.path.join(raw, name + '.composite.json')):
                rgb, a = composite(raw, name)
            else:
                rgb, a = matte(white, black)
        except ValueError as e:
            print(name, e, flush=True)
            continue
        print('matted', name, save(rgb, a, os.path.join(out, name + '.png')), flush=True)
