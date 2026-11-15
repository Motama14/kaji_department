// FRP (fire radiative power, MW) colour ramp: YlOrRd, sequential and colour-blind safe
// (monotonic lightness: readable for all CVD types and in grayscale), on a log scale.
export const FRP_STOPS: [number, string][] = [
  [1, '#ffffb2'],
  [5, '#fed976'],
  [15, '#feb24c'],
  [40, '#fd8d3c'],
  [100, '#fc4e2a'],
  [300, '#e31a1c'],
  [1000, '#b10026'],
];

export const TYPE_LABELS = ['Vegetation fire', 'Active volcano', 'Other static land source', 'Offshore'];
export const TYPE_SHAPES = ['circle', 'triangle', 'square', 'diamond'] as const;
export const SAT_LABELS: Record<string, string> = {
  Terra: 'MODIS · Terra',
  Aqua: 'MODIS · Aqua',
  N: 'VIIRS · Suomi NPP',
  N20: 'VIIRS · NOAA-20',
  N21: 'VIIRS · NOAA-21',
};

/** MapLibre expression: colour from a numeric FRP property. */
export function frpColorExpr(prop = 'frp'): any {
  const e: any[] = ['interpolate', ['linear'], ['ln', ['max', 1, ['get', prop]]]];
  for (const [v, c] of FRP_STOPS) e.push(Math.log(v), c);
  return e;
}

export function frpGradient(): string {
  return `linear-gradient(to right, ${FRP_STOPS.map((s) => s[1]).join(',')})`;
}
