// Shorthop Watch on the Integrations page (relay SHOR-31 / W7).
//
// Shorthop Watch is a dead man's switch the push relay runs OUTSIDE this installation: a check
// expects a ping on a schedule, and when it stops the relay pages this installation's phones
// itself. That is the only way to be paged when Shorthop, its database or the whole cluster
// is down — which Shorthop's own ShorthopIngestSilent cannot do, because it runs in here.
//
// Two places: a box on each Prometheus integration ("watch this from outside" = forward its
// Watchdog to a check), and a list of every check, including ones a cron job or backup pings.

import { useState } from "react";
import { api, type Integration, type WatchCheck, type WatchList } from "../api";
import { Icon } from "../components/Icon";
import { ErrorNotice, Field, ShowOnce } from "../components/ui";
import { relative } from "../time";

const STATE: Record<WatchCheck["state"], { tone: string; text: string }> = {
  new: { tone: "neutral", text: "never pinged" },
  up: { tone: "ok", text: "checking in" },
  down: { tone: "danger", text: "not checking in" },
};

function every(s: number): string {
  if (s % 86400 === 0) return `${s / 86400} d`;
  if (s % 3600 === 0) return `${s / 3600} h`;
  if (s % 60 === 0) return `${s / 60} min`;
  return `${s} s`;
}

function stateBadge(c: WatchCheck) {
  if (c.paused) return <span className="badge neutral">paused</span>;
  const s = STATE[c.state];
  return <span className={`badge ${s.tone}`}>{s.text}</span>;
}

/** The box on one Prometheus integration. */
export function WatchBox({ integ, watch, watchError, manage, now, onChange, onError }: {
  integ: Integration; watch: WatchList | null; watchError: string | null; manage: boolean;
  now: number; onChange: () => void; onError: (m: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const check = watch?.checks.find((c) => c.id === integ.watch_check_id) ?? null;

  const start = async () => {
    setBusy(true);
    try {
      await api.createWatchCheck({ name: integ.name, integration_id: integ.id });
      onChange();
    } catch (e) { onError((e as Error).message); } finally { setBusy(false); }
  };
  const stop = async () => {
    if (!window.confirm(`Stop forwarding ${integ.name}'s Watchdog? The Watch check stays, and will page your phones once its grace runs out unless something else pings it — pause or delete it below if it is no longer wanted.`)) return;
    try { await api.unbindWatch(integ.id); onChange(); } catch (e) { onError((e as Error).message); }
  };

  if (integ.watch_check_id) {
    return (
      <div className="shadow-box">
        {/* Green only while the check is actually fine: a DOWN check must not read as reassurance. */}
        <p className={check?.state === "down" && !check.paused ? "notice notice--warn notice--icon" : "shadow-ok"}>
          <Icon name={check?.state === "down" && !check.paused ? "alert" : "eye"} size={15} />
          <span>Watched from outside by Shorthop Watch
            {check ? <> — {stateBadge(check)}</> : null}
            <span className="muted small"> · Watchdog last forwarded {integ.watch_forwarded_at ? relative(integ.watch_forwarded_at, now) : "not yet"}</span>
          </span></p>
        {manage && <div className="row-actions"><button className="btn-ghost btn-sm" onClick={stop}>Stop forwarding</button></div>}
      </div>
    );
  }
  return (
    <div className="shadow-box">
      <p className="muted small">Not watched from outside. If this Prometheus's Watchdog stops reaching Shorthop, Shorthop
        raises ShorthopIngestSilent — but if Shorthop itself, its database or the cluster is down, nothing does.
        Shorthop Watch runs on the push relay and pages your phones directly when that happens.</p>
      {manage && (
        <div className="row-actions">
          <button className="btn-ghost btn-sm" disabled={busy || !!watchError} onClick={start}>
            <Icon name="eye" size={14} /> {busy ? "Starting…" : "Watch from outside"}
          </button>
          {watchError && <span className="muted small">{watchError}</span>}
        </div>
      )}
    </div>
  );
}

/** Every check on the relay. */
export function WatchSection({ watch, watchError, manage, now, onChange }: {
  watch: WatchList | null; watchError: string | null; manage: boolean; now: number; onChange: () => void;
}) {
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [interval, setEvery] = useState("60");
  const [grace, setGrace] = useState("15");
  const [shown, setShown] = useState<WatchCheck | null>(null);
  const [error, setError] = useState<string | null>(null);

  const act = async (fn: () => Promise<unknown>) => {
    setError(null);
    try { await fn(); onChange(); } catch (e) { setError((e as Error).message); }
  };
  const create = () => act(async () => {
    const c = await api.createWatchCheck({ name: name.trim(), interval_s: Math.round(Number(interval) * 60),
      grace_s: Math.round(Number(grace) * 60) });
    setShown(c); setCreating(false); setName("");
  });
  const pause = (c: WatchCheck) => {
    const h = window.prompt(`Pause ${c.name} for how many hours? (at most 30 days)`, "4");
    if (!h) return;
    const until = new Date(Date.now() + Number(h) * 3600_000).toISOString();
    void act(() => api.pauseWatchCheck(c.id, until));
  };
  const remove = (c: WatchCheck) => {
    if (!window.confirm(`Delete ${c.name}? It stops paging at once and its ping URL stops working.`)) return;
    void act(() => api.deleteWatchCheck(c.id));
  };
  const rotate = (c: WatchCheck) => {
    if (!window.confirm(`New ping URL for ${c.name}? The current one stops working now — whatever pings it must be updated.${c.forwarded_by ? " (Forwarding from " + c.forwarded_by.name + " switches over by itself.)" : ""}`)) return;
    void act(async () => setShown(await api.rotateWatchCheck(c.id)));
  };

  return (
    <section className="watch-section">
      <div className="section-head">
        <h2>Shorthop Watch</h2>
        {manage && !creating && !watchError && (
          <button className="btn-primary" onClick={() => { setCreating(true); setShown(null); }}>
            <Icon name="plus" size={15} /> New check
          </button>
        )}
      </div>
      <p className="glance">Checks that must be pinged on a schedule, watched by the push relay outside this installation.
        When one stops, your phones are paged directly — even if this server is down. Use them for the Watchdogs above, and
        for backups, cron jobs or anything else that should run.{watch ? ` First ${watch.free} free, then €1.99 a check a month.` : ""}</p>
      {watchError && <p className="notice notice--warn notice--icon"><Icon name="alert" size={15} /><span>{watchError}</span></p>}
      <ErrorNotice>{error}</ErrorNotice>

      {shown?.ping_url && (
        <ShowOnce label={`Ping URL for ${shown.name}`} value={shown.ping_url}>
          {shown.forwarded_by
            ? <p className="muted small">Shorthop already pings this for {shown.forwarded_by.name}'s Watchdog; nothing to copy.</p>
            : <>
                <p className="muted small">Ping it at the end of the job, e.g.:</p>
                <pre className="snippet">{shown.snippets?.cron}</pre>
                <p className="muted small">Or have Shorthop itself ping it (proves its housekeeping runs): <code>{shown.snippets?.heartbeat_env}</code></p>
              </>}
        </ShowOnce>
      )}

      {creating && (
        <form className="card pad form-stack narrow" onSubmit={(e) => { e.preventDefault(); void create(); }}>
          <Field label="Name"><input required maxLength={128} value={name} onChange={(e) => setName(e.target.value)} placeholder="backup-nightly" /></Field>
          <Field label="Expected every (minutes)" hint="How often whatever pings it runs.">
            <input type="number" min={1} required value={interval} onChange={(e) => setEvery(e.target.value)} />
          </Field>
          <Field label="Grace (minutes)" hint="How late a ping may be before your phones are paged.">
            <input type="number" min={1} required value={grace} onChange={(e) => setGrace(e.target.value)} />
          </Field>
          <div className="detail-actions">
            <button className="btn-primary" disabled={!name.trim()}>Create</button>
            <button type="button" className="btn-ghost" onClick={() => setCreating(false)}>Cancel</button>
          </div>
        </form>
      )}

      {watch && watch.checks.length === 0 && !creating && (
        <p className="muted small">No checks yet.</p>
      )}
      {watch && watch.checks.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead><tr><th>Check</th><th>State</th><th>Last ping</th><th>Every</th><th>Pinged by</th>{manage && <th />}</tr></thead>
            <tbody>
              {watch.checks.map((c) => (
                <tr key={c.id}>
                  <td>{c.name}{c.last_note ? <div className="muted small">{c.last_note}</div> : null}</td>
                  <td>{stateBadge(c)}{c.paused && c.paused_until ? <div className="muted small">ends {relative(c.paused_until, now)}</div> : null}</td>
                  <td className="nowrap">{c.last_ping_at ? relative(c.last_ping_at, now) : "never"}</td>
                  <td className="nowrap">{every(c.interval_s)} <span className="muted small">+{every(c.grace_s)}</span></td>
                  <td>{c.forwarded_by ? <>Watchdog of {c.forwarded_by.name}</> : <span className="muted">your own job</span>}</td>
                  {manage && (
                    <td><div className="row-actions">
                      {c.paused
                        ? <button className="btn-ghost btn-sm" onClick={() => act(() => api.resumeWatchCheck(c.id))}>Resume</button>
                        : <button className="btn-ghost btn-sm" onClick={() => pause(c)}>Pause</button>}
                      <button className="btn-ghost btn-sm" onClick={() => rotate(c)}><Icon name="refresh" size={14} /> New URL</button>
                      <button className="btn-ghost btn-sm btn-danger" aria-label={`Delete ${c.name}`} onClick={() => remove(c)}><Icon name="trash" size={14} /></button>
                    </div></td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
