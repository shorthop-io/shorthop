import { useEffect, useState } from "react";
import { api, type Alert, type Matcher, type Silence } from "../api";
import { Link, useMe, useNavigate, useNow, usePoll } from "../app";
import { Icon } from "../components/Icon";
import { MatcherEditor, matcherText, usable } from "../components/MatcherEditor";
import { Empty, ErrorNotice } from "../components/ui";
import { fromLocalInput, relative, toLocalInput, wall } from "../time";

export function Silences({ search }: { search: URLSearchParams }) {
  const navigate = useNavigate();
  const { can } = useMe();
  const now = useNow();
  const [state, setState] = useState<"current" | "expired">("current");
  const highlight = search.get("highlight");
  const { data, error, refresh } = usePoll(() => api.listSilences(state), [state], 30000);
  const [actionError, setActionError] = useState<string | null>(null);

  const expire = async (s: Silence) => {
    if (!window.confirm(`End this silence now?\n\n${s.matchers.map(matcherText).join(", ")}`)) return;
    setActionError(null);
    try {
      await api.expireSilence(s.id);
      refresh();
    } catch (e) {
      setActionError((e as Error).message);
    }
  };

  return (
    <div className="page">
      <div className="section-head">
        <h2>Silences</h2>
        <div className="row-actions">
          <div className="seg">
            {(["current", "expired"] as const).map((s) => (
              <button key={s} className={`seg-btn${state === s ? " active" : ""}`} onClick={() => setState(s)}>
                {s === "current" ? "Current" : "Expired"}
              </button>
            ))}
          </div>
          {can("alerts:write") && (
            <button className="btn-primary" onClick={() => navigate("/silences/new")}>
              <Icon name="plus" size={15} /> New silence
            </button>
          )}
        </div>
      </div>
      <p className="glance">A silence mutes matching alerts — they are still recorded and shown here, just marked silenced. Silences are never deleted.</p>
      <ErrorNotice>{error}</ErrorNotice>
      <ErrorNotice>{actionError}</ErrorNotice>
      {data && data.length === 0 && (
        <Empty icon="bellOff" title={state === "current" ? "No active silences" : "No expired silences"} />
      )}
      <ul className="silence-list">
        {(data ?? []).map((s) => (
          <li key={s.id} className={`card silence-row${highlight === s.id ? " highlighted" : ""}`}>
            <div className="silence-main">
              <div className="silence-matchers">
                {s.matchers.map((m, i) => <code key={i} className="matcher">{matcherText(m)}</code>)}
              </div>
              <p className="silence-comment">{s.comment || <span className="muted">no reason given</span>}</p>
              <p className="muted small">
                {s.created_by} · created {relative(s.created_at, now)}
                {s.expired_by && <> · ended early by {s.expired_by}</>}
              </p>
            </div>
            <div className="silence-side">
              <span className={`badge ${s.state === "active" ? "warn" : s.state === "pending" ? "info" : "neutral"}`}>{s.state}</span>
              <span className="small" title={s.ends_at}>
                {s.state === "expired" ? `ended ${relative(s.ends_at, now)}` :
                  s.state === "pending" ? `starts ${relative(s.starts_at, now)}` : `ends ${relative(s.ends_at, now)}`}
              </span>
              <span className="muted small">{wall(s.starts_at)} → {wall(s.ends_at)}</span>
              {s.state !== "expired" && can("alerts:write") && (
                <button className="btn-ghost btn-sm btn-danger" onClick={() => expire(s)}>End now</button>
              )}
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}

const DURATIONS = [
  { label: "1 h", hours: 1 }, { label: "4 h", hours: 4 }, { label: "1 day", hours: 24 },
  { label: "3 days", hours: 72 }, { label: "1 week", hours: 168 },
];

export function SilenceNew({ search }: { search: URLSearchParams }) {
  const navigate = useNavigate();
  const fromAlert = search.get("alert");
  const [matchers, setMatchers] = useState<Matcher[]>([{ name: "alertname", value: "", isRegex: false, isEqual: true }]);
  const [hours, setHours] = useState<number | null>(4);
  const [endsAt, setEndsAt] = useState(toLocalInput(new Date(Date.now() + 4 * 3600e3)));
  const [comment, setComment] = useState("");
  const [preview, setPreview] = useState<Alert[] | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [source, setSource] = useState<string | null>(null);

  // Silencing from an alert: start from its SIGNATURE labels — what makes it "this alert"
  // whichever pod it fires on — rather than every label, which would only ever match the
  // one pod that happens to be failing right now.
  useEffect(() => {
    if (!fromAlert) return;
    api.getAlert(fromAlert).then((a) => {
      setSource(a.alertname);
      const ordered = Object.entries(a.signature_labels)
        .sort(([x], [y]) => (x === "alertname" ? -1 : y === "alertname" ? 1 : x.localeCompare(y)));
      setMatchers(ordered.map(([name, value]) => ({ name, value, isRegex: false, isEqual: true })));
    }).catch((e) => setError(e.message));
  }, [fromAlert]);

  // Live preview, debounced: which OPEN alerts would this mute right now.
  useEffect(() => {
    const ok = usable(matchers);
    if (!ok.length) { setPreview(null); return; }
    const id = window.setTimeout(() => {
      api.previewSilence(ok).then((rows) => { setPreview(rows); setPreviewError(null); })
        .catch((e) => { setPreview(null); setPreviewError(e.message); });
    }, 350);
    return () => window.clearTimeout(id);
  }, [matchers]);

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const ends = hours ? new Date(Date.now() + hours * 3600e3).toISOString() : fromLocalInput(endsAt);
      const s = await api.createSilence({ matchers: usable(matchers), ends_at: ends, comment });
      navigate(`/silences?highlight=${s.id}`);
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  };

  return (
    <div className="page">
      <Link to="/silences" className="back-link"><Icon name="chevronLeft" size={15} /> Silences</Link>
      <h2>New silence{source && <span className="muted"> for {source}</span>}</h2>
      <div className="detail-grid">
        <form className="card pad form-stack" onSubmit={(e) => { e.preventDefault(); submit(); }}>
          <fieldset>
            <legend>Match alerts where</legend>
            <MatcherEditor value={matchers} onChange={setMatchers} />
          </fieldset>

          <fieldset>
            <legend>For how long</legend>
            <div className="seg seg-wrap">
              {DURATIONS.map((d) => (
                <button type="button" key={d.hours} className={`seg-btn${hours === d.hours ? " active" : ""}`}
                  onClick={() => setHours(d.hours)}>{d.label}</button>
              ))}
              <button type="button" className={`seg-btn${hours === null ? " active" : ""}`} onClick={() => setHours(null)}>Until…</button>
            </div>
            {hours === null && (
              <input type="datetime-local" aria-label="Ends at" value={endsAt}
                onChange={(e) => setEndsAt(e.target.value)} />
            )}
          </fieldset>

          <label>
            Why <span className="muted small">(optional)</span>
            <textarea rows={3} maxLength={2000} value={comment} onChange={(e) => setComment(e.target.value)}
              placeholder="e.g. deploying the fix for the consumer, re-check at 16:00" />
          </label>
          <ErrorNotice>{error}</ErrorNotice>
          <div><button className="btn-primary" disabled={busy}>Create silence</button></div>
        </form>

        <section className="card pad">
          <h3 className="sub">Would mute right now</h3>
          {previewError && <p className="notice notice--warn">{previewError}</p>}
          {!previewError && preview === null && <p className="muted small">Add a matcher to see which open alerts it catches.</p>}
          {preview && preview.length === 0 && <p className="muted small">No open alert matches. The silence still applies to alerts that fire later.</p>}
          {preview && preview.length > 0 && (
            <>
              <p className="history-glance"><strong>{preview.length}</strong> open alert{preview.length === 1 ? "" : "s"}</p>
              <ul className="preview-list">
                {preview.slice(0, 20).map((a) => (
                  <li key={a.id}><Link to={`/alerts/${a.id}`}>{a.alertname}</Link>
                    <span className="muted small"> {Object.entries(a.labels).filter(([k]) => k !== "alertname").slice(0, 3).map(([k, v]) => `${k}=${v}`).join(" ")}</span></li>
                ))}
              </ul>
              {preview.length > 20 && <p className="muted small">…and {preview.length - 20} more.</p>}
            </>
          )}
        </section>
      </div>
    </div>
  );
}
