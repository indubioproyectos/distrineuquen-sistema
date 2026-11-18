"""Images of the public site (distri_pedido/static/src/sitio/img), 19.0.1.104.0.

Sources (owner's folder /srv/odoo-lab/run/sitio-online-fotos, not in git):
  catalogo/         images extracted from the Dec 2024 catalog PDF
  pexels/           Pexels originals (free licence, no attribution needed):
                    6169670 loading a van at a depot (cover, owner's pick 18/11/2026),
                    6545444 store counter, 5025669 delivery van,
                    6696896 unlabelled soda bottles (Gaseosas tile)
Run with the lab image (Pillow with WebP):
  docker run --rm --user $(id -u) --entrypoint python3 \
    -v /srv/odoo-lab/run/sitio-online-fotos:/src:ro \
    -v /srv/odoo-lab/run/sitio-online-fotos/pexels:/dl:ro \
    -v /srv/odoo-lab/addons/distri_pedido/static/src/img/iconos:/icon:ro \
    -v /srv/odoo-lab/addons/distri_pedido/static/src/sitio/img:/out \
    -v /srv/odoo-lab/scripts/sitio:/tools:ro distri-odoo:19-ocr /tools/imagenes.py
Bump SITIO_V in controllers/sitio.py after changing any image.
"""
import os
from PIL import Image, ImageOps

SRC = "/src"            # /srv/odoo-lab/run/sitio-online-fotos
DL = "/dl"              # Pexels originals
ICON = "/icon/app-512.png"
OUT = "/out"
os.makedirs(OUT, exist_ok=True)


def flat(im, color=(255, 255, 255)):
    im = ImageOps.exif_transpose(im)
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bg = Image.new("RGBA", im.size, color + (255,))
        bg.alpha_composite(im)
        return bg.convert("RGB")
    return im.convert("RGB")


def save(im, name, webp_q=74, jpg_q=80, png=False):
    im.save(os.path.join(OUT, name + ".webp"), "WEBP", quality=webp_q, method=6)
    if png:
        im.save(os.path.join(OUT, name + ".png"), "PNG", optimize=True)
    else:
        im.save(os.path.join(OUT, name + ".jpg"), "JPEG", quality=jpg_q, optimize=True,
                progressive=True)


def widths(path, base, ws, crop=None):
    im = flat(Image.open(path))
    if crop:
        im = ImageOps.fit(im, crop, Image.LANCZOS, centering=(0.4, 0.5))
    for w in ws:
        h = round(im.height * w / im.width)
        save(im.resize((w, h), Image.LANCZOS), "%s-%d" % (base, w))
    return im


def bbox_nonwhite(im, thr=235):
    g = im.convert("L").point(lambda v: 255 if v < thr else 0)
    return g.getbbox()


def corner(im):
    px = [im.getpixel(p) for p in ((2, 2), (im.width - 3, 2), (2, im.height - 3),
                                   (im.width - 3, im.height - 3))]
    return tuple(sorted(c)[1] for c in zip(*px))


def tile(path, name, size=320, inner=272, bg=(255, 255, 255)):
    im = flat(Image.open(path), bg)
    im.thumbnail((inner, inner), Image.LANCZOS)
    canvas = Image.new("RGB", (size, size), bg)
    canvas.paste(im, ((size - im.width) // 2, (size - im.height) // 2))
    save(canvas, name, webp_q=78)


def logo(path, name, box=(232, 112), size=(264, 136)):
    im = flat(Image.open(path))
    bg = corner(im)
    im.thumbnail(box, Image.LANCZOS)
    canvas = Image.new("RGB", size, bg)
    canvas.paste(im, ((size[0] - im.width) // 2, (size[1] - im.height) // 2))
    save(canvas, name, webp_q=82, png=True)
    return bg


# cover: Pexels 6169670 (workers loading boxes into a van at a depot)
hero = widths(DL + "/p-6169670.jpg", "portada", (640, 960, 1280))
# about us: Pexels 6545444 (store counter); delivery: Pexels 5025669 (van)
widths(DL + "/p-6545444.jpg", "comercio", (480, 800))
widths(DL + "/p-5025669.jpg", "reparto", (480, 800))

# Open Graph 1200x630: the hero with the DN icon
og = ImageOps.fit(hero, (1200, 630), Image.LANCZOS, centering=(0.45, 0.5))
icon = Image.open(ICON).convert("RGBA").resize((168, 168), Image.LANCZOS)
og = og.convert("RGBA")
og.alpha_composite(icon, (64, 630 - 64 - 168))
og.convert("RGB").save(os.path.join(OUT, "og-distri-neuquen.jpg"), "JPEG", quality=82,
                       optimize=True, progressive=True)

C = SRC + "/catalogo/"
CATEGORIAS = {
    "cigarrillos": "p06-44-715x552.jpeg",
    "tabaco": "p07-55-720x560.jpeg",
    "insumos": "p08-71-473x248.jpeg",
    "golosinas": "p11-138-664x442.jpeg",
    "galletitas": "p10-113-198x292.jpeg",
    "farmacia": "p15-206-474x328.jpeg",
    "perfumeria": "p18-278-883x559.jpeg",
    "varios": "p20-313-503x353.jpeg",
    "encendedores": "p21-330-384x251.jpeg",
    "afeitadoras": "p21-331-521x800.png",
    "baterias": "p22-348-350x463.jpeg",
    "pegamentos": "p23-374-418x248.png",
}
for name, f in CATEGORIAS.items():
    tile(C + f, "cat-" + name)

MARCAS = {
    "red-point": "p01-463-165x157.jpeg", "kiel": "p01-460-186x185.jpeg",
    "bold": "p06-46-225x190.png", "parnor": "p09-96-186x159.jpeg",
    "trio": "p10-120-479x256.png", "club-social": "p12-151-225x225.png",
    "rocklets": "p12-152-587x361.jpeg", "halls": "p12-153-272x112.jpeg",
    "beldent": "p12-154-520x324.jpeg",
}
for name, f in MARCAS.items():
    print("logo", name, "#%02x%02x%02x" % logo(C + f, "logo-" + name))


# The best-known cigarette brands, cut from the catalog pages at full size
# (sharper than the small logos of page 1), as regular logo-* tiles.
DESTACADAS = {
    "red-point": ("p03-22-2482x3388.png", (1040, 530, 1440, 940)),
    "master": ("p04-27-2482x3365.png", (1020, 2000, 1460, 2230)),
    "kiel": ("p04-27-2482x3365.png", (1690, 505, 1950, 770)),
}
for name, (f, box) in DESTACADAS.items():
    crop = flat(Image.open(C + f)).crop(box)
    for prefix, inner, size in (("logo-", (232, 112), (264, 136)),):
        im = crop.copy()
        im.thumbnail(inner, Image.LANCZOS)
        canvas = Image.new("RGB", size, (255, 255, 255))
        canvas.paste(im, ((size[0] - im.width) // 2, (size[1] - im.height) // 2))
        save(canvas, prefix + name, webp_q=86, png=True)

# Vinos: three bottles of the catalog side by side
botellas = [flat(Image.open(C + f)) for f in ("p14-167-200x705.jpeg", "p14-170-196x648.jpeg",
                                               "p14-184-208x665.jpeg")]
canvas = Image.new("RGB", (320, 320), (255, 255, 255))
x = 34
for b in botellas:
    b.thumbnail((90, 280), Image.LANCZOS)
    canvas.paste(b, (x, 320 - 20 - b.height))
    x += 88
save(canvas, "cat-vinos", webp_q=78)

# Gaseosas: unlabelled soda bottles, Pexels 6696896, cropped square
soda = flat(Image.open(DL + "/p-6696896.jpg"))
w, h = soda.size
side = w
top = min(h - side, int(h * 0.30))
save(soda.crop((0, top, side, top + side)).resize((320, 320), Image.LANCZOS), "cat-gaseosas", webp_q=78)

# Jugazo: an illustrated banner (sky background), so it fills the tile
jugazo = flat(Image.open(C + "p12-150-767x463.jpeg"))
save(ImageOps.fit(jugazo, (264, 136), Image.LANCZOS, centering=(0.5, 0.55)), "logo-jugazo", webp_q=82, png=True)
save(ImageOps.fit(jugazo, (320, 320), Image.LANCZOS, centering=(0.5, 0.5)), "cat-jugos", webp_q=78)
