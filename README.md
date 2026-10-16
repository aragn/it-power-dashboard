# it-power-dashboard

Italian power market dashboard: `app/` is the static site (GitHub Pages), `etl/` the scripts that fetch the data, run by the workflows in `.github/workflows/`.

## Data files

The data files in `app/data/` are not on `main`. They live on `data` branches, each holding a single commit that the update workflows replace, so hourly updates don't grow the repository.

- This repository's `data` branch: the ENTSO-E, JAO, Terna, GIE, EEX, TransnetBW, MASE and e-distribuzione data (`generation_load.json`, `crossborder.json`, `neighbour_prices.json`, `jao_spreads.json`, `jao_capacity.json`, `forecasts.json`, `res_forecasts.json`, `eua.json`, `terna.json`, `capacity.json`, `available_capacity.json`, `outages.json`, `outage_units.json`, `data_checks.json`, `picasso.json`, `gas.json`, `mase_projects.json`, `connections.json`, the grid map's files under `grid/` (see below), the balancing files under `balancing/` (one per zone, loaded when the zone is opened) and `balancing_bids/`, and each bidding zone's own ENTSO-E files under `zones/<ZONE>/` (`generation_load.json`, `forecasts.json`, `res_forecasts.json`, `crossborder.json` with its borders to the other zones, the countries and Corsica, `reservoirs.json`), in the layout of Italy's, for the zone pages). The long time series also come in a recent version, `NAME.recent.json` (`etl/compact.py`).
- GME's data (PUN, zonal prices, MI-A, MI-XBID, the zonal quantities of the MGP and the MI-A auctions in `intraday_volumes.json` (GME's `ME_ZonalVolumes`, sold and bought per quarter-hour, `etl/fetch_intraday.py`) and of MI-XBID in `xbid_prices.json`, market coupling, the MGP flows and limits between zones with the Corsica zones' prices (`transits.json`), the MGP market units and merit order, the MSD and MB series under `balancing_gme/`, the daily gas prices at the PSV in `gas_prices.json`: GME's IG Index, the MGP-GAS auction and Snam's balancing average, `etl/fetch_gas_prices.py`) is kept in a private repository: GME's terms do not allow publishing it. Its workflows run there, with this repository's code (`DATA_REMOTE` in `.github/scripts/data-branch.sh`).

To get the latest data locally, for example to preview the site, run from the repo root in Git Bash:

```bash
bash .github/scripts/data-branch.sh restore
```

With access to the private repository (as the remote `private`), the full dashboard also gets GME's data:

```bash
DATA_REMOTE=private bash .github/scripts/data-branch.sh restore
```

## Grid map

Maps › Grid and power plants (`etl/fetch_grid.py`, weekly `update-grid.yml`) draws Italy's power lines, substations and plants from OpenStreetMap, with Terna's planned projects on a switch:

- `grid/lines.json`: lines and cables of 100 kV and more, in service and under construction (OSM power=line/cable), joined where OSM splits them and simplified; an end in a neighbouring country (`etl/neighbours.json`, Natural Earth) or a submarine cable landing off a foreign coast marks an interconnector.
- `grid/substations.json`: substations of 220 kV and more; `grid/plants.json`: power plants (OSM power=plant), wind farms mapped only as turbines grouped into farms; solar farms without a stated output sized from their area, turbines without one at 1.5 MW (marked as estimates).
- `grid/plans.json`: Terna's main projects. `etl/grid_plans.json` holds them by hand (Terna codes, stations at their OSM positions, schematic routes); their phases and in-service years come from Terna's summary tables (Excel), found and read on every run from Terna's Development Plan page (now the PdS 2025 table, at 31 December 2024; more tables can be listed under `tables`), except where the progress report of February 2026 (at 31 December 2025, the project's `update`) is newer. A newer table takes over by itself. Add new projects after each Development Plan (every two years; the next, PdS 2027, is in its environmental assessment); `python etl/fetch_grid.py --plans-only` rebuilds this file alone.
- `grid/plant_units.json` (private repository, `update-grid-units.yml`, `etl/match_grid_units.py`): the GME market units matched to the plants, shown in the full version only.

The source is Geofabrik's daily extract of Italy (`download.geofabrik.de/europe/italy-latest.osm.pbf`, about 2 GB), read with pyosmium; the lines are the OSM contributors' (ODbL, credited under the map). MASE's own layer of the grid (GRTN 2001, revised 2008) is out of date and its map services are switched off.

## Analysis tab

Data › Analysis opens on a set of cards, one per analysis; a card opens its view. The first built is the **merchant revenues battery calculator**: the user describes a battery (zone, MW, MWh, efficiency, state-of-charge window, cycles, availability, fade, costs) and a period, and every day of it is optimised on the day's actual prices, then summed into revenues against costs by day, month and year, with where the battery made money, what it bought and sold in each market, the average day, the dispatch, a day in detail and the comparison with Italy's real batteries.

- **Solver:** [HiGHS](https://highs.dev) 1.15 (University of Edinburgh, MIT licence), its WebAssembly build [highs-js](https://github.com/lovasoa/highs-js) 1.15.3 vendored in `app/lib/highs/` (from npm, checksum checked). It runs in the browser: `app/analysis/bess-worker.js` is a Web Worker that solves each day as a linear programme (again as a mixed-integer one where the answer would charge and discharge at once); the page runs a few workers side by side (a year in about two seconds). No server, nothing precomputed.
- **Markets**, in the order of their gates: MGP (zonal price), MI-A1, MI-A2, MSD (ex-ante), MI-A3, MI-XBID (15-minute reference prices, capped at a share of the power), MB. In MI-A and MI-XBID the battery sells at most a share (default 10%) of what the zone's sellers sold in the quarter-hour and buys at most that share of what its buyers bought; the MGP has no such cap. MSD and MB: the zone's average accepted prices, up to a share of the zone's accepted volume; left blank, that share is what Italy's batteries in the zone were accepted for per MW (the Storage tab's files), scaled to the battery.
- **Strategies:** sequential, gate by gate (default: each market knowing its own prices, on the position the earlier ones left); perfect foresight across all markets (the ceiling).
- **Data:** the GME files the page already has (`zonal_prices.json`, `intraday_prices.json`, `intraday_volumes.json`, `xbid_prices.json`, `balancing_gme/<ZONE>.json`, `storage/`).

The Prices tab ends with **Quantities by Market**: the zone's quantities (Italy: the seven zones summed), sold or bought, stacked by market (MGP, MI-A1, MI-A2, MI-A3, MI-XBID's 15-minute products), on the calendar and the 15 MIN / HOURLY / DAILY control.

Three more analyses are worked out on the page from files it already reads (no solver, a fraction of a second):

- **Solar and wind capture prices:** for Italy (against the PUN Index) or a zone (its zonal price), the output-weighted price of solar and wind (ENTSO-E 16.1.B&C) against the baseload price, by day, month or year; capture rates; a day-by-day cannibalisation scatter (output as a share of the load against the capture rate); the price by hour and month against the month's average; the average day; every zone side by side on request.
- **Imbalance cost of a solar or wind plant:** a plant of the size and yield entered (blank: a typical yield for the zone) produces its zone's fleet output scaled to it and sells the fleet's day-ahead forecast (ENTSO-E 14.1.D; the error can be scaled up for a single plant), settling the rest at the zone's imbalance price (ENTSO-E 17.1.G); optionally trading the intraday forecast first at MI-XBID's reference price. Cost per MWh and per year, by hour, long against short, forecast error by hour, and the imbalance price's gap to the MGP when the macrozone (Terna) was short or long.
- **Spark and clean spark spreads:** a gas plant of the efficiency entered, against the zone's MGP price (Italy: the PUN Index), GME's daily gas price (`gas_prices.json`, the IG Index by default) and the EU ETS price of the day (`eua.json`), with variable O&M and gas transport:
  - the spark and clean spark spreads by day, month or year (also over the weekday peak), and the plant's cost against the power price;
  - how often the plant was in the money, and how often the price sat in a gas plant's cost range (a 58% to a 45% efficient plant);
  - the clean spread by hour and month, and the margin of a plant running only when it paid;
  - a daily scatter of cost against price, and the gas and carbon prices.
- **MACSE and the capacity market:** the same battery three ways over the days on file:
  - merchant, the battery calculator's optimisation;
  - merchant with a capacity contract: Terna's premium on the derated capacity (2028 derating by zone and duration), less the reliability option's payback above a daily strike (an open-cycle turbine's cost on GME's gas price and the EU ETS price);
  - MACSE: the first auction's premium by area, plus 20% of the balancing margin.

  Shown: each route by month, what each earns and gives up, the MACSE premium that would match merchant, and the strike against the day's highest price.
- **Imbalance price spikes, day by day** (after Volue's account of Austria's 2,900 €/MWh spike of 24 September 2026): for a zone and day (by default the period's highest imbalance price):
  - the imbalance price against the MGP, and Terna's macrozonal imbalance;
  - the MGP, MI-A1/2/3 and MI-XBID prices;
  - wind, solar and residual load against their forecasts;
  - net imports, scheduled against physical;
  - the balancing energy activated by product (ENTSO-E 12.3.E) with GME's MB acceptances;
  - PICASSO's upward prices, Italy against its neighbours (`picasso.json`);
  - the standard aFRR up-ladder at the peak (`balancing_bids/`);
  - an account written from the data by rules, and the list of the window's highest spikes.
- **Most binding borders, month by month** (after the monthly reports of the most constraining CORE CNECs). A border's shadow price is the price gap across it while its flow runs at the limit, summed over the month (k€/MW). Covered: the six borders between zones (`transits.json`) and the coupled ones with France, Austria, Slovenia and Greece (`coupling.json` against `neighbour_prices.json`). Shown:
  - a ranking and a map;
  - for a border: its shadow price hour by hour, its flow against the limit, when it bound, and a driver table (binding hours against the same hours of other days, the month, the month before, correlation);
  - on the northern border, the elements JAO names as limiting the NTC in those hours (`jao_capacity.json`).
- **Zonal spreads and congestion:** each zone's MGP price against a reference zone (North by default): how often they split, average spread, spread by hour, duration curves, and what a 1 MW plant running all the time gains or loses for being in the zone; per border between zones (GME's MGP flows and limits, `transits.json`), the share of the time at the limit each way and the congestion rent (flow times price gap).

## Full and public versions

GME's terms allow its data to be shown publicly only as re-elaborations, without the exact values. The page is the same for both versions:

- **Public** (served from GitHub Pages, or locally with `?public` added to the address): no hover windows on any chart, no GME values in labels or subtitles, the market units under made-up names ("Gas plant 7"), the units list hidden, GME credited under each of its charts with a notice at the bottom. The GME files behind it are re-elaborated by `etl/publicise_gme.py` in the private repository (units renamed, no unit codes, operators or unit list; rounding available but off for now) and published to this repository's `data` branch, once the private repository has the secret `PUBLIC_DATA_TOKEN`.
- **Full** (opened locally, from `localhost`): everything, with the exact data restored from both data branches.

## Bidding zones

The zone menu at the top opens a zone's page: the same tabs as Italy's, drawn from the zone's data. The zone's MGP price takes the PUN Index's place; Overview from the zone's ENTSO-E files (`zones/<ZONE>/`), its installed capacity and outages, Terna's available capacity of its macro-area (North, or South & islands); Prices with the neighbouring zones (and Corsica's two zones on Sardinia's and Centre-North's pages) and the MGP spreads with them; Market and Storage with the zone's units and plants only; Interconnection with the zone's borders to the other zones, the countries and Corsica (ENTSO-E flows and schedules, GME's MGP flows against the limits), its coupled borders and, for North, JAO's northern border. North, South and Sardinia are on for now (`ZONES_WITH_DATA` in `app/index.html`).
