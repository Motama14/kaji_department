// SI-assisted: written with Claude Code (Anthropic LLM) under the author's direction and reviewed by the author.
import { AfterViewInit, Component, ElementRef, HostListener, OnDestroy, computed, effect, inject, input, signal, untracked, viewChild } from '@angular/core';
import { Subscription } from 'rxjs';
import { AvailableDates, Histogram, ViewerApiService } from './viewer-api.service';
import { Window, ViewerStateService, addDays, addMonths, fmtDate, parseDate, withYearMonth } from './viewer-state.service';

const DAY = 86_400_000;
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const H = 100; // histogram band height (px)
const AX = 52; // date axis height under the band (px)

interface Tick { x: number; major: boolean; label?: string; }

@Component({
  selector: 'app-timeline',
  templateUrl: './timeline.component.html',
  styleUrl: './timeline.component.css',
})
export class TimelineComponent implements AfterViewInit, OnDestroy {
  readonly Math = Math;
  readonly available = input.required<AvailableDates>();
  /** Leave room at the right of the toolbar for the download sidebar's collapse tab (global view). */
  readonly reserveRight = input(false);
  private state = inject(ViewerStateService);
  private api = inject(ViewerApiService);
  private svgBox = viewChild.required<ElementRef<HTMLDivElement>>('box');

  readonly months = MONTHS;
  readonly speeds = [1, 2, 4, 8];
  readonly H = H;
  readonly AX = AX;
  readonly pickerOpen = signal(false);
  readonly date = this.state.date;
  readonly window = this.state.window;

  readonly width = signal(800);
  readonly vs = signal(0); // visible start (ms, UTC)
  readonly ve = signal(0); // visible end (ms)
  readonly hist = signal<Histogram | null>(null);
  readonly playing = signal(false);
  readonly speed = signal(2);
  readonly typed = signal('');
  readonly loadingHist = signal(false);
  readonly countryBox = this.state.countryBox;

  readonly year = computed(() => (this.date() ? parseDate(this.date()).getUTCFullYear() : 0));
  readonly month = computed(() => (this.date() ? parseDate(this.date()).getUTCMonth() + 1 : 1));
  readonly firstMs = computed(() => parseDate(this.available().first).getTime());
  readonly lastMs = computed(() => parseDate(this.available().last).getTime());
  readonly spanDays = computed(() => (this.ve() - this.vs()) / DAY);
  readonly granularity = computed<'day' | 'month'>(() => (this.spanDays() <= 450 ? 'day' : 'month'));

  readonly bars = computed(() => {
    const h = this.hist();
    if (!h || !h.t.length) return [];
    const max = Math.max(...h.v, 1);
    return h.t.map((t, i) => {
      const s = parseDate(t).getTime();
      const e = h.granularity === 'day' ? s + DAY : parseDate(addMonths(t, 1)).getTime();
      const x0 = this.x(s), x1 = this.x(e);
      const bh = Math.max(1, (Math.sqrt(h.v[i]) / Math.sqrt(max)) * (H - 6));
      return { x: x0, w: Math.max(0.6, x1 - x0 - (x1 - x0 > 3 ? 1 : 0)), h: bh, v: h.v[i], n: h.n[i], t };
    }).filter((b) => b.x + b.w >= 0 && b.x <= this.width());
  });

  readonly ticks = computed<Tick[]>(() => {
    const vs = this.vs(), ve = this.ve(), W = this.width();
    if (!W || ve <= vs) return [];
    const pxDay = W / ((ve - vs) / DAY);
    const out: Tick[] = [];
    const d0 = new Date(vs), d1 = new Date(ve);
    if (pxDay >= 5) {
      for (let t = Date.UTC(d0.getUTCFullYear(), d0.getUTCMonth(), d0.getUTCDate()); t <= ve; t += DAY) {
        const d = new Date(t);
        const first = d.getUTCDate() === 1;
        const label = first ? `${MONTHS[d.getUTCMonth()]}${d.getUTCMonth() === 0 ? ' ' + d.getUTCFullYear() : ''}`
          : pxDay >= 26 || (pxDay >= 12 && d.getUTCDate() % 5 === 0) ? String(d.getUTCDate()) : undefined;
        out.push({ x: this.x(t), major: first, label });
      }
      return out;
    }
    const pxMonth = pxDay * 30.4;
    if (pxMonth >= 3) {
      for (let y = d0.getUTCFullYear(), m = d0.getUTCMonth(); Date.UTC(y, m, 1) <= ve; m++) {
        if (m > 11) { m = 0; y++; }
        const t = Date.UTC(y, m, 1);
        if (t < vs) continue;
        const jan = m === 0;
        const label = jan ? String(y) : pxMonth >= 30 ? MONTHS[m] : undefined;
        out.push({ x: this.x(t), major: jan, label });
      }
      return out;
    }
    const pxYear = pxDay * 365;
    const step = pxYear >= 30 ? 1 : pxYear >= 12 ? 2 : 5;
    for (let y = d0.getUTCFullYear(); y <= d1.getUTCFullYear() + 1; y++) {
      const t = Date.UTC(y, 0, 1);
      if (t < vs || t > ve) continue;
      out.push({ x: this.x(t), major: y % step === 0, label: y % step === 0 ? String(y) : undefined });
    }
    return out;
  });

  readonly selX = computed(() => (this.date() ? this.x(parseDate(this.date()).getTime() + DAY / 2) : -10));
  readonly winRect = computed(() => {
    const [a, b] = this.state.range();
    if (!a) return null;
    const x0 = this.x(parseDate(a).getTime()), x1 = this.x(parseDate(b).getTime() + DAY);
    return { x: x0, w: Math.max(2, x1 - x0) };
  });
  readonly noDataLeft = computed(() => Math.max(0, Math.min(this.width(), this.x(this.firstMs()))));
  readonly noDataRight = computed(() => Math.max(0, Math.min(this.width(), this.x(this.lastMs() + DAY))));
  /** "From … to …" text of the time picker: the stretch the timeline shows, clamped to the data. */
  readonly visibleFrom = computed(() => fmtDate(new Date(Math.max(this.firstMs(), this.vs()))));
  readonly visibleTo = computed(() => fmtDate(new Date(Math.min(this.lastMs(), this.ve()))));
  readonly pickerText = computed(() => `From ${dmy(this.visibleFrom())} to ${dmy(this.visibleTo())}`);
  readonly isLatest = computed(() => this.date() === this.available().last);
  readonly markerVisible = computed(() => this.selX() >= 0 && this.selX() <= this.width());
  readonly dateLabel = computed(() => {
    const d = this.date();
    if (!d) return '';
    const [y, m, dd] = d.split('-');
    return `${dd}/${m}/${y}`;
  });

  private ro?: ResizeObserver;
  private timer?: ReturnType<typeof setInterval>;
  private histSub?: Subscription;
  private histTimer?: ReturnType<typeof setTimeout>;
  private anim?: number;
  private drag: { x: number; vs: number; ve: number; moved: boolean } | null = null;
  private initialised = false;

  constructor() {
    // Initial window: ~12 months centred on the selected date.
    effect(() => {
      const d = this.date();
      if (!d || this.initialised) return;
      this.initialised = true;
      const c = parseDate(d).getTime() + DAY / 2;
      this.vs.set(c - 183 * DAY);
      this.ve.set(c + 183 * DAY);
    });
    // Keep the selected day in view (prev/next/play): shift when it leaves the visible range.
    effect(() => {
      const d = this.date();
      if (!d || !this.initialised) return;
      untracked(() => {
        const t = parseDate(d).getTime();
        const span = this.ve() - this.vs();
        if (t < this.vs() + span * 0.03 || t + DAY > this.ve() - span * 0.03) this.centerOn(d, true);
      });
    });
    effect(() => this.typed.set(this.dateLabel()));
    // Histogram for the visible range (debounced).
    effect(() => {
      const vs = this.vs(), ve = this.ve(), g = this.granularity(), f = this.state.filters();
      // By Country: counts inside the country's box (the API filters by box; the map cuts to the border).
      const cb = this.state.countryBox();
      const bbox = cb ? cb.bbox : f.region === 'visible' ? this.state.view().bbox : null;
      if (!ve) return;
      clearTimeout(this.histTimer);
      this.histTimer = setTimeout(() => {
        const pad = (ve - vs) * 0.25;
        const from = fmtDate(new Date(Math.max(this.firstMs(), vs - pad)));
        const to = fmtDate(new Date(Math.min(this.lastMs(), ve + pad)));
        // snap to month boundaries for monthly bars so cached responses are reused
        const a = g === 'month' ? from.slice(0, 8) + '01' : from;
        this.histSub?.unsubscribe();
        this.loadingHist.set(true);
        this.histSub = this.api.histogram(a, to, g, f, bbox).subscribe({
          next: (h) => { this.hist.set(h); this.state.timelineHist.set(h); this.loadingHist.set(false); },
          error: () => this.loadingHist.set(false),
        });
      }, 180);
    });
  }

  ngAfterViewInit(): void {
    this.ro = new ResizeObserver((e) => this.width.set(e[0].contentRect.width));
    this.ro.observe(this.svgBox().nativeElement);
  }

  ngOnDestroy(): void {
    this.ro?.disconnect();
    this.stop();
    this.histSub?.unsubscribe();
    cancelAnimationFrame(this.anim ?? 0);
  }

  x(t: number): number {
    return ((t - this.vs()) / Math.max(1, this.ve() - this.vs())) * this.width();
  }

  private tAt(px: number): number {
    return this.vs() + (px / this.width()) * (this.ve() - this.vs());
  }

  // ------------------------------------------------------------------ navigation
  setDate(d: string): void {
    const t = Math.min(this.lastMs(), Math.max(this.firstMs(), parseDate(d).getTime()));
    this.state.date.set(fmtDate(new Date(t)));
  }

  step(days: number): void {
    this.setDate(addDays(this.date(), days));
  }

  stepMonth(n: number): void {
    this.setDate(addMonths(this.date(), n));
  }

  latest(): void {
    this.jump(this.available().last);
  }

  /** Jump to a remote date: recentre the timeline with a short animation. */
  jump(d: string): void {
    this.setDate(d);
    this.centerOn(this.date(), true);
  }

  onYear(y: string): void {
    this.jump(withYearMonth(this.date(), +y, this.month()));
  }

  onMonth(m: string): void {
    this.jump(withYearMonth(this.date(), this.year(), +m));
  }

  onTyped(v: string): void {
    const m = v.trim().match(/^(\d{1,2})[\/\-.](\d{1,2})[\/\-.](\d{4})$/);
    if (!m) {
      this.typed.set(this.dateLabel());
      return;
    }
    const d = `${m[3]}-${m[2].padStart(2, '0')}-${m[1].padStart(2, '0')}`;
    if (isNaN(parseDate(d).getTime())) return;
    this.jump(d);
  }

  onPicker(v: string): void {
    if (v) this.jump(v);
  }

  setWindow(w: string): void {
    this.state.window.set(w as Window);
  }

  togglePlay(): void {
    this.playing() ? this.stop() : this.play();
  }

  play(): void {
    this.stop();
    if (this.date() >= this.available().last) this.setDate(this.available().first);
    this.playing.set(true);
    this.timer = setInterval(() => {
      if (this.date() >= this.available().last) return this.stop();
      this.step(this.window() === 'month' ? 31 : this.window() === '7d' ? 7 : 1);
    }, 1000 / this.speed());
  }

  stop(): void {
    clearInterval(this.timer);
    this.playing.set(false);
  }

  setSpeed(s: string): void {
    this.speed.set(+s);
    if (this.playing()) this.play();
  }

  centerOn(d: string, animate: boolean): void {
    const span = this.ve() - this.vs();
    const target = parseDate(d).getTime() + DAY / 2 - span / 2;
    cancelAnimationFrame(this.anim ?? 0);
    if (!animate) {
      this.vs.set(target);
      this.ve.set(target + span);
      return;
    }
    const from = this.vs(), t0 = performance.now(), dur = 350;
    const tick = (now: number) => {
      const k = Math.min(1, (now - t0) / dur);
      const e = 1 - Math.pow(1 - k, 3);
      const s = from + (target - from) * e;
      this.vs.set(s);
      this.ve.set(s + span);
      if (k < 1) this.anim = requestAnimationFrame(tick);
    };
    this.anim = requestAnimationFrame(tick);
  }

  zoom(factor: number, anchorPx = this.width() / 2): void {
    const a = this.tAt(anchorPx);
    const span = this.ve() - this.vs();
    const maxSpan = this.lastMs() - this.firstMs() + 730 * DAY;
    const ns = Math.min(maxSpan, Math.max(14 * DAY, span * factor));
    const k = (a - this.vs()) / span;
    this.vs.set(a - ns * k);
    this.ve.set(a - ns * k + ns);
  }

  // ------------------------------------------------------------------ time picker
  togglePicker(): void {
    this.pickerOpen.update((v) => !v);
  }

  /** Show the last `days` days of data on the timeline (or everything), keeping the selected day. */
  preset(days: number | 'all'): void {
    const end = this.lastMs() + DAY;
    const start = days === 'all' ? this.firstMs() : Math.max(this.firstMs(), end - days * DAY);
    this.setVisible(start, end);
    if (this.date() && parseDate(this.date()).getTime() < start) this.setDate(fmtDate(new Date(start)));
  }

  onVisibleFrom(v: string): void {
    if (!v) return;
    const s = parseDate(v).getTime();
    if (!isNaN(s)) this.setVisible(s, Math.max(this.ve(), s + 14 * DAY));
  }

  onVisibleTo(v: string): void {
    if (!v) return;
    const e = parseDate(v).getTime() + DAY;
    if (!isNaN(e)) this.setVisible(Math.min(this.vs(), e - 14 * DAY), e);
  }

  private setVisible(start: number, end: number): void {
    cancelAnimationFrame(this.anim ?? 0);
    const pad = (end - start) * 0.02;
    this.vs.set(start - pad);
    this.ve.set(end + pad);
  }

  @HostListener('document:pointerdown', ['$event'])
  onDocDown(e: PointerEvent): void {
    if (this.pickerOpen() && !(e.target as HTMLElement)?.closest?.('.picker-wrap')) this.pickerOpen.set(false);
  }

  @HostListener('document:keydown.escape')
  onEsc(): void {
    this.pickerOpen.set(false);
  }

  // ------------------------------------------------------------------ pointer
  onWheel(e: WheelEvent): void {
    e.preventDefault();
    const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
    this.zoom(Math.pow(1.15, Math.sign(e.deltaY) * Math.min(3, Math.abs(e.deltaY) / 60 || 1)), e.clientX - rect.left);
  }

  onDown(e: PointerEvent): void {
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
    this.drag = { x: e.clientX, vs: this.vs(), ve: this.ve(), moved: false };
  }

  onMove(e: PointerEvent): void {
    if (!this.drag) return;
    const dx = e.clientX - this.drag.x;
    if (Math.abs(dx) > 3) this.drag.moved = true;
    if (!this.drag.moved) return;
    const shift = (dx / this.width()) * (this.drag.ve - this.drag.vs);
    this.vs.set(this.drag.vs - shift);
    this.ve.set(this.drag.ve - shift);
  }

  onUp(e: PointerEvent): void {
    const d = this.drag;
    this.drag = null;
    if (!d || d.moved) return;
    const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
    const t = this.tAt(e.clientX - rect.left);
    const day = new Date(t);
    this.setDate(fmtDate(new Date(Date.UTC(day.getUTCFullYear(), day.getUTCMonth(), day.getUTCDate()))));
  }

  barTitle(b: { t: string; v: number; n: number }): string {
    const h = this.hist()!;
    const when = h.granularity === 'day' ? b.t : b.t.slice(0, 7);
    return h.mode === 'harmonized'
      ? `${when}: ${Math.round(b.v).toLocaleString()} VIIRS-equivalent (${b.n.toLocaleString()} detections)`
      : `${when}: ${b.n.toLocaleString()} detections`;
  }
}

function dmy(s: string): string {
  return s ? `${s.slice(8, 10)}/${s.slice(5, 7)}/${s.slice(0, 4)}` : '';
}
