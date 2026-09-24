# IT Power Market Dashboard — Architecture & Build Plan

## 1. Repo structure (mirrors lptva/gb-power-dashboard)

```
it-power-dashboard/
├── app/                  # static frontend, no build step
│   ├── index.html        # chart(s) + layout
│   ├── data/*.json        # pre-computed outputs the frontend fetches
│   └── (later) more pages/tabs as you add panels
├── etl/                  # python scripts, one per data source/panel
│   ├── fetch_pun.py       # GME ME_ZonalPrices -> PUN
│   ├── fetch_entsoe.py    # ENTSO-E generation/load/cross-border (to add)
│   ├── fetch_gaudi.py     # Terna UP/UPR/UPNR registry (to add)
│   └── build_dataset.py   # orchestrates the above, one entrypoint
├── tests/                # sanity checks on ETL outputs (schema, ranges, no gaps)
├── .github/workflows/
│   └── deploy.yml         # cron -> run ETL -> commit data -> publish to GitHub Pages
├── methodology.md         # what each panel means, formulas, caveats, attribution
├── .env.example           # GME_API_LOGIN, GME_API_PASSWORD, ENTSOE_API_KEY, ...
├── requirements.txt
└── README.md
```

## 2. Runtime model

No server, no live agent. GitHub Actions runs the ETL on a schedule (e.g. daily after MGP closes ~13:00 CET), commits refreshed JSON into `app/data/`, and GitHub Pages serves the static `app/` folder. The browser never talks to GME/ENTSO-E/Terna directly — it only reads your pre-built JSON. This keeps your API credentials server-side (in GitHub Actions secrets) and never exposed in client code.

## 3. GitHub setup checklist

- New public repo, MIT license (matches the GB project's approach), `README.md`, `.gitignore` (Python + node artifacts), branch protection optional.
- Repo **Settings → Pages**: source = GitHub Actions (not "branch"), so `deploy.yml` controls publishing.
- Repo **Settings → Secrets and variables → Actions**: add `GME_API_LOGIN`, `GME_API_PASSWORD`, and later `ENTSOE_API_KEY`. Never commit `.env`.
- `.github/workflows/deploy.yml`: cron trigger + manual `workflow_dispatch`, steps = checkout → setup Python → `pip install -r requirements.txt` → run `etl/build_dataset.py` → commit changed `app/data/*.json` → deploy `app/` to Pages.
- `.github/workflows/tests.yml`: run `pytest tests/` on every push/PR — catches schema drift before it breaks the frontend.
- Footer/`methodology.md` attribution lines for GME, ENTSO-E, and Terna as discussed.

## 4. Using Claude Code (or another coding agent) in this workflow

Claude Code (or Cursor, Copilot, etc.) is a development-time tool, not something that runs inside the shipped product. Practical way to use it:

1. **Scaffolding**: point it at this architecture doc and ask it to generate the repo skeleton (folders, `requirements.txt`, `.env.example`, workflow YAMLs) in one pass — this is exactly what `.claude/` and `plan/` in the GB repo suggest happened there.
2. **One ETL script per data source**: work source-by-source (PUN today, then zonal prices, then ENTSO-E generation, then Gaudì registry, then merit order). Give Claude Code the specific API doc excerpt (e.g. the `ME_ZonalPrices` fields above) plus a sample response, and ask it to write the fetch + parse + normalize function, not the whole app at once — smaller, testable units are easier to verify against real data.
3. **The unit-classification crosswalk**: this is the best use of an agent in this project. Feed it the Gaudì UP list and the GME UP codes appearing in `Offers_PublicDomain`, and have it write (and iterate on) a deterministic fuzzy-matching script — you review and correct the mapping, but the agent drafts and re-drafts the matching logic fast.
4. **Frontend iteration**: describe one chart/panel at a time (as you just did with the PUN chart) and ask for the ECharts config; keep the "no build step" constraint explicit so it doesn't default to React/Vite.
5. **Guardrails**: always run and inspect the ETL output yourself before committing — an agent can silently mis-map a field (e.g. average vs. sum, wrong timezone) and you're the one who has to catch it since this feeds a public-facing dashboard.
6. **Don't** let the agent auto-run in production or auto-commit on a schedule — that's what the GitHub Actions cron is for, deterministically, without a live model in the loop.

## 5. What's delivered now

- `pun_sample.json` — synthetic placeholder data (daily PUN-equivalent, 2024-01-01 → 2025-10-01) so the chart renders end-to-end today. Clearly not real prices — swap it out once you have live data.
- `index.html` — single chart panel: PUN line chart with an ECharts `dataZoom` slider (drag handles or use the 30D/90D/6M/1Y/All buttons) plus tooltip, dark "terminal" styling similar to the GB dashboard.
- `fetch_pun.py` — real GME API client (`/api/v1/Auth` → `/api/v1/RequestData`, `DataName=ME_ZonalPrices`, `Segment=MGP`, filtering to `Zone == "PUN"`), decodes the base64 zip response and writes `app/data/pun.json` in the exact schema `index.html` expects.

### To go live
Place `index.html` in `app/`, `pun_sample.json` in `app/data/`, `fetch_pun.py` in `etl/`. Set `GME_API_LOGIN` / `GME_API_PASSWORD` env vars, run `python etl/fetch_pun.py --start 20240101 --end 20261231`, then point `index.html`'s fetch call at `data/pun.json` instead of `data/pun_sample.json`.
