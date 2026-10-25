"use node";

/**
 * Reads the Tiger Data (TimescaleDB) telemetry that software/laminar/tiger.py writes, for the Runs page.
 * Tiger is the analytics store (100 Hz arm trace, every event); Convex stays the live store. Actions,
 * not queries, because they talk to Postgres: the page re-calls them on a timer while a run is open.
 *
 * Needs the deployment env var TIGER_DATABASE_URL (npx convex env set TIGER_DATABASE_URL "postgres://...").
 */
import { action } from "./_generated/server";
import { v } from "convex/values";
import pg from "pg";

// Tiger signs service certificates with its own root (CN=ca.timescale.com, expires 2027-10-20), which
// Node does not trust by default. Pinned here so the connection is verified; TIGER_CA overrides it.
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

let pool: pg.Pool | null = null;

function db(): pg.Pool {
  const url = process.env.TIGER_DATABASE_URL;
  if (!url) throw new Error("TIGER_DATABASE_URL is not set on this Convex deployment");
  if (!pool) {
    const u = new URL(url);
    u.searchParams.delete("sslmode"); // pg would replace the ssl options below with its own reading of it
    pool = new pg.Pool({
      connectionString: u.toString(), max: 2, idleTimeoutMillis: 20_000,
      ssl: { ca: process.env.TIGER_CA ?? TIMESCALE_CA, servername: u.hostname },
    });
  }
  return pool;
}

const ms = (d: Date | null) => (d ? d.getTime() : null);

/** Row counts and sizes of the Tiger tables: proof the data is landing, and how fast. */
export const overview = action({
  args: {},
  handler: async () => {
    // approximate_row_count is 0 until a table's stats exist; small tables are cheap to count exactly
    const { rows } = await db().query(`
      SELECT h.hypertable_name AS name,
             hypertable_size(format('%I.%I', h.hypertable_schema, h.hypertable_name)::regclass) AS bytes,
             approximate_row_count(format('%I.%I', h.hypertable_schema, h.hypertable_name)::regclass) AS approx_rows
      FROM timescaledb_information.hypertables h ORDER BY 1`);
    const recent = await db().query(`
      SELECT (SELECT count(*) FROM laminar_events WHERE ts > now() - interval '1 hour') AS events_1h,
             (SELECT count(*) FROM arm_trace WHERE ts > now() - interval '1 hour') AS trace_1h,
             (SELECT count(*) FROM arm_runs) AS runs,
             (SELECT max(ts) FROM laminar_events) AS last_event,
             (SELECT max(ts) FROM arm_trace) AS last_trace`);
    const r = recent.rows[0];
    for (const t of rows) {
      if (Number(t.approx_rows) === 0 && Number(t.bytes) < 50e6) {
        t.approx_rows = (await db().query(`SELECT count(*) AS n FROM ${pg.escapeIdentifier(t.name)}`)).rows[0].n;
      }
    }
    return {
      tables: rows.map((t) => ({ name: t.name as string, bytes: Number(t.bytes), approxRows: Number(t.approx_rows) })),
      events1h: Number(r.events_1h),
      trace1h: Number(r.trace_1h),
      runs: Number(r.runs),
      lastEvent: ms(r.last_event),
      lastTrace: ms(r.last_trace),
    };
  },
});

/** Recent runs, newest first, with their worst joint (highest RMS tracking error). */
export const runs = action({
  args: { limit: v.optional(v.number()) },
  handler: async (_ctx, args) => {
    const { rows } = await db().query(
      `SELECT r.run_id, r.name, r.status, r.started, r.ended, r.meta,
              w.joint AS worst_joint, w.rms_err AS worst_rms, w.max_err AS worst_max
       FROM arm_runs r
       LEFT JOIN LATERAL (
         SELECT * FROM run_smoothness(r.started - interval '1 second',
                                      coalesce(r.ended, now()) + interval '1 second')
         ORDER BY rms_err DESC NULLS LAST LIMIT 1) w ON true
       ORDER BY r.started DESC LIMIT $1`,
      [Math.min(args.limit ?? 25, 100)],
    );
    return rows.map((r) => ({
      runId: r.run_id as string,
      name: (r.name as string | null) ?? null,
      status: r.status as string,
      started: ms(r.started)!,
      ended: ms(r.ended),
      meta: r.meta as Record<string, unknown>,
      worstJoint: r.worst_joint as number | null,
      worstRms: r.worst_rms as number | null,
      worstMax: r.worst_max as number | null,
    }));
  },
});

/** One run: per-joint smoothness, the trace (downsampled to ~600 points), and its events. */
export const runDetail = action({
  args: { runId: v.string() },
  handler: async (_ctx, args) => {
    const run = (await db().query(`SELECT * FROM arm_runs WHERE run_id = $1`, [args.runId])).rows[0];
    if (!run) return null;
    const t0 = new Date(run.started.getTime() - 1000);
    const t1 = new Date((run.ended ?? new Date()).getTime() + 1000);
    const bucketMs = Math.max(20, Math.ceil((t1.getTime() - t0.getTime()) / 600));
    const [metrics, trace, events] = await Promise.all([
      db().query(`SELECT * FROM run_smoothness($1, $2)`, [t0, t1]),
      db().query(`SELECT * FROM run_trace($1, $2, $3::interval)`, [t0, t1, `${bucketMs} milliseconds`]),
      db().query(
        `SELECT ts, event, data FROM laminar_events WHERE run_id = $1 ORDER BY ts LIMIT 500`, [args.runId]),
    ]);
    return {
      runId: run.run_id as string,
      name: (run.name as string | null) ?? null,
      status: run.status as string,
      started: ms(run.started)!,
      ended: ms(run.ended),
      meta: run.meta as Record<string, unknown>,
      bucketMs,
      metrics: metrics.rows.map((m) => ({
        joint: m.joint as number,
        samples: Number(m.samples),
        rmsErr: m.rms_err as number,
        maxErr: m.max_err as number,
        peakVel: m.peak_vel as number,
        peakAcc: m.peak_acc as number,
        rmsJerk: m.rms_jerk as number | null,
        stallFrac: m.stall_frac as number | null,
        peakTorque: m.peak_torque as number,
      })),
      // column arrays, not row objects: ~3600 points stay small over the wire
      trace: {
        t: trace.rows.map((r) => r.t as number),
        joint: trace.rows.map((r) => r.joint as number),
        cmd: trace.rows.map((r) => r.cmd as number),
        pos: trace.rows.map((r) => r.pos as number),
        err: trace.rows.map((r) => r.err as number),
        vel: trace.rows.map((r) => r.vel as number),
        torque: trace.rows.map((r) => r.torque as number),
      },
      events: events.rows.map((e) => ({ t: ms(e.ts)!, event: e.event as string, data: e.data })),
    };
  },
});
