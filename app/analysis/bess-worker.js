/*
 * Merchant battery optimiser, for Analysis › Merchant revenues battery
 * calculator (app/index.html).  A Web Worker: the page sends it batches of
 * days, each with the zone's prices of the day, and it sends back the
 * battery's schedule and its money in each market.  Every day is solved on
 * its own, from and back to the same state of charge at midnight, as a
 * linear programme; where its answer charges and discharges in the same
 * quarter-hour, again as a mixed-integer one with a binary per quarter-hour
 * that rules it out.  The solver is HiGHS, the
 * open-source solver of the University of Edinburgh, built for WebAssembly
 * (highs-js, app/lib/highs, MIT licence).
 *
 * Markets, in the order their gates close (GATE_ORDER):
 *   MGP   day-ahead auction, zonal price (hourly products before 1 Oct 2025)
 *   MIA1  intraday auction MI-A1 (D-1 15:00, the whole day)
 *   MIA2  intraday auction MI-A2 (D-1 22:00, the whole day)
 *   MSD   ex-ante dispatching services market: upward sales and downward
 *         purchases at the zone's average accepted prices, each capped at a
 *         share of the zone's accepted volume (pay-as-bid; shares upward and
 *         downward apart)
 *   MIA3  intraday auction MI-A3 (D 10:00, 12:00 to 24:00)
 *   XBID  continuous intraday MI-XBID: the 15-minute products' reference
 *         price (volume-weighted average of the trades), capped at a share
 *         of the battery's power
 *   MB    balancing market, as MSD (the other services, not the secondary
 *         reserve's band)
 *
 * Strategies:
 *   sequential  market by market in that order: each market's position is
 *               the best the battery can do knowing that market's prices,
 *               on top of what the earlier markets left it, without knowing
 *               the later ones; every intermediate schedule is feasible.
 *   joint       all markets at once, knowing every price from the start
 *               (the ceiling): the schedule after each gate must still be
 *               one the battery can run (power and state of charge), so
 *               positions are not opened in one auction only to be closed
 *               in the next on paper.
 *   split       fixed shares of power and energy: a slice for the MGP, one
 *               for the intraday markets (MI-A1, MI-A2, MI-A3, MI-XBID in
 *               order) and one for MSD and MB, each run sequentially alone.
 *
 * Per day the page sends (T quarter-hours: 92, 96 or 100):
 *   { date, T, hourlyMgp, factor, prices: { MGP, MIA1, MIA2, MIA3, XBID },
 *     balancing: { MSD: { upP, upV, downP, downV }, MB: { ... } } }
 *   prices in EUR/MWh, volumes in MW, NaN where there is none; factor
 *   scales the energy (availability and capacity fade).
 * and gets back
 *   { date, T, ok, markets: { KEY: [MWh sold, MWh bought, EUR sold, EUR bought] },
 *     charged, discharged (MWh), cashIn, cashOut (EUR received and paid, netted
 *     quarter-hour by quarter-hour across the markets), throughputCost, chargeCost, fees (EUR),
 *     net: { KEY: Float32Array(T) } (MW sold minus bought, upward minus downward),
 *     soc: Float32Array(T) (MWh at the end of each quarter-hour) }
 */

importScripts("../lib/highs/highs.js");

const GATE_ORDER = ["MGP", "MIA1", "MIA2", "MSD", "MIA3", "XBID", "MB"];
const ENERGY = new Set(["MGP", "MIA1", "MIA2", "MIA3", "XBID"]);
const SLICES = [
  { key: "dayAhead", markets: ["MGP"] },
  { key: "intraday", markets: ["MIA1", "MIA2", "MIA3", "XBID"] },
  { key: "balancing", markets: ["MSD", "MB"] }
];
const DT = 0.25;                       // hours in a quarter-hour

let highsPromise = null;
const loadSolver = () => highsPromise || (highsPromise = Module({
  locateFile: file => new URL(`../lib/highs/${file}`, self.location.href).href
}));

/* A linear programme built row by row (CSR), maximised. */
class Lp {
  constructor() {
    this.cost = []; this.lower = []; this.upper = []; this.integer = [];
    this.rowLower = []; this.rowUpper = []; this.starts = [0]; this.indices = []; this.values = [];
  }
  col(lower, upper, cost = 0, integer = false) {
    this.cost.push(cost); this.lower.push(lower); this.upper.push(upper); this.integer.push(integer ? 1 : 0);
    return this.cost.length - 1;
  }
  row(lower, upper, indices, values) {
    indices.forEach((index, k) => { if (index >= 0 && values[k] !== 0) { this.indices.push(index); this.values.push(values[k]); } });
    this.starts.push(this.indices.length);
    this.rowLower.push(lower); this.rowUpper.push(upper);
  }
  data(highs) {
    const numCols = this.cost.length, numRows = this.rowLower.length;
    const model = {
      numCols, numRows, sense: highs.constants.objectiveSense.maximize,
      colCost: Float64Array.from(this.cost), colLower: Float64Array.from(this.lower), colUpper: Float64Array.from(this.upper),
      rowLower: Float64Array.from(this.rowLower, value => (value === -Infinity ? -highs.infinity : value)),
      rowUpper: Float64Array.from(this.rowUpper, value => (value === Infinity ? highs.infinity : value)),
      matrix: { format: "csr", numRows, numCols, starts: Int32Array.from(this.starts),
        indices: Int32Array.from(this.indices), values: Float64Array.from(this.values) }
    };
    if (this.integer.some(Boolean)) model.integrality = Int32Array.from(this.integer);
    return model;
  }
}

const finite = value => Number.isFinite(value);
const EPS = 1e-3;                      // MW: below it, the solver's tolerance, not a trade

/*
 * What the battery can do in a market on a day: the price and the most MW
 * each way per quarter-hour.  Volume shares of MSD and MB: { up, down } as
 * fractions of the zone's accepted volume.
 */
function marketSpec(key, day, params, power) {
  const T = day.T;
  const sellPrice = new Float64Array(T), buyPrice = new Float64Array(T);
  const sellCap = new Float64Array(T), buyCap = new Float64Array(T);
  if (ENERGY.has(key)) {
    const prices = day.prices[key];
    const cap = key === "XBID" ? params.xbidShare * power : 2 * power;
    for (let t = 0; t < T; t += 1) {
      const price = prices ? prices[t] : NaN;
      if (!finite(price) || !(cap > 0)) continue;
      sellPrice[t] = price; buyPrice[t] = price; sellCap[t] = cap; buyCap[t] = cap;
    }
  } else {
    const series = day.balancing[key];
    const share = params.shares[key] || { up: 0, down: 0 };
    for (let t = 0; t < T; t += 1) {
      if (!series) break;
      if (finite(series.upP[t]) && finite(series.upV[t]) && series.upV[t] > 0 && share.up > 0) {
        sellPrice[t] = series.upP[t]; sellCap[t] = Math.min(2 * power, share.up * series.upV[t]);
      }
      if (finite(series.downP[t]) && finite(series.downV[t]) && series.downV[t] > 0 && share.down > 0) {
        buyPrice[t] = series.downP[t]; buyCap[t] = Math.min(2 * power, share.down * series.downV[t]);
      }
    }
  }
  const active = sellCap.some(value => value > 1e-9) || buyCap.some(value => value > 1e-9);
  return { key, sellPrice, buyPrice, sellCap, buyCap, hourly: key === "MGP" && day.hourlyMgp, fee: ENERGY.has(key), active };
}

/* A battery schedule in the programme: charge, discharge, state of charge (and the binaries). */
function addPhysical(lp, battery, params, integer, costed) {
  const { T, power, eMin, eMax, e0, cycleMWh } = battery;
  const etaC = Math.sqrt(params.rte), etaD = Math.sqrt(params.rte);
  const c = [], d = [], e = [];
  for (let t = 0; t < T; t += 1) {
    c.push(lp.col(0, power, costed ? -params.chargeCost * DT : 0));
    d.push(lp.col(0, power, costed ? -params.throughputCost * DT : 0));
    e.push(t === T - 1 ? lp.col(e0, e0) : lp.col(eMin, eMax));
  }
  for (let t = 0; t < T; t += 1) {
    if (t === 0) lp.row(e0, e0, [e[t], c[t], d[t]], [1, -DT * etaC, DT / etaD]);
    else lp.row(0, 0, [e[t], e[t - 1], c[t], d[t]], [1, -1, -DT * etaC, DT / etaD]);
    if (integer) {
      const z = lp.col(0, 1, 0, true);
      lp.row(-Infinity, 0, [c[t], z], [1, -power]);
      lp.row(-Infinity, power, [d[t], z], [1, power]);
    }
  }
  lp.row(-Infinity, cycleMWh, d, d.map(() => DT));
  return { c, d, e };
}

/* Charging and discharging in the same quarter-hour: only where wasting energy pays. */
const overlaps = (x, physical) => physical.c.some((index, t) => x[index] > EPS && x[physical.d[t]] > EPS);

/*
 * One optimisation: the battery trades the markets given (specs, in gate
 * order) on top of the net position g0 that earlier markets left.  Joint:
 * the schedule after each market's gate is a battery schedule of its own
 * (power, state of charge, cycles); the last one is the battery's.
 * Solved as an LP; again with binaries if the LP's answer charges and
 * discharges at once.
 */
function solve(highs, battery, specs, g0, joint, params, integer = false) {
  const T = battery.T;
  const lp = new Lp();
  const sell = specs.map(() => new Int32Array(T).fill(-1));
  const buy = specs.map(() => new Int32Array(T).fill(-1));
  specs.forEach((spec, m) => {
    const fee = spec.fee ? params.fee : 0;
    for (let t = 0; t < T; t += 1) {
      if (spec.sellCap[t] > 1e-9) sell[m][t] = lp.col(0, spec.sellCap[t], (spec.sellPrice[t] - fee) * DT);
      if (spec.buyCap[t] > 1e-9) buy[m][t] = lp.col(0, spec.buyCap[t], (-spec.buyPrice[t] - fee) * DT);
    }
  });
  // Joint: a schedule after each gate; otherwise the final one only.
  const stages = joint ? specs.length : 1;
  let physical = null;
  for (let k = 0; k < stages; k += 1) {
    const last = k === stages - 1;
    const schedule = addPhysical(lp, battery, params, last && integer, last);
    const upTo = joint ? k : specs.length - 1;
    for (let t = 0; t < T; t += 1) {
      // Net injection = what was sold minus what was bought, in every market so far.
      const indices = [schedule.d[t], schedule.c[t]], values = [1, -1];
      for (let m = 0; m <= upTo; m += 1) { indices.push(sell[m][t], buy[m][t]); values.push(-1, 1); }
      lp.row(g0[t], g0[t], indices, values);
    }
    if (last) physical = schedule;
  }
  // Hourly products: the same position in the hour's four quarter-hours.
  specs.forEach((spec, m) => {
    if (!spec.hourly) return;
    for (let t = 0; t + 3 < T; t += 4) {
      for (let q = 1; q < 4; q += 1) {
        lp.row(0, 0, [sell[m][t + q], buy[m][t + q], sell[m][t], buy[m][t]], [1, -1, -1, 1]);
      }
    }
  });

  const model = highs.createModel(lp.data(highs));
  let status, x;
  try {
    model.options.set({ output_flag: false, time_limit: 20, mip_rel_gap: 1e-5 });
    model.run();
    status = model.getModelStatus();
    x = model.getSolution().colValue;
  } finally {
    model.dispose();
  }
  if (status !== highs.constants.modelStatus.optimal) throw new Error(`HiGHS status ${status}`);
  if (!integer && overlaps(x, physical)) return solve(highs, battery, specs, g0, joint, params, true);
  const value = index => (index >= 0 ? x[index] : 0);
  return {
    integer,
    c: Float64Array.from(physical.c, value), d: Float64Array.from(physical.d, value), e: Float64Array.from(physical.e, value),
    sell: sell.map(row => Float64Array.from(row, value)), buy: buy.map(row => Float64Array.from(row, value))
  };
}

/* The battery of a slice on a day. */
function batteryOf(day, params, share) {
  const power = params.power * params.availability * share;
  const energy = params.energy * params.availability * day.factor * share;
  return {
    T: day.T, power, eMin: params.socMin * energy, eMax: params.socMax * energy, e0: params.soc0 * energy,
    cycleMWh: params.cycles * (params.socMax - params.socMin) * energy
  };
}

const idleSchedule = battery => ({ sell: {}, buy: {}, c: new Float64Array(battery.T), d: new Float64Array(battery.T),
  e: new Float64Array(battery.T).fill(battery.e0), integer: 0 });

/* Market by market in gate order; returns positions per market and the final physical schedule. */
function runSequential(highs, day, params, keys, share) {
  const battery = batteryOf(day, params, share);
  const T = day.T;
  const g0 = new Float64Array(T);
  const out = idleSchedule(battery);
  keys.forEach(key => {
    const spec = marketSpec(key, day, params, battery.power);
    if (!spec.active) return;
    const result = solve(highs, battery, [spec], g0, false, params);
    out.sell[key] = result.sell[0]; out.buy[key] = result.buy[0];
    for (let t = 0; t < T; t += 1) g0[t] += result.sell[0][t] - result.buy[0][t];
    out.c = result.c; out.d = result.d; out.e = result.e;
    out.integer += result.integer ? 1 : 0;
  });
  return out;
}

function runJoint(highs, day, params, keys) {
  const battery = batteryOf(day, params, 1);
  const specs = keys.map(key => marketSpec(key, day, params, battery.power)).filter(spec => spec.active);
  if (!specs.length) return idleSchedule(battery);
  const result = solve(highs, battery, specs, new Float64Array(day.T), true, params);
  const out = { sell: {}, buy: {}, c: result.c, d: result.d, e: result.e, integer: result.integer ? 1 : 0 };
  specs.forEach((spec, m) => { out.sell[spec.key] = result.sell[m]; out.buy[spec.key] = result.buy[m]; });
  return out;
}

function runDay(highs, day, params) {
  const enabled = GATE_ORDER.filter(key => params.markets[key]);
  const T = day.T;
  let schedule;
  if (params.strategy === "joint") {
    schedule = runJoint(highs, day, params, enabled);
  } else if (params.strategy === "split") {
    schedule = { sell: {}, buy: {}, c: new Float64Array(T), d: new Float64Array(T), e: new Float64Array(T), integer: 0 };
    SLICES.forEach(slice => {
      const share = params.split[slice.key] || 0;
      const keys = slice.markets.filter(key => params.markets[key]);
      if (share <= 0) return;
      const part = runSequential(highs, day, params, keys, share);
      Object.keys(part.sell).forEach(key => { schedule.sell[key] = part.sell[key]; schedule.buy[key] = part.buy[key]; });
      for (let t = 0; t < T; t += 1) {
        schedule.c[t] += part.c[t]; schedule.d[t] += part.d[t]; schedule.e[t] += part.e[t];
      }
      schedule.integer += part.integer;
    });
    // A slice left out (share 0) keeps its energy at the midnight level.
    const idle = 1 - SLICES.reduce((sum, slice) => sum + Math.max(0, params.split[slice.key] || 0), 0);
    if (idle > 1e-9) {
      const e0 = batteryOf(day, params, idle).e0;
      for (let t = 0; t < T; t += 1) schedule.e[t] += e0;
    }
  } else {
    schedule = runSequential(highs, day, params, enabled, 1);
  }

  const markets = {}, net = {};
  const cash = new Float64Array(T);     // EUR received (+) or paid (-) in each quarter-hour, all markets together
  let fees = 0;
  GATE_ORDER.forEach(key => {
    const sell = schedule.sell[key], buy = schedule.buy[key];
    if (!sell) return;
    const spec = marketSpec(key, day, params, Infinity);
    let sold = 0, bought = 0, income = 0, spend = 0;
    const position = new Float32Array(T);
    for (let t = 0; t < T; t += 1) {
      // Below 1 kW is the solver's tolerance, not a trade.
      const s = sell[t] > EPS ? sell[t] : 0, b = buy[t] > EPS ? buy[t] : 0;
      sold += s * DT; bought += b * DT;
      income += s * DT * spec.sellPrice[t]; spend += b * DT * spec.buyPrice[t];
      cash[t] += (s * spec.sellPrice[t] - b * spec.buyPrice[t]) * DT;
      position[t] = s - b;
    }
    if (ENERGY.has(key)) fees += (sold + bought) * params.fee;
    markets[key] = [sold, bought, income, spend];
    net[key] = position;
  });
  // Revenues and purchases netted quarter-hour by quarter-hour: a position sold in one market and
  // bought back in a later one for the same quarter-hour counts once, as the margin between them.
  let charged = 0, discharged = 0, cashIn = 0, cashOut = 0;
  for (let t = 0; t < T; t += 1) {
    charged += schedule.c[t] * DT; discharged += schedule.d[t] * DT;
    if (cash[t] > 0) cashIn += cash[t]; else cashOut -= cash[t];
  }
  return {
    date: day.date, T, ok: true, markets, net, soc: Float32Array.from(schedule.e), integer: schedule.integer,
    charged, discharged, cashIn, cashOut, throughputCost: discharged * params.throughputCost, chargeCost: charged * params.chargeCost, fees
  };
}

self.onmessage = async event => {
  const { type, id, params, days } = event.data;
  if (type !== "run") return;
  let highs;
  try {
    highs = await loadSolver();
  } catch (error) {
    self.postMessage({ type: "error", id, message: `The solver did not load: ${error.message || error}` });
    return;
  }
  const results = [];
  days.forEach(day => {
    try {
      results.push(runDay(highs, day, params));
    } catch (error) {
      results.push({ date: day.date, T: day.T, ok: false, message: String(error.message || error) });
    }
  });
  const transfer = [];
  results.forEach(result => {
    if (!result.ok) return;
    transfer.push(result.soc.buffer, ...Object.values(result.net).map(array => array.buffer));
  });
  self.postMessage({ type: "days", id, results }, transfer);
};
