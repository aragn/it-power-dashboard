# it-power-dashboard

Italian power market dashboard: `app/` is the static site (GitHub Pages), `etl/` the scripts that fetch the data, run by the workflows in `.github/workflows/`.

## Data files

The data files in `app/data/` are not on `main`. They live on `data` branches, each holding a single commit that the update workflows replace, so hourly updates don't grow the repository.

- This repository's `data` branch: the ENTSO-E, JAO, Terna, GIE, EEX, TransnetBW, MASE and e-distribuzione data (`generation_load.json`, `crossborder.json`, `neighbour_prices.json`, `jao_spreads.json`, `jao_capacity.json`, `forecasts.json`, `res_forecasts.json`, `eua.json`, `terna.json`, `capacity.json`, `available_capacity.json`, `outages.json`, `outage_units.json`, `data_checks.json`, `picasso.json`, `gas.json`, `mase_projects.json`, `connections.json`, the balancing files under `balancing/` (one per zone, loaded when the zone is opened) and `balancing_bids/`). The long time series also come in a recent version, `NAME.recent.json` (`etl/compact.py`).
- GME's data (PUN, zonal prices, MI-A, MI-XBID, market coupling, the MGP market units and merit order, the MSD and MB series under `balancing_gme/`) is kept in a private repository: GME's terms do not allow publishing it. Its workflows run there, with this repository's code (`DATA_REMOTE` in `.github/scripts/data-branch.sh`).

To get the latest data locally, for example to preview the site, run from the repo root in Git Bash:

```bash
bash .github/scripts/data-branch.sh restore
```

With access to the private repository (as the remote `private`), the full dashboard also gets GME's data:

```bash
DATA_REMOTE=private bash .github/scripts/data-branch.sh restore
```

## Full and public versions

GME's terms allow its data to be shown publicly only as re-elaborations, without the exact values. The page is the same for both versions:

- **Public** (served from GitHub Pages, or locally with `?public` added to the address): no hover windows on any chart, no GME values in labels or subtitles, the market units under made-up names ("Gas plant 7"), the units list hidden, GME credited under each of its charts with a notice at the bottom. The GME files behind it are re-elaborated by `etl/publicise_gme.py` in the private repository (units renamed, no unit codes, operators or unit list; rounding available but off for now) and published to this repository's `data` branch, once the private repository has the secret `PUBLIC_DATA_TOKEN`.
- **Full** (opened locally, from `localhost`): everything, with the exact data restored from both data branches.
