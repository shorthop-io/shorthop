import { useState } from "react";
import { api, type Integration, type ShadowStatus } from "../api";
import { useMe, useNow, usePoll } from "../app";
import { Icon } from "../components/Icon";
import { Empty, ErrorNotice, Field, ShowOnce } from "../components/ui";
import { parseTs, relative } from "../time";
import { WatchBox, WatchSection } from "./WatchChecks";

const KINDS = [
  { value: "prometheus", label: "Prometheus (direct)", hint: "Prometheus sends here instead of, or as well as, an Alertmanager." },
  { value: "alertmanager", label: "Alertmanager webhook", hint: "For an Alertmanager you keep — its webhook_configs payload." },
  { value: "webhook", label: "Generic webhook", hint: "Anything that can POST JSON: Grafana, CI, scripts." },
  { value: "internal", label: "Built-in", hint: "Shorthop's own alerts about its inputs." },
];

/** A Prometheus that delivers nothing for this long is worth a look: it re-sends firing
 *  alerts every minute and the Watchdog alert is always firing. */
const STALE_MIN = 10;

function health(i: Integration, now: number): { tone: string; text: string } {
  if (i.kind === "internal") return { tone: "neutral", text: "built-in" };
  if (!i.enabled) return { tone: "neutral", text: "disabled" };
  if (!i.last_received_at) return { tone: "warn", text: "nothing received yet" };
  const ageMin = (now - parseTs(i.last_received_at).getTime()) / 60000;
  if (i.kind === "prometheus") {
    if (!i.last_watchdog_at) return { tone: "warn", text: "no Watchdog seen" };
    const wd = (now - parseTs(i.last_watchdog_at).getTime()) / 60000;
    if (wd > STALE_MIN) return { tone: "danger", text: `Watchdog silent for ${Math.round(wd)} min` };
    return { tone: "ok", text: "healthy" };
  }
  return ageMin > 24 * 60 ? { tone: "neutral", text: "quiet" } : { tone: "ok", text: "receiving" };
}

function promSnippet(origin: string, token: string) {
  const host = new URL(origin);
  return `alerting:
  alertmanagers:
    - scheme: ${host.protocol.replace(":", "")}
      path_prefix: /ingest/prometheus
      authorization:
        credentials: ${token}
      static_configs:
        - targets: [${host.host}]`;
}

export function Integrations() {
  const { can } = useMe();
  const now = useNow();
  const { data, error, refresh } = usePoll(() => api.listIntegrations(), [], 30000);
  // Shorthop Watch lives on the push relay; an unconfigured or unreachable relay is a reason
  // shown in the Watch parts of the page, never an error for the whole page.
  const watch = usePoll(() => api.listWatchChecks(), [], 60000);
  const refreshAll = () => { refresh(); watch.refresh(); };
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [kind, setKind] = useState("prometheus");
  const [labels, setLabels] = useState("");
  const [created, setCreated] = useState<Integration | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const parseLabels = (s: string): Record<string, string> | null => {
    const out: Record<string, string> = {};
    for (const part of s.split(",").map((p) => p.trim()).filter(Boolean)) {
      const m = /^([a-zA-Z_][a-zA-Z0-9_]*)=(.*)$/.exec(part);
      if (!m) return null;
      out[m[1]] = m[2];
    }
    return out;
  };

  const create = async () => {
    const extra = parseLabels(labels);
    if (!extra) { setFormError("Extra labels must look like cluster=big, env=lab"); return; }
    setFormError(null);
    try {
      const i = await api.createIntegration({ name: name.trim(), kind, extra_labels: extra });
      setCreated(i);
      setCreating(false);
      setName(""); setLabels("");
      refresh();
    } catch (e) {
      setFormError((e as Error).message);
    }
  };

  const rotate = async (i: Integration) => {
    if (!window.confirm(`Issue a new token for ${i.name}? The current one stops working immediately — the sender must be updated.`)) return;
    try {
      setCreated(await api.rotateIntegration(i.id));
      refresh();
    } catch (e) {
      setActionError((e as Error).message);
    }
  };

  const remove = async (i: Integration) => {
    const msg = i.last_received_at
      ? `Delete ${i.name}? Its token stops working now. The alerts it delivered stay in the history (the integration is archived, not erased).`
      : `Delete ${i.name}? Its token stops working now.`;
    if (!window.confirm(msg)) return;
    try {
      await api.deleteIntegration(i.id);
      refresh();
    } catch (e) {
      setActionError((e as Error).message);
    }
  };

  const toggle = async (i: Integration) => {
    try {
      await api.updateIntegration(i.id, { enabled: !i.enabled });
      refresh();
    } catch (e) {
      setActionError((e as Error).message);
    }
  };

  const [editUrl, setEditUrl] = useState<string | null>(null);
  const [urlDraft, setUrlDraft] = useState("");
  const [checking, setChecking] = useState<string | null>(null);

  const saveUrl = async (i: Integration) => {
    setActionError(null);
    try {
      await api.updateIntegration(i.id, { prometheus_url: urlDraft.trim() || null });
      setEditUrl(null);
      if (urlDraft.trim()) await api.checkIntegration(i.id);
      refresh();
    } catch (e) {
      setActionError((e as Error).message);
    }
  };

  const checkNow = async (i: Integration) => {
    setChecking(i.id);
    setActionError(null);
    try {
      await api.checkIntegration(i.id);
      refresh();
    } catch (e) {
      setActionError((e as Error).message);
    } finally {
      setChecking(null);
    }
  };

  const manage = can("config:write");

  return (
    <div className="page">
      <div className="section-head">
        <h2>Integrations</h2>
        {manage && !creating && (
          <button className="btn-primary" onClick={() => { setCreating(true); setCreated(null); }}>
            <Icon name="plus" size={15} /> New integration
          </button>
        )}
      </div>
      <p className="glance">Each sender has its own door and its own token. A token only works on the door of its kind.</p>

      {created?.token && (
        <ShowOnce label={`Token for ${created.name}`} value={created.token}>
          {created.kind === "prometheus" ? (
            <>
              <p className="muted small">Add to prometheus.yml (beside any Alertmanager you keep):</p>
              <pre className="snippet">{promSnippet(window.location.origin, created.token)}</pre>
            </>
          ) : (
            <p className="muted small">Send as <code>Authorization: Bearer …</code> to <code>{window.location.origin}{created.ingest_path}</code></p>
          )}
        </ShowOnce>
      )}

      {creating && (
        <form className="card pad form-stack narrow" onSubmit={(e) => { e.preventDefault(); create(); }}>
          <Field label="Name"><input required value={name} maxLength={200} onChange={(e) => setName(e.target.value)} placeholder="prometheus-big" /></Field>
          <Field label="Kind" hint={KINDS.find((k) => k.value === kind)?.hint}>
            <select value={kind} onChange={(e) => setKind(e.target.value)}>
              {KINDS.map((k) => <option key={k.value} value={k.value}>{k.label}</option>)}
            </select>
          </Field>
          <Field label="Extra labels (optional)" hint="Added to every alert from this sender, e.g. when two Prometheus servers set no external_labels.">
            <input value={labels} onChange={(e) => setLabels(e.target.value)} placeholder="cluster=big, env=lab" />
          </Field>
          <ErrorNotice>{formError}</ErrorNotice>
          <div className="detail-actions">
            <button className="btn-primary" disabled={!name.trim()}>Create</button>
            <button type="button" className="btn-ghost" onClick={() => setCreating(false)}>Cancel</button>
          </div>
        </form>
      )}

      <ErrorNotice>{error}</ErrorNotice>
      <ErrorNotice>{actionError}</ErrorNotice>
      {data && data.length === 0 && <Empty icon="plug" title="No integrations yet">Create one, then point a sender at it.</Empty>}

      <ul className="integration-list">
        {(data ?? []).map((i) => {
          const h = health(i, now);
          return (
            <li key={i.id} className="card integration-row">
              <div className="integration-main">
                <div className="alert-title">
                  <span className="alert-name">{i.name}</span>
                  <span className="badge neutral">{KINDS.find((k) => k.value === i.kind)?.label ?? i.kind}</span>
                  {i.kind !== "internal" && <span className={`badge ${h.tone}`}>{h.text}</span>}
                </div>
                {i.kind === "internal" ? (
                  <p className="muted small">Shorthop's own alerts about its inputs: a Prometheus whose Watchdog went silent, or one that disagrees with what is open here. No door, no usable token.</p>
                ) : (
                <dl className="kv kv-inline">
                  <div><dt>Last delivery</dt><dd>{relative(i.last_received_at, now)}</dd></div>
                  {i.kind === "prometheus" && <div><dt>Watchdog</dt><dd>{relative(i.last_watchdog_at, now)}</dd></div>}
                  <div><dt>Door</dt><dd><code>{i.ingest_path}</code></dd></div>
                  <div><dt>Token</dt><dd><code>{i.prefix}…</code></dd></div>
                  {Object.keys(i.extra_labels).length > 0 && (
                    <div><dt>Adds</dt><dd>{Object.entries(i.extra_labels).map(([k, v]) => `${k}=${v}`).join(", ")}</dd></div>
                  )}
                </dl>
                )}
                {i.kind === "prometheus" && (
                  <div className="shadow-box">
                    {editUrl === i.id ? (
                      <form className="form-row" onSubmit={(e) => { e.preventDefault(); saveUrl(i); }}>
                        <input aria-label="Prometheus URL" value={urlDraft} autoFocus
                          placeholder="http://monitoring-prometheus.monitoring.svc.cluster.local:9090"
                          onChange={(e) => setUrlDraft(e.target.value)} />
                        <button className="btn-primary btn-sm">Save</button>
                        <button type="button" className="btn-ghost btn-sm" onClick={() => setEditUrl(null)}>Cancel</button>
                      </form>
                    ) : i.prometheus_url ? (
                      <Shadow status={i.shadow_status} now={now} />
                    ) : (
                      <p className="muted small">Not compared with a Prometheus. Give its URL and Shorthop checks every minute that what Prometheus says is firing is what is open here.</p>
                    )}
                    {editUrl !== i.id && (
                      <div className="row-actions">
                        {i.prometheus_url && (
                          <button className="btn-ghost btn-sm" disabled={checking === i.id} onClick={() => checkNow(i)}>
                            <Icon name="refresh" size={14} /> {checking === i.id ? "Checking…" : "Check now"}
                          </button>
                        )}
                        {manage && (
                          <button className="btn-ghost btn-sm" onClick={() => { setEditUrl(i.id); setUrlDraft(i.prometheus_url ?? ""); }}>
                            <Icon name="edit" size={14} /> {i.prometheus_url ? "Change Prometheus" : "Compare with Prometheus"}
                          </button>
                        )}
                        {i.prometheus_url && <span className="muted small"><code>{i.prometheus_url}</code></span>}
                      </div>
                    )}
                  </div>
                )}
                {i.kind === "prometheus" && (
                  <WatchBox integ={i} watch={watch.data} watchError={watch.error} manage={manage} now={now}
                    onChange={refreshAll} onError={setActionError} />
                )}
              </div>
              {manage && i.kind !== "internal" && (
                <div className="row-actions">
                  <button className="btn-ghost btn-sm" onClick={() => rotate(i)}><Icon name="refresh" size={14} /> New token</button>
                  <button className="btn-ghost btn-sm" onClick={() => toggle(i)}>{i.enabled ? "Disable" : "Enable"}</button>
                  <button className="btn-ghost btn-sm btn-danger" aria-label={`Delete ${i.name}`} onClick={() => remove(i)}><Icon name="trash" size={14} /></button>
                </div>
              )}
            </li>
          );
        })}
      </ul>

      <WatchSection watch={watch.data} watchError={watch.error} manage={manage} now={now} onChange={refreshAll} />
    </div>
  );
}

function Shadow({ status, now }: { status: ShadowStatus | null; now: number }) {
  if (!status) return <p className="muted small">Not checked yet — the first comparison runs within a minute.</p>;
  const checked = <span className="muted small"> · checked {relative(status.checked_at, now)}</span>;
  if (status.error) {
    return <p className="notice notice--warn notice--icon"><Icon name="alert" size={15} />
      <span>Could not compare: {status.error}{checked}</span></p>;
  }
  const missing = status.missing.length;
  const extra = status.extra.length;
  if (!missing && !extra) {
    return (
      <p className="shadow-ok"><Icon name="check" size={15} />
        <span>Matches Prometheus — {status.prometheus_firing} firing there, {status.shorthop_open} open here{checked}
          {status.unconfirmed ? <span className="muted small"> · {status.unconfirmed} difference(s) being re-checked</span> : null}</span>
      </p>
    );
  }
  return (
    <div className="notice notice--warn shadow-diff">
      <p className="notice--icon"><Icon name="alert" size={15} />
        <span><strong>{missing ? `${missing} firing in Prometheus but not here` : ""}{missing && extra ? "; " : ""}{extra ? `${extra} open here but not firing in Prometheus` : ""}</strong>{checked}</span></p>
      <ul>
        {status.missing.map((d, k) => <li key={`m${k}`}><span className="badge danger">missing</span> {d.alertname} <code>{fmtLabels(d.labels)}</code></li>)}
        {status.extra.map((d, k) => <li key={`e${k}`}><span className="badge neutral">extra</span> {d.alertname} <code>{fmtLabels(d.labels)}</code></li>)}
      </ul>
    </div>
  );
}

function fmtLabels(labels: Record<string, string>) {
  return Object.entries(labels).filter(([k]) => k !== "alertname").slice(0, 4).map(([k, v]) => `${k}=${v}`).join(" ");
}
