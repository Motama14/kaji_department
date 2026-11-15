// SI-assisted: written with Claude Code (Anthropic LLM) under the author's direction and reviewed by the author.
import { Component, computed, inject, input, output } from '@angular/core';
import { AvailableDates } from './viewer-api.service';
import { BaseLayer, DEFAULT_FILTERS, Filters, TypeSource, ViewerStateService } from './viewer-state.service';
import { TYPE_LABELS } from './frp';
import { LegendComponent } from './legend.component';

const ALL_SENSORS = [
  { code: 'terra', label: 'MODIS Terra' },
  { code: 'aqua', label: 'MODIS Aqua' },
  { code: 'snpp', label: 'VIIRS S-NPP' },
  { code: 'n20', label: 'VIIRS NOAA-20' },
  { code: 'n21', label: 'VIIRS NOAA-21' },
];
// FRP slider: 0..100 mapped to 10^(v/25) MW, i.e. 1 .. 10 000 MW on a log scale.
const toFrp = (v: number) => Math.round(Math.pow(10, v / 25) * 10) / 10;
const fromFrp = (f: number) => Math.round(25 * Math.log10(Math.max(1, f)));

@Component({
  selector: 'app-filters-panel',
  imports: [LegendComponent],
  templateUrl: './filters-panel.component.html',
  styleUrl: './filters-panel.component.css',
})
export class FiltersPanelComponent {
  readonly available = input.required<AvailableDates>();
  readonly aggregated = input(false);
  /** By Country view: the area is the country (the Region selector is hidden). */
  readonly countryView = input(false);
  readonly countryName = input<string | null>(null);
  readonly calendar = output<void>();
  readonly fitRegion = output<string>();
  private state = inject(ViewerStateService);

  readonly f = this.state.filters;
  readonly base = this.state.baseLayer;
  readonly typeLabels = TYPE_LABELS;
  readonly sensors = computed(() => {
    const have = new Set(this.available().satellites.map((s) => s.code));
    return ALL_SENSORS.map((s) => ({ ...s, available: have.has(s.code),
      range: this.available().satellites.find((x) => x.code === s.code) }));
  });
  /** Warning required for near-real-time data: shown when the selected date has detections with a calculated type. */
  readonly showTypeNote = computed(() => {
    const first = this.available().nrt_first;
    return !!first && (this.state.date() ?? '') >= first;
  });
  readonly frpLo = computed(() => (this.f().frpMin === null ? 0 : fromFrp(this.f().frpMin!)));
  readonly frpHi = computed(() => (this.f().frpMax === null ? 100 : fromFrp(this.f().frpMax!)));
  readonly frpText = computed(() => {
    const a = this.f().frpMin, b = this.f().frpMax;
    return `${a === null ? 'any' : a + ' MW'} – ${b === null ? 'any' : b + ' MW'}`;
  });
  readonly seriesText = computed(() => {
    const r = this.available().series_rule;
    return `Harmonized mode shows the satellite that feeds the VIIRS-equivalent series on each date: ` +
      `S-NPP from ${r.viirs_start} (NOAA-20 in months when S-NPP has missing days), MODIS Aqua from ` +
      `${r.aqua_start}, MODIS Terra before.`;
  });

  toggle<K extends 'sensors' | 'conf' | 'daynight' | 'types'>(key: K, value: Filters[K][number]): void {
    const cur = this.f()[key] as any[];
    const next = cur.includes(value) ? cur.filter((x) => x !== value) : [...cur, value];
    this.state.patchFilters({ [key]: next } as Partial<Filters>);
  }

  has<K extends 'sensors' | 'conf' | 'daynight' | 'types'>(key: K, value: Filters[K][number]): boolean {
    return (this.f()[key] as any[]).includes(value);
  }

  setTypeSource(t: string): void {
    this.state.patchFilters({ typeSource: t as TypeSource });
  }

  setMode(m: string): void {
    this.state.patchFilters({ mode: m as Filters['mode'] });
  }

  setRegion(r: string): void {
    this.state.patchFilters({ region: r });
    if (r !== 'all' && r !== 'visible') this.fitRegion.emit(r);
  }

  setFrp(which: 'lo' | 'hi', v: string): void {
    let lo = this.frpLo(), hi = this.frpHi();
    if (which === 'lo') lo = Math.min(+v, hi); else hi = Math.max(+v, lo);
    this.state.patchFilters({ frpMin: lo <= 0 ? null : toFrp(lo), frpMax: hi >= 100 ? null : toFrp(hi) });
  }

  setHour(which: 'lo' | 'hi', v: string): void {
    const f = this.f();
    if (which === 'lo') this.state.patchFilters({ hourMin: Math.min(+v, f.hourMax) });
    else this.state.patchFilters({ hourMax: Math.max(+v, f.hourMin) });
  }

  setBase(b: string): void {
    this.state.baseLayer.set(b as BaseLayer);
  }

  reset(): void {
    this.state.filters.set({ ...DEFAULT_FILTERS });
  }
}
