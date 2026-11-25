"""Turn the driver's white & black backdrop grabs into transparent PNGs.

usage: matte.py <raw dir> <out dir>
       matte.py --outline <raw dir> <flat raw dir> <out dir>

The first form combines each NAME@white.png / NAME@black.png pair into a transparent NAME.png, trimmed
to its shadow. The second keeps only the window's outline: the shadowed grab within a pixel of the
flat (shadowless) grab's window, since macos draws a window's border as part of its shadow.
"""
import sys, os, glob
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

def pairs(raw):
    for white in sorted(glob.glob(os.path.join(raw, '*@white.png'))):
        name = os.path.basename(white).rsplit('@', 1)[0]
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
            rgb, a = matte(white, black)
        except ValueError as e:
            print(name, e, flush=True)
            continue
        print('matted', name, save(rgb, a, os.path.join(out, name + '.png')), flush=True)
