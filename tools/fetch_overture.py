"""Download Overture Maps data (OSM-derived) for the Baku map area.

Overture GeoParquet on AWS S3 is spatially clustered, so a bbox filter on the
`bbox` struct column only downloads the matching row groups.
"""
import sys, time, os
import pyarrow.fs as pafs
import pyarrow.dataset as ds
import pyarrow.parquet as pq

RELEASE = "2026-10-21.0"
# generous bbox around the area of the reference screenshot (lon/lat)
W, S, E, N = 49.770, 40.335, 49.935, 40.455

THEMES = [
    ("transportation", "segment"),
    ("transportation", "connector"),
    ("buildings", "building"),
    ("buildings", "building_part"),
    ("places", "place"),
    ("base", "water"),
    ("base", "land_use"),
    ("base", "land_cover"),
    ("base", "infrastructure"),
    ("base", "land"),
    ("addresses", "address"),
    ("divisions", "division_area"),
]

def main(out_dir, only=None):
    proxy = os.environ.get("HTTPS_PROXY") or None
    fs = pafs.S3FileSystem(anonymous=True, region="us-west-2", proxy_options=proxy)
    for theme, typ in THEMES:
        if only and typ not in only:
            continue
        out = os.path.join(out_dir, f"{typ}.parquet")
        if os.path.exists(out):
            print("skip", out); continue
        t = time.time()
        path = f"overturemaps-us-west-2/release/{RELEASE}/theme={theme}/type={typ}/"
        dset = ds.dataset(path, filesystem=fs, format="parquet")
        f = ((ds.field("bbox", "xmin") < E) & (ds.field("bbox", "xmax") > W) &
             (ds.field("bbox", "ymin") < N) & (ds.field("bbox", "ymax") > S))
        tbl = dset.to_table(filter=f)
        pq.write_table(tbl, out, compression="zstd")
        print(f"{theme}/{typ}: {tbl.num_rows} rows, {time.time()-t:.1f}s", flush=True)

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data/raw", sys.argv[2:] or None)
