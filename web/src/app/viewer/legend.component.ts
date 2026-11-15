// SI-assisted: written with Claude Code (Anthropic LLM) under the author's direction and reviewed by the author.
import { Component, input } from '@angular/core';
import { FRP_STOPS, TYPE_LABELS, frpGradient } from './frp';

@Component({
  selector: 'app-legend',
  template: `
    <div class="ramp" [style.background]="gradient" role="img" aria-label="FRP colour scale from 1 to 1000 megawatts, log scale"></div>
    <div class="ticks">@for (s of stops; track s[0]) { <span>{{ s[0] }}</span> }</div>
    <p class="cap">{{ aggregated() ? 'Mean FRP per cell (MW). Circle size = number of detections.' : 'Fire radiative power (MW), log scale' }}</p>
    <ul>
      @for (t of types; track t; let i = $index) {
        <li><svg width="13" height="13" viewBox="0 0 32 32" aria-hidden="true">
          @switch (i) {
            @case (0) { <circle cx="16" cy="16" r="11" class="veg" /> }
            @case (1) { <path d="M16 3 L29 27 L3 27 Z" /> }
            @case (2) { <rect x="6" y="6" width="20" height="20" /> }
            @default { <path d="M16 2 L30 16 L16 30 L2 16 Z" /> }
          }
        </svg>{{ t }}</li>
      }
    </ul>
    <p class="cap">Zoomed in, dots are drawn at the real pixel footprint (VIIRS 375 m, MODIS ≥ 1 km).</p>
  `,
  styles: [`
    .ramp { height: 10px; border-radius: 3px; }
    .ticks { display: flex; justify-content: space-between; font-size: .68rem; color: var(--c-panel-muted); margin-top: 2px; }
    .cap { margin: 4px 0; font-size: .7rem; color: var(--c-panel-muted); }
    ul { list-style: none; padding: 0; margin: 6px 0; display: grid; grid-template-columns: 1fr 1fr; gap: 3px 8px; font-size: .74rem; }
    li { display: flex; align-items: center; gap: 5px; }
    svg { fill: #fd8d3c; stroke: #fff; stroke-width: 3; flex: none; }
    svg .veg { stroke: rgba(0,0,0,.6); stroke-width: 1.5; }
  `],
})
export class LegendComponent {
  readonly aggregated = input(false);
  readonly gradient = frpGradient();
  readonly stops = FRP_STOPS;
  readonly types = TYPE_LABELS;
}
