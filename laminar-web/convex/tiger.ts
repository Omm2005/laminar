"use node";
/**
 * Tiger Data (TimescaleDB): Laminar's black-box recorder.
 *
 * Convex is the live store the UI subscribes to. Every pour and every audit event is also written to Tiger
 * as time series, where history and analytics live:
 *   lw_pours           hypertable, one row per pour (1-day chunks, compressed after 7 days, segmented by tube)
 *   lw_events          hypertable, one row per audit event (compressed after 7 days, segmented by actor)
 *   lw_pour_quality_1h real-time continuous aggregate: per hour, per tube, per source: n, mean error, MAE, spread, in-band
 *   lw_arm_tuning      measured joint-tracking results from the team's arm runs (B2b, 2026-10-24)
 *
 * Everything here is optional. Without TIGER_DATABASE_URL every function returns { configured: false }
 * and the bench and the console keep working. Writes never block a run: they happen in scheduled actions.
 */
import pg from "pg";
import { v } from "convex/values";
import { action, internalAction } from "./_generated/server";

// Tiger signs service certificates with its own root (CN=ca.timescale.com, valid to 2027-10-20), which Node
// does not trust by default. Pinned so the connection is still verified; TIGER_CA overrides it when it rotates.
const TIMESCALE_CA = `-----BEGIN CERTIFICATE-----
MIIBpzCCAUygAwIBAgIQfH0seuAygeQX2lTU/eVncDAKBggqhkjOPQQDAjAzMRYw
FAYDVQQKEw1UaW1lc2NhbGUgSW5jMRkwFwYDVQQDExBjYS50aW1lc2NhbGUuY29t
MB4XDTI1MDEyMzE1NDMzOVoXDTI3MTAyMDE1NDMzOVowMzEWMBQGA1UEChMNVGlt
ZXNjYWxlIEluYzEZMBcGA1UEAxMQY2EudGltZXNjYWxlLmNvbTBZMBMGByqGSM49
AgEGCCqGSM49AwEHA0IABKRa3FQeN67oUZK6PdG7FtZKYSv1WgJrZ64mfX9pLNlE
EeVzCnHIAcE9xsQ5j/gccgu9oyiJ/CcLPlkzBHe34M2jQjBAMA4GA1UdDwEB/wQE
AwICpDAPBgNVHRMBAf8EBTADAQH/MB0GA1UdDgQWBBTZ/kgiRgLuL0Tg7eYuwBps
25fwzDAKBggqhkjOPQQDAgNJADBGAiEAvH4JAMgGPL/BSARg47GxjBKJ9Mz+Q3CI
i21+5khjUHECIQCH1kzoKAKTnrkCuifWW9K0CzqXPLSjJBIh3jH2aaWZFQ==
-----END CERTIFICATE-----`;

const SCHEMA = [
  `CREATE TABLE IF NOT EXISTS lw_pours (
     ts TIMESTAMPTZ NOT NULL, run_id TEXT NOT NULL, tube TEXT NOT NULL, well SMALLINT NOT NULL,
     target_ml REAL NOT NULL, poured_ml REAL NOT NULL, error_ml REAL NOT NULL, source TEXT NOT NULL)`,
  `SELECT create_hypertable('lw_pours', by_range('ts', INTERVAL '1 day'), if_not_exists => TRUE)`,
  `CREATE INDEX IF NOT EXISTS lw_pours_tube_ts ON lw_pours (tube, ts DESC)`,
  `CREATE TABLE IF NOT EXISTS lw_events (
     ts TIMESTAMPTZ NOT NULL, run_id TEXT, actor TEXT NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL)`,
  `SELECT create_hypertable('lw_events', by_range('ts', INTERVAL '1 day'), if_not_exists => TRUE)`,
  `CREATE INDEX IF NOT EXISTS lw_events_run_ts ON lw_events (run_id, ts DESC)`,
  `CREATE MATERIALIZED VIEW IF NOT EXISTS lw_pour_quality_1h
     WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
     SELECT time_bucket('1 hour', ts) AS bucket, tube, source,
            count(*) AS n, avg(error_ml) AS mean_err, avg(abs(error_ml)) AS mae, stddev(error_ml) AS sd,
            sum(CASE WHEN abs(error_ml) <= 0.1 * target_ml THEN 1 ELSE 0 END) AS in_band
     FROM lw_pours GROUP BY bucket, tube, source WITH NO DATA`,
  `SELECT add_continuous_aggregate_policy('lw_pour_quality_1h', start_offset => INTERVAL '7 days',
     end_offset => INTERVAL '1 minute', schedule_interval => INTERVAL '5 minutes', if_not_exists => TRUE)`,
  `ALTER TABLE lw_pours SET (timescaledb.compress, timescaledb.compress_segmentby = 'tube', timescaledb.compress_orderby = 'ts DESC')`,
  `SELECT add_compression_policy('lw_pours', INTERVAL '7 days', if_not_exists => TRUE)`,
  `ALTER TABLE lw_events SET (timescaledb.compress, timescaledb.compress_segmentby = 'actor')`,
  `SELECT add_compression_policy('lw_events', INTERVAL '7 days', if_not_exists => TRUE)`,
  `CREATE TABLE IF NOT EXISTS lw_arm_tuning (
     test TEXT NOT NULL, joint SMALLINT NOT NULL, metric TEXT NOT NULL, unit TEXT NOT NULL,
     baseline REAL NOT NULL, improved REAL NOT NULL, baseline_run TEXT NOT NULL, improved_run TEXT NOT NULL,
     PRIMARY KEY (test, joint, metric))`,
];

// Measured on the team's laminar-telemetry Tiger service, from the B2b handoff (grab vial B, carry, return).
// Baseline run-20261024-184019-3cb7 vs improved run-20261024-184344-84be (gravity+friction feedforward fitted
// from the baseline trace, plus a smoothed grab).
const B2B: [number, number, number, number, number, number, number, number, number][] = [
  // joint, rms before, rms after, max before, max after, peak acc before, after, rms jerk before, after
  [1, 0.27, 0.29, 1.02, 0.72, 541, 317, 2392, 2219],
  [2, 2.16, 0.28, 3.42, 1.30, 678, 275, 2657, 2357],
  [3, 2.35, 2.38, 3.04, 3.02, 617, 348, 2390, 2441],
  [4, 2.14, 2.15, 4.13, 3.38, 1011, 268, 2023, 1202],
  [5, 0.36, 0.31, 1.80, 1.45, 506, 240, 1395, 1170],
  [6, 0.17, 0.15, 1.56, 0.40, 335, 192, 1198, 1105],
];

let pool: pg.Pool | null = null;
let ready: Promise<void> | null = null;

function db(): pg.Pool | null {
  const url = process.env.TIGER_DATABASE_URL;
  if (!url) return null;
  if (!pool) {
    const u = new URL(url);
    u.searchParams.delete("sslmode"); // pg would replace the ssl options below with its own reading of it
    pool = new pg.Pool({
      connectionString: u.toString(), max: 2, idleTimeoutMillis: 20_000, connectionTimeoutMillis: 8_000,
      ssl: { ca: process.env.TIGER_CA ?? TIMESCALE_CA, servername: u.hostname },
    });
  }
  return pool;
}

/** Create tables, aggregates and policies once per process. Idempotent. */
async function ensure(p: pg.Pool) {
  ready ??= (async () => {
    for (const sql of SCHEMA) await p.query(sql);
    for (const [j, rb, ra, mb, ma, ab, aa, jb, ja] of B2B) {
      for (const [metric, unit, b, a] of [["rms_error", "deg", rb, ra], ["max_error", "deg", mb, ma], ["peak_acc", "deg/s²", ab, aa], ["rms_jerk", "deg/s³", jb, ja]] as const) {
        await p.query(
          `INSERT INTO lw_arm_tuning VALUES ('B2b', $1, $2, $3, $4, $5, 'run-20261024-184019-3cb7', 'run-20261024-184344-84be')
           ON CONFLICT (test, joint, metric) DO NOTHING`, [j, metric, unit, b, a]);
      }
    }
  })().catch((e) => { ready = null; throw e; });
  return ready;
}

const NOT_CONFIGURED = { configured: false as const };

export const ingestPour = internalAction({
  args: {
    t: v.number(), runId: v.string(), tube: v.string(), well: v.number(),
    targetMl: v.number(), pouredMl: v.number(), errorMl: v.number(), source: v.string(),
  },
  handler: async (_ctx, a) => {
    const p = db();
    if (!p) return;
    await ensure(p);
    await p.query(`INSERT INTO lw_pours VALUES (to_timestamp($1 / 1000.0), $2, $3, $4, $5, $6, $7, $8)`,
      [a.t, a.runId, a.tube, a.well, a.targetMl, a.pouredMl, a.errorMl, a.source]);
  },
});

export const ingestEvent = internalAction({
  args: { t: v.number(), runId: v.union(v.string(), v.null()), actor: v.string(), action: v.string(), detail: v.string() },
  handler: async (_ctx, a) => {
    const p = db();
    if (!p) return;
    await ensure(p);
    await p.query(`INSERT INTO lw_events VALUES (to_timestamp($1 / 1000.0), $2, $3, $4, $5)`, [a.t, a.runId, a.actor, a.action, a.detail]);
  },
});

export const heartbeat = internalAction({
  args: {},
  handler: async () => {
    const p = db();
    if (p) await ensure(p);
  },
});

/** Hypertable sizes, row counts and compression, as proof the data is landing. */
export const overview = action({
  args: {},
  handler: async () => {
    const p = db();
    if (!p) return NOT_CONFIGURED;
    await ensure(p);
    const t0 = Date.now();
    const { rows } = await p.query(`
      SELECT h.hypertable_name AS name, h.num_chunks AS chunks, h.compression_enabled AS compressed,
             hypertable_size(format('%I.%I', h.hypertable_schema, h.hypertable_name)::regclass) AS bytes
      FROM timescaledb_information.hypertables h WHERE h.hypertable_name LIKE 'lw_%' ORDER BY 1`);
    const counts = (await p.query(`
      SELECT (SELECT count(*) FROM lw_pours) AS pours, (SELECT count(*) FROM lw_events) AS events,
             (SELECT count(*) FROM lw_pours WHERE ts > now() - interval '1 hour') AS pours_1h,
             (SELECT max(ts) FROM lw_pours) AS last_pour`)).rows[0];
    const policies = (await p.query(`
      SELECT application_name AS name, schedule_interval::text AS every
      FROM timescaledb_information.jobs WHERE hypertable_name LIKE 'lw_%' ORDER BY 1`)).rows;
    return {
      configured: true as const,
      queryMs: Date.now() - t0,
      tables: rows.map((r) => ({ name: r.name as string, chunks: Number(r.chunks), compression: Boolean(r.compressed), bytes: Number(r.bytes) })),
      pours: Number(counts.pours), events: Number(counts.events), pours1h: Number(counts.pours_1h),
      lastPour: counts.last_pour ? (counts.last_pour as Date).getTime() : null,
      policies: policies.map((r) => ({ name: r.name as string, every: r.every as string })),
    };
  },
});

/** Pour quality per hour and tube, read from the continuous aggregate. */
export const quality = action({
  args: { hours: v.optional(v.number()) },
  handler: async (_ctx, { hours }) => {
    const p = db();
    if (!p) return NOT_CONFIGURED;
    await ensure(p);
    const h = Math.min(Math.max(hours ?? 24, 1), 24 * 14);
    const t0 = Date.now();
    const { rows } = await p.query(
      `SELECT bucket, tube, sum(n)::int AS n, sum(mean_err * n) / sum(n) AS mean_err, sum(mae * n) / sum(n) AS mae,
              sum(in_band)::int AS in_band
       FROM lw_pour_quality_1h WHERE bucket > now() - make_interval(hours => $1)
       GROUP BY bucket, tube ORDER BY bucket, tube`, [h]);
    return {
      configured: true as const,
      queryMs: Date.now() - t0,
      rows: rows.map((r) => ({
        t: (r.bucket as Date).getTime(), tube: r.tube as string, n: r.n as number,
        meanErr: Number(r.mean_err), mae: Number(r.mae), inBand: r.in_band as number,
      })),
    };
  },
});

/** Is any tube drifting? Mean error of each tube's last 10 pours against all its earlier pours. */
export const drift = action({
  args: {},
  handler: async () => {
    const p = db();
    if (!p) return NOT_CONFIGURED;
    await ensure(p);
    const { rows } = await p.query(`
      WITH ranked AS (
        SELECT tube, error_ml, row_number() OVER (PARTITION BY tube ORDER BY ts DESC) AS k FROM lw_pours)
      SELECT tube,
             avg(error_ml) FILTER (WHERE k <= 10) AS recent, count(*) FILTER (WHERE k <= 10)::int AS recent_n,
             avg(error_ml) FILTER (WHERE k > 10) AS before, count(*) FILTER (WHERE k > 10)::int AS before_n
      FROM ranked GROUP BY tube ORDER BY tube`);
    return {
      configured: true as const,
      rows: rows.map((r) => ({
        tube: r.tube as string, recent: r.recent === null ? null : Number(r.recent), recentN: r.recent_n as number,
        before: r.before === null ? null : Number(r.before), beforeN: r.before_n as number,
      })),
    };
  },
});

/** The B2b joint-tracking results, stored in Tiger alongside the pours. */
export const armTuning = action({
  args: {},
  handler: async () => {
    const p = db();
    if (!p) return NOT_CONFIGURED;
    await ensure(p);
    const { rows } = await p.query(`SELECT * FROM lw_arm_tuning WHERE test = 'B2b' ORDER BY metric, joint`);
    return {
      configured: true as const,
      rows: rows.map((r) => ({ joint: r.joint as number, metric: r.metric as string, unit: r.unit as string, baseline: Number(r.baseline), improved: Number(r.improved) })),
      baselineRun: rows[0]?.baseline_run as string | undefined,
      improvedRun: rows[0]?.improved_run as string | undefined,
    };
  },
});

// ---------------------------------------------------------------- the team's arm runs (read-only)
// software/laminar/tiger.py logs every arm run on the team service: arm_runs (one row per run) and
// arm_trace (100 Hz commanded/measured/error/velocity/torque per joint), with SQL functions
// run_smoothness(t0, t1) and run_trace(t0, t1, bucket). These actions only read them.

const ms = (d: Date | null | undefined) => (d ? d.getTime() : null);

async function hasArmTables(p: pg.Pool) {
  const r = await p.query(`SELECT to_regclass('arm_runs') IS NOT NULL AS runs, to_regclass('arm_trace') IS NOT NULL AS trace,
    EXISTS (SELECT 1 FROM pg_proc WHERE proname = 'run_smoothness') AS fn`);
  return r.rows[0].runs && r.rows[0].trace && r.rows[0].fn;
}

/** Recent arm runs, newest first. */
export const armRuns = action({
  args: { limit: v.optional(v.number()) },
  handler: async (_ctx, { limit }) => {
    const p = db();
    if (!p) return NOT_CONFIGURED;
    if (!(await hasArmTables(p))) return { configured: true as const, available: false as const, runs: [] };
    const t0 = Date.now();
    // Just the run rows: per-joint numbers are computed for the selected run only (armRunDetail).
    const { rows } = await p.query(
      `SELECT run_id, name, status, started, ended FROM arm_runs ORDER BY started DESC LIMIT $1`,
      [Math.min(limit ?? 20, 60)],
    );
    return {
      configured: true as const, available: true as const, queryMs: Date.now() - t0,
      runs: rows.map((r) => ({
        runId: r.run_id as string, name: (r.name as string | null) ?? null, status: r.status as string,
        started: ms(r.started)!, ended: ms(r.ended),
      })),
    };
  },
});

/** One run: per-joint smoothness and the trace bucketed to ~600 points per joint, as column arrays. */
export const armRunDetail = action({
  args: { runId: v.string() },
  handler: async (_ctx, { runId }) => {
    const p = db();
    if (!p) return NOT_CONFIGURED;
    const run = (await p.query(`SELECT * FROM arm_runs WHERE run_id = $1`, [runId])).rows[0];
    if (!run) return { configured: true as const, found: false as const };
    const a = new Date(run.started.getTime() - 1000);
    const b = new Date((run.ended ?? new Date()).getTime() + 1000);
    const bucketMs = Math.max(20, Math.ceil((b.getTime() - a.getTime()) / 600));
    const t0 = Date.now();
    const [metrics, trace, raw] = await Promise.all([
      p.query(`SELECT * FROM run_smoothness($1, $2)`, [a, b]),
      p.query(`SELECT * FROM run_trace($1, $2, $3::interval)`, [a, b, `${bucketMs} milliseconds`]),
      p.query(`SELECT joint, count(*)::int AS n FROM arm_trace WHERE ts BETWEEN $1 AND $2 GROUP BY joint ORDER BY joint`, [a, b]),
    ]);
    const start = a.getTime() + 1000;
    return {
      configured: true as const, found: true as const, queryMs: Date.now() - t0,
      runId, name: (run.name as string | null) ?? null, status: run.status as string,
      started: ms(run.started)!, ended: ms(run.ended), bucketMs,
      rawSamples: Object.fromEntries(raw.rows.map((r) => [r.joint as number, r.n as number])),
      metrics: metrics.rows.map((m) => ({
        joint: m.joint as number, samples: Number(m.samples), rmsErr: Number(m.rms_err), maxErr: Number(m.max_err),
        peakVel: Number(m.peak_vel), peakAcc: Number(m.peak_acc), rmsJerk: m.rms_jerk === null ? null : Number(m.rms_jerk),
        stallFrac: m.stall_frac === null ? null : Number(m.stall_frac), peakTorque: Number(m.peak_torque),
      })),
      // seconds from the run's start, per joint
      trace: trace.rows.map((r) => ({
        s: Math.round(((r.t as number) - start) / 10) / 100, j: r.joint as number,
        cmd: r.cmd as number, pos: r.pos as number, err: r.err as number, vel: r.vel as number,
      })),
    };
  },
});
