// SI-assisted: written with Claude Code (Anthropic LLM) under the author's direction and reviewed by the author.
import { Component, computed, input } from '@angular/core';
import { DecimalPipe } from '@angular/common';
import { HotspotsResponse } from './viewer-api.service';
import { SAT_LABELS, TYPE_LABELS } from './frp';

interface Row { label: string; n: number; share: number; }

/**
 * Selection summary (step 9c): what the map shows right now, computed in the browser from the detections it
 * received (already filtered to the country in the By Country view). Aggregated grid responses only carry
 * per-cell totals, so the breakdowns need individual detections.
 */
@Component({
  selector: 'app-summary',
  imports: [DecimalPipe],
  template: `
    @if (s(); as s) {
      <p class="scope">{{ scope() }}</p>
      <dl class="tiles">
        <div><dt>Detections</dt><dd>{{ s.count | number }}</dd></div>
        @if (s.harmonized) {
          <div><dt title="Sum of the VIIRS S-NPP-equivalent weights of the shown detections">VIIRS-eq.</dt><dd>≈{{ s.weight | number: '1.0-0' }}</dd></div>
        }
        <div><dt>Max FRP</dt><dd>{{ s.frpMax === null ? '—' : (s.frpMax | number: '1.0-0') + ' MW' }}</dd></div>
      </dl>
      @if (s.count === 0) {
        <p class="hint">Nothing to summarise for this date and filters.</p>
      } @else if (s.aggregated) {
        <p class="hint">{{ s.frpMean === null ? '' : 'Mean FRP ' + (s.frpMean | number: '1.0-1') + ' MW. ' }}Too many detections for a
          breakdown: zoom in or use a shorter window to see it by satellite, type and day/night.{{ s.border ? ' Grid cells are assigned to the country by their centre.' : '' }}</p>
      } @else {
        @for (g of s.groups; track g.title) {
          <h4 [id]="'sum-' + $index">{{ g.title }}</h4>
          <ul class="bars" [attr.aria-labelledby]="'sum-' + $index">
            @for (r of g.rows; track r.label) {
              <li [title]="r.label + ': ' + (r.n | number) + ' (' + (r.share * 100 | number: '1.0-1') + ' %)'">
                <span class="lab">{{ r.label }}</span>
                <span class="track" aria-hidden="true"><span class="bar" [style.width.%]="r.share * 100"></span></span>
                <span class="val">{{ r.n | number }}</span>
              </li>
            }
          </ul>
        }
        <h4>Fire radiative power</h4>
        <p class="frp">median <b>{{ s.frpMedian | number: '1.0-1' }}</b> · 90th pct. <b>{{ s.frpP90 | number: '1.0-1' }}</b> · max <b>{{ s.frpMax | number: '1.0-0' }}</b> MW</p>
        @if (s.inferred > 0) {
          <p class="hint">* {{ s.inferred | number }} of these detections have a calculated type (near-real-time data).</p>
        }
      }
    } @else {
      <p class="hint">Loading…</p>
    }
  `,
  styles: [`
    :host { display: block; }
    .scope { margin: 0 0 6px; color: var(--c-panel-muted); font-size: .72rem; }
    .tiles { display: flex; gap: 6px; margin: 0 0 4px; }
    .tiles div { flex: 1; min-width: 0; background: var(--c-panel-input); border-radius: 6px; padding: 5px 8px; }
    dt { font-size: .66rem; color: var(--c-panel-muted); }
    dd { margin: 0; font-size: .95rem; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    h4 { margin: 8px 0 3px; font-size: .7rem; font-weight: 600; color: var(--c-panel-muted); }
    .bars { list-style: none; margin: 0; padding: 0; }
    .bars li { display: grid; grid-template-columns: minmax(0, 44%) minmax(0, 1fr) auto; align-items: center; gap: 6px; padding: 1px 0; font-size: .74rem; }
    .lab { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .track { height: 8px; }
    .bar { display: block; height: 8px; min-width: 1px; background: var(--c-accent); border-radius: 0 4px 4px 0; }
    .val { font-variant-numeric: tabular-nums; text-align: right; min-width: 4.5ch; }
    .frp { margin: 0; font-size: .74rem; }
    .hint { margin: 4px 0 0; color: var(--c-panel-muted); font-size: .72rem; line-height: 1.35; }
  `],
})
export class SummaryComponent {
  readonly data = input<HotspotsResponse | null>(null);
  readonly scope = input('');
  /** True when grid cells were assigned to a country by their centre (aggregated + By Country). */
  readonly border = input(false);

  readonly s = computed(() => {
    const d = this.data();
    if (!d) return null;
    const harmonized = d.mode === 'harmonized';
    if (d.aggregated) {
      const count = d.n.reduce((a, b) => a + b, 0);
      const frpSum = d.frp_sum.reduce((a, b) => a + b, 0);
      return {
        aggregated: true, harmonized, count, weight: d.weight_sum,
        frpMax: d.frp_max.length ? Math.max(...d.frp_max) : null, frpMean: count ? frpSum / count : null,
        border: this.border(), groups: [] as { title: string; rows: Row[] }[], frpMedian: 0, frpP90: 0, inferred: 0,
      };
    }
    const n = d.lon.length;
    const tally = (keys: number[], labels: string[], keepZero = false): Row[] => {
      const c = new Array(labels.length).fill(0);
      for (const k of keys) if (k >= 0 && k < c.length) c[k]++;
      return labels.map((label, i) => ({ label, n: c[i], share: n ? c[i] / n : 0 })).filter((r) => keepZero || r.n > 0);
    };
    const frp = Float64Array.from(d.frp.map((x) => x ?? 0)).sort();
    const q = (p: number) => (frp.length ? frp[Math.min(frp.length - 1, Math.floor(p * (frp.length - 1) + 0.5))] : 0);
    return {
      aggregated: false, harmonized, count: n, weight: d.weight_sum,
      frpMax: frp.length ? frp[frp.length - 1] : null, frpMean: null, border: false,
      groups: [
        { title: 'By satellite', rows: tally(d.sat, d.sat_index.map((s) => SAT_LABELS[s] ?? s)) },
        { title: 'By type', rows: tally(d.type, TYPE_LABELS) },
        { title: 'Day / night', rows: tally(d.dn.map((x) => (x ? 0 : 1)), ['Day', 'Night'], true) },
      ],
      frpMedian: q(0.5), frpP90: q(0.9), inferred: d.ti.reduce((a, b) => a + b, 0),
    };
  });
}
