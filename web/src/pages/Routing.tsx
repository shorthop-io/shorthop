import { useState } from "react";
import { api, paging, type Matcher, type Route, type RouteIn, type RouteTest } from "../api";
import { Link, useMe, usePoll } from "../app";
import { Icon } from "../components/Icon";
import { MatcherEditor, matcherText, usable } from "../components/MatcherEditor";
import { Empty, ErrorNotice, Field } from "../components/ui";

const blank = (): RouteIn => ({
  name: "", matchers: [], integration_ids: null, team_id: null, escalation_policy_id: null,
  group_by: ["alertname"], notify_resolved: true, enabled: true,
});

export function Routing() {
  const { can } = useMe();
  const routes = usePoll(() => paging.listRoutes(), []);
  const policies = usePoll(() => paging.listPolicies(), []);
  const teams = usePoll(() => api.listTeams(), []);
  const integrations = usePoll(() => api.listIntegrations(), []);
  const [editing, setEditing] = useState<string | "new" | null>(null);
  const [draft, setDraft] = useState<RouteIn>(blank());
  const [groupBy, setGroupBy] = useState("alertname");
  const [error, setError] = useState<string | null>(null);
  const manage = can("config:write");

  const startEdit = (r: Route | null) => {
    setError(null);
    if (r) {
      const { id: _id, position: _p, created_at: _c, ...rest } = r;
      setDraft(rest);
      setGroupBy(r.group_by.join(", "));
      setEditing(r.id);
    } else {
      setDraft(blank());
      setGroupBy("alertname");
      setEditing("new");
    }
  };

  const save = async () => {
    setError(null);
    const body = { ...draft, matchers: usable(draft.matchers),
      group_by: groupBy.split(",").map((x) => x.trim()).filter(Boolean) };
    try {
      if (editing === "new") await paging.createRoute(body);
      else if (editing) await paging.updateRoute(editing, body);
      setEditing(null);
      routes.refresh();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const move = async (idx: number, dir: -1 | 1) => {
    const list = [...(routes.data ?? [])];
    const j = idx + dir;
    if (j < 0 || j >= list.length) return;
    [list[idx], list[j]] = [list[j], list[idx]];
    try {
      await paging.orderRoutes(list.map((r) => r.id));
      routes.refresh();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const remove = async (r: Route) => {
    if (!window.confirm(`Delete route "${r.name}"? Alerts it would have taken fall through to the next match.`)) return;
    try {
      await paging.deleteRoute(r.id);
      routes.refresh();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const policyName = (id: string | null) => policies.data?.find((p) => p.id === id)?.name;
  const teamName = (id: string | null) => teams.data?.find((t) => t.id === id)?.name;
  const list = routes.data ?? [];

  return (
    <div className="page">
      <div className="section-head">
        <h2>Routing</h2>
        {manage && editing === null && <button className="btn-primary" onClick={() => startEdit(null)}><Icon name="plus" size={15} /> New route</button>}
      </div>
      <p className="glance">One flat list, read top to bottom: the first enabled route that matches takes the alert. An alert that matches none is recorded but pages nobody.</p>
      <ErrorNotice>{routes.error || error}</ErrorNotice>

      {editing !== null && (
        <form className="card pad form-stack" onSubmit={(e) => { e.preventDefault(); save(); }}>
          <h3 className="sub">{editing === "new" ? "New route" : `Edit ${draft.name}`}</h3>
          <Field label="Name"><input required maxLength={200} value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} placeholder="database alerts" /></Field>
          <fieldset>
            <legend>Matches alerts where</legend>
            <MatcherEditor value={draft.matchers} allowEmpty onChange={(m: Matcher[]) => setDraft({ ...draft, matchers: m })}
              emptyHint="No matchers: a catch-all. Keep it at the bottom." />
          </fieldset>
          <div className="form-grid">
            <Field label="Escalation policy" hint="Who is told. None = incidents are opened but nobody is paged.">
              <select value={draft.escalation_policy_id ?? ""} onChange={(e) => setDraft({ ...draft, escalation_policy_id: e.target.value || null })}>
                <option value="">None</option>
                {(policies.data ?? []).map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
              </select>
            </Field>
            <Field label="Team">
              <select value={draft.team_id ?? ""} onChange={(e) => setDraft({ ...draft, team_id: e.target.value || null })}>
                <option value="">None</option>
                {(teams.data ?? []).map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
              </select>
            </Field>
            <Field label="Group by" hint="Alerts with equal values of these labels are one incident.">
              <input value={groupBy} onChange={(e) => setGroupBy(e.target.value)} placeholder="alertname, namespace" />
            </Field>
          </div>
          <fieldset>
            <legend>Only from integrations</legend>
            <div className="scope-grid">
              <label className="check"><input type="checkbox" checked={draft.integration_ids === null}
                onChange={(e) => setDraft({ ...draft, integration_ids: e.target.checked ? null : [] })} /> Any</label>
              {draft.integration_ids !== null && (integrations.data ?? []).map((i) => (
                <label key={i.id} className="check"><input type="checkbox" checked={draft.integration_ids!.includes(i.id)}
                  onChange={(e) => setDraft({ ...draft, integration_ids: e.target.checked
                    ? [...draft.integration_ids!, i.id] : draft.integration_ids!.filter((x) => x !== i.id) })} /> {i.name}</label>
              ))}
            </div>
          </fieldset>
          <label className="check"><input type="checkbox" checked={draft.notify_resolved} onChange={(e) => setDraft({ ...draft, notify_resolved: e.target.checked })} /> Tell everyone who was paged when it resolves</label>
          <label className="check"><input type="checkbox" checked={draft.enabled} onChange={(e) => setDraft({ ...draft, enabled: e.target.checked })} /> Enabled</label>
          <div className="detail-actions">
            <button className="btn-primary">Save route</button>
            <button type="button" className="btn-ghost" onClick={() => setEditing(null)}>Cancel</button>
          </div>
        </form>
      )}

      {routes.data && list.length === 0 && (
        <Empty icon="route" title="No routes yet">Without a route, alerts are recorded but nobody is paged. Start with a catch-all.</Empty>
      )}
      <ol className="route-list">
        {list.map((r, idx) => (
          <li key={r.id} className={`card route-row${r.enabled ? "" : " row-muted"}`}>
            <span className="route-pos">{idx + 1}</span>
            <div className="route-main">
              <div className="alert-title">
                <span className="alert-name">{r.name}</span>
                {!r.enabled && <span className="badge neutral">disabled</span>}
                {r.matchers.length === 0 && <span className="badge info">catch-all</span>}
              </div>
              <div className="silence-matchers">
                {r.matchers.map((m, k) => <code key={k} className="matcher">{matcherText(m)}</code>)}
              </div>
              <div className="alert-meta">
                <span className="meta-item"><span className="muted">pages</span> {policyName(r.escalation_policy_id) ?? <em>nobody</em>}</span>
                {r.team_id && <span className="meta-item"><span className="muted">team</span> {teamName(r.team_id)}</span>}
                <span className="meta-item"><span className="muted">group by</span> {r.group_by.join(", ") || "—"}</span>
                {r.integration_ids && <span className="meta-item"><span className="muted">from</span> {r.integration_ids.length} integration(s)</span>}
              </div>
            </div>
            {manage && (
              <div className="row-actions">
                <button className="btn-ghost btn-sm" aria-label="Move up" disabled={idx === 0} onClick={() => move(idx, -1)}><Icon name="arrowUp" size={14} /></button>
                <button className="btn-ghost btn-sm" aria-label="Move down" disabled={idx === list.length - 1} onClick={() => move(idx, 1)}><Icon name="arrowDown" size={14} /></button>
                <button className="btn-ghost btn-sm" onClick={() => startEdit(r)}><Icon name="edit" size={14} /> Edit</button>
                <button className="btn-ghost btn-sm btn-danger" aria-label={`Delete ${r.name}`} onClick={() => remove(r)}><Icon name="trash" size={14} /></button>
              </div>
            )}
          </li>
        ))}
      </ol>

      <Tester />
    </div>
  );
}

function Tester() {
  const [text, setText] = useState("alertname=KubePodCrashLooping\nnamespace=service\nseverity=warning");
  const [result, setResult] = useState<RouteTest | null>(null);
  const [error, setError] = useState<string | null>(null);

  const run = async () => {
    setError(null);
    const labels: Record<string, string> = {};
    for (const line of text.split("\n").map((l) => l.trim()).filter(Boolean)) {
      const i = line.indexOf("=");
      if (i < 1) { setError(`"${line}" is not label=value`); return; }
      labels[line.slice(0, i).trim()] = line.slice(i + 1).trim().replace(/^"|"$/g, "");
    }
    try {
      setResult(await paging.testRoutes(labels));
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <section className="card pad">
      <h3 className="sub">Test the routing</h3>
      <div className="detail-grid">
        <form className="form-stack" onSubmit={(e) => { e.preventDefault(); run(); }}>
          <label>Alert labels, one per line
            <textarea rows={5} className="mono" value={text} onChange={(e) => setText(e.target.value)} />
          </label>
          <div><button className="btn-primary"><Icon name="search" size={15} /> Which route?</button></div>
          <ErrorNotice>{error}</ErrorNotice>
        </form>
        <div>
          {!result && <p className="muted small">Shows which route would take an alert with these labels, and why every route above it did not.</p>}
          {result && (
            <>
              <p className="history-glance">{result.chosen_route_id
                ? <>Taken by <strong>{result.routes.find((r) => r.chosen)?.name}</strong></>
                : <strong>No route matches — recorded, nobody paged.</strong>}</p>
              <ol className="verdicts">
                {result.routes.map((v) => (
                  <li key={v.route_id} className={v.chosen ? "chosen" : v.matched ? "also" : ""}>
                    <Icon name={v.chosen ? "check" : v.matched ? "dot" : "x"} size={14} />
                    <span><strong>{v.name}</strong> — {v.chosen ? "matches, takes it" : v.matched ? "would match, but an earlier route took it" : v.why}</span>
                  </li>
                ))}
              </ol>
            </>
          )}
        </div>
      </div>
      <p className="notice">Routes refer to <Link to="/escalation">escalation policies</Link>, which refer to people, teams and <Link to="/channels">channels</Link>.</p>
    </section>
  );
}
