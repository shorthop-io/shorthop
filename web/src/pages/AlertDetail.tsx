import { useState } from "react";
import { api, insightApi, RESOLUTION_CATEGORIES, type AlertDetail as Detail, type HistoryItem, type Related } from "../api";
import { Link, safeHref, useMe, useNavigate, useNow, usePoll } from "../app";
import { Icon } from "../components/Icon";
import { Empty, ErrorNotice, Labels, SeverityBadge, StatusBadge } from "../components/ui";
import { duration, relative, wall } from "../time";

const categoryLabel = (v: string | null) =>
  RESOLUTION_CATEGORIES.find((c) => c.value === v)?.label ?? v ?? "";

const HOW: Record<string, string> = {
  source: "resolved by the sender",
  timeout: "stopped being sent",
  manual: "resolved by hand",
};

const EVENT_ICON: Record<string, "alert" | "check" | "note" | "user" | "clock"> = {
  fired: "alert", acknowledged: "user", resolved: "check", note: "note", resolution_noted: "note",
};

export function AlertDetail({ id }: { id: string }) {
  const navigate = useNavigate();
  const { can } = useMe();
  const now = useNow();
  const { data, error, setData } = usePoll(() => api.getAlert(id), [id], 30000);
  const [category, setCategory] = useState("");
  const [note, setNote] = useState("");
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const act = async (fn: () => Promise<Detail>, after?: () => void) => {
    setBusy(true);
    setActionError(null);
    try {
      setData(await fn());
      after?.();
    } catch (e) {
      setActionError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  if (error && !data) {
    return <div className="page"><BackLink /><ErrorNotice>{error}</ErrorNotice></div>;
  }
  if (!data) return <div className="page"><BackLink /><p className="muted">Loading…</p></div>;

  const a = data;
  const resolved = a.status === "resolved";
  const runbook = safeHref(a.annotations.runbook_url);
  const source = safeHref(a.generator_url);
  const fixRecorded = Boolean(a.resolution_category || a.resolution_note);
  const otherAnnotations = Object.entries(a.annotations)
    .filter(([k]) => !["summary", "description", "runbook_url"].includes(k));

  return (
    <div className="page detail-page">
      <BackLink />
      <div className="detail-head">
        <div>
          <h2>{a.alertname}</h2>
          <div className="detail-badges">
            <SeverityBadge severity={a.severity} />
            <StatusBadge alert={a} />
            <span className="muted small">
              started {wall(a.started_at)} · {resolved ? `lasted ${duration(a.started_at, a.resolved_at, now)}` : `firing for ${duration(a.started_at, null, now)}`}
            </span>
          </div>
        </div>
        <div className="detail-links">
          {runbook && <a className="btn-ghost btn-sm" href={runbook} target="_blank" rel="noreferrer"><Icon name="book" size={14} /> Runbook</a>}
          {source && <a className="btn-ghost btn-sm" href={source} target="_blank" rel="noreferrer"><Icon name="chart" size={14} /> Source</a>}
        </div>
      </div>

      {(a.annotations.summary || a.annotations.description) && (
        <div className="card detail-summary">
          {a.annotations.summary && <p className="summary-line">{a.annotations.summary}</p>}
          {a.annotations.description && <p className="muted">{a.annotations.description}</p>}
        </div>
      )}

      <ErrorNotice>{actionError}</ErrorNotice>

      <div className="detail-grid">
        <div className="detail-col">
          {can("alerts:write") && (
            <section className="card pad">
              <h3 className="sub">{resolved ? (fixRecorded ? "What fixed it" : "Record what fixed it") : "Respond"}</h3>
              {!resolved && (
                <div className="detail-actions">
                  {a.status === "firing" && (
                    <button className="btn-primary" disabled={busy} onClick={() => act(() => api.ack(a.id))}>
                      <Icon name="check" size={15} /> Acknowledge
                    </button>
                  )}
                  {a.status === "acknowledged" && <span className="muted small">Acknowledged by {a.acked_by} {relative(a.acked_at, now)}</span>}
                  <button className="btn-ghost" onClick={() => navigate(`/silences/new?alert=${encodeURIComponent(a.id)}`)}>
                    <Icon name="bellOff" size={15} /> Silence…
                  </button>
                </div>
              )}
              {resolved && fixRecorded && (
                <p className="fix-line">
                  {a.resolution_category && <span className="badge info">{categoryLabel(a.resolution_category)}</span>}{" "}
                  {a.resolution_note}
                </p>
              )}
              <form className="resolve-form" onSubmit={(e) => {
                e.preventDefault();
                act(() => api.resolve(a.id, { category: category || undefined, note: note.trim() || undefined }),
                  () => { setNote(""); setCategory(""); });
              }}>
                <label>
                  {resolved ? "Category" : "Resolve as"}
                  <select value={category} onChange={(e) => setCategory(e.target.value)}>
                    <option value="">— choose —</option>
                    {RESOLUTION_CATEGORIES.map((c) => <option key={c.value} value={c.value}>{c.label}</option>)}
                  </select>
                </label>
                <label>
                  What fixed it?
                  <textarea rows={3} value={note} maxLength={4000} onChange={(e) => setNote(e.target.value)}
                    placeholder="The next person paged for this alert reads this." />
                </label>
                <div>
                  <button className="btn-primary" disabled={busy || (resolved && !category && !note.trim())}>
                    {resolved ? (fixRecorded ? "Update the fix" : "Save the fix") : "Resolve"}
                  </button>
                </div>
                {!resolved && <p className="notice">If the condition is still true, the sender fires it again as a new occurrence.</p>}
              </form>
            </section>
          )}

          <History history={a.history} now={now} />
          <FiresWith signature={a.signature} />

          <section className="card pad">
            <h3 className="sub">Timeline</h3>
            <ol className="timeline">
              {a.events.map((ev, i) => (
                <li key={i}>
                  <span className={`tl-dot tl-${ev.kind}`}><Icon name={EVENT_ICON[ev.kind] ?? "clock"} size={13} /></span>
                  <div>
                    <div><strong>{ev.kind.replace("_", " ")}</strong>{ev.actor && <span className="muted"> · {ev.actor}</span>}</div>
                    {ev.detail && <div className="tl-detail">{ev.detail}</div>}
                    <div className="muted small" title={ev.at}>{wall(ev.at)} · {relative(ev.at, now)}</div>
                  </div>
                </li>
              ))}
            </ol>
            {can("alerts:write") && (
              <form className="note-form" onSubmit={(e) => {
                e.preventDefault();
                if (comment.trim()) act(() => api.note(a.id, comment.trim()), () => setComment(""));
              }}>
                <input value={comment} maxLength={4000} placeholder="Add a note — findings, links, what you tried"
                  aria-label="Add a note" onChange={(e) => setComment(e.target.value)} />
                <button className="btn-ghost" disabled={busy || !comment.trim()}>Add note</button>
              </form>
            )}
          </section>
        </div>

        <div className="detail-col">
          <section className="card pad">
            <h3 className="sub">Labels</h3>
            <Labels labels={a.labels} highlight={a.signature_labels} ruleSet={a.rule_labels} />
            <p className="notice">Highlighted labels form the alert's <em>signature</em> — what makes this "the same alert" as last time, whichever pod or instance it was on.</p>
            {a.rule_labels && Object.keys(a.rule_labels).length > 0 && (
              <p className="notice">✎ set by a <Link to="/label-rules">label rule</Link>: {Object.entries(a.rule_labels).map(([k, r]) => `${k} (${r})`).join(", ")}. Not part of the signature.</p>
            )}
          </section>
          {otherAnnotations.length > 0 && (
            <section className="card pad">
              <h3 className="sub">Annotations</h3>
              <dl className="kv">
                {otherAnnotations.map(([k, v]) => <div key={k}><dt>{k}</dt><dd>{v}</dd></div>)}
              </dl>
            </section>
          )}
          <section className="card pad">
            <h3 className="sub">Details</h3>
            <dl className="kv">
              <div><dt>Last received</dt><dd>{relative(a.last_seen_at, now)} · {a.send_count}×</dd></div>
              {!resolved && <div><dt>Expires if not re-sent</dt><dd>{relative(a.expires_at, now)}</dd></div>}
              {resolved && <div><dt>Resolved</dt><dd>{wall(a.resolved_at)} · {HOW[a.resolved_how ?? ""] ?? a.resolved_how}{a.resolved_by && a.resolved_how === "manual" ? ` (${a.resolved_by})` : ""}</dd></div>}
              <div><dt>Incident</dt><dd>{a.incident_id ? <Link to={`/incidents/${a.incident_id}`}>open the incident</Link> : <span className="muted">none — matched no route, nobody paged</span>}</dd></div>
              {a.silenced_by.length > 0 && <div><dt>Silenced by</dt><dd>{a.silenced_by.map((s) => <Link key={s} to={`/silences?highlight=${s}`}>{s.slice(0, 8)}</Link>)}</dd></div>}
              <div><dt>Fingerprint</dt><dd><code>{a.fingerprint.slice(0, 12)}</code></dd></div>
            </dl>
          </section>
        </div>
      </div>
    </div>
  );
}

function BackLink() {
  return <Link to="/alerts" className="back-link"><Icon name="chevronLeft" size={15} /> Alerts</Link>;
}

function History({ history, now }: { history: Detail["history"]; now: number }) {
  if (history.total === 0) {
    return (
      <section className="card pad history">
        <h3 className="sub">History</h3>
        <Empty icon="clock" title="First time this alert has fired">
          When it is resolved, say what fixed it — that is what the next person sees here.
        </Empty>
      </section>
    );
  }
  return (
    <section className="card pad history card--subject">
      <h3 className="sub">History</h3>
      <p className="history-glance">
        Seen <strong>{history.total}</strong> time{history.total === 1 ? "" : "s"} before
        {history.last_90_days !== history.total && <> · {history.last_90_days} in the last 90 days</>}
        {history.last_started_at && <> · last {relative(history.last_started_at, now)}</>}
      </p>
      {history.recent_fixes.length > 0 ? (
        <ul className="fix-list">
          {history.recent_fixes.map((f) => <Fix key={f.id} item={f} now={now} />)}
        </ul>
      ) : (
        <p className="muted small">Nobody recorded what fixed it the previous times.</p>
      )}
      <details className="history-more">
        <summary>All earlier occurrences</summary>
        <table>
          <thead><tr><th>Started</th><th>Lasted</th><th>How it ended</th></tr></thead>
          <tbody>
            {history.recent.map((o) => (
              <tr key={o.id}>
                <td><Link to={`/alerts/${o.id}`}>{wall(o.started_at)}</Link></td>
                <td>{o.resolved_at ? duration(o.started_at, o.resolved_at, now) : "still open"}</td>
                <td>{o.resolution_category ? categoryLabel(o.resolution_category) : HOW[o.resolved_how ?? ""] ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>
    </section>
  );
}

/** Other alerts that start within minutes of this one — the likely shared cause, or what this
 *  one is a symptom of. Shown only when there is something; it is a hint, not a section to fill. */
function FiresWith({ signature }: { signature: string }) {
  const { data } = usePoll<Related>(() => insightApi.related(signature, 30), [signature], 300000);
  if (!data || data.related.length === 0) return null;
  return (
    <section className="card pad">
      <h3 className="sub">Usually fires with</h3>
      <p className="muted small">Started within 5 minutes of this alert in the last 30 days. Often one cause behind several pages.</p>
      <ul className="pair-list">
        {data.related.map((r) => (
          <li key={r.signature}>
            <span className="pair-names"><Link to={`/alerts?q=${encodeURIComponent(r.alertname)}&status=all`}><strong>{r.alertname}</strong></Link></span>
            <span className="pair-bar" aria-hidden><span style={{ width: `${Math.round(r.ratio * 100)}%` }} /></span>
            <span className="muted small nowrap">{Math.round(r.ratio * 100)}% · {r.together}× together</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

function Fix({ item, now }: { item: HistoryItem; now: number }) {
  return (
    <li>
      <div className="fix-head">
        {item.resolution_category && <span className="badge info">{categoryLabel(item.resolution_category)}</span>}
        <Link to={`/alerts/${item.id}`} className="muted small">
          {relative(item.resolved_at ?? item.started_at, now)}{item.resolved_by ? ` · ${item.resolved_by}` : ""}
        </Link>
      </div>
      {item.resolution_note && <p>{item.resolution_note}</p>}
    </li>
  );
}
