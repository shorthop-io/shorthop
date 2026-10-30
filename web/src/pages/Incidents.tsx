import { useState } from "react";
import { api, oncallApi, paging, RESOLUTION_CATEGORIES, type Incident, type IncidentDetail as Detail, type Level, type Notification } from "../api";
import { Link, useMe, useNavigate, useNow, usePoll } from "../app";
import { Icon } from "../components/Icon";
import { Empty, ErrorNotice, Labels, SeverityBadge, StatusBadge } from "../components/ui";
import { duration, parseTs, relative, wall } from "../time";

const STATUSES = [
  { value: "open", label: "Open" },
  { value: "triggered", label: "Triggered" },
  { value: "acknowledged", label: "Acknowledged" },
  { value: "resolved", label: "Resolved" },
];

export function IncidentStatus({ status }: { status: Incident["status"] }) {
  const tone = status === "triggered" ? "danger" : status === "acknowledged" ? "info" : "ok";
  return <span className={`badge ${tone}`}>{status}</span>;
}

/** When the next escalation step is due — the one fact that says "someone else is about to
 *  be woken up" and is otherwise invisible. */
function nextStep(i: Incident, now: number): string | null {
  if (i.status !== "triggered" || !i.next_step_at) return null;
  // Overdue means the dispatch loop is about to run it (or is behind) — never "41 min ago".
  if (parseTs(i.next_step_at).getTime() <= now) return "escalating now";
  return `next escalation ${relative(i.next_step_at, now)}`;
}

export function Incidents({ search }: { search: URLSearchParams }) {
  const navigate = useNavigate();
  const { me, can } = useMe();
  const now = useNow();
  const status = search.get("status") || "open";
  // Default to the caller's own teams: an incident handed to another team leaves this list.
  // Someone in no team sees everything, since "mine" would only show unowned incidents.
  const hasTeams = me.teams.length > 0;
  const team = hasTeams ? (search.get("team") === "all" ? "all" : "mine") : "all";
  const { data, error, refresh } = usePoll(() => paging.listIncidents(status, team), [status, team], 15000);
  const go = (st: string, tm: string) => {
    const sp = new URLSearchParams();
    if (st !== "open") sp.set("status", st);
    if (tm === "all" && hasTeams) sp.set("team", "all");
    const qs = sp.toString();
    navigate(qs ? `/incidents?${qs}` : "/incidents");
  };
  const [actionError, setActionError] = useState<string | null>(null);
  const oncall = usePoll(() => oncallApi.now(), [], 60000);

  const ack = async (i: Incident) => {
    setActionError(null);
    try {
      await paging.ackIncident(i.id);
      refresh();
    } catch (e) {
      setActionError((e as Error).message);
    }
  };

  const rows = data ?? [];
  return (
    <div className="page">
      <div className="section-head">
        <h2>Incidents</h2>
        <div className="row-actions">
          {hasTeams && (
            <div className="seg">
              <button className={`seg-btn${team === "mine" ? " active" : ""}`} onClick={() => go(status, "mine")}
                title={`Incidents owned by ${me.teams.map((t) => t.name).join(", ")}, plus any no team owns`}>My teams</button>
              <button className={`seg-btn${team === "all" ? " active" : ""}`} onClick={() => go(status, "all")}>All teams</button>
            </div>
          )}
          <div className="seg">
            {STATUSES.map((s) => (
              <button key={s.value} className={`seg-btn${status === s.value ? " active" : ""}`}
                onClick={() => go(s.value, team)}>{s.label}</button>
            ))}
          </div>
        </div>
      </div>
      {oncall.data && oncall.data.length > 0 && (
        <div className="oncall-strip">
          <Icon name="calendar" size={15} />
          {oncall.data.map((o) => (
            <Link key={o.schedule_id} to={`/schedules/${o.schedule_id}`} className="oncall-chip">
              <span className="muted">{o.schedule}</span> {o.user ? (o.user.name || o.user.email) : <strong className="error">nobody</strong>}
            </Link>
          ))}
        </div>
      )}
      <p className="glance">An incident is what pages people: alerts that matched the same route with the same group labels. Ten crash-looping pods are one incident.</p>
      <ErrorNotice>{error}</ErrorNotice>
      <ErrorNotice>{actionError}</ErrorNotice>
      {data && rows.length === 0 && (
        <Empty icon="check" title={status === "resolved" ? "No resolved incidents" : team === "mine" ? "Nothing is paging your teams" : "Nothing is paging"}>
          {status === "open" && (team === "mine"
            ? "Other teams' incidents are under All teams."
            : "An incident opens when an alert matches a route. Alerts that match no route are recorded but page nobody.")}
        </Empty>
      )}
      <ul className="alert-list">
        {rows.map((i) => {
          const next = nextStep(i, now);
          return (
            <li key={i.id} className={`alert-row card sev-${i.status === "resolved" ? "ok" : i.severity.toLowerCase()}`}>
              <Link to={`/incidents/${i.id}`} className="alert-main">
                <div className="alert-title">
                  <span className="inc-number">#{i.number}</span>
                  <span className="alert-name">{i.title}</span>
                  {i.severity && <SeverityBadge severity={i.severity} />}
                  <IncidentStatus status={i.status} />
                </div>
                <div className="alert-meta">
                  <span className="meta-item">{i.team_name ?? <span className="muted">no team</span>}</span>
                  <span className="meta-item">{i.open_alerts} open of {i.alert_count} alert{i.alert_count === 1 ? "" : "s"}</span>
                  {i.acked_by && <span className="meta-item"><span className="muted">acknowledged by</span> {i.acked_by}</span>}
                  {next && <span className="meta-item next-step">{next}</span>}
                </div>
              </Link>
              <div className="alert-side">
                <div className="alert-when">
                  {i.status === "resolved" ? <>resolved {relative(i.resolved_at, now)}</> : <>opened {relative(i.opened_at, now)}</>}
                  <span className="muted small">{i.status === "resolved" ? `lasted ${duration(i.opened_at, i.resolved_at, now)}` : `for ${duration(i.opened_at, null, now)}`}</span>
                </div>
                {i.status === "triggered" && can("alerts:write") && (
                  <button className="btn-ghost btn-sm" onClick={() => ack(i)}><Icon name="check" size={14} /> Acknowledge</button>
                )}
              </div>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

const DELIVERY_TONE: Record<Notification["status"], string> = {
  sent: "ok", pending: "warn", failed: "danger", suppressed: "neutral",
};

export function DeliveryTable({ rows, now, showIncident = false }: { rows: Notification[]; now: number; showIncident?: boolean }) {
  if (!rows.length) return <p className="muted small">Nothing sent yet.</p>;
  return (
    <div className="table-wrap">
      <table>
        <thead><tr><th>When</th>{showIncident && <th>Incident</th>}<th>Event</th><th>To</th><th>Status</th></tr></thead>
        <tbody>
          {rows.map((n) => (
            <tr key={n.id}>
              <td title={wall(n.created_at)}>{relative(n.created_at, now)}</td>
              {showIncident && <td><Link to={`/incidents/${n.incident_id}`}>open</Link></td>}
              <td>{n.event}{n.step !== null && <span className="muted small"> · step {n.step + 1}</span>}</td>
              <td className="cell-wrap"><div>{n.target}</div><div className="muted small">{n.kind} · {n.destination}</div></td>
              <td>
                <span className={`badge ${DELIVERY_TONE[n.status]}`}>{n.status}</span>
                {n.attempts > 1 && <span className="muted small"> · {n.attempts} attempts</span>}
                {n.error && <div className="small delivery-error">{n.error}</div>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function IncidentDetail({ id }: { id: string }) {
  const { can } = useMe();
  const now = useNow();
  const { data, error, setData } = usePoll(() => paging.getIncident(id), [id], 15000);
  const [category, setCategory] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const act = async (fn: () => Promise<Detail>) => {
    setBusy(true);
    setActionError(null);
    try {
      setData(await fn());
    } catch (e) {
      setActionError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const back = <Link to="/incidents" className="back-link"><Icon name="chevronLeft" size={15} /> Incidents</Link>;
  if (error && !data) return <div className="page">{back}<ErrorNotice>{error}</ErrorNotice></div>;
  if (!data) return <div className="page">{back}<p className="muted">Loading…</p></div>;
  const i = data;
  const open = i.alerts.filter((a) => !a.resolved_at);
  const next = nextStep(i, now);

  return (
    <div className="page detail-page">
      {back}
      <div className="detail-head">
        <div>
          <h2><span className="inc-number">#{i.number}</span> {i.title}</h2>
          <div className="detail-badges">
            {i.level && <span className={`badge ${i.level_manual ? "neutral" : "info"}`} title={i.level_manual ? `set by ${i.level_set_by}` : "from the alerts"}>{i.level}</span>}
            {i.severity && <SeverityBadge severity={i.severity} />}
            <IncidentStatus status={i.status} />
            <span className="muted small">opened {wall(i.opened_at)} · {i.status === "resolved" ? `lasted ${duration(i.opened_at, i.resolved_at, now)}` : `open for ${duration(i.opened_at, null, now)}`}</span>
          </div>
        </div>
      </div>
      <ErrorNotice>{actionError}</ErrorNotice>
      <div className="detail-grid">
        <div className="detail-col">
          {can("alerts:write") && i.status !== "resolved" && (
            <section className="card pad">
              <h3 className="sub">Respond</h3>
              <div className="detail-actions">
                {i.status === "triggered" && (
                  <button className="btn-primary" disabled={busy} onClick={() => act(() => paging.ackIncident(i.id))}>
                    <Icon name="check" size={15} /> Acknowledge
                  </button>
                )}
                {i.status === "acknowledged" && <span className="muted small">Acknowledged by {i.acked_by} {relative(i.acked_at, now)} — escalation stopped.</span>}
                {next && <span className="notice notice--warn">{next}</span>}
                {i.status === "triggered" && i.next_step_at && parseTs(i.next_step_at).getTime() > now && (
                  <button className="btn-ghost btn-sm" disabled={busy} onClick={() => act(() => paging.escalateIncident(i.id))}
                    title={`Run step ${i.next_step + 1} now instead of waiting`}>
                    <Icon name="arrowUp" size={14} /> Escalate now
                  </button>
                )}
              </div>
              <LevelPicker incident={i} busy={busy} onPick={(level) => act(() => paging.setIncidentLevel(i.id, level))} />
              <div className="more-actions">
                <Reassign incident={i} busy={busy} onSubmit={(body) => act(() => paging.reassignIncident(i.id, body))} />
                <AddResponder busy={busy} onSubmit={(body) => act(() => paging.addResponder(i.id, body))} />
                <Snooze busy={busy} onSubmit={(minutes, comment) => act(async () => {
                  await paging.snoozeIncident(i.id, minutes, comment);
                  return paging.getIncident(i.id);
                })} />
                <AddNote busy={busy} onSubmit={(text) => act(() => paging.addIncidentNote(i.id, text))} />
              </div>
              <form className="resolve-form" onSubmit={(e) => {
                e.preventDefault();
                act(() => paging.resolveIncident(i.id, { category: category || undefined, note: note.trim() || undefined }));
              }}>
                <label>Resolve as
                  <select value={category} onChange={(e) => setCategory(e.target.value)}>
                    <option value="">— choose —</option>
                    {RESOLUTION_CATEGORIES.map((c) => <option key={c.value} value={c.value}>{c.label}</option>)}
                  </select>
                </label>
                <label>What fixed it?
                  <textarea rows={3} value={note} maxLength={4000} onChange={(e) => setNote(e.target.value)}
                    placeholder="Recorded on every alert in this incident — the next person paged reads it." />
                </label>
                <div><button className="btn-primary" disabled={busy}>Resolve all {open.length} open alert{open.length === 1 ? "" : "s"}</button></div>
              </form>
            </section>
          )}

          {can("alerts:write") && i.status === "resolved" && (
            <section className="card pad">
              <h3 className="sub">After the fact</h3>
              <div className="more-actions">
                <AddNote busy={busy} onSubmit={(text) => act(() => paging.addIncidentNote(i.id, text))} />
                <Snooze busy={busy} onSubmit={(minutes, comment) => act(async () => {
                  await paging.snoozeIncident(i.id, minutes, comment);
                  return paging.getIncident(i.id);
                })} />
              </div>
              <p className="muted small">Flapping? Snoozing a resolved incident silences its labels, so the next flap is recorded but pages nobody.</p>
            </section>
          )}

          <section className="card pad">
            <h3 className="sub">Alerts in this incident</h3>
            <ul className="incident-alerts">
              {i.alerts.map((a) => (
                <li key={a.id}>
                  <Link to={`/alerts/${a.id}`} className="alert-name">{a.alertname}</Link> <StatusBadge alert={a} />
                  {a.annotations.summary && <div className="small">{a.annotations.summary}</div>}
                  {a.annotations.description && <div className="small muted alert-description">{a.annotations.description}</div>}
                  <Labels labels={a.labels} skip={["alertname", ...Object.keys(i.group_labels)]} />
                </li>
              ))}
            </ul>
          </section>

          <section className="card pad">
            <h3 className="sub">Who was told</h3>
            <DeliveryTable rows={i.notifications} now={now} />
          </section>
        </div>

        <div className="detail-col">
          <section className="card pad">
            <h3 className="sub">Grouped by</h3>
            <Labels labels={i.group_labels} />
            <dl className="kv" style={{ marginTop: "0.75rem" }}>
              <div><dt>Team</dt><dd>{i.team_name ?? <span className="muted">none</span>}</dd></div>
              <div><dt>Level</dt><dd>{i.level ?? <span className="muted">none</span>}{i.level_manual && i.level_set_at ? <span className="muted small"> · set by {i.level_set_by} {relative(i.level_set_at, now)}</span> : i.level ? <span className="muted small"> · from the alerts</span> : null}</dd></div>
              {i.reassigned_at && <div><dt>Moved</dt><dd>{relative(i.reassigned_at, now)} by {i.reassigned_by} — {i.reassign_note}</dd></div>}
              <div><dt>Route</dt><dd>{i.route_name ? <Link to="/routing">{i.route_name}</Link> : <span className="muted">deleted</span>}</dd></div>
              <div><dt>Escalation</dt><dd>{i.policy_name ? <Link to="/escalation">{i.policy_name}</Link> : <span className="muted">none — nobody is paged</span>}</dd></div>
              {i.status === "triggered" && <div><dt>Next step</dt><dd>{i.next_step_at ? `step ${i.next_step + 1}, ${nextStep(i, now)?.replace("next escalation ", "")}` : "none left"}</dd></div>}
              {i.resolved_at && <div><dt>Resolved</dt><dd>{wall(i.resolved_at)}{i.resolved_by ? ` · ${i.resolved_by}` : ""}</dd></div>}
              {(i.jira_issues ?? []).map((j) => (
                <div key={j.issue_key}><dt>Jira</dt><dd>
                  <a href={j.issue_url} target="_blank" rel="noreferrer">{j.issue_key}</a>
                  <span className="muted small"> · {j.closed_at ? "closed" : "open"}{j.pending ? ` · ${j.pending} update${j.pending === 1 ? "" : "s"} waiting` : ""}</span>
                  {j.error && <div className="error small">{j.error}</div>}
                </dd></div>
              ))}
            </dl>
          </section>
        </div>
      </div>
    </div>
  );
}


/** Move the incident to another team. Collapsed until asked for: most incidents are where
 *  they belong, and the Respond card's first job is Acknowledge. */
function Reassign({ incident: i, busy, onSubmit }: {
  incident: Detail; busy: boolean;
  onSubmit: (body: { team_id: string; escalation_policy_id: string | null; note: string; page: boolean }) => void;
}) {
  const [open, setOpen] = useState(false);
  const [teamId, setTeamId] = useState("");
  const [policyId, setPolicyId] = useState("");
  const [note, setNote] = useState("");
  const [page, setPage] = useState(true);
  const teams = usePoll(() => (open ? api.listTeams() : Promise.resolve([])), [open]);
  const policies = usePoll(() => (open ? paging.listPolicies() : Promise.resolve([])), [open]);

  if (!open) {
    return <div className="reassign-open"><button type="button" className="btn-ghost btn-sm" onClick={() => setOpen(true)}><Icon name="users" size={14} /> Move to another team</button></div>;
  }
  const own = (policies.data ?? []).filter((p) => p.team_id === teamId);
  const others = (policies.data ?? []).filter((p) => p.team_id !== teamId);
  const pickTeam = (id: string) => {
    setTeamId(id);
    const mine = (policies.data ?? []).filter((p) => p.team_id === id);
    setPolicyId(mine.length === 1 ? mine[0].id : "");
  };
  const teamName = (teams.data ?? []).find((t) => t.id === teamId)?.name;
  return (
    <form className="reassign-form form-stack" onSubmit={(e) => {
      e.preventDefault();
      onSubmit({ team_id: teamId, escalation_policy_id: policyId || null, note: note.trim(), page });
    }}>
      <h4>Move to another team</h4>
      <div className="form-grid">
        <label>Team
          <select required value={teamId} onChange={(e) => pickTeam(e.target.value)}>
            <option value="">— choose —</option>
            {(teams.data ?? []).filter((t) => t.id !== i.team_id).map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
          </select>
        </label>
        <label>Paged by
          <select required value={policyId} onChange={(e) => setPolicyId(e.target.value)} disabled={!teamId}>
            <option value="">— choose —</option>
            {own.length > 0 && <optgroup label={`${teamName ?? "team"}'s policies`}>{own.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}</optgroup>}
            {others.length > 0 && <optgroup label="Other policies">{others.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}</optgroup>}
          </select>
        </label>
      </div>
      <label>Why (shown to the new team in the page)
        <input value={note} maxLength={2000} onChange={(e) => setNote(e.target.value)} placeholder="replication lag — the database, not the app" />
      </label>
      {i.status === "acknowledged" && (
        <label className="check"><input type="checkbox" checked={page} onChange={(e) => setPage(e.target.checked)} /> Page the new team now (clears the acknowledgement)</label>
      )}
      <p className="muted small">{i.status === "acknowledged" && !page
        ? "Ownership moves; the incident stays acknowledged and nobody is paged."
        : "The new team is paged from step 1 of that policy now. Pages still queued for the current team are cancelled."}</p>
      <div className="detail-actions">
        <button className="btn-primary" disabled={busy || !teamId || !policyId}>Move{teamName ? ` to ${teamName}` : ""}</button>
        <button type="button" className="btn-ghost" onClick={() => setOpen(false)}>Cancel</button>
      </div>
    </form>
  );
}


/** The level (P1 …). Picking one sets the severity it maps to, and both stop following the
 *  alerts — the hint says so, because "why did a new critical alert not raise it?" is the
 *  question that follows. */
function LevelPicker({ incident: i, busy, onPick }: { incident: Detail; busy: boolean; onPick: (level: string) => void }) {
  const levels = usePoll(() => paging.listLevels(), []);
  const list: Level[] = levels.data?.levels ?? [];
  if (!list.length) return null;
  return (
    <div className="level-picker">
      <span className="muted small">Level</span>
      <div className="seg" role="group" aria-label="Level">
        {list.map((l) => (
          <button key={l.name} type="button" className={`seg-btn${i.level === l.name ? " active" : ""}`}
            disabled={busy || i.level === l.name} title={`${l.severity}${l.description ? ` — ${l.description}` : ""}`}
            onClick={() => onPick(l.name)}>{l.name}</button>
        ))}
      </div>
      <span className="muted small">{i.level_manual ? "Set by hand — new alerts no longer change it." : "Follows the alerts until you pick one."}</span>
    </div>
  );
}

function Collapsible({ icon, label, children }: { icon: Parameters<typeof Icon>[0]["name"]; label: string; children: (close: () => void) => React.ReactNode }) {
  const [open, setOpen] = useState(false);
  if (!open) return <button type="button" className="btn-ghost btn-sm" onClick={() => setOpen(true)}><Icon name={icon} size={14} /> {label}</button>;
  return <div className="reassign-form form-stack">{children(() => setOpen(false))}</div>;
}

/** Page one more person — or whoever is on call — now, outside the escalation policy. */
function AddResponder({ busy, onSubmit }: { busy: boolean; onSubmit: (b: { user_id?: string | null; schedule_id?: string | null; note?: string }) => void }) {
  const [pick, setPick] = useState("");
  const [note, setNote] = useState("");
  const users = usePoll(() => api.listUsers(), []);
  const schedules = usePoll(() => oncallApi.list(), []);
  return (
    <Collapsible icon="bell" label="Add responder">{(close) => (
      <form onSubmit={(e) => {
        e.preventDefault();
        const [kind, id] = pick.split(":");
        onSubmit(kind === "user" ? { user_id: id, note: note.trim() } : { schedule_id: id, note: note.trim() });
        close();
      }}>
        <h4>Add a responder</h4>
        <label>Page
          <select required value={pick} onChange={(e) => setPick(e.target.value)}>
            <option value="">— choose —</option>
            <optgroup label="Whoever is on call">{(schedules.data ?? []).map((s) => <option key={s.id} value={`schedule:${s.id}`}>{s.name}</option>)}</optgroup>
            <optgroup label="People">{(users.data ?? []).filter((u) => !u.disabled).map((u) => <option key={u.id} value={`user:${u.id}`}>{u.name || u.email}</option>)}</optgroup>
          </select>
        </label>
        <label>Why (in the page)
          <input value={note} maxLength={2000} onChange={(e) => setNote(e.target.value)} placeholder="need someone who knows the database" />
        </label>
        <p className="muted small">Paged now by every one of their contact rules, outside the escalation policy — acknowledged or not.</p>
        <div className="detail-actions">
          <button className="btn-primary" disabled={busy || !pick}>Page them</button>
          <button type="button" className="btn-ghost" onClick={close}>Cancel</button>
        </div>
      </form>
    )}</Collapsible>
  );
}

const SNOOZES = [[30, "30 min"], [60, "1 hour"], [120, "2 hours"], [240, "4 hours"], [480, "8 hours"], [1440, "24 hours"]] as const;

/** Silence this incident's alerts for a while. The silence is an ordinary one (Silences page). */
function Snooze({ busy, onSubmit }: { busy: boolean; onSubmit: (minutes: number, comment: string) => void }) {
  const [minutes, setMinutes] = useState(60);
  const [comment, setComment] = useState("");
  return (
    <Collapsible icon="bellOff" label="Snooze">{(close) => (
      <form onSubmit={(e) => { e.preventDefault(); onSubmit(minutes, comment.trim()); close(); }}>
        <h4>Snooze</h4>
        <div className="form-grid">
          <label>For
            <select value={minutes} onChange={(e) => setMinutes(Number(e.target.value))}>
              {SNOOZES.map(([m, l]) => <option key={m} value={m}>{l}</option>)}
            </select>
          </label>
          <label>Why <span className="muted small">(optional)</span>
            <input value={comment} maxLength={2000} onChange={(e) => setComment(e.target.value)} placeholder="deploy in progress" />
          </label>
        </div>
        <p className="muted small">Silences the alerts this incident groups on. Escalation holds and resumes where it was when the silence ends; it shows on the Silences page.</p>
        <div className="detail-actions">
          <button className="btn-primary" disabled={busy}>Snooze</button>
          <button type="button" className="btn-ghost" onClick={close}>Cancel</button>
        </div>
      </form>
    )}</Collapsible>
  );
}

function AddNote({ busy, onSubmit }: { busy: boolean; onSubmit: (text: string) => void }) {
  const [text, setText] = useState("");
  return (
    <Collapsible icon="note" label="Add note">{(close) => (
      <form onSubmit={(e) => { e.preventDefault(); if (text.trim()) { onSubmit(text.trim()); setText(""); close(); } }}>
        <h4>Add a note</h4>
        <label>Note
          <textarea rows={3} value={text} maxLength={4000} onChange={(e) => setText(e.target.value)}
            placeholder="Goes on the timeline of every open alert — and into the Slack thread." />
        </label>
        <div className="detail-actions">
          <button className="btn-primary" disabled={busy || !text.trim()}>Add note</button>
          <button type="button" className="btn-ghost" onClick={close}>Cancel</button>
        </div>
      </form>
    )}</Collapsible>
  );
}
