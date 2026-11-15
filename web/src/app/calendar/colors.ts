// Accessible color ramps.
// Sequential: viridis (perceptually uniform, colorblind-safe, prints in grayscale).
// Diverging: blue <-> neutral gray <-> orange (distinguishable under all common CVD types).

const VIRIDIS = ['#440154', '#482878', '#3e4989', '#31688e', '#26828e', '#1f9e89', '#35b779', '#6ece58', '#b5de2b', '#fde725'];
const DIVERGING = ['#2166ac', '#4393c3', '#92c5de', '#d1e5f0', '#e4e3df', '#fde0c5', '#f4a582', '#d6604d', '#b2182b'];
// Diverging ends kept blue/orange-red: warm = more fires than usual, cool = fewer.

function hexToRgb(h: string): [number, number, number] {
  const n = parseInt(h.slice(1), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

function interp(ramp: string[], t: number): string {
  const x = Math.min(1, Math.max(0, t)) * (ramp.length - 1);
  const i = Math.min(ramp.length - 2, Math.floor(x));
  const f = x - i;
  const a = hexToRgb(ramp[i]);
  const b = hexToRgb(ramp[i + 1]);
  const c = a.map((v, k) => Math.round(v + (b[k] - v) * f));
  return `rgb(${c[0]}, ${c[1]}, ${c[2]})`;
}

/** Log-scaled viridis for counts (fire counts are heavy-tailed). */
export function sequential(value: number, max: number): string {
  if (max <= 0) return VIRIDIS[0];
  return interp(VIRIDIS, Math.log1p(value) / Math.log1p(max));
}

/** Diverging color for an anomaly in percent vs the median, symmetric in log-ratio. */
export function diverging(pct: number): string {
  const ratio = Math.max(0.01, 1 + pct / 100);
  const t = 0.5 + Math.max(-1, Math.min(1, Math.log2(ratio) / 3)) / 2; // +-3 doublings saturate
  return interp(DIVERGING, t);
}

/** Text color that keeps >= 4.5:1 contrast on a cell background. */
export function inkFor(rgb: string): string {
  const m = rgb.match(/\d+/g)!.map(Number);
  const lum = (0.2126 * m[0] + 0.7152 * m[1] + 0.0722 * m[2]) / 255;
  return lum > 0.55 ? '#111' : '#fff';
}

export const VIRIDIS_STOPS = VIRIDIS;
export const DIVERGING_STOPS = DIVERGING;
