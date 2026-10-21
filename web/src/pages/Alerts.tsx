import { useState } from "react";
import { api, type Alert } from "../api";
import { Link, useMe, useNavigate, useNow, usePoll } from "../app";
import { Icon } from "../components/Icon";
import { Empty, ErrorNotice, SeverityBadge, StatusBadge } from "../components/ui";
import { duration, relative } from "../time";

const STATUSES = [
  { value: "open", label: "Open" },
  { value: "firing", label: "Firing" },
  { value: "acknowledged", label: "Acknowledged" },
  { value: "resolved", label: "Resolved" },
];

/** Labels worth showing on the list row: the ones that say WHERE, not the whole set. */
const CONTEXT_LABELS = ["integration", "cluster", "namespace", "service", "job", "job_name", "instance"];

export function Alerts({ search }: { search: URLSearchParams }) {
  const navigate = useNavigate();
  const { can } = useMe();
  const now = useNow();
  const status = search.get("status") || "open";
  const labels = search.getAll("label");
  const [q, setQ] = useState(search.get("q") || "");
  const [labelDraft, setLabelDraft] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const { data, error, refresh } = usePoll(
    () => api.listAlerts({ status, q: search.get("q") || "", label: labels, limit: 200 }),
    [status, search.toString()],
    20000,
  );

  const go = (next: { status?: string; q?: string; label?: string[] }) => {
    const sp = new URLSearchParams();
    const st = next.status ?? status;
    if (st !== "open") sp.set("status", st);
    const qq = next.q ?? (search.get("q") || "");
    if (qq) sp.set("q", qq);
    (next.label ?? labels).forEach((l) => sp.append("label", l));
    const s = sp.toString();
    navigate(`/alerts${s ? `?${s}` : ""}`);
  };

  const addLabel = () => {
    const v = labelDraft.trim();
    if (!/^[^=\s]+=.*$/.test(v)) return;
    setLabelDraft("");
    go({ label: [...labels.filter((l) => l !== v), v] });
  };

  const ack = async (a: Alert) => {
    setBusy(a.id);
    setActionError(null);
    try {
      await api.ack(a.id);
      refresh();
    } catch (e) {
      setActionError((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const rows = data ?? [];
  const firing = rows.filter((a) => a.status === "firing").length;
  const acked = rows.filter((a) => a.status === "acknowledged").length;
  const silenced = rows.filter((a) => a.silenced_by.length > 0).length;

  return (
    <div className="page">
      <div className="section-head">
        <h2>Alerts</h2>
        <div className="seg" role="tablist" aria-label="Which alerts">
          {STATUSES.map((s) => (
            <button key={s.value} role="tab" aria-selected={status === s.value}
              className={`seg-btn${status === s.value ? " active" : ""}`}
              onClick={() => go({ status: s.value })}>{s.label}</button>
          ))}
        </div>
      </div>

      <div className="filter-bar">
        <form className="search-box" onSubmit={(e) => { e.preventDefault(); go({ q }); }}>
          <Icon name="search" size={16} />
          <input type="search" placeholder="Alert name…" value={q} aria-label="Search by alert name"
            onChange={(e) => setQ(e.target.value)} onBlur={() => go({ q })} />
        </form>
        <form className="search-box" onSubmit={(e) => { e.preventDefault(); addLabel(); }}>
          <Icon name="tag" size={16} />
          <input placeholder="label=value, then Enter" value={labelDraft} aria-label="Filter by label"
            onChange={(e) => setLabelDraft(e.target.value)} />
        </form>
        {labels.map((l) => (
          <button key={l} className="filter-chip" title="Remove this filter"
            onClick={() => go({ label: labels.filter((x) => x !== l) })}>
            {l} <Icon name="x" size={12} />
          </button>
        ))}
      </div>

      {data && status !== "resolved" && (
        <p className="glance">
          <span><strong>{firing}</strong> firing</span>
          <span><strong>{acked}</strong> acknowledged</span>
          {silenced > 0 && <span><strong>{silenced}</strong> silenced</span>}
        </p>
      )}

      <ErrorNotice>{error && (data ? `Could not refresh: ${error}` : error)}</ErrorNotice>
      <ErrorNotice>{actionError}</ErrorNotice>

      {data && rows.length === 0 && (
        status === "resolved"
          ? <Empty icon="check" title="Nothing resolved matches" />
          : <Empty icon="check" title="Nothing is firing">
              {labels.length || search.get("q") ? "Nothing matches these filters." :
                "When an integration delivers an alert it appears here."}
            </Empty>
      )}

      <ul className="alert-list">
        {rows.map((a) => {
          const summary = a.annotations.summary || a.annotations.description || "";
          const where = CONTEXT_LABELS.filter((k) => a.labels[k]).slice(0, 3);
          return (
            <li key={a.id} className={`alert-row card sev-${a.status === "resolved" ? "ok" : a.silenced_by.length ? "muted" : a.severity.toLowerCase()}`}>
              <Link to={`/alerts/${a.id}`} className="alert-main">
                <div className="alert-title">
                  <span className="alert-name">{a.alertname}</span>
                  <SeverityBadge severity={a.severity} />
                  <StatusBadge alert={a} />
                </div>
                {summary && <div className="alert-summary">{summary}</div>}
                <div className="alert-meta">
                  {where.map((k) => <span key={k} className="meta-item"><span className="muted">{k}</span> {a.labels[k]}</span>)}
                </div>
              </Link>
              <div className="alert-side">
                <div className="alert-when" title={a.started_at}>
                  {a.status === "resolved"
                    ? <>resolved {relative(a.resolved_at, now)}</>
                    : <>started {relative(a.started_at, now)}</>}
                  <span className="muted small">
                    {a.status === "resolved" ? `lasted ${duration(a.started_at, a.resolved_at, now)}` : `for ${duration(a.started_at, null, now)}`}
                  </span>
                </div>
                {a.status === "firing" && can("alerts:write") && (
                  <button className="btn-ghost btn-sm" disabled={busy === a.id} onClick={() => ack(a)}>
                    <Icon name="check" size={14} /> Acknowledge
                  </button>
                )}
              </div>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
