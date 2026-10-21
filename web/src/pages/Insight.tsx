import { useState } from "react";
import { insightApi, RESOLUTION_CATEGORIES, type NoiseRow, type ResponseStats, type Times } from "../api";
import { Link, useNow, usePoll } from "../app";
import { Icon } from "../components/Icon";
import { Empty, ErrorNotice, SeverityBadge } from "../components/ui";
import { relative } from "../time";

/** "4m", "1h 20m", "2d 3h" — a response time is read at a glance, not to the second. */
export function secs(s: number | null | undefined): string {
  if (s == null) return "—";
  const m = Math.round(s / 60);
  if (s < 60) return `${Math.round(s)}s`;
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h}h${m % 60 ? ` ${m % 60}m` : ""}`;
  return `${Math.floor(h / 24)}d ${h % 24}h`;
}

const FLAG: Record<NoiseRow["flags"][number], { label: string; tone: string; hint: string }> = {
  flapping: { label: "flapping", tone: "warn", hint: "Fires again within 15 minutes of resolving, on the same series. Usually a threshold sitting on the noise floor — add a `for:` or hysteresis." },
  nobody_acts: { label: "nobody acts", tone: "danger", hint: "Almost always ends by itself without anybody acknowledging it. Either it should not page, or it is being ignored." },
  short_lived: { label: "short-lived", tone: "neutral", hint: "Usually over in under 5 minutes — likely gone before anybody could look." },
};

const FIX_LABEL = Object.fromEntries(RESOLUTION_CATEGORIES.map((c) => [c.value, c.label]));
const pct = (a: number, b: number) => (b ? `${Math.round((a / b) * 100)}%` : "—");

export function Insight() {
  const now = useNow(60000);
  const [days, setDays] = useState(30);
  const { data, error } = usePoll(() => insightApi.report(days), [days], 120000);

  return (
    <div className="page">
      <div className="section-head">
        <h2>Insight</h2>
        <div className="seg">
          {[7, 30, 90].map((d) => (
            <button key={d} className={`seg-btn${days === d ? " active" : ""}`} onClick={() => setDays(d)}>{d} days</button>
          ))}
        </div>
      </div>
      <p className="glance">What the history says about the alerts themselves: which ones are noise, how fast pages are answered, and which alerts fire together — the reasons people stop trusting a pager.</p>
      <ErrorNotice>{error}</ErrorNotice>
      {data?.truncated && <p className="notice notice--warn">Busy window: only the most recent 50,000 alert episodes are counted.</p>}
      {data && (
        <>
          <Tiles r={data.response} pages={data.notifications} episodes={data.episodes} />
          <section className="card pad">
            <h3 className="sub">Noisiest alerts</h3>
            {data.noise.length === 0 ? (
              <Empty icon="chart" title="No alerts in this window" />
            ) : (
              <>
                <p className="muted small">
                  {data.noise_total} alert{data.noise_total === 1 ? "" : "s"} fired {data.episodes} time{data.episodes === 1 ? "" : "s"}.
                  Flags need at least {data.thresholds.min_episodes_for_flag} episodes — fewer is an anecdote.
                </p>
                <div className="table-wrap">
                  <table className="insight-table">
                    <thead><tr><th>Alert</th><th className="num">Fired</th><th className="num">Flaps</th><th className="num">Acked</th><th className="num">Typical length</th><th>Usual fix</th><th>Last</th></tr></thead>
                    <tbody>
                      {data.noise.map((r) => (
                        <tr key={r.signature}>
                          <td>
                            <div className="insight-name">
                              <Link to={`/alerts?q=${encodeURIComponent(r.alertname)}&status=all`}><strong>{r.alertname || "(no alertname)"}</strong></Link>
                              <SeverityBadge severity={r.severity} />
                              {r.flags.map((f) => <span key={f} className={`badge ${FLAG[f].tone}`} title={FLAG[f].hint}>{FLAG[f].label}</span>)}
                            </div>
                            <div className="muted small">
                              {r.team ?? "no team"}{r.series > 1 ? ` · ${r.series} series` : ""}{r.open ? ` · ${r.open} firing now` : ""}
                            </div>
                          </td>
                          <td className="num" data-label="Fired">{r.episodes}</td>
                          <td className="num" data-label="Flaps">{r.flaps || "—"}</td>
                          <td className="num" data-label="Acked">{pct(r.acked, r.episodes)}</td>
                          <td className="num" data-label="Typical length">{secs(r.median_duration_s)}</td>
                          <td data-label="Usual fix">{r.usual_fix ? FIX_LABEL[r.usual_fix] ?? r.usual_fix : <span className="muted">not recorded</span>}</td>
                          <td className="muted small nowrap" data-label="Last">{relative(r.last_started_at, now)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <dl className="flag-legend">
                  {Object.values(FLAG).map((f) => <div key={f.label}><dt><span className={`badge ${f.tone}`}>{f.label}</span></dt><dd>{f.hint}</dd></div>)}
                </dl>
              </>
            )}
          </section>

          <section className="card pad">
            <h3 className="sub">Fire together</h3>
            <p className="muted small">Alerts whose episodes start within {Math.round(data.thresholds.co_window_s / 60)} minutes of each other, most consistent first. A pair near 100% is usually one cause and its symptom — group them in one route, or inhibit the symptom.</p>
            {data.correlations.length === 0 ? <p className="muted small">No alerts consistently fire together in this window.</p> : (
              <ul className="pair-list">
                {data.correlations.map((c) => (
                  <li key={`${c.a}-${c.b}`}>
                    <span className="pair-names"><strong>{c.a_alertname}</strong><Icon name="link" size={13} /><strong>{c.b_alertname}</strong></span>
                    <span className="pair-bar" aria-hidden><span style={{ width: `${Math.round(c.ratio * 100)}%` }} /></span>
                    <span className="muted small nowrap">{Math.round(c.ratio * 100)}% · together {c.together}× ({c.a_episodes} / {c.b_episodes})</span>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section className="card pad">
            <h3 className="sub">Response by team</h3>
            {data.response_by_team.length === 0 ? <p className="muted small">No incidents in this window.</p> : (
              <div className="table-wrap">
                <table>
                  <thead><tr><th>Team</th><th className="num">Incidents</th><th className="num">Ack p50</th><th className="num">Ack p90</th><th className="num">Resolve p50</th><th className="num">Resolve p90</th><th className="num">Never acked</th></tr></thead>
                  <tbody>
                    {data.response_by_team.map((t) => (
                      <tr key={t.team_id ?? "none"}>
                        <td>{t.team ?? <span className="muted">no team</span>}</td>
                        <td className="num">{t.incidents}</td>
                        <td className="num">{secs(t.time_to_ack.p50_s)}</td>
                        <td className="num">{secs(t.time_to_ack.p90_s)}</td>
                        <td className="num">{secs(t.time_to_resolve.p50_s)}</td>
                        <td className="num">{secs(t.time_to_resolve.p90_s)}</td>
                        <td className="num">{t.resolved_unacked || "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </>
      )}
    </div>
  );
}

function Tile({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <div className="tile">
      <span className="tile-label">{label}</span>
      <span className="tile-value">{value}</span>
      {note && <span className="tile-note">{note}</span>}
    </div>
  );
}

function timesNote(t: Times) {
  return t.n ? `p90 ${secs(t.p90_s)} · ${t.n} measured` : "nothing to measure yet";
}

function Tiles({ r, pages, episodes }: { r: ResponseStats; pages: { sent: number; failed: number }; episodes: number }) {
  return (
    <div className="tiles">
      <Tile label="Incidents" value={String(r.incidents)} note={`${r.open} open · ${episodes} alert episodes`} />
      <Tile label="Time to acknowledge (p50)" value={secs(r.time_to_ack.p50_s)} note={timesNote(r.time_to_ack)} />
      <Tile label="Time to resolve (p50)" value={secs(r.time_to_resolve.p50_s)} note={timesNote(r.time_to_resolve)} />
      <Tile label="Ended unacknowledged" value={pct(r.resolved_unacked, r.resolved)} note={`${r.resolved_unacked} of ${r.resolved} resolved`} />
      <Tile label="Pages sent" value={String(pages.sent)} note={pages.failed ? `and ${pages.failed} that failed for good` : "none failed"} />
    </div>
  );
}
