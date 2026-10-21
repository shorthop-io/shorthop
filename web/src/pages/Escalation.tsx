import { useState } from "react";
import { api, oncallApi, paging, type Policy, type PolicyIn, type Step, type Target } from "../api";
import { useMe, usePoll } from "../app";
import { Icon } from "../components/Icon";
import { Empty, ErrorNotice, Field } from "../components/ui";

const blank = (): PolicyIn => ({ name: "", team_id: null, repeat: 0, steps: [{ delay_minutes: 0, targets: [] }] });

export function Escalation() {
  const { can } = useMe();
  const policies = usePoll(() => paging.listPolicies(), []);
  const users = usePoll(() => api.listUsers(), []);
  const teams = usePoll(() => api.listTeams(), []);
  const channels = usePoll(() => paging.listChannels(), []);
  const schedules = usePoll(() => oncallApi.list(), []);
  const [editing, setEditing] = useState<string | "new" | null>(null);
  const [draft, setDraft] = useState<PolicyIn>(blank());
  const [error, setError] = useState<string | null>(null);
  const manage = can("config:write");

  const label = (t: Target): string => {
    if (t.type === "user") { const u = users.data?.find((x) => x.id === t.id); return u ? (u.name || u.email) : "deleted user"; }
    if (t.type === "team") return `team ${teams.data?.find((x) => x.id === t.id)?.name ?? "(deleted)"}`;
    if (t.type === "schedule") return `on call: ${schedules.data?.find((x) => x.id === t.id)?.name ?? "(deleted)"}`;
    return `channel ${channels.data?.find((x) => x.id === t.id)?.name ?? "(deleted)"}`;
  };

  const options: { value: string; label: string }[] = [
    ...(schedules.data ?? []).map((sc) => ({ value: `schedule:${sc.id}`, label: `On call: ${sc.name}` })),
    ...(users.data ?? []).filter((u) => !u.disabled).map((u) => ({ value: `user:${u.id}`, label: u.name || u.email })),
    ...(teams.data ?? []).map((t) => ({ value: `team:${t.id}`, label: `Team: ${t.name}` })),
    ...(channels.data ?? []).map((c) => ({ value: `channel:${c.id}`, label: `Channel: ${c.name} (${c.kind})` })),
  ];

  const setStep = (i: number, s: Step) => setDraft({ ...draft, steps: draft.steps.map((x, j) => (j === i ? s : x)) });

  const save = async () => {
    setError(null);
    try {
      if (editing === "new") await paging.createPolicy(draft);
      else if (editing) await paging.updatePolicy(editing, draft);
      setEditing(null);
      policies.refresh();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const remove = async (p: Policy) => {
    if (!window.confirm(`Delete "${p.name}"?`)) return;
    try {
      await paging.deletePolicy(p.id);
      policies.refresh();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <div className="page">
      <div className="section-head">
        <h2>Escalation policies</h2>
        {manage && editing === null && <button className="btn-primary" onClick={() => { setDraft(blank()); setEditing("new"); setError(null); }}><Icon name="plus" size={15} /> New policy</button>}
      </div>
      <p className="glance">Who is told, in what order, while an incident stays unacknowledged. Acknowledging stops it; a silence holds it.</p>
      <ErrorNotice>{policies.error || error}</ErrorNotice>

      {editing !== null && (
        <form className="card pad form-stack" onSubmit={(e) => { e.preventDefault(); save(); }}>
          <h3 className="sub">{editing === "new" ? "New policy" : `Edit ${draft.name}`}</h3>
          <div className="form-grid">
            <Field label="Name"><input required maxLength={200} value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} placeholder="platform on-call" /></Field>
            <Field label="Team">
              <select value={draft.team_id ?? ""} onChange={(e) => setDraft({ ...draft, team_id: e.target.value || null })}>
                <option value="">None</option>
                {(teams.data ?? []).map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
              </select>
            </Field>
            <Field label="If nobody acknowledges" hint="Run the whole list again this many times.">
              <select value={draft.repeat} onChange={(e) => setDraft({ ...draft, repeat: Number(e.target.value) })}>
                {[0, 1, 2, 3, 4, 5].map((n) => <option key={n} value={n}>{n === 0 ? "stop after the last step" : `repeat ${n}×`}</option>)}
              </select>
            </Field>
          </div>
          <ol className="step-list">
            {draft.steps.map((s, i) => (
              <li key={i} className="step-card">
                <div className="step-head">
                  <strong>Step {i + 1}</strong>
                  {i === 0 ? <span className="muted small">immediately when the incident opens</span> : (
                    <label className="check">after
                      <input className="in-num" type="number" min={1} max={1440} value={s.delay_minutes || ""}
                        onChange={(e) => setStep(i, { ...s, delay_minutes: Number(e.target.value) })} aria-label={`Delay for step ${i + 1}`} />
                      minutes unacknowledged
                    </label>
                  )}
                  {draft.steps.length > 1 && (
                    <button type="button" className="btn-ghost btn-sm" aria-label={`Remove step ${i + 1}`}
                      onClick={() => setDraft({ ...draft, steps: draft.steps.filter((_, j) => j !== i) })}><Icon name="trash" size={14} /></button>
                  )}
                </div>
                <div className="target-chips">
                  {s.targets.map((t, k) => (
                    <span key={k} className="filter-chip">{label(t)}
                      <button type="button" aria-label="Remove target" className="chip-x"
                        onClick={() => setStep(i, { ...s, targets: s.targets.filter((_, j) => j !== k) })}><Icon name="x" size={12} /></button></span>
                  ))}
                  <select aria-label={`Add a target to step ${i + 1}`} value="" onChange={(e) => {
                    const [type, id] = e.target.value.split(":") as [Target["type"], string];
                    if (id && !s.targets.some((t) => t.type === type && t.id === id)) setStep(i, { ...s, targets: [...s.targets, { type, id }] });
                  }}>
                    <option value="">+ add who to notify…</option>
                    {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                  </select>
                </div>
              </li>
            ))}
          </ol>
          <div><button type="button" className="btn-ghost btn-sm" onClick={() => setDraft({ ...draft, steps: [...draft.steps, { delay_minutes: 10, targets: [] }] })}><Icon name="plus" size={14} /> Add a step</button></div>
          <p className="notice">People are told by email at the address they sign in with. Push to the app comes with the native app.</p>
          <div className="detail-actions">
            <button className="btn-primary">Save policy</button>
            <button type="button" className="btn-ghost" onClick={() => setEditing(null)}>Cancel</button>
          </div>
        </form>
      )}

      {policies.data && policies.data.length === 0 && <Empty icon="users" title="No escalation policies yet">Create one, then point a route at it.</Empty>}
      <div className="team-grid">
        {(policies.data ?? []).map((p) => (
          <section key={p.id} className="card pad">
            <div className="section-head"><h3 className="sub">{p.name}</h3>
              {manage && <div className="row-actions">
                <button className="btn-ghost btn-sm" onClick={() => { const { id: _i, created_at: _c, ...rest } = p; setDraft(rest); setEditing(p.id); setError(null); }}><Icon name="edit" size={14} /> Edit</button>
                <button className="btn-ghost btn-sm btn-danger" aria-label={`Delete ${p.name}`} onClick={() => remove(p)}><Icon name="trash" size={14} /></button>
              </div>}
            </div>
            <ol className="step-summary">
              {p.steps.map((s, i) => (
                <li key={i}><span className="muted small">{i === 0 ? "at once" : `+${s.delay_minutes} min`}</span> {s.targets.map(label).join(", ")}</li>
              ))}
            </ol>
            {p.repeat > 0 && <p className="muted small">then repeats {p.repeat}×</p>}
          </section>
        ))}
      </div>
    </div>
  );
}
