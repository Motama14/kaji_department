// SI-assisted: written with Claude Code (Anthropic LLM) under the author's direction and reviewed by the author.
import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable, shareReplay } from 'rxjs';
import { environment } from '../../environments/environment';

export interface Area {
  geometry?: GeoJSON.Geometry;
  bbox?: [number, number, number, number];
  point?: { lat: number; lon: number; radius_km: number };
}

export interface Meta {
  first_month: string;
  last_month: string;
  data_through: string;
  partial_month: { month: string; days_fraction: number; days: number; days_in_month: number } | null;
  type_inferred_months: string[];
  nrt_first: string | null;
  type_inferred_note: string;
  unit: string;
  csv_max_rows: number;
}

export interface CalendarResponse {
  years: number[];
  months: number[];
  harmonized: (number | null)[][];
  raw_modis: (number | null)[][];
  raw_viirs: (number | null)[][];
  source: (string | null)[][];
  days_fraction: (number | null)[][];
  type_inferred: boolean[][];
  monthly_median: (number | null)[];
  anomaly_pct: (number | null)[][];
  year_totals: number[];
  data_through: string;
  type_inferred_note: string;
  unit: string;
}

export interface AnomalyResponse {
  year: number;
  month: number;
  current: number;
  median: number;
  mean: number;
  p10: number;
  p25: number;
  p75: number;
  p90: number;
  anomaly_abs: number;
  anomaly_pct_vs_median: number | null;
  z_score: number | null;
  percentile_rank: number;
  n_baseline_years: number;
  baseline_years: [number, number];
  is_complete: boolean;
  days_fraction: number;
  baseline_prorated: boolean;
  data_through: string;
  type_inferred: boolean;
  type_inferred_note: string;
  source: string;
}

@Injectable({ providedIn: 'root' })
export class ApiService {
  private http = inject(HttpClient);
  private base = environment.apiBase;
  private meta$?: Observable<Meta>;

  meta(): Observable<Meta> {
    this.meta$ ??= this.http.get<Meta>(`${this.base}/api/meta`).pipe(shareReplay(1));
    return this.meta$;
  }

  calendar(area: Area, start?: string, end?: string): Observable<CalendarResponse> {
    return this.http.post<CalendarResponse>(`${this.base}/api/calendar`, { ...area, start, end });
  }

  anomaly(area: Area, year: number, month: number): Observable<AnomalyResponse> {
    return this.http.post<AnomalyResponse>(`${this.base}/api/anomaly`, { ...area, year, month });
  }

  csv(area: Area, start?: string, end?: string, onlyCalendar = false): Observable<Blob> {
    return this.http.post(`${this.base}/api/hotspots.csv`, { ...area, start, end, only_calendar: onlyCalendar }, {
      responseType: 'blob',
    });
  }
}
