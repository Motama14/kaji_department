# SI-assisted: written with Claude Code (Anthropic LLM) under the author's direction and reviewed by the author.
"""Step 9b - country borders for the viewer's "By Country" view.

Source: Natural Earth 1:50m Admin 0 countries (public domain), pinned to release v5.1.2. Output:
web/public/countries.geojson, one feature per Natural Earth unit with

  id     ADM0_A3 code (unique; ISO_A3 is -99 for France, Norway and a few others)
  name   English name (NAME_EN)
  bbox   [west, south, east, north] of all its parts, used to request the detections. When a country crosses the
         antimeridian (Russia, Fiji, the USA's Aleutians...), negative longitudes are shifted by +360 so the box
         stays narrow: east can then be > 180 (the API normalises it).
  zoom   box of its largest part, used to zoom the map (France's overseas departments would otherwise zoom out to
         half the planet). Same longitude convention as bbox.
  shift  true when bbox uses the shifted longitudes (the client shifts points and rings the same way to test
         whether a detection is inside the border)

Coordinates are rounded to 3 decimals (about 100 m, finer than the 1:50m source).

Run from the repo root:
    .venv/Scripts/python -m pipeline.countries                 # downloads the source (about 3 MB)
    .venv/Scripts/python -m pipeline.countries --src FILE      # uses an already downloaded copy
"""
from __future__ import annotations

import argparse
import json
import urllib.request
from pathlib import Path

from . import config

SOURCE_URL = ("https://raw.githubusercontent.com/nvkelso/natural-earth-vector/v5.1.2/geojson/"
              "ne_50m_admin_0_countries.geojson")
OUT = config.ROOT / "web" / "public" / "countries.geojson"
DECIMALS = 3


def _ring(r: list) -> list:
    out = []
    for x, y in r:
        p = [round(x, DECIMALS), round(y, DECIMALS)]
        if not out or p != out[-1]:
            out.append(p)
    if out and out[0] != out[-1]:
        out.append(out[0])
    return out if len(out) >= 4 else []


def _polygons(geom: dict) -> list:
    polys = [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]
    out = []
    for poly in polys:
        rings = [_ring(r) for r in poly]
        if rings and rings[0]:
            out.append([r for r in rings if r])
    return out


def _area(ring: list) -> float:
    return abs(sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(ring, ring[1:]))) / 2


def _box(points: list, shift: bool) -> list:
    xs = [x + 360 if shift and x < 0 else x for x, _ in points]
    ys = [y for _, y in points]
    return [round(min(xs), DECIMALS), round(min(ys), DECIMALS), round(max(xs), DECIMALS), round(max(ys), DECIMALS)]


def build(src: Path) -> None:
    data = json.loads(src.read_text(encoding="utf-8"))
    features = []
    for f in data["features"]:
        p = f["properties"]
        polys = _polygons(f["geometry"])
        if not polys:
            continue
        pts = [pt for poly in polys for pt in poly[0]]
        plain, shifted = _box(pts, False), _box(pts, True)
        # Shift only when it really narrows the box (Antarctica spans every longitude either way: shifting it
        # would break its rings at 0 degrees for the client's point-in-polygon test).
        shift = shifted[2] - shifted[0] < plain[2] - plain[0] - 90
        main = max(polys, key=lambda poly: _area(poly[0]))
        zoom = _box(main[0], shift)
        if zoom[0] >= 180:  # the largest part lies wholly east of the antimeridian: back to -180..180
            zoom = [round(zoom[0] - 360, DECIMALS), zoom[1], round(zoom[2] - 360, DECIMALS), zoom[3]]
        geom = {"type": "Polygon", "coordinates": polys[0]} if len(polys) == 1 else \
            {"type": "MultiPolygon", "coordinates": polys}
        features.append({
            "type": "Feature",
            "properties": {"id": p["ADM0_A3"], "name": p.get("NAME_EN") or p["NAME"],
                           "bbox": shifted if shift else plain, "zoom": zoom, "shift": shift},
            "geometry": geom,
        })
    features.sort(key=lambda f: f["properties"]["name"])
    ids = [f["properties"]["id"] for f in features]
    if len(ids) != len(set(ids)):
        raise SystemExit("duplicate ADM0_A3 codes")
    OUT.write_text(json.dumps({"type": "FeatureCollection", "source": "Natural Earth 1:50m Admin 0 v5.1.2 (public domain)",
                               "features": features}, separators=(",", ":")), encoding="utf-8")
    shifted_ids = [f["properties"]["id"] for f in features if f["properties"]["shift"]]
    print(f"{len(features)} countries -> {OUT} ({OUT.stat().st_size / 1e6:.2f} MB); "
          f"antimeridian-shifted boxes: {', '.join(shifted_ids)}")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", type=Path, help="already downloaded ne_50m_admin_0_countries.geojson")
    args = ap.parse_args(argv)
    src = args.src
    if src is None:
        src = config.TMP_DIR / "ne_50m_admin_0_countries.geojson"
        src.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(SOURCE_URL, src)
    build(src)


if __name__ == "__main__":
    main()
