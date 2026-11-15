// SI-assisted: written with Claude Code (Anthropic LLM) under the author's direction and reviewed by the author.
import { AfterViewInit, Component, ElementRef, OnInit, computed, inject, signal, viewChild } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { DecimalPipe } from '@angular/common';
import L from 'leaflet';
import { AnomalyResponse, ApiService, Area, CalendarResponse, Meta } from './api.service';
import { DIVERGING_STOPS, VIRIDIS_STOPS, diverging, inkFor, sequential } from './colors';
import { CountriesService } from '../countries.service';

type View = 'harmonized' | 'anomaly' | 'raw';
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const SOURCE_LABELS: Record<string, string> = {
  VIIRS_SNPP: 'VIIRS S-NPP (observed)',
  VIIRS_N20_SCALED: 'VIIRS NOAA-20 scaled to S-NPP (S-NPP had missing days)',
  VIIRS_N21_SCALED: 'VIIRS NOAA-21 scaled to S-NPP (S-NPP had missing days)',
  VIIRS_EXPOSURE: 'VIIRS S-NPP adjusted for its missing days',
  MODIS_AQUA_MODEL: 'MODIS Aqua converted with the calibration model',
  MODIS_TERRA_MODEL: 'MODIS Terra converted with the calibration model',
};

@Component({
  selector: 'app-calendar',
  imports: [FormsModule, DecimalPipe],
  templateUrl: './calendar.html',
  styleUrl: './calendar.css',
})
export class CalendarComponent implements OnInit, AfterViewInit {
  private api = inject(ApiService);
  private countries = inject(CountriesService);
  private mapEl = viewChild.required<ElementRef<HTMLDivElement>>('map');
  private map!: L.Map;
  private drawn!: L.FeatureGroup;

  readonly months = MONTHS;
  readonly viridis = `linear-gradient(to right, ${VIRIDIS_STOPS.join(',')})`;
  readonly divergingGrad = `linear-gradient(to right, ${DIVERGING_STOPS.join(',')})`;

  area = signal<Area | null>(null);
  areaLabel = signal('No area selected — draw a polygon/rectangle or enter coordinates');
  meta = signal<Meta | null>(null);
  start = signal('2000-11');
  end = signal('2026-11');
  lat = signal(42.6);
  lon = signal(-7.8);
  radius = signal(50);
  view = signal<View>('harmonized');
  showValues = signal(false);
  loading = signal(false);
  error = signal<string | null>(null);
  cal = signal<CalendarResponse | null>(null);
  selected = signal<{ year: number; month: number } | null>(null);
  anomaly = signal<AnomalyResponse | null>(null);

  matrix = computed(() => {
    const c = this.cal();
    if (!c) return null;
    const v = this.view();
    return v === 'anomaly' ? c.anomaly_pct : v === 'raw' ? this.rawMatrix(c) : c.harmonized;
  });
  maxValue = computed(() => {
    const c = this.cal();
    if (!c) return 0;
    const m = this.view() === 'raw' ? this.rawMatrix(c) : c.harmonized;
    return Math.max(0, ...m.flat().filter((x): x is number => x !== null));
  });
  maxTotal = computed(() => Math.max(1, ...(this.cal()?.year_totals ?? [1])));
  /** Months in the calendar whose detection type was calculated (NRT data), e.g. "Aug 2026, Sep 2026". */
  inferredMonths = computed(() => {
    const c = this.cal();
    if (!c) return '';
    const out: string[] = [];
    c.years.forEach((y, yi) => c.type_inferred[yi].forEach((t, mi) => t && out.push(`${MONTHS[mi]} ${y}`)));
    return out.join(', ');
  });
  /** The partial last month, when it is in the calendar. */
  partialNote = computed(() => {
    const c = this.cal(), p = this.meta()?.partial_month;
    if (!c || !p) return '';
    const [y, m] = p.month.split('-').map(Number);
    const yi = c.years.indexOf(y);
    if (yi < 0 || c.harmonized[yi][m - 1] === null) return '';
    return `${MONTHS[m - 1]} ${y} has data up to ${c.data_through} (${p.days} of ${p.days_in_month} days): ` +
      `its anomaly compares it with the median of the same month pro-rated to those days.`;
  });

  ngOnInit(): void {
    this.api.meta().subscribe({
      next: (m) => {
        this.meta.set(m);
        this.start.set(m.first_month);
        this.end.set(m.last_month);
      },
      error: (e) => this.fail(e),
    });
  }

  ngAfterViewInit(): void {
    // leaflet-draw is a classic script that augments the global L.
    (window as any).L = L;
    import('leaflet-draw').then(() => this.initMap());
  }

  private initMap(): void {
    this.map = L.map(this.mapEl().nativeElement, { worldCopyJump: true }).setView([40, -4], 5);
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 18,
      attribution: '&copy; OpenStreetMap contributors',
    }).addTo(this.map);
    this.drawn = new L.FeatureGroup().addTo(this.map);
    const draw = new L.Control.Draw({
      edit: { featureGroup: this.drawn, remove: true },
      draw: { polygon: {}, rectangle: {}, polyline: false, circle: false, marker: false, circlemarker: false },
    });
    this.map.addControl(draw);
    this.map.on(L.Draw.Event.CREATED, (e: any) => {
      this.drawn.clearLayers();
      this.drawn.addLayer(e.layer);
      this.setArea({ geometry: e.layer.toGeoJSON().geometry }, 'Drawn area');
    });
    this.map.on(L.Draw.Event.EDITED, () => {
      const layer = this.drawn.getLayers()[0] as L.Polygon | undefined;
      if (layer) this.setArea({ geometry: layer.toGeoJSON().geometry }, 'Drawn area (edited)');
    });
    this.map.on(L.Draw.Event.DELETED, () => this.area.set(null));

    // Shareable links: ?lat=42.6&lon=-7.8&r=50[&view=anomaly]
    const qs = new URLSearchParams(location.search);
    if (qs.has('lat') && qs.has('lon')) {
      this.lat.set(+qs.get('lat')!);
      this.lon.set(+qs.get('lon')!);
      if (qs.has('r')) this.radius.set(+qs.get('r')!);
      const v = qs.get('view');
      if (v === 'anomaly' || v === 'raw' || v === 'harmonized') this.view.set(v);
      this.usePoint();
    } else if (qs.has('country')) {
      // From the viewer's By Country view: ?country=ADM0_A3 (Natural Earth border, step 9b)
      const id = qs.get('country');
      this.countries.countries().subscribe({
        next: (list) => {
          const c = list.find((x) => x.id === id);
          if (!c) {
            this.error.set(`Unknown country code ${id}`);
            return;
          }
          this.drawn.clearLayers();
          L.geoJSON(c.geometry as any).eachLayer((l) => this.drawn.addLayer(l));
          this.map.fitBounds([[c.zoom[1], c.zoom[0]], [c.zoom[3], c.zoom[2]]]);
          this.setArea({ geometry: c.geometry }, `${c.name} (Natural Earth border)`);
        },
        error: () => this.error.set('Could not load the country borders (countries.geojson).'),
      });
    } else if (qs.has('bbox')) {
      // Coming from the world viewer: ?bbox=minLon,minLat,maxLon,maxLat
      const b = qs.get('bbox')!.split(',').map(Number);
      if (b.length === 4 && b.every((x) => isFinite(x))) {
        this.drawn.clearLayers();
        const rect = L.rectangle([[b[1], b[0]], [b[3], b[2]]]).addTo(this.drawn);
        this.map.fitBounds(rect.getBounds());
        this.setArea({ bbox: b as [number, number, number, number] }, 'Area from the hotspot viewer');
      }
    }
  }

  usePoint(): void {
    const lat = +this.lat(), lon = +this.lon(), r = +this.radius();
    if (!(Math.abs(lat) <= 90 && Math.abs(lon) <= 180 && r > 0)) {
      this.error.set('Invalid coordinates or radius');
      return;
    }
    this.drawn.clearLayers();
    const circle = L.circle([lat, lon], { radius: r * 1000 }).addTo(this.drawn);
    this.map.fitBounds(circle.getBounds());
    this.setArea({ point: { lat, lon, radius_km: r } }, `Point ${lat.toFixed(3)}, ${lon.toFixed(3)} ± ${r} km`);
  }

  private setArea(a: Area, label: string): void {
    this.area.set(a);
    this.areaLabel.set(label);
    this.refresh();
  }

  refresh(): void {
    const a = this.area();
    if (!a) return;
    this.loading.set(true);
    this.error.set(null);
    this.api.calendar(a, this.start(), this.end()).subscribe({
      next: (c) => {
        this.cal.set(c);
        this.loading.set(false);
        const sel = this.selected();
        if (sel) this.pick(sel.year, sel.month);
      },
      error: (e) => this.fail(e),
    });
  }

  pick(year: number, month: number): void {
    const a = this.area();
    if (!a) return;
    this.selected.set({ year, month });
    this.api.anomaly(a, year, month).subscribe({ next: (r) => this.anomaly.set(r), error: (e) => this.fail(e) });
  }

  downloadCsv(onlyCalendar: boolean): void {
    const a = this.area();
    if (!a) return;
    const end = this.end();
    const [y, m] = end.split('-').map(Number);
    const lastDay = new Date(y, m, 0).getDate();
    this.loading.set(true);
    this.error.set(null);
    this.api.csv(a, `${this.start()}-01`, `${end}-${lastDay}`, onlyCalendar).subscribe({
      next: (blob) => {
        const url = URL.createObjectURL(blob);
        const link = document.createElement('a');
        link.href = url;
        link.download = `hotspots_${this.start()}_${end}.csv`;
        link.click();
        URL.revokeObjectURL(url);
        this.loading.set(false);
      },
      error: (e) => this.fail(e),
    });
  }

  cellColor(v: number | null): string {
    if (v === null) return 'transparent';
    return this.view() === 'anomaly' ? diverging(v) : sequential(v, this.maxValue());
  }

  ink(v: number | null): string {
    return v === null ? 'inherit' : inkFor(this.cellColor(v));
  }

  cellTitle(yi: number, mi: number): string {
    const c = this.cal()!;
    const name = `${MONTHS[mi]} ${c.years[yi]}`;
    const h = c.harmonized[yi][mi];
    if (h === null) return `${name}: no data`;
    const a = c.anomaly_pct[yi][mi];
    const med = c.monthly_median[mi];
    const frac = c.days_fraction[yi][mi] ?? 1;
    const src = c.source[yi][mi];
    let t = `${name}\nHarmonized: ${h.toFixed(0)} (VIIRS-eq.)\nRaw MODIS: ${c.raw_modis[yi][mi]?.toFixed(0)} · Raw VIIRS S-NPP: ${c.raw_viirs[yi][mi]?.toFixed(0)}\n` +
      `Median ${MONTHS[mi]}: ${med?.toFixed(0)}${frac < 1 && med !== null ? ` (pro-rated: ${(med * frac).toFixed(0)})` : ''}` +
      ` · Anomaly: ${a === null ? 'n/a' : (a > 0 ? '+' : '') + a.toFixed(0) + '%'}\n` +
      `Source: ${src ? (SOURCE_LABELS[src] ?? src) : 'none'}`;
    if (frac < 1) t += `\nPartial month: data up to ${c.data_through}`;
    if (c.type_inferred[yi][mi]) t += `\n* Detection type calculated: near-real-time data had no type`;
    return t;
  }

  fmt(v: number | null): string {
    if (v === null) return '';
    if (this.view() === 'anomaly') return (v > 0 ? '+' : '') + v.toFixed(0);
    return v >= 1000 ? (v / 1000).toFixed(1) + 'k' : v.toFixed(0);
  }

  private rawMatrix(c: CalendarResponse): (number | null)[][] {
    // Naive record: MODIS before VIIRS S-NPP, VIIRS S-NPP afterwards (shows the 2012 step).
    return c.harmonized.map((row, yi) =>
      row.map((_, mi) => (c.source[yi][mi]?.startsWith('VIIRS') ? c.raw_viirs[yi][mi] : c.raw_modis[yi][mi])),
    );
  }

  private fail(e: any): void {
    this.loading.set(false);
    if (e?.error instanceof Blob) {
      // CSV requests ask for a Blob, so the API's JSON error (e.g. 413: too many rows) arrives as a Blob.
      e.error.text().then((t: string) => {
        try {
          this.error.set(JSON.parse(t).detail ?? 'Request failed');
        } catch {
          this.error.set(t || 'Request failed');
        }
      });
      return;
    }
    this.error.set(e?.error?.detail ?? e?.message ?? 'Request failed');
  }
}
