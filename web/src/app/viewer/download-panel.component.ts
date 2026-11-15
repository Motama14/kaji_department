// SI-assisted: written with Claude Code (Anthropic LLM) under the author's direction and reviewed by the author.
import { Component, computed, inject, input, signal } from '@angular/core';
import { DecimalPipe } from '@angular/common';
import { ViewerApiService } from './viewer-api.service';
import { ViewerStateService } from './viewer-state.service';

type Area = 'visible' | 'world';

/**
 * "Download Data" sidebar (global view): the detections of the dates on the map as CSV (existing
 * POST /api/hotspots.csv, area + dates only) and the counts drawn in the timeline as CSV (client side).
 */
@Component({
  selector: 'app-download-panel',
  imports: [DecimalPipe],
  template: `
    <section aria-labelledby="h-dl-pts">
      <h3 id="h-dl-pts">Hotspot detections (CSV)</h3>
      <p class="what">{{ rangeText() }} · {{ area() === 'visible' ? 'visible map area' : 'whole world' }}</p>
      <div class="seg" role="radiogroup" aria-label="Area to download">
        <button type="button" role="radio" [attr.aria-checked]="area() === 'visible'" [class.on]="area() === 'visible'" (click)="area.set('visible')">Visible area</button>
        <button type="button" role="radio" [attr.aria-checked]="area() === 'world'" [class.on]="area() === 'world'" (click)="area.set('world')">Whole world</button>
      </div>
      <label class="chk"><input type="checkbox" [checked]="onlyCal()" (change)="onlyCal.set($any($event.target).checked)" />
        Only detections used in the burn calendar</label>
      <button type="button" class="wide primary" (click)="downloadPoints()" [disabled]="busy() || !state.date()">
        {{ busy() ? 'Preparing…' : 'Download detections' }}</button>
      <p class="hint">Every detection of these dates and area, with sensor, type source and VIIRS-equivalent weight.
        The sidebar filters (type, confidence, FRP…) are not applied. Up to 1,000,000 rows.</p>
      @if (msg()) { <p class="msg" role="alert">{{ msg() }}</p> }
    </section>

    <section aria-labelledby="h-dl-tl">
      <h3 id="h-dl-tl">Timeline counts (CSV)</h3>
      <p class="what">
        @if (state.timelineHist(); as h) {
          {{ h.t.length | number }} {{ h.granularity === 'day' ? 'days' : 'months' }} · {{ h.t[0] }} → {{ h.t[h.t.length - 1] }}
        } @else { Waiting for the timeline… }
      </p>
      <button type="button" class="wide" (click)="downloadCounts()" [disabled]="!state.timelineHist()?.t?.length">Download counts</button>
      <p class="hint">The bars of the timeline: detections (and VIIRS-equivalent in harmonized mode) per day or month, with the current filters.</p>
    </section>
  `,
  styles: [`
    :host { display: block; padding: 0 14px 16px; font-size: .8rem; }
    section { border-top: 1px solid var(--c-panel-line); padding: 8px 0; }
    h3 { margin: 0 0 4px; font-size: .74rem; font-weight: 600; text-transform: uppercase; letter-spacing: .04em; color: var(--c-panel-muted); }
    .what { margin: 0 0 6px; font-variant-numeric: tabular-nums; }
    .hint { margin: 6px 0 0; color: var(--c-panel-muted); font-size: .72rem; line-height: 1.35; }
    .msg { margin: 6px 0 0; color: var(--c-err); font-size: .75rem; }
    .chk { display: flex; align-items: center; gap: 6px; padding: 6px 0 2px; cursor: pointer; }
    .seg { display: grid; grid-template-columns: 1fr 1fr; gap: 4px; }
    button { font: inherit; font-size: .78rem; background: var(--c-panel-input); color: var(--c-panel-ink); border: 1px solid var(--c-panel-line); border-radius: 6px; padding: 4px 8px; cursor: pointer; }
    button.on, button.primary { background: var(--c-accent); border-color: var(--c-accent); color: var(--c-accent-ink); font-weight: 600; }
    button:disabled { opacity: .55; cursor: default; }
    button.wide { width: 100%; margin-top: 8px; }
    button:focus-visible, input:focus-visible { outline: 2px solid var(--c-focus); outline-offset: 1px; }
  `],
})
export class DownloadPanelComponent {
  readonly visibleBbox = input<number[] | null>(null);
  readonly state = inject(ViewerStateService);
  private api = inject(ViewerApiService);

  readonly area = signal<Area>('visible');
  readonly onlyCal = signal(false);
  readonly busy = signal(false);
  readonly msg = signal<string | null>(null);
  readonly rangeText = computed(() => {
    const [a, b] = this.state.range();
    return a === b ? a : `${a} → ${b}`;
  });

  downloadPoints(): void {
    const [a, b] = this.state.range();
    const bbox = this.area() === 'visible' ? this.visibleBbox() : null;
    this.busy.set(true);
    this.msg.set(null);
    this.api.hotspotsCsv(bbox, a, b, this.onlyCal()).subscribe({
      next: (blob) => {
        this.busy.set(false);
        save(blob, `hotspots_${a}${a === b ? '' : '_' + b}${bbox ? '_area' : '_world'}.csv`);
      },
      error: async (e) => {
        this.busy.set(false);
        let detail = e?.message ?? 'Download failed';
        try { detail = JSON.parse(await (e.error as Blob).text()).detail ?? detail; } catch { /* not JSON */ }
        this.msg.set(e?.status === 413 ? `Too many rows: ${detail} Zoom in or pick a shorter window.` : detail);
      },
    });
  }

  downloadCounts(): void {
    const h = this.state.timelineHist();
    if (!h) return;
    const harm = h.mode === 'harmonized';
    const rows = [harm ? `${h.granularity},detections,viirs_equivalent` : `${h.granularity},detections`];
    h.t.forEach((t, i) => rows.push(harm ? `${t},${h.n[i]},${h.v[i]}` : `${t},${h.n[i]}`));
    save(new Blob([rows.join('\n') + '\n'], { type: 'text/csv' }),
      `timeline_${h.granularity}_${h.t[0]}_${h.t[h.t.length - 1]}_${h.mode}.csv`);
  }
}

function save(blob: Blob, name: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
