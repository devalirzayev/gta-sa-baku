"""Top-down raster renderer of the generated world (for ground LOD textures and the radar map)."""
from __future__ import annotations

import numpy as np
import shapely
from PIL import Image, ImageDraw, ImageFilter

# average colours of the ground materials (sRGB)
MAT_RGB = {
    "bk_soil": (168, 152, 120), "bk_grass": (92, 118, 62), "bk_grassdry": (140, 134, 94),
    "bk_dirt": (140, 120, 94), "bk_conc": (146, 144, 138), "bk_plaza": (172, 166, 152),
    "bk_gravel": (118, 114, 108), "bk_sand": (204, 186, 148), "bk_seabed": (90, 104, 96),
    "road": (70, 72, 76), "minor": (88, 88, 88), "sidewalk": (150, 148, 142), "path": (158, 146, 124),
    "water": (64, 96, 116), "roof": (128, 118, 110), "rail": (100, 96, 92),
}


class TopDown:
    def __init__(self, bbox, size, supersample=2, bg=MAT_RGB["bk_soil"]):
        self.x0, self.y0, self.x1, self.y1 = bbox
        self.w, self.h = (size, size) if isinstance(size, int) else size
        self.ss = supersample
        self.img = Image.new("RGB", (self.w * self.ss, self.h * self.ss), bg)
        self.dr = ImageDraw.Draw(self.img)
        self.box = shapely.box(*bbox)

    def _px(self, coords):
        a = np.asarray(coords, np.float64)
        sx = self.w * self.ss / (self.x1 - self.x0)
        sy = self.h * self.ss / (self.y1 - self.y0)
        return [((x - self.x0) * sx, (self.y1 - y) * sy) for x, y in a]

    def polygon(self, geom, color, clip=True):
        if geom is None or geom.is_empty:
            return
        if clip:
            geom = geom.intersection(self.box.buffer(2))
        for p in getattr(geom, "geoms", [geom]):
            if p.geom_type == "Polygon" and not p.is_empty:
                if not p.interiors:
                    self.dr.polygon(self._px(p.exterior.coords), fill=tuple(color))
                    continue
                # polygon with holes: paint through a mask so whatever lies under the holes
                # (e.g. the city blocks inside a road network) stays visible
                ext = self._px(p.exterior.coords)
                xs = [q[0] for q in ext]
                ys = [q[1] for q in ext]
                x0 = max(0, int(min(xs)) - 1)
                y0 = max(0, int(min(ys)) - 1)
                x1 = min(self.img.size[0], int(max(xs)) + 2)
                y1 = min(self.img.size[1], int(max(ys)) + 2)
                if x1 <= x0 or y1 <= y0:
                    continue
                m = Image.new("L", (x1 - x0, y1 - y0), 0)
                md = ImageDraw.Draw(m)
                md.polygon([(x - x0, y - y0) for x, y in ext], fill=255)
                for h in p.interiors:
                    md.polygon([(x - x0, y - y0) for x, y in self._px(h.coords)], fill=0)
                self.img.paste(tuple(color), (x0, y0, x1, y1), m)
            elif p.geom_type in ("MultiPolygon", "GeometryCollection"):
                self.polygon(p, color, clip=False)

    def polygon_with_holes(self, geom, color, hole_color):
        if geom is None or geom.is_empty:
            return
        geom = geom.intersection(self.box.buffer(2))
        for p in getattr(geom, "geoms", [geom]):
            if p.geom_type != "Polygon" or p.is_empty:
                continue
            self.dr.polygon(self._px(p.exterior.coords), fill=tuple(color))
            for h in p.interiors:
                self.dr.polygon(self._px(h.coords), fill=tuple(hole_color))

    def line(self, coords, color, width_units):
        px = self._px(coords)
        w = max(1, int(round(width_units * self.w * self.ss / (self.x1 - self.x0))))
        self.dr.line(px, fill=tuple(color), width=w)

    def result(self, noise=0.0, seed=0):
        im = self.img.resize((self.w, self.h), Image.LANCZOS)
        if noise:
            a = np.asarray(im, np.float64)
            r = np.random.default_rng(seed)
            a = a + r.normal(0, noise, a.shape[:2])[..., None]
            im = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))
        return im
