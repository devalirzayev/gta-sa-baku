"""Download Terrarium elevation tiles (AWS Open Data 'elevation-tiles-prod') for the map area
and mosaic them into data/raw/dem/terrarium_z{z}.npz (elevation metres, WebMercator grid)."""
import io, math, os, sys, time
import numpy as np, requests
from PIL import Image

Z = 14
W, S, E, N = 49.770, 40.335, 49.935, 40.455

def tile_xy(lon, lat, z):
    n = 2 ** z
    x = (lon + 180) / 360 * n
    y = (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n
    return x, y

def main(out_dir):
    x0, y0 = tile_xy(W, N, Z); x1, y1 = tile_xy(E, S, Z)
    tx0, ty0, tx1, ty1 = int(x0), int(y0), int(x1), int(y1)
    nx, ny = tx1 - tx0 + 1, ty1 - ty0 + 1
    mosaic = np.zeros((ny * 256, nx * 256), np.float32)
    s = requests.Session()
    for ty in range(ty0, ty1 + 1):
        for tx in range(tx0, tx1 + 1):
            url = f"https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{Z}/{tx}/{ty}.png"
            for attempt in range(5):
                r = s.get(url, timeout=60)
                if r.status_code == 200: break
                time.sleep(2 ** attempt)
            r.raise_for_status()
            a = np.asarray(Image.open(io.BytesIO(r.content)).convert("RGB"), np.float32)
            elev = a[..., 0] * 256 + a[..., 1] + a[..., 2] / 256 - 32768
            mosaic[(ty - ty0) * 256:(ty - ty0 + 1) * 256, (tx - tx0) * 256:(tx - tx0 + 1) * 256] = elev
    np.savez_compressed(os.path.join(out_dir, f"terrarium_z{Z}.npz"), elev=mosaic, z=Z, tx0=tx0, ty0=ty0)
    print("mosaic", mosaic.shape, "range", mosaic.min(), mosaic.max())

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data/raw/dem")
