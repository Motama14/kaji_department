# `data/` — raw NASA FIRMS active-fire hot spots (global)

Original downloads from NASA FIRMS **Archive Download** (https://firms.modaps.eosdis.nasa.gov/download/),
covering the **whole planet** (bbox `[-180, -90, 180, 90]`), from **2000-11-01 to 2026-11-13**.

` Based on a quick look (2026-11-14): header plus first and last row of every file, and the first ~60 MB of 6 representative files.
Row counts and percentages are **estimates**. Exact numbers will come from the full streamed inventory (CLAUDE.md §7, step 1). `

---

## 1. What the data represents

Each row is **one active-fire detection (hot spot)**: a satellite pixel that a fire-detection algorithm flagged as
containing one or more thermal anomalies (fires, volcanoes, gas flares, industrial heat…) at a given overpass.
The latitude/longitude is the **pixel centre**, not the exact fire location. A large fire shows up as many
neighbouring pixels, and at 375 m VIIRS splits the same fire into more points than MODIS at 1 km does.

| Folder | Instrument / satellite | Product | Pixel | Overpass (local solar time) | Period in files |
|---|---|---|---|---|---|
| `MODIS/` | MODIS on **Terra** + **Aqua** (mixed in the same files) | MCD14ML C6.1 (`M-C61`) | 1 km | Terra ~10:30/22:30, Aqua ~13:30/01:30 | 2000-11-01 → 2026-11-13 |
| `SV_C2/` | VIIRS on **Suomi NPP** | VNP14IMG C2 (`SV-C2`) | 375 m | ~13:30/01:30 | 2012-01-20 → **2026-11-02** |
| `J1V_C2/` | VIIRS on **NOAA-20** (JPSS-1) | VJ114IMG C2 (`J1V-C2`) | 375 m | ~13:30/01:30 (≈50 min offset) | 2018-04-01 → 2026-11-13 |
| `J2V_C2/` | VIIRS on **NOAA-21** (JPSS-2) | VJ214IMG C2 (`J2V-C2`) | 375 m | ~13:30/01:30 (≈50 min offset) | 2023-03-01 → 2026-11-13 |

## 2. Size and volume

| Folder | Files | Archive (SP) size | NRT size | Est. rows (SP + NRT) |
|---|---|---|---|---|
| `MODIS/` | 27 (26 SP + 1 NRT) | 9.0 GB | 69 MB | ~116 M + ~0.8 M |
| `SV_C2/` | 16 (15 SP + 1 NRT) | 23.6 GB | 555 MB | ~295 M + ~6.6 M |
| `J1V_C2/` | 10 (9 SP + 1 NRT) | 13.4 GB | 626 MB | ~168 M + ~7.4 M |
| `J2V_C2/` | 5 (4 SP + 1 NRT) | 5.4 GB | 601 MB | ~67 M + ~7.1 M |
| **Total** | **58** | **51.4 GB** | **1.85 GB** | **~670 M detections** |

Rows are ~78–87 bytes each. Single VIIRS files reach 1.8 GB: **never load them whole into memory** (use DuckDB or chunks).

## 3. File organisation

- `fire_archive_<code>_<id>.csv` = **standard processing (SP)**, the final science-quality record.
- `fire_nrt_<code>_<id>.csv` = **near real time (NRT)**, the recent stretch that has no SP yet.
- Every file is **sorted by `acq_date`/`acq_time`**. The files are **~1-year chunks that do not follow calendar years**:
  they roll over on a day that shifts each year (e.g. MODIS 2001-11-03 → 2002-11-04, VIIRS 2019-01-30 → 2020-01-31).
  A calendar year is therefore split across two files.
- The newest id per sensor holds the latest SP stretch **and** the only NRT file. **SP and NRT do not overlap**:
  NRT starts the day after SP ends.

| Sensor | Last SP date | NRT range |
|---|---|---|
| MODIS | 2026-08-31 | 2026-09-01 → 2026-11-13 |
| S-NPP | 2026-07-31 | 2026-08-01 → 2026-11-02 |
| NOAA-20 | 2026-07-31 | 2026-08-01 → 2026-11-13 |
| NOAA-21 | 2026-07-31 | 2026-08-01 → 2026-11-13 |

### File id → date range (first and last row)

<details><summary>MODIS (26 SP + 1 NRT)</summary>

| id | From | To |
|---|---|---|
| 845577 | 2000-11-01 | 2001-11-02 |
| 845585 | 2001-11-03 | 2002-11-04 |
| 845589 | 2002-11-05 | 2003-11-06 |
| 845593 | 2003-11-07 | 2004-11-07 |
| 845597 | 2004-11-08 | 2005-11-09 |
| 845601 | 2005-11-10 | 2006-11-11 |
| 845607 | 2006-11-12 | 2007-11-13 |
| 845611 | 2007-11-14 | 2008-11-14 |
| 845616 | 2008-11-15 | 2009-11-16 |
| 845621 | 2009-11-17 | 2010-11-18 |
| 845626 | 2010-11-19 | 2011-11-20 |
| 845649 | 2011-11-21 | 2012-01-19 (**only ~2 months**, which explains the small 61 MB size; no gap) |
| 845653 | 2012-01-20 | 2013-01-20 |
| 845661 | 2013-01-21 | 2014-01-22 |
| 845665 | 2014-01-22 | 2015-01-23 |
| 845669 | 2015-01-23 | 2016-01-24 |
| 845682 | 2016-01-25 | 2017-01-25 |
| 845690 | 2017-01-26 | 2018-01-27 |
| 845694 | 2018-01-28 | 2019-01-29 |
| 845698 | 2019-01-30 | 2020-01-31 |
| 845702 | 2020-02-01 | 2021-02-01 |
| 845716 | 2021-02-02 | 2022-02-03 |
| 845720 | 2022-02-04 | 2023-02-05 |
| 845724 | 2023-02-06 | 2024-02-07 |
| 845734 | 2024-02-08 | 2025-02-08 |
| 845738 | 2025-02-09 | 2026-02-10 |
| 845758 | 2026-02-11 | 2026-08-31 |
| 845758 NRT | 2026-09-01 | 2026-11-13 |

A few boundary days appear in two consecutive files (e.g. 2014-01-22, 2015-01-23). This is probably the same day
split at a time of day; check for duplicates during conversion.
</details>

<details><summary>VIIRS S-NPP (15 SP + 1 NRT)</summary>

| id | From | To |
|---|---|---|
| 845656 | 2012-01-20 | 2013-01-20 |
| 845664 | 2013-01-21 | 2014-01-22 |
| 845668 | 2014-01-22 | 2015-01-23 |
| 845672 | 2015-01-23 | 2016-01-24 |
| 845685 | 2016-01-25 | 2017-01-25 |
| 845693 | 2017-01-26 | 2018-01-27 |
| 845697 | 2018-01-28 | 2019-01-29 |
| 845701 | 2019-01-30 | 2020-01-31 |
| 845705 | 2020-02-01 | 2021-02-01 |
| 845719 | 2021-02-02 | 2022-02-03 |
| 845723 | 2022-02-04 | 2023-02-05 |
| 845727 | 2023-02-06 | 2024-02-07 |
| 845737 | 2024-02-08 | 2025-02-08 |
| 845741 | 2025-02-09 | 2026-02-10 |
| 845761 | 2026-02-11 | 2026-07-31 |
| 845761 NRT | 2026-08-01 | 2026-11-02 |
</details>

<details><summary>VIIRS NOAA-20 (9 SP + 1 NRT)</summary>

| id | From | To |
|---|---|---|
| 845695 | 2018-04-01 | 2019-01-29 |
| 845699 | 2019-01-30 | 2020-01-31 |
| 845703 | 2020-02-01 | 2021-02-01 |
| 845717 | 2021-02-02 | 2022-02-03 |
| 845721 | 2022-02-04 | 2023-02-05 |
| 845725 | 2023-02-06 | 2024-02-07 |
| 845735 | 2024-02-08 | 2025-02-08 |
| 845739 | 2025-02-09 | 2026-02-10 |
| 845759 | 2026-02-11 | 2026-07-31 |
| 845759 NRT | 2026-08-01 | 2026-11-13 |
</details>

<details><summary>VIIRS NOAA-21 (4 SP + 1 NRT)</summary>

| id | From | To |
|---|---|---|
| 845726 | 2023-03-01 | 2024-02-07 |
| 845736 | 2024-02-08 | 2025-02-08 |
| 845740 | 2025-02-09 | 2026-02-10 |
| 845760 | 2026-02-11 | 2026-07-31 |
| 845760 NRT | 2026-08-01 | 2026-11-13 |
</details>

## 4. Columns

**All four sensors share the same header.** VIIRS files also use `brightness`/`bright_t31` (not `bright_ti4`/`bright_ti5`):

```
latitude,longitude,brightness,scan,track,acq_date,acq_time,satellite,instrument,confidence,version,bright_t31,frp,daynight,type
```

NRT files have the same columns **except `type`** (14 columns).

| Column | Meaning | MODIS | VIIRS |
|---|---|---|---|
| `latitude`, `longitude` | Pixel centre (WGS84, degrees) | 4 decimals | 5 decimals |
| `brightness` | Brightness temperature of the fire pixel (K). MODIS band 21/22, VIIRS I-4 | ✓ | ✓ (I-4; saturates at 367 K) |
| `scan`, `track` | Actual pixel size along scan / track (km). Grows towards the swath edge | 1.0 → ~4.8 | 0.32 → ~0.8 |
| `acq_date` | Acquisition date (UTC), `YYYY-MM-DD` | ✓ | ✓ |
| `acq_time` | Acquisition time (UTC), `HHMM` with leading zeros, e.g. `0013` | ✓ | ✓ |
| `satellite` | Platform | `Terra`, `Aqua` | `SNPP`, `N20`, `N21` |
| `instrument` | Sensor | `MODIS` | `VIIRS` (N20/N21), **`SNPP`** (S-NPP files!) |
| `confidence` | Detection confidence | integer **0–100** | categorical **`l`/`n`/`h`** |
| `version` | Collection + processing | `6.03` → **`61.03`** (switch between Feb 2022 and Feb 2023), `6.1NRT` | `2`, `2.0NRT` |
| `bright_t31` | Background brightness temperature (K). MODIS band 31, VIIRS I-5 | ✓ | ✓ (I-5) |
| `frp` | Fire Radiative Power (MW) | ✓ | ✓ (much smaller per pixel) |
| `daynight` | `D` = day, `N` = night overpass | ✓ | ✓ |
| `type` (SP only) | 0 = presumed vegetation fire, 1 = active volcano, 2 = other static land source, 3 = offshore | ✓ | ✓ |

## 5. Value distributions (60 MB samples, indicative only)

| | MODIS SP (2020) | S-NPP SP (2020) | NOAA-20 SP (2023) | NOAA-21 SP (2024) |
|---|---|---|---|---|
| `satellite` | Aqua 67 % / Terra 33 % | SNPP 100 % | N20 100 % | N21 100 % |
| `confidence` | <30: 4.5 %, 30–79: 69 %, ≥80: 27 % | l 15 %, n 80 %, h 5 % | l 15 %, n 80 %, h 5 % | l 14 %, n 81 %, h 5 % |
| `type` 0 / 1 / 2 / 3 | 95.6 / 0.08 / 3.8 / 0.6 % | 94.9 / 0.08 / 4.4 / 0.6 % | 95.3 / 0.10 / 4.1 / 0.5 % | 93.4 / 0.08 / 5.9 / 0.7 % |

So ~95 % of detections are vegetation fires (`type = 0`). Static sources (gas flares, industry) are ~4–6 %, volcanoes <0.1 %.

## 6. Oddities and points to keep in mind

1. **No `type` column in NRT** (Aug/Sep–Nov 2026). Static sources and volcanoes have to be filtered another way:
   by persistence, or by inheriting the class from SP cells.
2. **S-NPP NRT stops on 2026-11-02**, while the other sensors run to 2026-11-13. This is an 11-day gap at the end of the record.
3. **S-NPP rows have `instrument = SNPP`**, not `VIIRS`. Map them to sensor `VIIRS` and satellite `N` in the internal schema.
4. **MODIS `version` changes from `6.03` to `61.03`** somewhere inside file 845720 (2022-02-04 → 2023-02-05) (same C6.1 collection). Treat both as the same product.
5. **Files are not calendar years.** Never assume "file = year". Partition by `acq_date` during conversion.
6. **MODIS file `845649` covers only 2011-11-21 → 2012-01-19.** It is small, but there is no gap.
7. **Possible duplicate boundary days** between consecutive SP files. Check and deduplicate when converting.
8. MODIS confidence 0–100 and VIIRS `l/n/h` must be normalised (`confidence_norm`) before comparing.
9. VIIRS `brightness` saturates at 367 K (seen frequently on high-confidence `h` pixels). It is not a data error.
10. The Terra and Aqua orbits drift (Terra since ~2020, Aqua since ~2022), which shifts their local overpass time in recent years.
