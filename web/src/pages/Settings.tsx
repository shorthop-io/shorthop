import { useEffect, useState } from "react";
import { settingsApi, slackApi, type Level, type RelayStatus, type SettingItem, type SettingKey, type SettingsIn, type SlackStatus } from "../api";
import { useMe, useNavigate, useNow } from "../app";
import { Icon } from "../components/Icon";
import { ErrorNotice, Field } from "../components/ui";
import { relative } from "../time";

type ByKey = Record<SettingKey, SettingItem>;

const TABS = [
  { key: "levels", label: "Incident levels" },
  { key: "slack", label: "Slack" },
  { key: "email", label: "Email" },
  { key: "phones", label: "Phones" },
  { key: "network", label: "Network" },
] as const;
type Tab = (typeof TABS)[number]["key"];

/** Where a value comes from, and the way back to the deployment's value. */
function Source({ item, now, onReset }: { item: SettingItem; now: number; onReset: () => void }) {
  if (item.source === "environment") {
    return <span className="field-hint">From the environment (<code>{item.env}</code>)</span>;
  }
  return (
    <span className="field-hint">
      Set here{item.updated_by ? ` by ${item.updated_by}` : ""} {relative(item.updated_at, now)} ·{" "}
      <button type="button" className="link-btn" onClick={onReset}>use <code>{item.env}</code> instead</button>
    </span>
  );
}

export function Settings({ search }: { search: URLSearchParams }) {
  const { me, can } = useMe();
  const navigate = useNavigate();
  const asked = search.get("tab");
  const tab: Tab = (TABS.find((t) => t.key === asked)?.key ?? "levels") as Tab;
  const now = useNow(60000);
  const [items, setItems] = useState<ByKey | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);

  const [hosts, setHosts] = useState("");
  const [hookHosts, setHookHosts] = useState("");
  const [host, setHost] = useState("");
  const [port, setPort] = useState("");
  const [user, setUser] = useState("");
  const [password, setPassword] = useState("");
  const [sender, setSender] = useState("");
  const [starttls, setStarttls] = useState(true);
  const [relayUrl, setRelayUrl] = useState("");
  const [relayKey, setRelayKey] = useState("");
  const [testTo, setTestTo] = useState("");
  const [testResult, setTestResult] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = (list: SettingItem[]) => {
    const m = Object.fromEntries(list.map((s) => [s.key, s])) as ByKey;
    setItems(m);
    setHosts(((m.prometheus_allowed_hosts.value as string[]) ?? []).join("\n"));
    setHookHosts(((m.webhook_private_hosts?.value as string[]) ?? []).join("\n"));
    setHost(String(m.smtp_host.value ?? ""));
    setPort(m.smtp_port.value == null ? "" : String(m.smtp_port.value));
    setUser(String(m.smtp_user.value ?? ""));
    setSender(String(m.smtp_from.value ?? ""));
    setStarttls(Boolean(m.smtp_starttls.value));
    setPassword("");
    setRelayUrl(String(m.relay_url.value ?? ""));
    setRelayKey("");
  };

  useEffect(() => {
    if (!can("admin")) return;
    settingsApi.get().then((r) => load(r.settings)).catch((e) => setError((e as Error).message));
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const [relayStatus, setRelayStatus] = useState<RelayStatus | null>(null);
  const loadRelayStatus = () => settingsApi.relayStatus().then(setRelayStatus).catch(() => setRelayStatus(null));
  useEffect(() => {
    if (can("admin")) loadRelayStatus();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const [slack, setSlack] = useState<SlackStatus | null>(null);
  useEffect(() => {
    if (can("admin")) slackApi.status().then(setSlack).catch(() => undefined);
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const put = async (body: SettingsIn, what: string) => {
    setError(null); setSaved(null); setBusy(true);
    try {
      const r = await settingsApi.put(body);
      load(r.settings);
      if (r.slack) setSlack(r.slack);
      let msg = r.changed.length ? `${what} saved.` : "Nothing changed.";
      if (r.relay_phones) {
        loadRelayStatus();
        const p = r.relay_phones;
        msg += ` Phones signed in at the relay: ${p.registered}`
          + (p.refused ? `, refused: ${p.refused} (see My account → Phones)` : "")
          + (p.error ? `, could not reach the relay for ${p.error} (tried again on the next page)` : "") + ".";
      }
      setSaved(msg);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  // Only fields that CHANGED are sent: saving must not turn every value the environment
  // supplies into an override that silently stops following the deployment.
  const changedOnly = (want: SettingsIn): SettingsIn => {
    const out: SettingsIn = {};
    for (const [k, v] of Object.entries(want) as [SettingKey, SettingsIn[SettingKey]][]) {
      const cur = items?.[k]?.value ?? null;
      if (JSON.stringify(cur) !== JSON.stringify(v)) out[k] = v;
    }
    return out;
  };

  const saveHosts = () => put(changedOnly({
    prometheus_allowed_hosts: hosts.split(/[\s,]+/).map((h) => h.trim().toLowerCase()).filter(Boolean),
  }), "Prometheus hosts");

  const saveHookHosts = () => put(changedOnly({
    webhook_private_hosts: hookHosts.split(/[\s,]+/).map((h) => h.trim().toLowerCase()).filter(Boolean),
  }), "Internal webhook hosts");

  const saveSmtp = () => {
    const body = changedOnly({
      smtp_host: host.trim() || null, smtp_port: port.trim() ? Number(port) : null,
      smtp_user: user.trim() || null, smtp_from: sender.trim() || null, smtp_starttls: starttls,
    });
    if (password) body.smtp_password = password; // omitted = keep the stored one
    return put(body, "Email settings");
  };

  const saveRelay = () => {
    const body = changedOnly({ relay_url: relayUrl.trim().replace(/\/+$/, "") || null });
    if (relayKey.trim()) body.relay_key = relayKey.trim(); // omitted = keep the stored one
    return put(body, "Push relay");
  };

  const test = async () => {
    setTestResult("sending…");
    try {
      const r = await settingsApi.testSmtp(testTo.trim() || undefined);
      setTestResult(r.ok ? `✓ sent to ${r.to}` : `✗ ${r.error}`);
    } catch (e) {
      setTestResult(`✗ ${(e as Error).message}`);
    }
  };

  if (!can("admin")) {
    return <div className="page"><h2>Settings</h2><p className="notice">Only admins can see and change settings.</p></div>;
  }

  return (
    <div className="page">
      <div className="section-head"><h2>Settings</h2></div>
      <p className="glance">Values set here override the environment variables the deployment sets. Only fields you change become overrides; “use … instead” hands one back to the environment.</p>
      <div className="seg seg--tabs" role="tablist" aria-label="Settings sections">
        {TABS.map((t) => (
          <button key={t.key} role="tab" aria-selected={tab === t.key} className={`seg-btn${tab === t.key ? " active" : ""}`}
            onClick={() => { setSaved(null); setError(null); navigate(t.key === "levels" ? "/settings" : `/settings?tab=${t.key}`); }}>{t.label}</button>
        ))}
      </div>
      <ErrorNotice>{error}</ErrorNotice>
      {saved && <p className="small ok-ink">{saved}</p>}

      {items && tab === "levels" && <LevelsCard item={items.incident_levels} busy={busy} now={now} put={put} />}
      {items && tab === "slack" && <SlackCard items={items} status={slack} busy={busy} now={now} put={put} />}
      {items && tab === "network" && (
        <>
          <form className="card pad form-stack narrow settings-card" onSubmit={(e) => { e.preventDefault(); saveHosts(); }}>
            <h3><Icon name="chart" size={16} /> Prometheus hosts</h3>
            <p className="muted small">The hosts an integration may be compared with (Integrations → Compare with Prometheus). Shorthop only calls hosts on this list.</p>
            <Field label="Allowed hosts" hint="One per line — host name or IP only, no scheme or port. Example: monitoring-prometheus.monitoring.svc.cluster.local">
              <textarea className="mono" rows={4} value={hosts} onChange={(e) => setHosts(e.target.value)} placeholder="prometheus.monitoring.svc.cluster.local" />
            </Field>
            <Source item={items.prometheus_allowed_hosts} now={now} onReset={() => put({ prometheus_allowed_hosts: null }, "Prometheus hosts")} />
            <div className="detail-actions"><button className="btn-primary" disabled={busy}>Save</button></div>
          </form>

          {items.webhook_private_hosts && (
            <form className="card pad form-stack narrow settings-card" onSubmit={(e) => { e.preventDefault(); saveHookHosts(); }}>
              <h3><Icon name="link" size={16} /> Internal webhook hosts</h3>
              <p className="muted small">Webhook channels may only call public addresses, so a channel cannot be used to reach this server's own network. List a host here to allow it anyway, for example an internal chat server.</p>
              <Field label="Allowed internal hosts" hint="One per line — host name or IP only, no scheme or port. Example: chat.internal.example.com">
                <textarea className="mono" rows={3} value={hookHosts} onChange={(e) => setHookHosts(e.target.value)} placeholder="chat.internal.example.com" />
              </Field>
              <Source item={items.webhook_private_hosts} now={now} onReset={() => put({ webhook_private_hosts: null }, "Internal webhook hosts")} />
              <div className="detail-actions"><button className="btn-primary" disabled={busy}>Save</button></div>
            </form>
          )}
        </>
      )}

      {items && tab === "email" && (
        <>
          <form className="card pad form-stack narrow settings-card" onSubmit={(e) => { e.preventDefault(); saveSmtp(); }}>
            <h3><Icon name="mail" size={16} /> Email (SMTP)</h3>
            <p className="muted small">The server Shorthop sends pages and channel email through.</p>
            <div className="smtp-grid">
              <Field label="Server"><input value={host} onChange={(e) => setHost(e.target.value)} placeholder="smtp.example.com" /></Field>
              <Field label="Port"><input inputMode="numeric" value={port} onChange={(e) => setPort(e.target.value.replace(/\D/g, ""))} placeholder="587" /></Field>
            </div>
            <Source item={items.smtp_host} now={now} onReset={() => put({ smtp_host: null }, "Server")} />
            <label className="check"><input type="checkbox" checked={starttls} onChange={(e) => setStarttls(e.target.checked)} /> Use STARTTLS (port 465 always uses TLS)</label>
            <Field label="Username"><input autoComplete="off" value={user} onChange={(e) => setUser(e.target.value)} /></Field>
            <Field label="Password" hint={items.smtp_password.is_set ? "A password is stored — it is never shown. Leave empty to keep it." : "No password stored."}>
              <input type="password" autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} placeholder={items.smtp_password.is_set ? "••••••" : ""} />
            </Field>
            {items.smtp_password.source === "settings" && (
              <span className="field-hint"><button type="button" className="link-btn" onClick={() => put({ smtp_password: null }, "Password")}>Remove the stored password</button> (use <code>SMTP_PASSWORD</code> instead)</span>
            )}
            <Field label="From" hint="An address, or 'Name <address>'."><input value={sender} onChange={(e) => setSender(e.target.value)} placeholder="Shorthop <alerts@example.com>" /></Field>
            <Source item={items.smtp_from} now={now} onReset={() => put({ smtp_from: null }, "From address")} />
            <div className="detail-actions"><button className="btn-primary" disabled={busy}>Save</button></div>
          </form>

          <TestEmail me={me?.email} testTo={testTo} setTestTo={setTestTo} testResult={testResult} onTest={test} />
        </>
      )}

      {items && tab === "phones" && (
        <>
          <form className="card pad form-stack narrow settings-card" onSubmit={(e) => { e.preventDefault(); saveRelay(); }}>
            <h3><Icon name="phone" size={16} /> Push relay</h3>
            <p className="muted small">Pages reach the Shorthop app through the relay at shorthop.io. It receives each phone's push token and name, and for each page only an incident id — never alert content. Leave both empty to push straight to Expo with this install's own credentials.</p>
            {relayStatus?.configured && <RelayAccount s={relayStatus} />}
            <Field label="Relay URL" hint="https:// and the host only, e.g. https://relay.shorthop.io">
              <input value={relayUrl} onChange={(e) => setRelayUrl(e.target.value)} placeholder="https://relay.shorthop.io" />
            </Field>
            <Source item={items.relay_url} now={now} onReset={() => put({ relay_url: null }, "Relay URL")} />
            <Field label="Installation key" hint={items.relay_key.is_set ? "A key is stored — it is never shown. Leave empty to keep it." : "From your account at shorthop.io (shr_…)."}>
              <input type="password" autoComplete="off" className="mono" value={relayKey} onChange={(e) => setRelayKey(e.target.value)} placeholder={items.relay_key.is_set ? "••••••" : "shr_…"} />
            </Field>
            {items.relay_key.source === "settings" && (
              <span className="field-hint"><button type="button" className="link-btn" onClick={() => put({ relay_key: null }, "Installation key")}>Remove the stored key</button> (use <code>RELAY_KEY</code> instead)</span>
            )}
            <p className="muted small">Saving signs every phone in at the relay again. The first 3 phones are free; past that the relay refuses new phones until a payment method is added — phones already signed in keep receiving pages.</p>
            <div className="detail-actions"><button className="btn-primary" disabled={busy}>Save</button></div>
          </form>

        </>
      )}
    </div>
  );
}


type Put = (body: SettingsIn, what: string) => Promise<void>;

function TestEmail({ me, testTo, setTestTo, testResult, onTest }: {
  me: string | undefined; testTo: string; setTestTo: (v: string) => void; testResult: string | null; onTest: () => void;
}) {
  return (
    <form className="card pad form-stack narrow settings-card" onSubmit={(e) => { e.preventDefault(); onTest(); }}>
      <h3><Icon name="megaphone" size={16} /> Send a test email</h3>
      <p className="muted small">Uses the saved settings above — save first.</p>
      <div className="form-row">
        <input aria-label="Send the test to" value={testTo} onChange={(e) => setTestTo(e.target.value)} placeholder={me ?? "you@example.com"} />
        <button className="btn-ghost">Send test</button>
      </div>
      {testResult && <p className={`small ${testResult.startsWith("✓") ? "ok-ink" : testResult.startsWith("✗") ? "error" : "muted"}`}>{testResult}</p>}
    </form>
  );
}

/** The Slack app: two tokens, how to make them, and whether this server is connected. */
function SlackCard({ items, status, busy, now, put }: { items: ByKey; status: SlackStatus | null; busy: boolean; now: number; put: Put }) {
  const [bot, setBot] = useState("");
  const [app, setApp] = useState("");
  const [manifest, setManifest] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const showManifest = async () => {
    const m = await slackApi.manifest();
    setManifest(JSON.stringify(m.manifest, null, 2));
  };
  const save = () => {
    const body: SettingsIn = {};
    if (bot.trim()) body.slack_bot_token = bot.trim();
    if (app.trim()) body.slack_app_token = app.trim();
    return put(body, "Slack").then(() => { setBot(""); setApp(""); });
  };
  const state = !status ? null
    : status.connected ? <span className="badge ok">Connected to {status.team} as @{status.bot_user}</span>
      : status.configured ? <span className="badge danger">Not connected{status.error ? ` — ${status.error}` : ""}</span>
        : <span className="badge neutral">Not set up</span>;
  return (
    <form className="card pad form-stack narrow settings-card" onSubmit={(e) => { e.preventDefault(); save(); }}>
      <h3><Icon name="chat" size={16} /> Slack</h3>
      <p className="muted small">Incidents appear in Slack as one live message each, with buttons to acknowledge, resolve, set the level, move to another team, escalate, add a responder, snooze and add a note — done as the Shorthop user with the same email as the person who clicks. Shorthop connects <em>out</em> to Slack (Socket Mode), so no public URL is needed.</p>
      <div className="slack-status">{state}{status?.since && <span className="muted small">since {relative(status.since, now)} (this server)</span>}</div>
      <ol className="small setup-steps">
        <li>At <a href="https://api.slack.com/apps" target="_blank" rel="noreferrer">api.slack.com/apps</a>: <strong>Create New App → From a manifest</strong>, pick the workspace, paste{" "}
          {manifest === null
            ? <button type="button" className="link-btn" onClick={showManifest}>this manifest</button>
            : <button type="button" className="link-btn" onClick={() => { navigator.clipboard?.writeText(manifest); setCopied(true); }}>{copied ? "copied ✓" : "copy the manifest"}</button>}.</li>
        <li><strong>Install to Workspace</strong>, then copy the <strong>Bot User OAuth Token</strong> (xoxb-…).</li>
        <li><strong>Basic Information → App-Level Tokens → Generate</strong> with the scope <code>connections:write</code> (xapp-…).</li>
        <li>Paste both here, then add a channel of kind <strong>Slack app</strong> and use it in an escalation policy. People can also choose “Slack” in My account → How I'm paged.</li>
      </ol>
      {manifest !== null && <pre className="manifest-box">{manifest}</pre>}
      <Field label="Bot token" hint={items.slack_bot_token?.is_set ? "A token is stored — it is never shown. Leave empty to keep it." : "xoxb-…"}>
        <input type="password" autoComplete="off" className="mono" value={bot} onChange={(e) => setBot(e.target.value)} placeholder={items.slack_bot_token?.is_set ? "••••••" : "xoxb-…"} />
      </Field>
      <Field label="App-level token" hint={items.slack_app_token?.is_set ? "A token is stored — it is never shown. Leave empty to keep it." : "xapp-… (connections:write)"}>
        <input type="password" autoComplete="off" className="mono" value={app} onChange={(e) => setApp(e.target.value)} placeholder={items.slack_app_token?.is_set ? "••••••" : "xapp-…"} />
      </Field>
      {(items.slack_bot_token?.source === "settings" || items.slack_app_token?.source === "settings") && (
        <span className="field-hint"><button type="button" className="link-btn" onClick={() => put({ slack_bot_token: null, slack_app_token: null }, "Slack")}>Remove the stored tokens</button> (use <code>SLACK_BOT_TOKEN</code> / <code>SLACK_APP_TOKEN</code> instead — unset there turns Slack off)</span>
      )}
      <p className="muted small">The command is <code>{status?.command ?? "/shorthop"}</code>: <code>oncall</code>, <code>incidents</code>, <code>show 42</code>, <code>ack 42</code>, <code>level 42 P1</code>, <code>snooze 42 2h</code>. Every server connects; Slack delivers each click to one of them.</p>
      <div className="detail-actions"><button className="btn-primary" disabled={busy || (!bot.trim() && !app.trim())}>Save and connect</button></div>
    </form>
  );
}

/** Incident levels: the list people pick from, most urgent first, each mapped to a severity. */
function LevelsCard({ item, busy, now, put }: { item: SettingItem | undefined; busy: boolean; now: number; put: Put }) {
  const stored = (item?.value as Level[] | null) ?? [];
  const [rows, setRows] = useState<Level[]>(stored);
  useEffect(() => setRows(stored), [JSON.stringify(stored)]); // eslint-disable-line react-hooks/exhaustive-deps
  if (!item) return null;
  const set = (i: number, patch: Partial<Level>) => setRows(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  const move = (i: number, d: number) => {
    const j = i + d;
    if (j < 0 || j >= rows.length) return;
    const next = [...rows];
    [next[i], next[j]] = [next[j], next[i]];
    setRows(next);
  };
  const dirty = JSON.stringify(rows) !== JSON.stringify(stored);
  return (
    <form className="card pad form-stack narrow settings-card" onSubmit={(e) => { e.preventDefault(); put({ incident_levels: rows }, "Incident levels"); }}>
      <h3><Icon name="flag" size={16} /> Incident levels</h3>
      <p className="muted small">What responders pick on an incident (in Shorthop and in Slack), most urgent first. Each level sets the incident's severity, which the contact rules read. Until someone picks one, an incident takes the most urgent <code>level</code> label on its alerts (a label rule can set it), else the level its severity maps to.</p>
      <ul className="contact-rules">
        {rows.map((r, i) => (
          <li key={i} className="level-row">
            <input aria-label="Level name" maxLength={20} value={r.name} onChange={(e) => set(i, { name: e.target.value })} placeholder="P1" />
            <input aria-label="Description" maxLength={200} value={r.description} onChange={(e) => set(i, { description: e.target.value })} placeholder="Customers are affected now" />
            <input aria-label="Severity" maxLength={40} value={r.severity} onChange={(e) => set(i, { severity: e.target.value.toLowerCase() })} placeholder="critical" />
            <span className="row-actions">
              <button type="button" className="btn-ghost btn-sm" aria-label="Move up" disabled={i === 0} onClick={() => move(i, -1)}><Icon name="arrowUp" size={13} /></button>
              <button type="button" className="btn-ghost btn-sm" aria-label="Move down" disabled={i === rows.length - 1} onClick={() => move(i, 1)}><Icon name="arrowDown" size={13} /></button>
              <button type="button" className="btn-ghost btn-sm btn-danger" aria-label="Remove level" disabled={rows.length === 1} onClick={() => setRows(rows.filter((_, j) => j !== i))}><Icon name="trash" size={13} /></button>
            </span>
          </li>
        ))}
      </ul>
      <div><button type="button" className="btn-ghost btn-sm" disabled={rows.length >= 10} onClick={() => setRows([...rows, { name: "", severity: "", description: "" }])}><Icon name="plus" size={14} /> Add level</button></div>
      <Source item={item} now={now} onReset={() => put({ incident_levels: null }, "Incident levels")} />
      <p className="muted small">Changing the list does not change incidents that already have a level.</p>
      <div className="detail-actions"><button className="btn-primary" disabled={busy || !dirty}>Save</button></div>
    </form>
  );
}


/** SHOR-14: what the relay says about this install, and the way to the customer account page
 * (phones, card, invoices). The link comes from the relay itself, so lab and live each point at
 * their own; the server only passes on an https:// address. */
function RelayAccount({ s }: { s: RelayStatus }) {
  if (!s.ok) {
    return <div className="small notice--inline-warn">The push relay could not be asked: {s.error}</div>;
  }
  const over = (s.signed_in ?? 0) >= (s.free ?? 0) && !s.card_on_file;
  return (
    <div className="relay-account">
      <div className="small">
        <strong>{s.name}</strong> · {s.signed_in} {s.signed_in === 1 ? "phone" : "phones"} signed in, {s.free} free
        {" · "}{s.card_on_file ? "card on file" : "no card"}
      </div>
      {over && <div className="small notice--inline-warn">All free phones are in use. Add a card to sign in another — phones already signed in keep receiving pages.</div>}
      {s.account_url && (
        <a className="btn-ghost relay-account-link" href={s.account_url} target="_blank" rel="noopener noreferrer">
          <Icon name="card" size={14} /> Manage phones, card and invoices
        </a>
      )}
      <div className="field-hint">The account page signs in with the installation's contact email.</div>
    </div>
  );
}
