// SI-assisted: written with Claude Code (Anthropic LLM) under the author's direction and reviewed by the author.
import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable, map, shareReplay } from 'rxjs';

/**
 * Country borders for the "By Country" view and the burn calendar (step 9b).
 * Source: Natural Earth 1:50m Admin 0 v5.1.2 (public domain), built by `python -m pipeline.countries` into
 * web/public/countries.geojson (properties described there).
 */
export interface Country {
  id: string; // ADM0_A3
  name: string;
  /** [west, south, east, north] of all parts; east may be > 180 when `shift` (antimeridian). */
  bbox: [number, number, number, number];
  /** Box of the largest part, used to zoom the map. */
  zoom: [number, number, number, number];
  shift: boolean;
  geometry: GeoJSON.Polygon | GeoJSON.MultiPolygon;
}

/** Point-in-polygon index: polygon edges bucketed in latitude bands (even-odd rule over every ring). */
class PolygonIndex {
  private edges: Float64Array;
  private bands: Int32Array[];
  private y0: number;
  private bh: number;

  constructor(private c: Country) {
    const rings = this.rings();
    const list: number[] = [];
    let ymin = Infinity, ymax = -Infinity;
    for (const r of rings) {
      for (let i = 0; i + 1 < r.length; i++) {
        const [x1, y1] = r[i], [x2, y2] = r[i + 1];
        if (y1 === y2) continue;
        list.push(this.sx(x1), y1, this.sx(x2), y2);
        ymin = Math.min(ymin, y1, y2);
        ymax = Math.max(ymax, y1, y2);
      }
    }
    this.edges = Float64Array.from(list);
    const n = list.length / 4;
    const nb = Math.max(16, Math.min(2048, Math.ceil(n / 4)));
    this.y0 = ymin;
    this.bh = Math.max(1e-9, (ymax - ymin) / nb);
    this.nb = nb;
    const tmp: number[][] = Array.from({ length: nb }, () => []);
    for (let e = 0; e < n; e++) {
      const a = this.edges[e * 4 + 1], b = this.edges[e * 4 + 3];
      const lo = this.band(Math.min(a, b)), hi = this.band(Math.max(a, b));
      for (let k = lo; k <= hi; k++) tmp[k].push(e);
    }
    this.bands = tmp.map((t) => Int32Array.from(t));
  }

  private nb = 1;

  private rings(): number[][][] {
    const g = this.c.geometry;
    return g.type === 'Polygon' ? g.coordinates : g.coordinates.flat();
  }

  private sx(x: number): number {
    return this.c.shift && x < 0 ? x + 360 : x;
  }

  private band(y: number): number {
    return Math.min(this.nb - 1, Math.max(0, Math.floor((y - this.y0) / this.bh)));
  }

  contains(lon: number, lat: number): boolean {
    const [w, s, e, n] = this.c.bbox;
    const x = this.sx(lon);
    if (x < w || x > e || lat < s || lat > n) return false;
    const k = this.band(lat);
    const ed = this.edges;
    let inside = false;
    for (const i of this.bands[k]) {
      const x1 = ed[i * 4], y1 = ed[i * 4 + 1], x2 = ed[i * 4 + 2], y2 = ed[i * 4 + 3];
      if ((y1 > lat) !== (y2 > lat) && x < ((x2 - x1) * (lat - y1)) / (y2 - y1) + x1) inside = !inside;
    }
    return inside;
  }
}

@Injectable({ providedIn: 'root' })
export class CountriesService {
  private http = inject(HttpClient);
  private list$?: Observable<Country[]>;
  private index = new Map<string, PolygonIndex>();

  /** All countries sorted by name (loaded once, lazily: about 0.6 MB compressed). */
  countries(): Observable<Country[]> {
    // Relative to <base href>, so it also works under the GitHub Pages repository path.
    const url = new URL('countries.geojson', document.baseURI).href;
    this.list$ ??= this.http.get<GeoJSON.FeatureCollection>(url).pipe(
      map((fc) => fc.features.map((f) => ({ ...(f.properties as Omit<Country, 'geometry'>), geometry: f.geometry as Country['geometry'] }))),
      shareReplay(1),
    );
    return this.list$;
  }

  contains(c: Country, lon: number, lat: number): boolean {
    let ix = this.index.get(c.id);
    if (!ix) {
      ix = new PolygonIndex(c);
      this.index.set(c.id, ix);
    }
    return ix.contains(lon, lat);
  }

  /** Country under a map click (longitude already wrapped to -180..180), or null over the sea. */
  findAt(list: Country[], lon: number, lat: number): Country | null {
    return list.find((c) => this.contains(c, lon, lat)) ?? null;
  }

  /** Everything except the country (its outer rings become holes), to dim the rest of the world. */
  mask(c: Country): GeoJSON.Feature<GeoJSON.MultiPolygon> {
    const polys = c.geometry.type === 'Polygon' ? [c.geometry.coordinates] : c.geometry.coordinates;
    const world = [[-180, -90], [180, -90], [180, 90], [-180, 90], [-180, -90]];
    const holes = polys.map((p) => p[0]);
    const lakes = polys.flatMap((p) => p.slice(1).map((r) => [r])); // the country's own holes stay dimmed
    return { type: 'Feature', properties: {}, geometry: { type: 'MultiPolygon', coordinates: [[world, ...holes], ...lakes] } };
  }
}
