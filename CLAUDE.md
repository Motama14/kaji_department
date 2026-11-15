# CLAUDE.md — MODIS/VIIRS hot spot harmonization (NASA Space Apps Challenge 2026)

> Project memory for Claude Code. Read it in full before starting any task.
> Working language: **English for everything** — conversation with the user, code, variable names, comments, commits and all documentation.

---

## 0. Working mode: STEP BY STEP (mandatory)

The user wants to give input before every advance. Therefore:

1. **Before each step**, explain in a few lines what you are going to do, which files you will touch and what decisions it involves. **Wait for the user's OK** before executing.
2. **One step at a time.** Do not chain phases (e.g. do not go from inspecting data to writing the adapter, or from the adapter to calibration) without stopping and asking.
3. **When a step is done, stop** and show a short summary: what was done, key figures (rows, ranges, timings, metrics), anything unexpected, and what you propose as the next step.
4. When there is a choice between options (threshold, grid, model, how to handle NRT, how to group regions…), **present 2–3 alternatives with pros and cons and let the user decide**. Do not choose on your own except for trivial things.
5. If something fails or the data is not as expected, **stop and report it** instead of applying a silent workaround.
6. Small, verifiable steps: five 10-minute steps are better than one one-hour step.
7. For long-running jobs (multi-GB conversions), give the estimated time and disk space beforehand.

---

## 1. Context

- **Event:** NASA Space Apps Challenge 2026, a **48-hour** hackathon. **The hackathon has already started.**
- **Participant:** Mota, competing **solo**. Backend-leaning full-stack profile (Java/Spring Boot, Angular, MongoDB, REST API design), with a strong interest in data science, ML and MLOps.
- **Chosen challenge:** build a web app with a **harmonized burning-activity calendar**, based on active-fire hot spots, combining the long historical record of **MODIS** with the continuity and higher detail of **VIIRS**.

### Folders
| Folder | What it is | Permissions |
|---|---|---|
| `D:\nasa_space_apps` | **Hackathon project** (this repo). All code, processed data and new documentation go here. | Read and write |
| `D:\nasa_space_apps_prep` | **Prior preparation** with practice data (Spain and Brazil): pipeline, API, web, docs, tests, research. | **Read-only: do not modify anything** |

- The prep folder is added in `.claude/settings.json` (`permissions.additionalDirectories`, with `Edit`/`Write` denied on it). If it is not accessible, start with `claude --add-dir D:\nasa_space_apps_prep`.
- Current repo contents (2026-11-14): `CLAUDE.md`, `.gitignore`, `.claude/settings.json`, `data/` (raw CSVs only). No code yet.
- `.gitignore` currently ignores `/.claude` and the four raw data folders. It does **not** yet ignore `.env` or processed outputs: propose adding them when they are created.
- **What to consult in the prep folder:** `docs/` (PROGRESS, DECISIONS, DATA, RESULTS, RUNBOOK), `pipeline/` (adapter, aggregate, calibrate, config), `api/`, `web/`, `tests/`, `research/` and the `SKILL.md` skill (hotspot-map-viewer) at its root. Prep docs may be written in Spanish.
- **How to reuse it:** read it, understand it and **propose to the user** what to port as-is and what to adapt. Do not blindly copy whole folders: the prep code assumes per-country data (`data/spain`, `data/brazil`), while the new data is **global** and uses a different file format.

---

## 2. What the challenge asks for (summary of NASA's statement)

**Problem:** MODIS (Terra and Aqua) provides more than two decades of observations, but it is reaching the end of its life and is being replaced by VIIRS. Their records cannot be compared directly without harmonization, because they differ in spatial resolution, sensitivity and observation time.

**Two-part goal:**

1. **MODIS–VIIRS harmonization**, so that in the end the source sensor no longer matters and the focus is on patterns, trends and anomalies.
2. **Web app** that takes the harmonized data, lets the user pick a **region** and a **date range**, and offers downloadable analytics and visualizations.

**Deliverables and features mentioned in the statement:**

- Burning calendar with the number of hot spots from the early 2000s to today: which periods of the year have more fires, which years had more activity, and how a current fire compares with previous ones.
- Comparison of the **current month against the historical median** for the selected area.
- **Accessible color ramps** to show similarities and differences between months.
- **Downloadable CSV** with the hot spots of the query.
- Interactivity: **map to draw the area** of interest or **enter latitude and longitude**.
- Key indicators: historical comparisons, **anomalies against multi-year means** and number of detected fires.
- Target audience: early-warning and emergency services, scientists, land managers and firefighters.

---

## 3. Key concepts (important to get right)

### Sensors and satellites
- **MODIS** is an instrument on board **Terra** (data since Nov 2000) and **Aqua** (since mid-2002). Both MODIS are the same instrument and observe **the whole planet** (land and sea). The names Terra/Aqua only reflect each mission's original science focus, not which areas they observe.
- **VIIRS** is a different instrument, on board three satellites. Availability **in the downloaded data**:
  | Satellite | Since | 375 m product |
  |---|---|---|
  | **Suomi NPP** (S-NPP) | 20 Jan 2012 | VNP14IMGT / VNP14IMGTDL |
  | **NOAA-20** (JPSS-1) | **1 Apr 2018** | VJ114IMGT / VJ114IMGTDL |
  | **NOAA-21** (JPSS-2) | **1 Mar 2023** | VJ214IMGTDL |
- **There is no "VIIRS for Terra".**

### Overpass time (critical for harmonization)
- **Terra:** ~10:30 / ~22:30 (morning). Its orbit drifts since ~2020.
- **Aqua:** ~13:30 / ~01:30 (afternoon). Its orbit drifts since ~2022.
- **VIIRS (S-NPP, NOAA-20, NOAA-21):** ~13:30 / ~01:30, same as Aqua. All three share the afternoon orbit but are offset by ~50 min from each other, so they do not see exactly the same instant.
- Consequence: **VIIRS replaces Aqua; Terra's morning slot has no successor** at NASA/NOAA. Fires have a diurnal cycle, so Terra and Aqua do not see the same fires.
- **The clean pair for MODIS↔VIIRS calibration is Aqua ↔ Suomi NPP** (same overpass time: only the sensor and resolution difference remains).
- NOAA-20 and NOAA-21 carry the same instrument as S-NPP: they provide **continuity** for the VIIRS series as S-NPP ages, and their overlap with S-NPP (since 2018 and 2023) allows checking whether a VIIRS-to-VIIRS adjustment is needed. **Pending decision** (see §5.4).

### Resolution
- MODIS: **1 km** pixel at nadir.
- VIIRS I-band: **375 m**. It detects many more small fires, and a single fire shows up as several points.

### Product names (for finding documentation)
- MODIS: **MCD14ML** (standard archive), MCD14DL (near real time). Collection 6.1.
- VIIRS: see table above. Collection 2.
- Codes in FIRMS file names: `M-C61` = MODIS C6.1, `SV-C2` = S-NPP, `J1V-C2` = NOAA-20, `J2V-C2` = NOAA-21.
- ⚠️ Searching just "MODIS Collection 6.1" leads to non-fire sites (e.g. the atmosphere team: aerosols and clouds). Always search by product name.

---

## 4. Data

### Source: NASA FIRMS
- **Archive Download** (custom area, dates and format): https://firms.modaps.eosdis.nasa.gov/download/ — **the hackathon data comes from here**.
  - Mixes standard processing (SP, `fire_archive_*` files) and near-real-time (NRT, `fire_nrt_*` files). SP replaces NRT with a delay of several months.
- **Annual per-country summaries**: https://firms.modaps.eosdis.nasa.gov/country/ (used during preparation).
- **FIRMS API** (requires a free **MAP_KEY**): for recent NRT data and the "current month" feature.
- Use **SP (standard processing)** data for the historical series. NRT only for the recent stretch that has no SP yet.

### Hackathon data (downloaded by the user)
- **Coverage: the whole planet**, bbox `[-180, -90, 180, 90]` (min lon, min lat, max lon, max lat).
- **Inventoried in step 1 (2026-11-14):** 59 files, 53.3 GB, 660.9 M rows. Exact figures, the file id → date range map and all oddities are in `docs/DATA.md`.
- Location and status (inventory as of 2026-11-14). **All files are already unzipped plain CSVs** (the original ZIPs are gone). Folder names follow the FIRMS product codes:
```
data/
├── MODIS/    # Terra + Aqua — 27 × fire_archive_M-C61_<id>.csv (60–420 MB each)
│             #               + fire_nrt_M-C61_845758.csv                        → ~8.5 GB total
├── SV_C2/    # VIIRS S-NPP  — 15 × fire_archive_SV-C2_<id>.csv (1.4–1.8 GB each)
│             #               + fire_nrt_SV-C2_845761.csv                        → ~22.4 GB total
├── J1V_C2/   # VIIRS NOAA-20 —  9 × fire_archive_J1V-C2_<id>.csv (1.3–1.8 GB each)
│             #               + fire_nrt_J1V-C2_845759.csv                       → ~13 GB total
└── J2V_C2/   # VIIRS NOAA-21 —  4 × fire_archive_J2V-C2_<id>.csv (1.4–1.8 GB each)
              #               + fire_nrt_J2V-C2_845760.csv                       → ~5.6 GB total
```
  **≈ 50 GB of raw CSV in total.** Individual VIIRS files are up to ~1.8 GB: never open them whole in memory.
- **Confirmed:** each sensor has exactly one NRT file, and it shares its id with the newest archive file (`845758` MODIS, `845761` S-NPP, `845759` NOAA-20, `845760` NOAA-21). That id is the most recent stretch, split into SP (`fire_archive_*`) and NRT (`fire_nrt_*`).
- **Verified in step 1 (details in `docs/DATA.md`):**
  - Archive files are rolling ~1-year chunks, **not calendar years** (27 MODIS, 15 S-NPP, 9 NOAA-20, 4 NOAA-21). Partition by `acq_date`, never by file.
  - **SP and NRT do not overlap.** SP ends 2026-08-31 (MODIS) and 2026-07-31 (VIIRS). NRT runs to 2026-11-13, except S-NPP, which stops on 2026-11-02.
  - **Four whole days are duplicated** between consecutive SP files (2014-01-22 and 2015-01-23, in both MODIS and S-NPP; 144,085 rows). The adapter must deduplicate.
  - **Whole-day outages** exist for every satellite (Terra 57 days, Aqua 26, S-NPP 49, NOAA-20 6, NOAA-21 2). Monthly counts have to account for them.
  - The MODIS `845649` file is small because it only covers 2011-11-21 → 2012-01-19. There is no gap.
- **Converted in steps 3–4 (2026-11-15):** the single detections table is `data/processed/hotspots/` (hive-partitioned Parquet, `sensor=/satellite=/year=`, 660.7 M rows, 10.8 GB, duplicates already removed). Read it with `pipeline.config.hotspots_sql()`; later steps use it instead of the raw CSVs. Schema and audit in `docs/DATA.md`.
- The practice data (Spain/Brazil, per country) is still in `D:\nasa_space_apps_prep\data\`, useful for comparing results.

### FIRMS columns (verified on all 59 files in step 1)
- **MODIS:** `latitude, longitude, brightness, scan, track, acq_date, acq_time, satellite, instrument, confidence, version, bright_t31, frp, daynight, type`
- **VIIRS:** identical header in these files (`brightness` and `bright_t31`, not `bright_ti4` and `bright_ti5` as in the practice data).
- `acq_time` is in **UTC**, always 4 digits (`HHMM`). Read it as text, or the leading zeros are lost.
- `confidence`: **MODIS is numeric (0–100)**; **VIIRS is categorical** (`l`/`n`/`h` only).
- `satellite`: `Terra`, `Aqua`, `SNPP`, `N20`, `N21`. Map `SNPP` → `N` in the internal schema. S-NPP rows have `instrument = SNPP`, so take the sensor from the folder or file-name code.
- `type`: 0 = presumed vegetation fire, 1 = active volcano, 2 = other static land source (refineries, gas flares), 3 = offshore. **It exists in the SP files only: the four NRT files lack it.** Decided in step 5: NRT rows inherit the type of their 0.01° cell from the SP history (`static_mask_d0.01.parquet`), and the app must warn that this type was calculated (see §5.2 and §6).
- `frp` is ≤ 0 in 0.02 % of rows (zeros everywhere, negatives in MODIS SP and VIIRS NRT).

### Optional auxiliary data
- Administrative boundaries: **Natural Earth** or **GADM** (selection by country or province).
- Volcanoes: the **Global Volcanism Program (Smithsonian)** list, to validate `type = 1` and show a separate layer.

---

## 5. Harmonization methodology

> What follows are the decisions made during preparation with **Spain and Brazil** data. With **global data and three VIIRS satellites** some must be revisited: they are marked **[REVIEW]**. Do not change them without proposing it to the user first.

### 5.1 Adapter (first of all)
- A layer that reads **any** source (CSV, SP or NRT, any sensor folder) and converts it to a **single internal schema**. The sensor/satellite can be inferred from the folder or file-name code (`M-C61`, `SV-C2`, `J1V-C2`, `J2V-C2`) and cross-checked with the `satellite` column.
- Internal schema:
  `sensor (MODIS|VIIRS), satellite (Terra|Aqua|N|N20|N21), lat, lon, datetime_utc, date, year, month, daynight, confidence_norm (low|nominal|high), frp, type, processing (SP|NRT), source_file`
- Convert to **partitioned Parquet** (e.g. by `sensor/satellite/year`) so DuckDB only reads what it needs.
- Starting point: `pipeline/adapter.py` in the prep folder.

### 5.2 Cleaning
- Burning calendar: only **`type = 0`**. Volcanoes (`type = 1`) and static sources: separate layer, out of the calendar.
- Confidence threshold: **MODIS ≥ 30** and **VIIRS nominal or high** (adjustable).
- Non-vegetation events that FIRMS flags as `type = 0` are excluded via `EXCLUSIONS` in the config: La Palma 2021, Tolbachik, Holuhraun, Kīlauea, Nyiragongo, Reykjanes, Mauna Loa 2022 (decided in step 5).
- **NRT rows (no `type`)**: type inherited from the SP history of their 0.01° cell. Kept rows with an inferred type are counted per cell-month (`n_type_inferred`). **User requirement: the app shows a warning that the type of these detections was calculated, because the data did not originally include it**; the CSV marks them (`type_source`).
- Calendar latitude limited to 60°S–80°N (false alarms beyond). Rows with `frp` ≤ 0 stay; FRP sums clip negatives to 0.
- Outage days: counts stay raw; `coverage_sat_month.parquet` describes observed/missing/low days per satellite × month.

### 5.3 Aggregation
- Common grid: **0.25° × month** (configurable) with **DuckDB** over Parquet.
- Metrics per cell and month: number of detections, mean and summed FRP, density, split by satellite and by day/night.
- Grid kept at **0.25°** (user decision, step 5): `cell_month_g0.25.parquet`, 12.7 M cell-months, 190,772 active cells, 316 MB. Other grids via `--grid`.

### 5.4 Calibration
- Step 1, main calibration: **Aqua vs. Suomi NPP** over the overlap period. For each cell (or group of similar cells), fit the relationship between the VIIRS count and the MODIS count.
- **Target series: "VIIRS equivalent"**. Aqua is back-projected to take advantage of VIIRS's higher detail.
- Step 2, filling 2000–2002 with Terra: fit the **Terra→Aqua** relationship over **2002-07 → 2019** and validate it on 2020–2021 (decided in step 6; Terra's drift shows in the data from 2022) to estimate the Aqua series for the early months.
- Final result: a single coherent series of **afternoon and night overpasses in VIIRS equivalent**, from 2000 to today, that can keep being updated once Terra and Aqua stop operating.
- **Done in preparation (v2, see `docs/DECISIONS.md` and `docs/RESULTS.md` in the prep folder):** **separate day/night** calibration (VIIRS/Aqua ≈ 4.5 by day and 15–20 at night), LightGBM Poisson with 3×3 neighborhood, FRP and climatology, blended 50 % with per-cell ratios (`blend_dn`), one model per region. Automatic selection by WAPE on 1° blocks in validation.
- **Decided with the user in step 6 (details in `docs/DECISIONS.md`):**
  - **One global model** (no regions); metrics also per latitude band.
  - **S-NPP is the reference**; NOAA-20 / NOAA-21 only fill months where S-NPP has missing days (and after S-NPP's data ends), scaled to S-NPP with day/night ratios.
  - The **full dense training panel is held in memory** (float32), no sampling.
  - **Outage days:** counts become full-month equivalents with the coverage table; the Poisson fit weights months by exposure (equivalent to an offset).
  - Implemented in `pipeline/calibrate.py` (step 7); the series is `data/processed/harmonized_g0.25.parquet`.
  - **Step 7 result (user decision):** the series is built with **LightGBM alone (`lgbm_dn`)**: validation 1° block
    WAPE 0.225, total bias −2.8 % (global day/night ratio: 0.331). `blend_dn` tied on WAPE (0.224) but had more bias.
    On global data LightGBM needs `max_delta_step = 2` (night model diverged without it). Metrics in `docs/RESULTS.md`.
- Optional if time allows: group detections into **events** with spatio-temporal DBSCAN.

### 5.5 Validation
- **Calibrate on 2012–2019**, **validate on 2020–2021**, **analyze 2022+ separately** (effect of Terra and Aqua orbital drift). User requirement (step 6): validation must be the years right before the drift starts; 2020–2021 meets it.
- Key chart for the presentation: **raw series** (with a step in 2012) vs. **harmonized series** (no step), plus the error in the validation years.
- With global data: report metrics **per latitude band** too, not just a single global number (user chose no extra region download).

### 5.6 Future work (mention, do not implement)
- **Sentinel-3 SLSTR** (ESA, ~10:00 overpass) as a possible replacement for Terra's morning coverage. It is not in FIRMS.

---

## 6. Web app

> **The visual design follows the user's sketches in `ui-design/`** (step 9; palette tokens in `web/src/theme.css`). Propose any visual change before making it; no polish or redesigns on your own initiative.

### Recommended stack
- **Backend:** Python + **FastAPI** + **DuckDB** (spatial extension) reading Parquet. No separate database.
- **Frontend:** **Angular** (the user knows it well) with **Leaflet + leaflet-draw** (draw polygon, enter coordinates).
- Precompute the aggregated table by cell and month: a polygon's calendar is a sum of cells, and only the CSV queries the raw points.

### Views
- `/` **hot spot viewer** (MapLibre GL + daily timeline + filters + selection summary), with a **Global** view and a **By Country** view (Natural Earth 1:50m borders, `web/public/countries.geojson` from `pipeline/countries.py`; country picked in the list or on the map, rest of the world dimmed, detections cut to the border in the browser). Its spec is the `hotspot-map-viewer` skill in `.claude/skills/`: always use it when touching the map, timeline, filters or `/api/hotspots`.
- `/calendar` **burning calendar**, anomalies and CSV (Leaflet to draw the area). Opened from the viewer with `?bbox=` or `?country=ADM0_A3`.
- Viewer endpoints: `GET /api/available-dates`, `GET /api/hotspots`, `GET /api/timeline-histogram`.

### Features by priority
1. **Burning calendar:** heatmap of years (rows, 2000 to present) × months (columns), accessible ramp (**viridis/cividis**).
2. **Anomalies:** current month vs. historical median for that month (and percentiles); anomaly view with a **colorblind-safe diverging ramp**.
3. **CSV download** of the query's points, with source sensor and harmonized value.
- **Required (user, step 5):** wherever NRT data appears (calendar months, anomaly, map, CSV), show a warning that the hot-spot type was **calculated** (inferred from the SP history) because the data did not originally include it. Use `n_type_inferred` in the cell table; the CSV gets a `type_source` column. Done in step 8 (calendar asterisk + note, anomaly cards, viewer notice and popup, CSV `type_source`).
- **Partial current month (user, step 8):** the last data month shows its real count, flagged as partial; its anomaly uses the historical values pro-rated to the covered days.
4. Optional: NRT data via the FIRMS API for the current month; volcano and static-source layer.

### Indicative endpoints
- `POST /api/calendar` → {polygon or bbox, date range} → harmonized year × month matrix.
- `POST /api/anomaly` → {polygon, reference month} → current value, median, percentiles.
- `POST /api/hotspots.csv` → {polygon, date range} → CSV of points.

---

## 7. Work plan (every step ends with a stop and user review)

| # | Step | What is shown to the user before moving on |
|---|---|---|
| 1 | **Data inventory** (with DuckDB, reading only headers/samples and doing streamed counts): per CSV — columns, date range (→ id-to-year map), approx. row count, values of `satellite`, `confidence` and `type`, SP vs NRT | Summary table in `docs/DATA.md` + oddities found |
| 2 | **Review the prep folder**: read docs and code in `D:\nasa_space_apps_prep` and propose what to port as-is, what to adapt and what to drop | List of proposals to approve |
| 3 | **Global adapter** (SP + NRT, all four sensor folders) and conversion to partitioned Parquet, tested first on **a single file** | Resulting schema and a sample |
| 4 | Full conversion of all files | Sizes, timings, counts per sensor/year |
| 5 | Cleaning + grid aggregation | Aggregated table and basic checks |
| 6 | Decide the **[REVIEW]** points of §5.4 with the user | Decisions in `docs/DECISIONS.md` |
| 7 | Calibration and validation | Metrics and before/after chart in `docs/RESULTS.md` |
| 8 | Connect the existing API and web app to the new data (no design changes) | Working end-to-end demo |
| 9 | Apply the design references once the user provides them | — |
| 10 | Deploy: API to Google Cloud, web app to GitHub | Public URLs working |
| 11 | Video, slides (before/after chart), submission | — |

**Time target:** have the working demo (step 8) by mid-hackathon at the latest, leaving room for design and submission.

---

## 8. Current status

**Preparation (in `D:\nasa_space_apps_prep`, with practice data):**
- [x] Adapter, aggregation, v2 calibration (`blend_dn`) and validation for Spain and Brazil.
- [x] Web app: FastAPI API + Angular (calendar, anomalies, CSV) and MapLibre global viewer.

**Hackathon (this repo):**
- [x] Global data downloaded and unzipped: MODIS, VIIRS S-NPP, NOAA-20 and NOAA-21 (≈ 50 GB CSV in `data/`).
- [x] Repo set up: `CLAUDE.md`, `.gitignore`, `.claude/settings.json` (read access to the prep folder).
- [x] Step 1: data inventory (`pipeline/inventory.py`, results in `docs/DATA.md`).
- [x] Step 2: prep review and porting plan (approved; decisions in `docs/DECISIONS.md`).
- [x] Step 3: global adapter (`pipeline/config.py`, `pipeline/adapter.py`), tested on 4 files (results in `docs/DATA.md`).
- [x] Step 4: full conversion to Parquet (`data/processed/hotspots/`: 660.7 M rows, 10.8 GB, audited against the inventory).
- [x] Step 5: cleaning and aggregation (`pipeline/aggregate.py`; `cell_month_g0.25.parquet`, static mask, coverage table; results in `docs/DATA.md`).
- [x] Steps 6–7: calibration decisions, calibration and validation (`pipeline/calibrate.py`; results in `docs/RESULTS.md`).
- [x] Step 8: API and web app connected to the new data (`pipeline/publish.py`, `api/`, `web/`, `tests/`; run it with `docs/RUNBOOK.md`).
- [x] Step 9: design with the user's references (9a layout/palette/download, 9b By Country, 9c summary + type-source filter, collapse-tab restyle; approved by the user 2026-11-15).
- [x] Step 10: deploy (API → Google Cloud, web → GitHub). Handled by the user (2026-11-15); container files prepared in the repo (`docs/RUNBOOK.md`).
- [ ] 2026 / NRT data for the "current month".
- [ ] Submission.

---

## 9. Living documentation (must be maintained)

This `CLAUDE.md` is the project's **stable index**: do not use it as a log. While working, document in separate files inside `docs/` of **this repo**, in English (create them if missing; the prep folder's docs are consulted, not edited):

| File | Contents | When to update |
|---|---|---|
| `docs/PROGRESS.md` | Chronological log: date/time, what was done, what is left, blockers | **At the end of every step** |
| `docs/DECISIONS.md` | Technical decisions: context → decision → discarded alternatives → reason. Note whether the user made it | Whenever choosing between options |
| `docs/DATA.md` | What is learned from the **real** data: columns, types, ranges, nulls, oddities, sizes, differences from the practice data | When inspecting any new file |
| `docs/RESULTS.md` | Calibration and validation metrics (per region and period), chart paths, conclusions | Whenever training, validating or generating a figure |
| `docs/RUNBOOK.md` | How to run everything from scratch: installation, commands in order, environment variables (no secrets) | Whenever any execution step changes |
| `docs/SI_USAGE.md` | Where and how SI tools were used (see §10) | Whenever SI-generated code is added |

Rules:
- **Short and concrete** entries, dated. One useful line beats a paragraph.
- If a decision in `docs/DECISIONS.md` contradicts something in this `CLAUDE.md`, **also update the affected section of this file** so no outdated instructions remain.
- Update section 8 (Current status) by ticking boxes as steps are completed. It is the only part of `CLAUDE.md` that changes often.
- Never write keys (MAP_KEY, etc.) in the documentation. They go in `.env`, which must be in `.gitignore`.
- When starting a new session, read `docs/PROGRESS.md` (latest entries) before continuing.

---

## 10. Use of SI tools (Space Apps rules) — code

Space Apps allows "Super Intelligence" (SI) tools (LLMs, chatbots, ML…) to accelerate the work, but requires **clearly indicating where and how they were used**. Judges will see these indicators and evaluate originality, intent and execution based on them. Rules for this project:

- **The solution idea belongs to the user.** SI must not be used to come up with the idea of how to solve the challenge. Claude Code implements, explains and offers technical options when asked; the user decides the approach (consistent with §0).
- **Code:** every source file written or substantially modified by Claude Code gets a short header comment, e.g.
  `# SI-assisted: written with Claude Code (Anthropic LLM) under the author's direction and reviewed by the author.`
  (use the comment syntax of each language: `//` for TS/JS, `<!-- -->` for HTML, etc.). Files written entirely by the user do not get it.
- **Descriptive text:** maintain `docs/SI_USAGE.md` with a short list of where and how SI was used (which modules, what for, what the user reviewed or decided). It will feed a "Use of SI tools" section in the final `README.md` and in the submission.
- **No NASA branding** in generated content: no NASA logos, flags, insignia or mission identifiers in the web app, figures or slides.

---

## 11. Notes for Claude Code

- **Always follow §0 (step by step).** When in doubt, ask.
- **Always communicate in English**, including summaries, questions and documentation.
- **Always apply §10** (SI header in code files, `docs/SI_USAGE.md` up to date).
- **Do not modify anything in `D:\nasa_space_apps_prep`.** Read-only.
- Always verify the real file columns before assuming the schema in §4.
- User's machine: Windows laptop with 16 GB of RAM (**do not use all available RAM**). **Do not load the CSVs whole into pandas** (VIIRS files reach ~1.8 GB each); use DuckDB with a memory limit (`SET memory_limit`) and a `temp_directory`, or read in chunks.
- **Disk space is tight** (≈ 50 GB of raw CSV already on disk). Before the full Parquet conversion, check free space and estimate the output size from a single converted file. Never delete raw CSVs without the user's explicit permission. **All data processing runs locally** on the user's laptop (decided 2026-11-14): work within its limits (DuckDB memory limit, process file by file).
- **Deployment target (decided 2026-11-14):** the **API on Google Cloud** (likely Cloud Run) and the **web app on GitHub** (static Angular build, e.g. GitHub Pages). Keep this in mind from the start: API base URL configurable via Angular environment files, CORS enabled in FastAPI for the GitHub Pages origin, Angular `base-href` set for the repo path, and the API should ship only the small precomputed aggregates (raw points for the CSV download may need Cloud Storage or a size limit — propose options when we get there). Always propose, never set up cloud resources on your own.
- `data/` holds the **originals**: do not edit or move them; outputs go to `data/processed/` (or the agreed path).
- Do not mix Terra with VIIRS in the main calibration (different overpass time).
- Keep the code parameterized (region/bbox, grid size, calibration/validation years, included satellites) so the configuration can change without rewriting anything.
- Do not touch the web app's visual design until the user provides the references (§6).
