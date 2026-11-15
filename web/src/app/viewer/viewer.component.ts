// SI-assisted: written with Claude Code (Anthropic LLM) under the author's direction and reviewed by the author.
import { Component, HostListener, OnDestroy, OnInit, computed, effect, inject, signal, viewChild } from '@angular/core';
import { Router } from '@angular/router';
import { Subject, Subscription, debounceTime, switchMap, catchError, of, tap } from 'rxjs';
import { MapComponent } from './map.component';
import { TimelineComponent } from './timeline.component';
import { FiltersPanelComponent } from './filters-panel.component';
import { TopBarComponent } from './top-bar.component';
import { DownloadPanelComponent } from './download-panel.component';
import { SummaryComponent } from './summary.component';
import { AvailableDates, HotspotsResponse, ViewerApiService } from './viewer-api.service';
import { Filters, ViewerStateService, Window } from './viewer-state.service';
import { CountriesService, Country } from '../countries.service';

const BBOX_ZOOM = 5; // from this zoom on, only the visible area is requested

interface Query { date: string; window: Window; f: Filters; bbox: number[] | null; zoom: number; }

@Component({
  selector: 'app-viewer',
  imports: [MapComponent, TimelineComponent, FiltersPanelComponent, TopBarComponent, DownloadPanelComponent, SummaryComponent],
  templateUrl: './viewer.component.html',
  styleUrl: './viewer.component.css',
})
export class ViewerComponent implements OnInit, OnDestroy {
  readonly state = inject(ViewerStateService);
  private api = inject(ViewerApiService);
  private countriesApi = inject(CountriesService);
  private router = inject(Router);
  private map = viewChild(MapComponent);
  private timeline = viewChild(TimelineComponent);

  readonly available = signal<AvailableDates | null>(null);
  readonly data = signal<HotspotsResponse | null>(null);
  readonly loading = signal(false);
  readonly error = signal<string | null>(null);
  readonly aggregated = computed(() => !!this.data()?.aggregated);
  readonly isCountry = computed(() => this.state.viewMode() === 'country');
  /** Country borders (Natural Earth), loaded the first time the By Country view opens. */
  readonly countries = signal<Country[] | null>(null);
  readonly countriesError = signal(false);
  /** The country the By Country view shows (null in the global view or before one is picked). */
  readonly country = computed<Country | null>(() => {
    const id = this.state.country(), list = this.countries();
    return this.isCountry() && id && list ? list.find((c) => c.id === id) ?? null : null;
  });
  /** What the map draws: the API response, cut to the country border in the By Country view (client-side). */
  readonly shown = computed<HotspotsResponse | null>(() => {
    const d = this.data(), c = this.country();
    return d && c ? this.clipToCountry(d, c) : d;
  });
  readonly summaryScope = computed(() => {
    const c = this.country();
    if (c) return `Inside ${c.name}`;
    return this.effBbox() ? 'Visible map area' : 'Whole world';
  });
  /** Visible map area, clamped to valid coordinates (used by the download sidebar). */
  readonly visibleBbox = computed<number[] | null>(() => {
    const b = this.state.view().bbox;
    if (!b) return null;
    const c = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));
    return [c(b[0], -180, 180), c(b[1], -90, 90), c(b[2], -180, 180), c(b[3], -90, 90)];
  });

  /** bbox sent to the API: the country's box in the By Country view; otherwise none when zoomed out (whole
   *  dataset), snapped to 1° when zoomed in. */
  private readonly effBbox = computed<number[] | null>(() => {
    const c = this.country();
    if (c) return c.bbox;
    const v = this.state.view();
    const region = this.state.filters().region;
    if (!v.bbox || (v.zoom < BBOX_ZOOM && region !== 'visible')) return null;
    const [x0, y0, x1, y1] = v.bbox;
    return [Math.floor(x0), Math.floor(y0), Math.ceil(x1), Math.ceil(y1)];
  }, { equal: (a, b) => JSON.stringify(a) === JSON.stringify(b) });
  private readonly zoomBucket = computed(() => {
    const z = this.state.view().zoom;
    return z < 4 ? 2 : z < 6 ? 5 : 8;
  });

  private queries = new Subject<Query>();
  private sub?: Subscription;

  constructor() {
    effect(() => {
      const date = this.state.date();
      if (!date || !this.available()) return;
      if (this.isCountry() && this.state.country() && !this.country() && !this.countriesError()) return; // borders still loading
      this.queries.next({ date, window: this.state.window(), f: this.state.filters(), bbox: this.effBbox(), zoom: this.zoomBucket() });
    });
    // Load the borders the first time the By Country view opens.
    effect(() => {
      if (!this.isCountry() || this.countries() || this.countriesError()) return;
      this.countriesApi.countries().subscribe({
        next: (list) => this.countries.set(list),
        error: () => { this.countriesError.set(true); this.error.set('Could not load the country borders (countries.geojson).'); },
      });
    });
    // Top bar counter and the timeline's area follow what the map shows.
    effect(() => {
      const d = this.shown();
      if (!d) return;
      this.state.visibleCount.set(d.count);
      this.state.weightSum.set(d.mode === 'harmonized' ? d.weight_sum : null);
    });
    effect(() => {
      const c = this.country();
      this.state.countryBox.set(c ? { name: c.name, bbox: c.bbox } : null);
    });
  }

  ngOnInit(): void {
    const hadDate = this.state.readFromUrl();
    this.sub = this.queries.pipe(
      debounceTime(120),
      tap(() => { this.loading.set(true); this.error.set(null); }),
      switchMap((q) => this.api.hotspots(q.date, q.window, q.f, q.bbox, q.zoom).pipe(
        catchError((e) => {
          this.error.set(e?.error?.detail ?? e?.message ?? 'Could not load hotspots');
          return of(null);
        }),
      )),
    ).subscribe((d) => {
      this.loading.set(false);
      if (d) this.data.set(d);
    });
    this.api.availableDates().subscribe({
      next: (a) => {
        this.available.set(a);
        if (!hadDate) this.state.date.set(a.last);
      },
      error: () => this.error.set('The API is not reachable. Start it with: uvicorn api.main:app --port 8000'),
    });
  }

  ngOnDestroy(): void {
    this.sub?.unsubscribe();
    this.timeline()?.stop();
  }

  togglePanel(): void {
    this.state.panelOpen.update((v) => !v);
  }

  toggleDownload(): void {
    this.state.downloadOpen.update((v) => !v);
  }

  /** Pick a country (sidebar list or map click) and zoom to its largest part. */
  selectCountry(id: string | null): void {
    this.state.country.set(id || null);
    const c = this.countries()?.find((x) => x.id === id);
    if (c) this.map()?.fitBbox(c.zoom);
  }

  onMapClick(p: { lon: number; lat: number }): void {
    const list = this.countries();
    if (!this.isCountry() || !list) return;
    const c = this.countriesApi.findAt(list, p.lon, p.lat);
    if (c && c.id !== this.state.country()) this.selectCountry(c.id);
  }

  /** Keeps the detections (or grid cells, by their centre) inside the country border. */
  private clipToCountry(d: HotspotsResponse, c: Country): HotspotsResponse {
    const keep: number[] = [];
    for (let i = 0; i < d.lon.length; i++) if (this.countriesApi.contains(c, d.lon[i], d.lat[i])) keep.push(i);
    const pick = <T>(a: T[]): T[] => keep.map((i) => a[i]);
    const sum = (a: number[]) => a.reduce((x, y) => x + (y ?? 0), 0);
    if (d.aggregated) {
      const n = pick(d.n), w = pick(d.w);
      return { ...d, lon: pick(d.lon), lat: pick(d.lat), n, w, frp_sum: pick(d.frp_sum), frp_max: pick(d.frp_max),
        count: sum(n), weight_sum: Math.round(sum(w) * 10) / 10, type_inferred_count: -1 };
    }
    const ti = pick(d.ti), w = pick(d.w);
    return { ...d, lon: pick(d.lon), lat: pick(d.lat), frp: pick(d.frp), t: pick(d.t), sat: pick(d.sat),
      type: pick(d.type), conf: pick(d.conf), dn: pick(d.dn), w, scan: pick(d.scan), track: pick(d.track),
      cal: pick(d.cal), ti, version: pick(d.version),
      count: keep.length, weight_sum: Math.round(sum(w) * 10) / 10, type_inferred_count: sum(ti) };
  }

  fitRegion(name: string): void {
    const r = this.available()?.regions.find((x) => x.name === name);
    if (r?.bbox) this.map()?.fitBbox(r.bbox);
  }

  openCalendar(): void {
    const c = this.country();
    if (c) {
      this.router.navigate(['/calendar'], { queryParams: { country: c.id } });
      return;
    }
    const f = this.state.filters();
    const reg = this.available()?.regions.find((r) => r.name === f.region);
    const b = reg?.bbox ?? this.map()?.visibleBounds();
    if (!b) return;
    const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));
    const bbox = [clamp(b[0], -180, 180), clamp(b[1], -85, 85), clamp(b[2], -180, 180), clamp(b[3], -85, 85)];
    this.router.navigate(['/calendar'], { queryParams: { bbox: bbox.map((x) => x.toFixed(3)).join(',') } });
  }

  // ← → day, Shift+← → month, space play/pause (ignored while typing in a form field)
  @HostListener('document:keydown', ['$event'])
  onKey(e: KeyboardEvent): void {
    const tag = (e.target as HTMLElement)?.tagName;
    if (['INPUT', 'SELECT', 'TEXTAREA'].includes(tag) || (e.target as HTMLElement)?.isContentEditable) return;
    const t = this.timeline();
    if (!t) return;
    if (e.key === 'ArrowLeft') { e.shiftKey ? t.stepMonth(-1) : t.step(-1); e.preventDefault(); }
    else if (e.key === 'ArrowRight') { e.shiftKey ? t.stepMonth(1) : t.step(1); e.preventDefault(); }
    else if (e.key === ' ' && tag !== 'BUTTON') { t.togglePlay(); e.preventDefault(); }
  }
}
