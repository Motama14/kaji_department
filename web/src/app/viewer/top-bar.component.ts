// SI-assisted: written with Claude Code (Anthropic LLM) under the author's direction and reviewed by the author.
import { Component, computed, inject, input } from '@angular/core';
import { DecimalPipe } from '@angular/common';
import { ViewerStateService, ViewMode } from './viewer-state.service';

/**
 * Top bar of the design sketches: the Global | By Country switch on the left and, in the centre, a live
 * line describing exactly what the map shows right now (dates, count, mode, cursor position).
 */
@Component({
  selector: 'app-top-bar',
  imports: [DecimalPipe],
  template: `
    <div class="switch" role="radiogroup" aria-label="View">
      @for (m of modes; track m[0]) {
        <button type="button" role="radio" [attr.aria-checked]="state.viewMode() === m[0]" [class.on]="state.viewMode() === m[0]"
                (click)="setMode(m[0])">{{ m[1] }}</button>
      }
    </div>
    <p class="info" aria-live="polite">
      @if (loading()) { <span class="spin" aria-label="Loading"></span> }
      <span title="Dates shown on the map (UTC)">{{ rangeText() }} UTC</span>
      @if (state.countryBox(); as cb) {
        <span class="sep" aria-hidden="true">·</span>
        <span title="Country shown (By Country view)">{{ cb.name }}</span>
      }
      <span class="sep" aria-hidden="true">·</span>
      <span title="Detections shown with the current filters"><b>{{ state.visibleCount() === null ? '—' : (state.visibleCount() | number) }}</b> hotspots</span>
      @if (state.filters().mode === 'harmonized' && state.weightSum()) {
        <span class="sep opt" aria-hidden="true">·</span>
        <span class="opt" title="Sum of VIIRS-equivalent weights of the shown detections">≈{{ state.weightSum() | number: '1.0-0' }} VIIRS-eq.</span>
      }
      <span class="sep opt" aria-hidden="true">·</span>
      <span class="opt">{{ state.filters().mode === 'harmonized' ? 'Harmonized' : 'Raw' }}</span>
      <span class="sep coords-sep" aria-hidden="true">·</span>
      <span class="coords" title="Coordinates under the cursor">
        {{ state.cursor() ? (state.cursor()!.lat | number: '1.3-3') + ', ' + (state.cursor()!.lon | number: '1.3-3') : 'lat, lon' }}
      </span>
    </p>
  `,
  styles: [`
    :host { display: flex; align-items: center; gap: 24px; padding: 0 24px; height: 60px; background: var(--c-bar); color: var(--c-bar-ink); flex: none; }
    .switch { display: flex; background: var(--c-toggle-track); border-radius: 999px; padding: 0; flex: none; }
    .switch button { font: inherit; font-size: 1.05rem; background: transparent; color: var(--c-toggle-ink); border: 0; border-radius: 999px; padding: 5px 16px; cursor: pointer; }
    .switch button:first-child { padding-left: 24px; }
    .switch button.on { background: var(--c-toggle-on); color: var(--c-toggle-on-ink); }
    .switch button:focus-visible { outline: 2px solid var(--c-focus); outline-offset: 2px; }
    .info { flex: 1; margin: 0; display: flex; gap: 8px; justify-content: center; align-items: center; white-space: nowrap; overflow: hidden; font-size: 1.05rem; letter-spacing: .01em; }
    .sep { color: var(--c-bar-muted); }
    .coords { font-variant-numeric: tabular-nums; min-width: 150px; color: var(--c-bar-muted); }
    .spin { width: 12px; height: 12px; border: 2px solid var(--c-bar-muted); border-top-color: var(--c-bar-ink); border-radius: 50%; animation: s .7s linear infinite; flex: none; }
    @keyframes s { to { transform: rotate(360deg); } }
    @media (max-width: 1100px) { .info { font-size: .85rem; } .coords, .coords-sep { display: none; } }
    @media (max-width: 700px) { :host { gap: 10px; padding: 0 10px; height: 52px; } .switch button { font-size: .85rem; padding: 4px 10px; } .info { justify-content: flex-end; font-size: .78rem; gap: 5px; } .opt { display: none; } }
  `],
})
export class TopBarComponent {
  readonly state = inject(ViewerStateService);
  readonly loading = input(false);
  readonly modes: [ViewMode, string][] = [['global', 'Global'], ['country', 'By Country']];
  readonly rangeText = computed(() => {
    const [a, b] = this.state.range();
    const f = (s: string) => (s ? `${s.slice(8, 10)}/${s.slice(5, 7)}/${s.slice(0, 4)}` : '—');
    return a === b ? f(a) : `${f(a)} – ${f(b)}`;
  });

  setMode(m: ViewMode): void {
    this.state.viewMode.set(m);
  }
}
