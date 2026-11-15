// SI-assisted: written with Claude Code (Anthropic LLM) under the author's direction and reviewed by the author.
import { Injectable, inject } from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { Observable, shareReplay } from 'rxjs';
import { environment } from '../../environments/environment';
import { Filters, Window } from './viewer-state.service';

export interface AvailableDates {
  first: string;
  last: string;
  years: number[];
  satellites: { code: string; label: string; first: string; last: string; count: number }[];
  regions: { name: string; label: string; first: string; last: string; count: number; bbox: number[] | null }[];
  series_rule: { viirs_start: string; aqua_start: string };
  /** First date with near-real-time data, whose detection type was calculated (null if none). */
  nrt_first: string | null;
  type_inferred_note: string;
}

interface HotspotsBase {
  date: string;
  window: Window;
  from: string;
  to: string;
  mode: string;
  count: number;
  weight_sum: number;
  type_inferred_count: number;
  lon: number[];
  lat: number[];
}

export interface HotspotPoints extends HotspotsBase {
  aggregated: false;
  sat_index: string[];
  conf_index: string[];
  frp: number[];
  t: string[];
  sat: number[];
  type: number[];
  conf: number[];
  dn: number[];
  w: number[];
  scan: number[];
  track: number[];
  cal: number[];
  /** 1 when the detection type was calculated (near-real-time data). */
  ti: number[];
  version: string[];
}

export interface HotspotGrid extends HotspotsBase {
  aggregated: true;
  cell_deg: number;
  n: number[];
  frp_sum: number[];
  w: number[];
  frp_max: number[];
}

export type HotspotsResponse = HotspotPoints | HotspotGrid;

export interface Histogram {
  granularity: 'day' | 'month';
  mode: string;
  unit: string;
  t: string[];
  n: number[];
  v: number[];
}

@Injectable({ providedIn: 'root' })
export class ViewerApiService {
  private http = inject(HttpClient);
  private base = environment.apiBase;
  private available$?: Observable<AvailableDates>;

  availableDates(): Observable<AvailableDates> {
    this.available$ ??= this.http.get<AvailableDates>(`${this.base}/api/available-dates`).pipe(shareReplay(1));
    return this.available$;
  }

  hotspots(date: string, window: Window, f: Filters, bbox: number[] | null, zoom: number): Observable<HotspotsResponse> {
    let p = this.filterParams(f, bbox).set('date', date).set('window', window).set('zoom', zoom.toFixed(1));
    return this.http.get<HotspotsResponse>(`${this.base}/api/hotspots`, { params: p });
  }

  histogram(from: string, to: string, granularity: 'day' | 'month', f: Filters, bbox: number[] | null): Observable<Histogram> {
    const p = this.filterParams(f, bbox).set('from', from).set('to', to).set('granularity', granularity);
    return this.http.get<Histogram>(`${this.base}/api/timeline-histogram`, { params: p });
  }

  /** Detections CSV (POST /api/hotspots.csv): area + date range only, the map filters are not applied server-side. */
  hotspotsCsv(bbox: number[] | null, start: string, end: string, onlyCalendar: boolean): Observable<Blob> {
    const body = { bbox: bbox ?? [-180, -90, 180, 90], start, end, only_calendar: onlyCalendar };
    return this.http.post(`${this.base}/api/hotspots.csv`, body, { responseType: 'blob' });
  }

  private filterParams(f: Filters, bbox: number[] | null): HttpParams {
    let p = new HttpParams()
      .set('sensors', f.sensors.join(','))
      .set('mode', f.mode)
      .set('types', f.types.join(','))
      .set('conf', f.conf.join(','))
      .set('daynight', f.daynight.join(','))
      .set('region', f.region);
    if (bbox) p = p.set('bbox', bbox.map((x) => x.toFixed(3)).join(','));
    if (f.frpMin !== null) p = p.set('frp_min', String(f.frpMin));
    if (f.frpMax !== null) p = p.set('frp_max', String(f.frpMax));
    if (f.hourMin > 0) p = p.set('hour_min', String(f.hourMin));
    if (f.hourMax < 23) p = p.set('hour_max', String(f.hourMax));
    if (f.typeSource !== 'all') p = p.set('ti', f.typeSource);
    return p;
  }
}
