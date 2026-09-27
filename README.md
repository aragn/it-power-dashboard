# it-power-dashboard

Italian power market dashboard: `app/` is the static site (GitHub Pages), `etl/` the scripts that fetch GME and ENTSO-E data, run by the workflows in `.github/workflows/`.

## Data files

The JSON files in `app/data/` (`pun.json`, `zonal_prices.json`, `generation_load.json`, `crossborder.json`) are not on `main`. They live on the `data` branch, which always holds a single commit that the update workflows replace, so hourly updates don't grow the repository.

To get the latest data locally, for example to preview the site, run from the repo root in Git Bash:

```bash
bash .github/scripts/data-branch.sh restore
```
