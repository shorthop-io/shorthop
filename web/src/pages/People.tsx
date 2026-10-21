import { useState } from "react";
import { api, type Team, type User } from "../api";
import { useMe, useNow, usePoll } from "../app";
import { Icon } from "../components/Icon";
import { ErrorNotice, Field } from "../components/ui";
import { relative } from "../time";

const ROLES = [
  { value: "viewer", label: "Viewer", hint: "Reads alerts and schedules" },
  { value: "responder", label: "Responder", hint: "Acknowledges, resolves, silences" },
  { value: "admin", label: "Admin", hint: "Everything, including people and integrations" },
];

export function People() {
  const { me, can } = useMe();
  const now = useNow(60000);
  const users = usePoll(() => api.listUsers(), []);
  const teams = usePoll(() => api.listTeams(), []);
  const [tab, setTab] = useState<"users" | "teams">("users");
  const [error, setError] = useState<string | null>(null);

  const run = async (fn: () => Promise<unknown>, after: () => void) => {
    setError(null);
    try {
      await fn();
      after();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <div className="page">
      <div className="section-head"><h2>People</h2></div>
      <div className="section-tabs">
        <button className={`section-tab${tab === "users" ? " active" : ""}`} onClick={() => setTab("users")}>
          Users <span className="tab-count">{users.data?.length ?? "…"}</span>
        </button>
        <button className={`section-tab${tab === "teams" ? " active" : ""}`} onClick={() => setTab("teams")}>
          Teams <span className="tab-count">{teams.data?.length ?? "…"}</span>
        </button>
      </div>
      <ErrorNotice>{users.error || teams.error}</ErrorNotice>
      <ErrorNotice>{error}</ErrorNotice>
      {tab === "users" ? (
        <Users users={users.data ?? []} meId={me.id} admin={can("admin")} now={now}
          run={(fn) => run(fn, users.refresh)} />
      ) : (
        <Teams teams={teams.data ?? []} users={users.data ?? []} manage={can("config:write")}
          meId={me.id} run={(fn) => run(fn, teams.refresh)} />
      )}
    </div>
  );
}

function Users({ users, meId, admin, now, run }: {
  users: User[]; meId: string; admin: boolean; now: number;
  run: (fn: () => Promise<unknown>) => void;
}) {
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [role, setRole] = useState("responder");
  const [pwFor, setPwFor] = useState<string | null>(null);
  const [password, setPassword] = useState("");
  const [saved, setSaved] = useState<string | null>(null);

  return (
    <>
      {admin && (
        <form className="card pad inline-form" onSubmit={(e) => {
          e.preventDefault();
          run(async () => { await api.createUser({ email: email.trim(), name: name.trim(), role }); setEmail(""); setName(""); });
        }}>
          <Field label="Invite by email"><input type="email" required value={email} onChange={(e) => setEmail(e.target.value)} placeholder="name@example.com" /></Field>
          <Field label="Name"><input value={name} onChange={(e) => setName(e.target.value)} /></Field>
          <Field label="Role">
            <select value={role} onChange={(e) => setRole(e.target.value)}>
              {ROLES.map((r) => <option key={r.value} value={r.value}>{r.label}</option>)}
            </select>
          </Field>
          <button className="btn-primary"><Icon name="plus" size={15} /> Invite</button>
          <p className="notice wide">They sign in with Google using this address. There is no self-signup.</p>
        </form>
      )}
      {saved && <p className="notice notice--ok">{saved}</p>}
      <div className="table-wrap">
        <table>
          <thead><tr><th>Person</th><th>Role</th><th>Teams</th><th>Last sign-in</th>{admin && <th></th>}</tr></thead>
          <tbody>
            {users.map((u) => (
              <tr key={u.id} className={u.disabled ? "row-muted" : ""}>
                <td><div>{u.name || u.email}</div>{u.name && <div className="muted small">{u.email}</div>}
                  {u.disabled && <span className="badge neutral">disabled</span>}
                  {u.has_password && <span className="badge neutral" title="Has a break-glass password">password</span>}</td>
                <td>
                  {admin && u.id !== meId ? (
                    <select aria-label={`Role for ${u.email}`} value={u.role}
                      onChange={(e) => run(() => api.updateUser(u.id, { role: e.target.value as User["role"] }))}>
                      {ROLES.map((r) => <option key={r.value} value={r.value}>{r.label}</option>)}
                    </select>
                  ) : ROLES.find((r) => r.value === u.role)?.label}
                </td>
                <td>{u.teams.map((t) => t.name).join(", ") || <span className="muted">—</span>}</td>
                <td>{u.last_login_at ? relative(u.last_login_at, now) : <span className="muted">never</span>}</td>
                {admin && (
                  <td>
                    {u.id !== meId && (
                      <div className="row-actions">
                        <button className="btn-ghost btn-sm" onClick={() => { setPwFor(pwFor === u.id ? null : u.id); setPassword(""); setSaved(null); }}>
                          <Icon name="key" size={14} /> Password
                        </button>
                        <button className="btn-ghost btn-sm" onClick={() => run(() => api.updateUser(u.id, { disabled: !u.disabled }))}>
                          {u.disabled ? "Enable" : "Disable"}
                        </button>
                      </div>
                    )}
                    {pwFor === u.id && (
                      <form className="pw-form" onSubmit={(e) => {
                        e.preventDefault();
                        run(async () => { await api.setPassword(u.id, password); setPwFor(null); setSaved(`Break-glass password set for ${u.email}.`); });
                      }}>
                        <input type="password" minLength={12} required autoComplete="new-password" value={password}
                          aria-label="New password" placeholder="12+ characters" onChange={(e) => setPassword(e.target.value)} />
                        <button className="btn-primary btn-sm">Set</button>
                      </form>
                    )}
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="notice">A break-glass password is for when Google sign-in itself is broken. Normal sign-in is Google.</p>
    </>
  );
}

function Teams({ teams, users, manage, meId, run }: {
  teams: Team[]; users: User[]; manage: boolean; meId: string;
  run: (fn: () => Promise<unknown>) => void;
}) {
  const [slug, setSlug] = useState("");
  const [name, setName] = useState("");
  const [adding, setAdding] = useState<Record<string, string>>({});

  return (
    <>
      {manage && (
        <form className="card pad inline-form" onSubmit={(e) => {
          e.preventDefault();
          run(async () => { await api.createTeam({ slug: slug.trim(), name: name.trim(), description: "" }); setSlug(""); setName(""); });
        }}>
          <Field label="Team name"><input required value={name} onChange={(e) => {
            setName(e.target.value);
            setSlug(e.target.value.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, ""));
          }} placeholder="Platform" /></Field>
          <Field label="Slug"><input required pattern="[a-z][a-z0-9-]{1,62}" value={slug} onChange={(e) => setSlug(e.target.value)} /></Field>
          <button className="btn-primary"><Icon name="plus" size={15} /> Create team</button>
        </form>
      )}
      {teams.length === 0 && <p className="muted">No teams yet. Teams will own routes and on-call schedules.</p>}
      <div className="team-grid">
        {teams.map((t) => {
          const owner = t.members.some((m) => m.user_id === meId && m.role === "owner");
          const canEdit = manage || owner;
          const candidates = users.filter((u) => !t.members.some((m) => m.user_id === u.id) && !u.disabled);
          return (
            <section key={t.id} className="card pad">
              <h3 className="sub">{t.name} <span className="muted small">{t.slug}</span></h3>
              <ul className="member-list">
                {t.members.map((m) => (
                  <li key={m.user_id}>
                    <span>{m.name || m.email}</span>
                    {m.role === "owner" && <span className="badge info">owner</span>}
                    {canEdit && (
                      <button className="btn-ghost btn-sm" aria-label={`Remove ${m.email}`}
                        onClick={() => run(() => api.removeMember(t.id, m.user_id))}><Icon name="x" size={13} /></button>
                    )}
                  </li>
                ))}
                {t.members.length === 0 && <li className="muted small">No members yet.</li>}
              </ul>
              {canEdit && candidates.length > 0 && (
                <div className="form-row">
                  <select aria-label={`Add to ${t.name}`} value={adding[t.id] ?? ""}
                    onChange={(e) => setAdding({ ...adding, [t.id]: e.target.value })}>
                    <option value="">Add a person…</option>
                    {candidates.map((u) => <option key={u.id} value={u.id}>{u.name || u.email}</option>)}
                  </select>
                  <button className="btn-ghost btn-sm" disabled={!adding[t.id]}
                    onClick={() => run(async () => { await api.addMember(t.id, adding[t.id], "member"); setAdding({ ...adding, [t.id]: "" }); })}>Add</button>
                </div>
              )}
            </section>
          );
        })}
      </div>
    </>
  );
}
