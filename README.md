# it-power-dashboard

Italian power market dashboard: `app/` is the static site (GitHub Pages), `etl/` the scripts that fetch GME and ENTSO-E data, run by the workflows in `.github/workflows/`.

## Data files

The JSON files in `app/data/` (`pun.json`, `zonal_prices.json`, `generation_load.json`, `crossborder.json`, `intraday_prices.json`, `xbid_prices.json`, `neighbour_prices.json`, `coupling.json`, `forecasts.json`, `res_forecasts.json`, `eua.json`, `terna.json`, `capacity.json`, `outages.json`, `outage_units.json`, `data_checks.json`) are not on `main`. They live on the `data` branch, which always holds a single commit that the update workflows replace, so hourly updates don't grow the repository.

To get the latest data locally, for example to preview the site, run from the repo root in Git Bash:

```bash
bash .github/scripts/data-branch.sh restore
```
