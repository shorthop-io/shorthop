import { useMemo, useState } from "react";
import { api, type ContactRule } from "../api";
import { useMe, useNavigate } from "../app";
import { Icon } from "../components/Icon";
import { ErrorNotice, Field } from "../components/ui";
import { THEME_CHOICES, useTheme } from "../theme";
import { CalendarPanel } from "./Calendar";
import { Phones } from "./Phones";
import { TokensPanel } from "./Tokens";

const TABS = [
  { key: "profile", label: "Profile" },
  { key: "paging", label: "How I'm paged" },
  { key: "phones", label: "Phones" },
  { key: "calendar", label: "Calendar" },
  { key: "api", label: "API & MCP" },
  { key: "appearance", label: "Appearance" },
] as const;
type Tab = (typeof TABS)[number]["key"];

export function Account({ search }: { search: URLSearchParams }) {
  const navigate = useNavigate();
  const asked = search.get("tab");
  const tab: Tab = (TABS.find((t) => t.key === asked)?.key ?? "profile") as Tab;
  return (
    <div className="page">
      <div className="section-head"><h2>My account</h2></div>
      <div className="seg seg--tabs" role="tablist" aria-label="Account sections">
        {TABS.map((t) => (
          <button key={t.key} role="tab" aria-selected={tab === t.key} className={`seg-btn${tab === t.key ? " active" : ""}`}
            onClick={() => navigate(t.key === "profile" ? "/account" : `/account?tab=${t.key}`)}>{t.label}</button>
        ))}
      </div>
      {tab === "profile" && <Profile />}
      {tab === "paging" && <Paging />}
      {tab === "phones" && <Phones />}
      {tab === "calendar" && <CalendarPanel preselect={search.get("schedule")} />}
      {tab === "api" && <TokensPanel />}
      {tab === "appearance" && <Appearance />}
    </div>
  );
}

function zones(): string[] {
  try {
    return (Intl as unknown as { supportedValuesOf: (k: string) => string[] }).supportedValuesOf("timeZone");
  } catch {
    return [];
  }
}

function Profile() {
  const { me, reload } = useMe();
  const [name, setName] = useState(me.name);
  const [tz, setTz] = useState(me.timezone ?? "");
  const [phone, setPhone] = useState(me.phone ?? "");
  const [msg, setMsg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const all = useMemo(zones, []);
  const browserTz = Intl.DateTimeFormat().resolvedOptions().timeZone;

  const save = async () => {
    setError(null); setMsg(null);
    try {
      await api.updateMe({ name: name.trim(), timezone: tz || null, phone: phone.trim() || null });
      reload();
      setMsg("Saved.");
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <form className="card pad form-stack narrow" onSubmit={(e) => { e.preventDefault(); save(); }}>
      <Field label="Email" hint="How you sign in. An admin changes it."><input value={me.email} disabled /></Field>
      <Field label="Role" hint="Set by an admin on People."><input value={me.role} disabled /></Field>
      <Field label="Name"><input maxLength={200} value={name} onChange={(e) => setName(e.target.value)} placeholder="Ada Lovelace" /></Field>
      <Field label="Time zone" hint={`Used where Shorthop states times for you (handovers, your shifts). Empty = this device's (${browserTz}).`}>
        {all.length ? (
          <select value={tz} onChange={(e) => setTz(e.target.value)}>
            <option value="">This device's ({browserTz})</option>
            {all.map((z) => <option key={z} value={z}>{z}</option>)}
          </select>
        ) : <input value={tz} onChange={(e) => setTz(e.target.value)} placeholder="Europe/Stockholm" />}
      </Field>
      <Field label="Phone" hint="For an SMS or voice fallback later. Not used for paging yet.">
        <input type="tel" value={phone} onChange={(e) => setPhone(e.target.value)} placeholder="+46 70 123 45 67" />
      </Field>
      <ErrorNotice>{error}</ErrorNotice>
      {msg && <p className="small ok-ink">{msg}</p>}
      <div><button className="btn-primary">Save</button></div>
    </form>
  );
}

const DELAYS = [0, 1, 2, 5, 10, 15, 30, 60];

function Paging() {
  const { me, reload } = useMe();
  const [rules, setRules] = useState<ContactRule[]>(me.contact_rules.map((r) => ({ ...r })));
  const [error, setError] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const set = (i: number, patch: Partial<ContactRule>) => setRules(rules.map((r, j) => (j === i ? { ...r, ...patch } : r)));

  const save = async (value: ContactRule[] | null) => {
    setError(null); setMsg(null);
    try {
      const m = await api.setContactRules(value);
      setRules(m.contact_rules.map((r) => ({ ...r })));
      reload();
      setMsg(value === null ? "Back to the default." : "Saved.");
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const gap = !rules.some((r) => r.delay_minutes === 0 && !r.severities?.length);
  return (
    <div className="form-stack">
      <p className="glance">When an escalation step reaches you, Shorthop pages you like this. A delayed page is
        dropped if somebody acknowledges first — "email me only if I haven't reacted in 5 minutes".</p>
      <div className="card pad form-stack">
        {me.contact_rules_default && <p className="notice">You are on the default: push to every paired phone and an email, at once.</p>}
        <ol className="contact-rules">
          {rules.map((r, i) => (
            <li key={i} className="contact-rule">
              <select aria-label="How" value={r.method} onChange={(e) => set(i, { method: e.target.value as ContactRule["method"] })}>
                <option value="push">Push to my phones</option>
                <option value="email">Email</option>
                <option value="slack">Slack DM</option>
              </select>
              <select aria-label="When" value={r.delay_minutes} onChange={(e) => set(i, { delay_minutes: Number(e.target.value) })}>
                {[...new Set([...DELAYS, r.delay_minutes])].sort((a, b) => a - b).map((d) =>
                  <option key={d} value={d}>{d === 0 ? "at once" : `after ${d} min if not acknowledged`}</option>)}
              </select>
              <input aria-label="Only for severities" value={(r.severities ?? []).join(", ")}
                onChange={(e) => set(i, { severities: e.target.value.split(",").map((x) => x.trim()).filter(Boolean) || null })}
                placeholder="any severity" />
              <button type="button" className="btn-ghost btn-sm btn-danger" aria-label="Remove rule"
                disabled={rules.length === 1} onClick={() => setRules(rules.filter((_, j) => j !== i))}><Icon name="trash" size={14} /></button>
            </li>
          ))}
        </ol>
        <div><button type="button" className="btn-ghost btn-sm" onClick={() => setRules([...rules, { method: "email", delay_minutes: 5, severities: null }])}>
          <Icon name="plus" size={14} /> Add a rule</button></div>
        {gap && <p className="notice notice--warn">No rule pages you at once for every severity — some pages reach you only after a delay, or not at all.</p>}
        {me.contact_rules.some((r) => r.method === "push") && <PhonesHint />}
        {rules.some((r) => r.method === "slack") && <p className="muted small">A Slack DM goes to the Slack account with your email ({me.email}) in the workspace the Shorthop Slack app is installed in — the message has the same buttons as in a channel. Needs an admin to set up Slack (Settings → Slack).</p>}
        <ErrorNotice>{error}</ErrorNotice>
        {msg && <p className="small ok-ink">{msg}</p>}
        <div className="detail-actions">
          <button className="btn-primary" onClick={() => save(rules.map((r) => ({ ...r, severities: r.severities?.length ? r.severities : null })))}>Save</button>
          {!me.contact_rules_default && <button type="button" className="btn-ghost" onClick={() => save(null)}>Use the default</button>}
        </div>
      </div>
    </div>
  );
}

function PhonesHint() {
  const navigate = useNavigate();
  return <p className="muted small">Push goes to every phone paired on <button type="button" className="link-btn" onClick={() => navigate("/account?tab=phones")}>Phones</button> — with none paired, push rules reach nobody.</p>;
}

function Appearance() {
  const { choice, setChoice } = useTheme();
  return (
    <div className="card pad form-stack narrow">
      <p className="muted small">Kept on this device, so a phone at night and a laptop in daylight can differ.</p>
      <div className="seg" role="radiogroup" aria-label="Theme">
        {THEME_CHOICES.map((c) => (
          <button key={c} type="button" role="radio" aria-checked={choice === c} className={`seg-btn${choice === c ? " active" : ""}`} onClick={() => setChoice(c)}>
            {c === "system" ? "Follow the system" : c === "light" ? "Light" : "Dark"}
          </button>
        ))}
      </div>
    </div>
  );
}
