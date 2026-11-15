// SI-assisted: written with Claude Code (Anthropic LLM) under the author's direction and reviewed by the author.
import { Injectable, computed, effect, signal } from '@angular/core';
import type { Histogram } from './viewer-api.service';

export type Window = 'day' | '7d' | 'month';
export type Mode = 'harmonized' | 'raw';
export type BaseLayer = 'imagery' | 'night' | 'streets';
/** Global = world map with the sidebar on the right; country = map of one country with the sidebar on the left. */
export type ViewMode = 'global' | 'country';
/** Who delivered the detection type: FIRMS (standard processing) or calculated by us (near-real-time data). */
export type TypeSource = 'all' | 'firms' | 'inferred';

export interface Filters {
  sensors: string[]; // terra, aqua, snpp, n20, n21
  mode: Mode;
  types: number[]; // 0 vegetation, 1 volcano, 2 static land source, 3 offshore
  conf: string[]; // low, nominal, high
  daynight: string[]; // D, N
  frpMin: number | null;
  frpMax: number | null;
  region: string; // all | <region name> | visible
  hourMin: number;
  hourMax: number;
  typeSource: TypeSource;
}

export interface MapView {
  lat: number;
  lon: number;
  zoom: number;
  bbox: [number, number, number, number] | null;
}

export const DEFAULT_FILTERS: Filters = {
  sensors: ['terra', 'aqua', 'snpp', 'n20', 'n21'],
  mode: 'harmonized',
  types: [0, 1, 2, 3],
  conf: ['low', 'nominal', 'high'],
  daynight: ['D', 'N'],
  frpMin: null,
  frpMax: null,
  region: 'all',
  hourMin: 0,
  hourMax: 23,
  typeSource: 'all',
};

/** Shared viewer state (date, window, filters, map view) mirrored in the URL. */
@Injectable({ providedIn: 'root' })
export class ViewerStateService {
  readonly date = signal<string>(''); // YYYY-MM-DD (UTC)
  readonly window = signal<Window>('day');
  readonly filters = signal<Filters>({ ...DEFAULT_FILTERS });
  readonly view = signal<MapView>({ lat: 10, lon: -30, zoom: 1.6, bbox: null });
  readonly baseLayer = signal<BaseLayer>('imagery');
  readonly viewMode = signal<ViewMode>('global');
  /** Selected country (ADM0_A3 code) in the By Country view. */
  readonly country = signal<string | null>(null);
  /** Name and box of the selected country while the By Country view shows it (the timeline counts inside the box). */
  readonly countryBox = signal<{ name: string; bbox: number[] } | null>(null);
  readonly panelOpen = signal<boolean>(typeof window !== 'undefined' ? window.innerWidth > 900 : true);
  readonly downloadOpen = signal<boolean>(typeof window !== 'undefined' ? window.innerWidth > 900 : true);
  /** Counts currently drawn in the timeline (visible stretch, current filters); used by the CSV download. */
  readonly timelineHist = signal<Histogram | null>(null);
  readonly cursor = signal<{ lat: number; lon: number } | null>(null);
  readonly visibleCount = signal<number | null>(null);
  readonly weightSum = signal<number | null>(null);

  /** Range of dates covered by the current window. */
  readonly range = computed(() => windowRange(this.date(), this.window()));

  private urlReady = false;

  constructor() {
    effect(() => {
      const state = {
        date: this.date(),
        window: this.window(),
        f: this.filters(),
        v: this.view(),
        base: this.baseLayer(),
        mode: this.viewMode(),
        country: this.country(),
      };
      if (!this.urlReady || !state.date) return;
      this.writeUrl(state.date, state.window, state.f, state.v, state.base, state.mode, state.country);
    });
  }

  /** Reads ?date=&window=&lat=&lon=&z=&sensor=... ; returns true if a date was present. */
  readFromUrl(): boolean {
    const qs = new URLSearchParams(location.search);
    const f = { ...DEFAULT_FILTERS };
    const list = (k: string) => (qs.get(k) ?? '').split(',').filter(Boolean);
    if (qs.has('sensor')) f.sensors = list('sensor');
    if (qs.get('mode') === 'raw') f.mode = 'raw';
    if (qs.has('types')) f.types = list('types').map(Number);
    if (qs.has('conf')) f.conf = list('conf');
    if (qs.has('dn')) f.daynight = list('dn');
    if (qs.has('frp_min')) f.frpMin = +qs.get('frp_min')!;
    if (qs.has('frp_max')) f.frpMax = +qs.get('frp_max')!;
    if (qs.has('region')) f.region = qs.get('region')!;
    if (qs.has('h0')) f.hourMin = +qs.get('h0')!;
    if (qs.has('h1')) f.hourMax = +qs.get('h1')!;
    const ts = qs.get('ts');
    if (ts === 'firms' || ts === 'inferred') f.typeSource = ts;
    this.filters.set(f);
    const w = qs.get('window');
    if (w === 'day' || w === '7d' || w === 'month') this.window.set(w);
    const b = qs.get('base');
    if (b === 'imagery' || b === 'night' || b === 'streets') this.baseLayer.set(b);
    if (qs.get('view') === 'country') this.viewMode.set('country');
    const c = qs.get('country');
    if (c && /^[A-Z0-9]{3}$/.test(c)) this.country.set(c);
    if (qs.has('lat') && qs.has('lon')) {
      this.view.set({ lat: +qs.get('lat')!, lon: +qs.get('lon')!, zoom: +(qs.get('z') ?? 4), bbox: null });
    }
    const d = qs.get('date');
    if (d && /^\d{4}-\d{2}-\d{2}$/.test(d)) this.date.set(d);
    this.urlReady = true;
    return !!d;
  }

  private writeUrl(date: string, w: Window, f: Filters, v: MapView, base: BaseLayer, mode: ViewMode, country: string | null): void {
    const p = new URLSearchParams();
    if (mode !== 'global') p.set('view', mode);
    if (mode === 'country' && country) p.set('country', country);
    p.set('date', date);
    p.set('window', w);
    p.set('lat', v.lat.toFixed(3));
    p.set('lon', v.lon.toFixed(3));
    p.set('z', v.zoom.toFixed(2));
    const d = DEFAULT_FILTERS;
    if (f.sensors.join() !== d.sensors.join()) p.set('sensor', f.sensors.join(','));
    if (f.mode !== d.mode) p.set('mode', f.mode);
    if (f.types.join() !== d.types.join()) p.set('types', f.types.join(','));
    if (f.conf.join() !== d.conf.join()) p.set('conf', f.conf.join(','));
    if (f.daynight.join() !== d.daynight.join()) p.set('dn', f.daynight.join(','));
    if (f.frpMin !== null) p.set('frp_min', String(f.frpMin));
    if (f.frpMax !== null) p.set('frp_max', String(f.frpMax));
    if (f.region !== 'all') p.set('region', f.region);
    if (f.hourMin !== 0) p.set('h0', String(f.hourMin));
    if (f.hourMax !== 23) p.set('h1', String(f.hourMax));
    if (f.typeSource !== 'all') p.set('ts', f.typeSource);
    if (base !== 'imagery') p.set('base', base);
    history.replaceState(null, '', `${location.pathname}?${p.toString()}`);
  }

  patchFilters(p: Partial<Filters>): void {
    this.filters.update((f) => ({ ...f, ...p }));
  }
}

// ---------------------------------------------------------------- date helpers (UTC)
export function parseDate(s: string): Date {
  const [y, m, d] = s.split('-').map(Number);
  return new Date(Date.UTC(y, m - 1, d));
}

export function fmtDate(d: Date): string {
  return d.toISOString().slice(0, 10);
}

export function addDays(s: string, n: number): string {
  const d = parseDate(s);
  d.setUTCDate(d.getUTCDate() + n);
  return fmtDate(d);
}

/** Same day in another month/year, clamped to the month length. */
export function withYearMonth(s: string, year: number, month: number): string {
  const day = parseDate(s).getUTCDate();
  const last = new Date(Date.UTC(year, month, 0)).getUTCDate();
  return fmtDate(new Date(Date.UTC(year, month - 1, Math.min(day, last))));
}

export function addMonths(s: string, n: number): string {
  const d = parseDate(s);
  const total = d.getUTCFullYear() * 12 + d.getUTCMonth() + n;
  return withYearMonth(s, Math.floor(total / 12), (total % 12) + 1);
}

export function windowRange(date: string, w: Window): [string, string] {
  if (!date) return ['', ''];
  if (w === 'day') return [date, date];
  if (w === '7d') return [addDays(date, -6), date];
  const d = parseDate(date);
  const start = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), 1));
  const end = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + 1, 0));
  return [fmtDate(start), fmtDate(end)];
}
