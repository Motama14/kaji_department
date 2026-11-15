// SI-assisted: written with Claude Code (Anthropic LLM) under the author's direction and reviewed by the author.
import { AfterViewInit, Component, ElementRef, OnDestroy, effect, inject, input, output, viewChild } from '@angular/core';
import * as maplibregl from 'maplibre-gl';
import type { GeoJSONSource, Map as MlMap, Popup } from 'maplibre-gl';
import { HotspotGrid, HotspotPoints, HotspotsResponse } from './viewer-api.service';
import { BaseLayer, ViewerStateService } from './viewer-state.service';
import { SAT_LABELS, TYPE_LABELS, frpColorExpr } from './frp';
import { CountriesService, Country } from '../countries.service';

const BASES: Record<BaseLayer, { tiles: string[]; attribution: string; maxzoom: number }> = {
  imagery: {
    tiles: ['https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}'],
    attribution: 'Imagery © Esri, Maxar, Earthstar Geographics, and the GIS User Community',
    maxzoom: 18,
  },
  night: {
    tiles: ['https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/VIIRS_Black_Marble/default/2016-01-01/GoogleMapsCompatible_Level8/{z}/{y}/{x}.png'],
    attribution: 'Black Marble: NASA Earth Observatory / GIBS (public domain)',
    maxzoom: 8,
  },
  streets: {
    tiles: ['https://tile.openstreetmap.org/{z}/{x}/{y}.png'],
    attribution: '© OpenStreetMap contributors',
    maxzoom: 19,
  },
};

// MapLibre 6 resolves its web worker relative to its own module URL. Under `ng serve`
// Vite serves the library from .angular/cache/.../deps/, where the worker does not exist,
// so GeoJSON sources never get processed (points silently missing). The worker and its
// shared chunk are copied to the site root as assets (angular.json), so point there.
maplibregl.setWorkerUrl(new URL('maplibre-gl-worker.mjs', document.baseURI).href);

const EMPTY: GeoJSON.FeatureCollection = { type: 'FeatureCollection', features: [] };
const M_PER_PX_Z0 = 156543.03;

/** Radius in px that follows the real pixel footprint, never below a zoom-dependent minimum. */
function footprintRadius(): any {
  const stops: [number, number][] = [[0, 2.4], [3, 2.8], [5, 3.2], [8, 3.4], [10, 3.4], [12, 3.4], [14, 3.4], [16, 3.4], [22, 3.4]];
  const e: any[] = ['interpolate', ['exponential', 2], ['zoom']];
  for (const [z, min] of stops) e.push(z, ['max', min, ['*', ['get', 'rc'], Math.pow(2, z)]]);
  return e;
}

/** Signed-distance-field icons for non-vegetation types (shape is not colour-only). */
function shapeIcon(kind: 'triangle' | 'square' | 'diamond'): ImageData {
  const s = 32;
  const c = document.createElement('canvas');
  c.width = c.height = s;
  const g = c.getContext('2d')!;
  g.fillStyle = '#000';
  g.beginPath();
  if (kind === 'triangle') { g.moveTo(16, 3); g.lineTo(29, 27); g.lineTo(3, 27); }
  if (kind === 'square') { g.rect(6, 6, 20, 20); }
  if (kind === 'diamond') { g.moveTo(16, 2); g.lineTo(30, 16); g.lineTo(16, 30); g.lineTo(2, 16); }
  g.closePath();
  g.fill();
  return g.getImageData(0, 0, s, s);
}

@Component({
  selector: 'app-map',
  template: `<div #el class="map" role="application" aria-label="World map of satellite hotspot detections"></div>`,
  styles: [`:host { display: block; position: absolute; inset: 0; } .map { position: absolute; inset: 0; }`],
})
export class MapComponent implements AfterViewInit, OnDestroy {
  readonly data = input<HotspotsResponse | null>(null);
  /** Country of the By Country view: the rest of the world is dimmed and its border outlined (step 9b). */
  readonly country = input<Country | null>(null);
  readonly ready = output<void>();
  /** Click on the map away from any detection (used to pick a country). */
  readonly mapClick = output<{ lon: number; lat: number }>();
  private el = viewChild.required<ElementRef<HTMLDivElement>>('el');
  private state = inject(ViewerStateService);
  private countries = inject(CountriesService);
  private map?: MlMap;
  private popup?: Popup;
  private loaded = false;

  constructor() {
    effect(() => {
      const d = this.data();
      if (this.loaded) this.render(d);
    });
    effect(() => {
      const c = this.country();
      if (this.loaded) this.drawCountry(c);
    });
    effect(() => {
      const base = this.state.baseLayer();
      if (!this.loaded || !this.map) return;
      for (const k of Object.keys(BASES)) this.map.setLayoutProperty(`base-${k}`, 'visibility', k === base ? 'visible' : 'none');
    });
  }

  ngAfterViewInit(): void {
    const v = this.state.view();
    const sources: any = {};
    // Map background from the global palette (src/theme.css)
    const bg = getComputedStyle(document.documentElement).getPropertyValue('--c-map-bg').trim() || '#0b0d12';
    const layers: any[] = [{ id: 'bg', type: 'background', paint: { 'background-color': bg } }];
    for (const [k, b] of Object.entries(BASES)) {
      sources[`base-${k}`] = { type: 'raster', tiles: b.tiles, tileSize: 256, attribution: b.attribution, maxzoom: b.maxzoom };
      layers.push({
        id: `base-${k}`, type: 'raster', source: `base-${k}`,
        layout: { visibility: k === this.state.baseLayer() ? 'visible' : 'none' },
        paint: k === 'imagery' ? { 'raster-brightness-max': 0.75, 'raster-saturation': -0.25 } : {},
      });
    }
    const map = new maplibregl.Map({
      container: this.el().nativeElement,
      style: { version: 8, sources, layers },
      center: [v.lon, v.lat],
      zoom: v.zoom,
      renderWorldCopies: true,
      attributionControl: false,
    });
    this.map = map;
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-left');
    map.addControl(new maplibregl.GeolocateControl({ positionOptions: { enableHighAccuracy: false } }), 'top-left');
    map.addControl(new maplibregl.ScaleControl({ unit: 'metric' }), 'bottom-left');
    map.addControl(new maplibregl.AttributionControl({ compact: true }), 'bottom-left');

    map.on('load', () => {
      const m = this.map!;
      m.addImage('shape-triangle', shapeIcon('triangle'), { sdf: true });
      m.addImage('shape-square', shapeIcon('square'), { sdf: true });
      m.addImage('shape-diamond', shapeIcon('diamond'), { sdf: true });
      m.addSource('pts', { type: 'geojson', data: EMPTY });
      m.addSource('grid', { type: 'geojson', data: EMPTY });
      // By Country: dim everything outside the selected country and outline its border (below the detections).
      const css = getComputedStyle(document.documentElement);
      m.addSource('mask', { type: 'geojson', data: EMPTY });
      m.addSource('country', { type: 'geojson', data: EMPTY });
      m.addLayer({ id: 'mask', type: 'fill', source: 'mask',
        paint: { 'fill-color': css.getPropertyValue('--c-mask').trim() || 'rgba(0,0,0,0.55)', 'fill-antialias': false } });
      m.addLayer({ id: 'country-line', type: 'line', source: 'country',
        layout: { 'line-join': 'round' },
        paint: { 'line-color': css.getPropertyValue('--c-country-line').trim() || '#ffffff', 'line-width': 1.6 } });
      m.addLayer({
        id: 'grid', type: 'circle', source: 'grid',
        paint: {
          'circle-color': frpColorExpr('frp_mean'),
          'circle-radius': ['interpolate', ['linear'], ['sqrt', ['get', 'n']], 1, 1.5, 10, 4, 40, 9, 120, 16],
          'circle-opacity': 0.85, 'circle-stroke-color': '#000', 'circle-stroke-width': 0.4,
        },
      });
      m.addLayer({
        id: 'pts-veg', type: 'circle', source: 'pts', filter: ['==', ['get', 'type'], 0],
        paint: {
          'circle-color': frpColorExpr(), 'circle-radius': footprintRadius(),
          'circle-stroke-color': 'rgba(0,0,0,0.55)', 'circle-stroke-width': 0.5,
          'circle-opacity': ['interpolate', ['linear'], ['zoom'], 8, 1, 12, 0.75],
        },
      });
      m.addLayer({
        id: 'pts-other', type: 'symbol', source: 'pts', filter: ['!=', ['get', 'type'], 0],
        layout: {
          'icon-image': ['match', ['get', 'type'], 1, 'shape-triangle', 2, 'shape-square', 'shape-diamond'],
          'icon-size': ['interpolate', ['linear'], ['zoom'], 1, 0.2, 6, 0.34, 12, 0.6],
          'icon-allow-overlap': true, 'icon-ignore-placement': true,
        },
        paint: { 'icon-color': frpColorExpr(), 'icon-halo-color': '#ffffff', 'icon-halo-width': ['interpolate', ['linear'], ['zoom'], 2, 0.8, 8, 1.6] },
      });
      for (const id of ['pts-veg', 'pts-other', 'grid']) {
        m.on('click', id, (e) => this.showPopup(e));
        m.on('mouseenter', id, () => (m.getCanvas().style.cursor = 'pointer'));
        m.on('mouseleave', id, () => (m.getCanvas().style.cursor = ''));
      }
      m.on('click', (e) => {
        const hit = m.queryRenderedFeatures(e.point, { layers: ['pts-veg', 'pts-other', 'grid'] });
        if (!hit.length) {
          const p = e.lngLat.wrap();
          this.mapClick.emit({ lon: p.lng, lat: p.lat });
        }
      });
      this.loaded = true;
      this.render(this.data());
      this.drawCountry(this.country());
      this.emitView();
      this.ready.emit();
    });
    map.on('moveend', () => this.emitView());
    map.on('mousemove', (e) => this.state.cursor.set({ lat: e.lngLat.lat, lon: e.lngLat.lng }));
    map.on('mouseout', () => this.state.cursor.set(null));
  }

  ngOnDestroy(): void {
    this.map?.remove();
  }

  flyTo(lat: number, lon: number, zoom: number): void {
    this.map?.flyTo({ center: [lon, lat], zoom, duration: 800 });
  }

  fitBbox(b: number[]): void {
    this.map?.fitBounds([[b[0], b[1]], [b[2], b[3]]], { padding: 40, duration: 800 });
  }

  private drawCountry(c: Country | null): void {
    const m = this.map!;
    (m.getSource('mask') as GeoJSONSource).setData(c ? this.countries.mask(c) : EMPTY);
    (m.getSource('country') as GeoJSONSource).setData(c ? { type: 'Feature', properties: {}, geometry: c.geometry } : EMPTY);
  }

  visibleBounds(): number[] | null {
    const b = this.map?.getBounds();
    return b ? [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()] : null;
  }

  private emitView(): void {
    const m = this.map!;
    const c = m.getCenter();
    const b = m.getBounds();
    this.state.view.set({
      lat: c.lat, lon: c.lng, zoom: m.getZoom(),
      bbox: [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()],
    });
  }

  private render(d: HotspotsResponse | null): void {
    const m = this.map!;
    const pts = m.getSource('pts') as GeoJSONSource;
    const grid = m.getSource('grid') as GeoJSONSource;
    this.popup?.remove();
    if (!d) {
      pts.setData(EMPTY);
      grid.setData(EMPTY);
      return;
    }
    if (d.aggregated) {
      pts.setData(EMPTY);
      grid.setData(this.gridFeatures(d));
    } else {
      grid.setData(EMPTY);
      pts.setData(this.pointFeatures(d));
    }
  }

  private pointFeatures(d: HotspotPoints): GeoJSON.FeatureCollection {
    const features: GeoJSON.Feature[] = new Array(d.lon.length);
    for (let i = 0; i < d.lon.length; i++) {
      // Half the along-scan pixel size, in metres, converted to px at zoom 0 at this latitude.
      const halfM = (d.scan[i] * 1000) / 2;
      const rc = halfM / (M_PER_PX_Z0 * Math.max(0.05, Math.cos((d.lat[i] * Math.PI) / 180)));
      features[i] = {
        type: 'Feature',
        geometry: { type: 'Point', coordinates: [d.lon[i], d.lat[i]] },
        properties: { i, frp: d.frp[i], type: d.type[i], rc },
      };
    }
    return { type: 'FeatureCollection', features };
  }

  private gridFeatures(d: HotspotGrid): GeoJSON.FeatureCollection {
    return {
      type: 'FeatureCollection',
      features: d.lon.map((lon, i) => ({
        type: 'Feature',
        geometry: { type: 'Point', coordinates: [lon, d.lat[i]] },
        properties: { i, n: d.n[i], frp_mean: d.frp_sum[i] / Math.max(1, d.n[i]) },
      })),
    };
  }

  private showPopup(e: any): void {
    const d = this.data();
    const f = e.features?.[0];
    if (!d || !f) return;
    const i = f.properties.i as number;
    let html: string;
    if (d.aggregated) {
      html = `<strong>${d.n[i].toLocaleString()} detections</strong> in a ${d.cell_deg}° cell<br>
        ${d.from === d.to ? d.from : `${d.from} → ${d.to}`}<br>
        Max FRP ${d.frp_max[i]} MW · mean ${(d.frp_sum[i] / d.n[i]).toFixed(1)} MW<br>
        ${d.mode === 'harmonized' ? `≈ ${d.w[i].toLocaleString()} VIIRS-equivalent detections<br>` : ''}
        <span class="muted">Zoom in to see individual detections</span>`;
    } else {
      const sat = d.sat_index[d.sat[i]];
      const conf = d.conf_index[d.conf[i]] ?? 'n/a';
      const harm = d.mode === 'harmonized'
        ? d.cal[i]
          ? `<b>Harmonized</b>: counts as ${d.w[i].toFixed(2)} VIIRS-equivalent detection${d.w[i] === 1 ? '' : 's'}`
          : `<b>Raw</b> (not part of the burn calendar: ${d.type[i] !== 0 ? 'non-vegetation' : 'low confidence or excluded'})`
        : `<b>Raw</b> detection${d.cal[i] ? ` · harmonized weight ${d.w[i].toFixed(2)}` : ''}`;
      html = `<strong>${d.t[i]} UTC</strong><br>
        ${SAT_LABELS[sat] ?? sat} <span class="muted">(${d.version[i] ? 'v' + d.version[i] : ''})</span><br>
        ${TYPE_LABELS[d.type[i]] ?? 'Unknown type'}${d.ti[i] ? '*' : ''} · ${d.dn[i] ? 'Day' : 'Night'}<br>
        FRP <b>${d.frp[i]} MW</b> · confidence ${conf}<br>
        Pixel ${d.scan[i]} × ${d.track[i]} km<br>
        ${d.lat[i].toFixed(4)}, ${d.lon[i].toFixed(4)}<br>
        ${harm}
        ${d.ti[i] ? `<br><span class="muted">* Type calculated from this location's history: near-real-time data has no type.</span>` : ''}`;
    }
    this.popup?.remove();
    this.popup = new maplibregl.Popup({ closeButton: true, maxWidth: '280px', className: 'hs-popup' })
      .setLngLat(f.geometry.coordinates)
      .setHTML(html)
      .addTo(this.map!);
  }
}
