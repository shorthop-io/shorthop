import { useState } from "react";
import { paging, slackApi, type Channel, type ChannelConfig, type WebhookEvent } from "../api";
import { useMe, usePoll } from "../app";
import { Icon } from "../components/Icon";
import { Empty, ErrorNotice, Field, ShowOnce } from "../components/ui";

const MASK = "••••••";
const EVENTS: WebhookEvent[] = ["triggered", "escalated", "resolved"];
const VARIABLES = ["text", "headline", "title", "severity", "number", "incident_id", "url", "status", "verb", "event",
  "summary", "description", "history", "alerts_text", "alert_count", "open_count", "acknowledged_by"];

interface Preset {
  key: string; label: string; url: string; method: "POST" | "PUT" | "PATCH";
  headers: [string, string][]; template: string; events: WebhookEvent[]; hint: string;
}

// Starting points, not modes: picking one fills the form, everything stays editable.
const PRESETS: Preset[] = [
  { key: "shorthop", label: "Shorthop JSON", url: "", method: "POST", headers: [], template: "", events: [],
    hint: "Shorthop's own JSON body, signed with the channel secret. For your own receiver." },
  { key: "slack", label: "Slack (plain message)", url: "https://hooks.slack.com/services/…", method: "POST", headers: [],
    template: '{"text": "{{text}}"}', events: [],
    hint: "An Incoming Webhook: text only, no buttons. For buttons, use the Slack app channel kind instead." },
  { key: "discord", label: "Discord", url: "https://discord.com/api/webhooks/…", method: "POST", headers: [],
    template: '{"content": "{{text}}"}', events: [], hint: "Channel settings → Integrations → Webhooks." },
  { key: "teams", label: "Teams", url: "https://…logic.azure.com/workflows/…", method: "POST", headers: [],
    template: '{"type": "message", "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": {"type": "AdaptiveCard", "version": "1.4", "body": [{"type": "TextBlock", "text": "{{headline}}", "weight": "Bolder", "wrap": true}, {"type": "TextBlock", "text": "{{alerts_text}}", "wrap": true}, {"type": "TextBlock", "text": "{{url}}", "wrap": true}]}}]}',
    events: [], hint: "A Teams Workflow \"When a webhook request is received\" URL." },
  { key: "telegram", label: "Telegram", url: "https://api.telegram.org/bot<BOT_TOKEN>/sendMessage", method: "POST", headers: [],
    template: '{"chat_id": "<CHAT_ID>", "text": "{{text}}", "disable_web_page_preview": true}', events: [],
    hint: "Replace <BOT_TOKEN> (from @BotFather) and <CHAT_ID>." },
  { key: "jira", label: "Jira issue", url: "https://<site>.atlassian.net/rest/api/2/issue", method: "POST",
    headers: [["Authorization", "Basic <base64 of email:api-token>"]],
    template: '{"fields": {"project": {"key": "OPS"}, "issuetype": {"name": "Task"}, "summary": "{{headline}}", "description": "{{text}}"}}',
    events: ["triggered"], hint: "Opens one issue per incident — so only on 'triggered'. A webhook never learns the issue's key, so it cannot comment or close it: for that, use the Jira channel kind instead." },
  { key: "ai-triage", label: "AI triage agent", url: "", method: "POST", headers: [],
    template: '{"incident_id": "{{incident_id}}", "title": "{{title}}", "severity": "{{severity}}", "url": "{{url}}", "alerts": "{{alerts_text}}", "history": "{{history}}"}',
    events: ["triggered"],
    hint: "Your own agent: it investigates and writes back a note with kind \"suggestion\". Put this channel in the first escalation step. See docs/connect/ai-triage.md." },
];

interface Draft {
  id: string | null; name: string; kind: "email" | "webhook" | "slack" | "jira"; to: string; url: string;
  slackId: string; slackName: string;
  jira: { site: string; email: string; token: string; project: string; issueType: string; labels: string; done: string };
  method: "POST" | "PUT" | "PATCH"; headers: [string, string][]; template: string; events: WebhookEvent[];
  enabled: boolean;
}
const EMPTY_JIRA: Draft["jira"] = { site: "", email: "", token: "", project: "", issueType: "Task", labels: "", done: "" };
const EMPTY: Draft = { id: null, name: "", kind: "email", to: "", url: "", slackId: "", slackName: "", jira: EMPTY_JIRA,
  method: "POST", headers: [], template: "", events: [], enabled: true };

function fromChannel(c: Channel): Draft {
  return { id: c.id, name: c.name, kind: c.kind, to: (c.config.to ?? []).join(", "), url: c.config.url ?? "",
    slackId: c.config.channel_id ?? "", slackName: c.config.channel_name ?? "",
    jira: { site: c.config.site ?? "", email: c.config.email ?? "", token: c.config.api_token ?? "",
      project: c.config.project_key ?? "", issueType: c.config.issue_type ?? "Task",
      labels: (c.config.labels ?? []).join(" "), done: c.config.done_transition ?? "" },
    method: c.config.method ?? "POST", headers: Object.entries(c.config.headers ?? {}),
    template: c.config.body_template ?? "", events: c.config.events ?? [], enabled: c.enabled };
}

function toConfig(d: Draft): ChannelConfig {
  if (d.kind === "email") return { to: d.to.split(",").map((x) => x.trim()).filter(Boolean) };
  if (d.kind === "slack") return { channel_id: d.slackId, channel_name: d.slackName };
  if (d.kind === "jira") {
    const j = d.jira;
    return { site: j.site.trim(), email: j.email.trim() || undefined, api_token: j.token, project_key: j.project.trim(),
      issue_type: j.issueType.trim() || "Task", labels: j.labels.split(/[\s,]+/).filter(Boolean),
      done_transition: j.done.trim() || undefined };
  }
  const headers = Object.fromEntries(d.headers.filter(([k]) => k.trim()).map(([k, v]) => [k.trim(), v]));
  return { url: d.url.trim(), method: d.method, headers, body_template: d.template.trim() || undefined,
    events: d.events.length ? d.events : undefined };
}

export function Channels() {
  const { can } = useMe();
  const { data, error, refresh } = usePoll(() => paging.listChannels(), []);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [created, setCreated] = useState<Channel | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [results, setResults] = useState<Record<string, string>>({});
  const manage = can("config:write");
  const set = (patch: Partial<Draft>) => setDraft((d) => (d ? { ...d, ...patch } : d));

  const save = async () => {
    if (!draft) return;
    setFormError(null);
    const body = { name: draft.name.trim(), kind: draft.kind, team_id: null, config: toConfig(draft), enabled: draft.enabled };
    try {
      if (draft.id) await paging.updateChannel(draft.id, body);
      else setCreated(await paging.createChannel(body));
      setDraft(null);
      refresh();
    } catch (e) {
      setFormError((e as Error).message);
    }
  };

  const test = async (c: Channel) => {
    setResults({ ...results, [c.id]: "sending…" });
    try {
      const r = await paging.testChannel(c.id);
      setResults((x) => ({ ...x, [c.id]: r.ok ? `✓ ${r.detail ?? "delivered"}` : `✗ ${r.error}` }));
    } catch (e) {
      setResults((x) => ({ ...x, [c.id]: `✗ ${(e as Error).message}` }));
    }
  };

  const rotate = async (c: Channel) => {
    if (!window.confirm(`New signing secret for ${c.name}? The receiver must be updated.`)) return;
    try { setCreated(await paging.rotateChannelSecret(c.id)); } catch (e) { setFormError((e as Error).message); }
  };

  const remove = async (c: Channel) => {
    if (!window.confirm(`Delete channel ${c.name}?`)) return;
    try { await paging.deleteChannel(c.id); refresh(); } catch (e) { setFormError((e as Error).message); }
  };

  const applyPreset = (p: Preset) =>
    set({ url: p.url, method: p.method, headers: p.headers, template: p.template, events: p.events });

  return (
    <div className="page">
      <div className="section-head">
        <h2>Channels</h2>
        {manage && !draft && <button className="btn-primary" onClick={() => { setDraft({ ...EMPTY }); setCreated(null); }}><Icon name="plus" size={15} /> New channel</button>}
      </div>
      <p className="glance">Destinations that are not a person: a shared mailbox, a Slack channel through the Shorthop Slack app (one live message per incident, with buttons), a Jira project (one issue per incident, commented as it changes and closed when it resolves), or a webhook — Teams, Discord, Telegram, or your own endpoint. People are reached by their own contact rules.</p>

      {created?.secret && (
        <ShowOnce label={`Signing secret for ${created.name}`} value={created.secret}>
          <p className="muted small">Verify each request: <code>X-Shorthop-Signature</code> = <code>sha256=HMAC-SHA256(secret, X-Shorthop-Timestamp + "." + body)</code>. Services like Slack ignore it.</p>
        </ShowOnce>
      )}

      {draft && (
        <form className="card pad form-stack narrow" onSubmit={(e) => { e.preventDefault(); save(); }}>
          <Field label="Name"><input required maxLength={200} value={draft.name} onChange={(e) => set({ name: e.target.value })} placeholder="ops mailbox" /></Field>
          {!draft.id && (
            <div className="seg">
              {(["email", "slack", "jira", "webhook"] as const).map((k) => <button key={k} type="button" className={`seg-btn${draft.kind === k ? " active" : ""}`} onClick={() => set({ kind: k })}>{({ email: "Email", slack: "Slack app", jira: "Jira", webhook: "Webhook" })[k]}</button>)}
            </div>
          )}
          {draft.kind === "slack" ? (
            <SlackChannelPicker id={draft.slackId} onPick={(slackId, slackName) => set({ slackId, slackName, name: draft.name || slackName })} />
          ) : draft.kind === "jira" ? (
            <JiraFields value={draft.jira} editing={!!draft.id} onChange={(jira) => set({ jira })} />
          ) : draft.kind === "email" ? (
            <Field label="Send to" hint="Comma-separated, up to 20 addresses."><input required value={draft.to} onChange={(e) => set({ to: e.target.value })} placeholder="ops@example.com" /></Field>
          ) : (
            <>
              <Field label="Start from" hint={PRESETS.find((p) => p.template === draft.template && p.url === draft.url)?.hint ?? "Fills the fields below; everything stays editable."}>
                <select value="" onChange={(e) => { const p = PRESETS.find((x) => x.key === e.target.value); if (p) applyPreset(p); }}>
                  <option value="">Choose a template…</option>
                  {PRESETS.map((p) => <option key={p.key} value={p.key}>{p.label}</option>)}
                </select>
              </Field>
              <div className="method-url">
                <Field label="Method">
                  <select value={draft.method} onChange={(e) => set({ method: e.target.value as Draft["method"] })}>
                    {(["POST", "PUT", "PATCH"] as const).map((m) => <option key={m}>{m}</option>)}
                  </select>
                </Field>
                <Field label="URL"><input required value={draft.url} onChange={(e) => set({ url: e.target.value })} placeholder="https://hooks.example.com/…" /></Field>
              </div>
              <fieldset>
                <legend>Headers</legend>
                {draft.headers.map(([k, v], i) => (
                  <div key={i} className="header-row">
                    <input aria-label="Header name" value={k} placeholder="Authorization"
                      onChange={(e) => set({ headers: draft.headers.map((h, j) => (j === i ? [e.target.value, h[1]] : h)) })} />
                    <input aria-label="Header value" value={v} placeholder="Bearer …"
                      onFocus={() => { if (v === MASK) set({ headers: draft.headers.map((h, j) => (j === i ? [h[0], ""] : h)) }); }}
                      onChange={(e) => set({ headers: draft.headers.map((h, j) => (j === i ? [h[0], e.target.value] : h)) })} />
                    <button type="button" className="btn-ghost btn-sm" aria-label="Remove header" onClick={() => set({ headers: draft.headers.filter((_, j) => j !== i) })}><Icon name="trash" size={14} /></button>
                  </div>
                ))}
                <button type="button" className="btn-ghost btn-sm add-header" onClick={() => set({ headers: [...draft.headers, ["", ""]] })}><Icon name="plus" size={14} /> Add header</button>
                <span className="field-hint">Values are never shown again after saving — leave {MASK} to keep a stored value.</span>
              </fieldset>
              <Field label="Body template" hint={`Empty = Shorthop's own JSON. Otherwise JSON with placeholders inside quotes: ${VARIABLES.map((v) => `{{${v}}}`).join(" ")}. Any alert label: {{labels.name}}, annotation: {{annotations.name}}; a label a label rule sets also works bare, e.g. {{level}}.`}>
                <textarea className="mono" rows={5} value={draft.template} onChange={(e) => set({ template: e.target.value })} placeholder='{"text": "{{text}}"}' />
              </Field>
              <fieldset>
                <legend>Send on</legend>
                <div className="check-row">
                  {EVENTS.map((ev) => (
                    <label key={ev} className="check">
                      <input type="checkbox" checked={draft.events.length === 0 || draft.events.includes(ev)}
                        onChange={(e) => {
                          const cur = draft.events.length ? draft.events : EVENTS;
                          const next = e.target.checked ? [...cur, ev] : cur.filter((x) => x !== ev);
                          set({ events: next.length === EVENTS.length ? [] : EVENTS.filter((x) => next.includes(x)) });
                        }} /> {ev}
                    </label>
                  ))}
                </div>
              </fieldset>
            </>
          )}
          {draft.id && <label className="check"><input type="checkbox" checked={draft.enabled} onChange={(e) => set({ enabled: e.target.checked })} /> Enabled</label>}
          <ErrorNotice>{formError}</ErrorNotice>
          <div className="detail-actions">
            <button className="btn-primary" disabled={!draft.name.trim() || !(draft.kind === "email" ? draft.to.trim() : draft.kind === "slack" ? draft.slackId
              : draft.kind === "jira" ? draft.jira.site.trim() && draft.jira.token && draft.jira.project.trim() : draft.url.trim())}>{draft.id ? "Save" : "Create"}</button>
            <button type="button" className="btn-ghost" onClick={() => setDraft(null)}>Cancel</button>
          </div>
        </form>
      )}
      <ErrorNotice>{error}</ErrorNotice>
      {!draft && <ErrorNotice>{formError}</ErrorNotice>}
      {data && data.length === 0 && <Empty icon="mail" title="No channels yet" />}
      <ul className="integration-list">
        {(data ?? []).map((c) => (
          <li key={c.id} className="card integration-row">
            <div className="integration-main">
              <div className="alert-title">
                <span className="alert-name">{c.name}</span>
                <span className="badge neutral">{c.kind}</span>
                {c.config.body_template && <span className="badge neutral">custom body</span>}
                {c.config.events && <span className="badge neutral">{c.config.events.join(", ")} only</span>}
                {!c.enabled && <span className="badge neutral">disabled</span>}
              </div>
              <p className="small cell-wrap" style={{ margin: "0.3rem 0 0" }}>
                {c.kind === "email" ? c.config.to?.join(", ")
                  : c.kind === "slack" ? <>#{c.config.channel_name || c.config.channel_id} <span className="muted">· posted by the Slack app, kept up to date, with buttons</span></>
                  : c.kind === "jira" ? <>{c.config.project_key} · {c.config.issue_type} on <code>{c.config.site}</code> <span className="muted">· one issue per incident, commented, closed on resolve</span></>
                    : <code>{c.config.method ?? "POST"} {c.config.url}</code>}
              </p>
              {results[c.id] && <p className={`small ${results[c.id].startsWith("✓") ? "ok-ink" : "error"}`}>{results[c.id]}</p>}
            </div>
            {manage && (
              <div className="row-actions">
                <button className="btn-ghost btn-sm" onClick={() => test(c)}><Icon name="megaphone" size={14} /> Send test</button>
                <button className="btn-ghost btn-sm" onClick={() => { setDraft(fromChannel(c)); setCreated(null); }}>Edit</button>
                {c.kind === "webhook" && <button className="btn-ghost btn-sm" onClick={() => rotate(c)}><Icon name="key" size={14} /> New secret</button>}
                <button className="btn-ghost btn-sm btn-danger" aria-label={`Delete ${c.name}`} onClick={() => remove(c)}><Icon name="trash" size={14} /></button>
              </div>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}


/** The channels the Slack app can see. Private ones need the app invited first; public ones
 *  work without (chat:write.public). */
function SlackChannelPicker({ id, onPick }: { id: string; onPick: (id: string, name: string) => void }) {
  const { data, error } = usePoll(() => slackApi.channels(), []);
  const list = data?.channels ?? [];
  const chosen = list.find((c) => c.id === id);
  return (
    <>
      <Field label="Slack channel" hint={chosen?.private && !chosen.member
        ? "A private channel: invite the app first (/invite @Shorthop), or its messages are refused."
        : "One message per incident, updated as it changes; acks, escalations and notes go in its thread."}>
        <select required value={id} onChange={(e) => { const c = list.find((x) => x.id === e.target.value); if (c) onPick(c.id, c.name); }}>
          <option value="">{data ? "— choose —" : "Loading channels…"}</option>
          {list.map((c) => <option key={c.id} value={c.id}>#{c.name}{c.private ? (c.member ? " (private)" : " (private — invite the app)") : ""}</option>)}
        </select>
      </Field>
      <ErrorNotice>{error ?? (data?.error ? `Slack: ${data.error}` : null)}</ErrorNotice>
    </>
  );
}


/** A Jira project. Cloud: the account's email + an API token (id.atlassian.com → Security →
 *  API tokens). Data Center: leave the email empty and paste a personal access token. */
function JiraFields({ value: j, editing, onChange }: {
  value: Draft["jira"]; editing: boolean; onChange: (j: Draft["jira"]) => void;
}) {
  const set = (patch: Partial<Draft["jira"]>) => onChange({ ...j, ...patch });
  return (
    <>
      <Field label="Jira site" hint="Your Jira's address — not a board or issue link.">
        <input required value={j.site} onChange={(e) => set({ site: e.target.value })} placeholder="https://acme.atlassian.net" />
      </Field>
      <div className="jira-auth">
        <Field label="Account email" hint="Jira Cloud. Leave empty for a Data Center personal access token.">
          <input type="email" value={j.email} onChange={(e) => set({ email: e.target.value })} placeholder="bot@acme.io" />
        </Field>
        <Field label="API token" hint={editing ? `Never shown again — leave ${MASK} to keep the stored one.` : "Never shown again after saving."}>
          <input required type="password" autoComplete="off" value={j.token}
            onFocus={() => { if (j.token === MASK) set({ token: "" }); }}
            onChange={(e) => set({ token: e.target.value })} />
        </Field>
      </div>
      <div className="jira-auth">
        <Field label="Project key"><input required value={j.project} onChange={(e) => set({ project: e.target.value.toUpperCase() })} placeholder="OPS" /></Field>
        <Field label="Issue type"><input value={j.issueType} onChange={(e) => set({ issueType: e.target.value })} placeholder="Task" /></Field>
      </div>
      <Field label="Labels" hint="Space-separated; “shorthop” is always added.">
        <input value={j.labels} onChange={(e) => set({ labels: e.target.value })} placeholder="prod on-call" />
      </Field>
      <Field label="Close with transition" hint="Empty = the first transition to a “done” status. Name one if your workflow has several.">
        <input value={j.done} onChange={(e) => set({ done: e.target.value })} placeholder="Done" />
      </Field>
      <p className="muted small">Put the channel in the first escalation step. Closing the issue in Jira does not resolve the alerts — Shorthop keeps paging while they fire. Use <strong>Send test</strong> to check the token and project without opening an issue.</p>
    </>
  );
}
