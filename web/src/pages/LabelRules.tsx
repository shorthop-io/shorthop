import { useState } from "react";
import { api, paging, type LabelRule, type LabelRuleIn, type LabelRuleTest, type Matcher } from "../api";
import { useMe, usePoll } from "../app";
import { Icon } from "../components/Icon";
import { MatcherEditor, matcherText, usable } from "../components/MatcherEditor";
import { Empty, ErrorNotice, Field } from "../components/ui";

const blank = (): LabelRuleIn => ({
  name: "", matchers: [], text_pattern: null, text_field: null, set_labels: {},
  integration_ids: null, stop: false, enabled: true,
});

/** "key=value" lines <-> a label map. */
function parsePairs(text: string): [Record<string, string>, string | null] {
  const out: Record<string, string> = {};
  for (const line of text.split("\n").map((l) => l.trim()).filter(Boolean)) {
    const i = line.indexOf("=");
    if (i < 1) return [out, `"${line}" is not name=value`];
    out[line.slice(0, i).trim()] = line.slice(i + 1).trim().replace(/^"|"$/g, "");
  }
  return [out, null];
}
const pairsText = (m: Record<string, string>) => Object.entries(m).map(([k, v]) => `${k}=${v}`).join("\n");

export function LabelRules() {
  const { can } = useMe();
  const rules = usePoll(() => paging.listLabelRules(), []);
  const integrations = usePoll(() => api.listIntegrations(), []);
  const [editing, setEditing] = useState<string | "new" | null>(null);
  const [draft, setDraft] = useState<LabelRuleIn>(blank());
  const [setText, setSetText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const manage = can("config:write");

  const startEdit = (r: LabelRule | null) => {
    setError(null);
    if (r) {
      const { id: _id, position: _p, created_at: _c, ...rest } = r;
      setDraft(rest);
      setSetText(pairsText(r.set_labels));
      setEditing(r.id);
    } else {
      setDraft(blank());
      setSetText("level=P2");
      setEditing("new");
    }
  };

  const save = async () => {
    setError(null);
    const [set_labels, bad] = parsePairs(setText);
    if (bad) { setError(`Set labels: ${bad}`); return; }
    const body = { ...draft, set_labels, matchers: usable(draft.matchers),
      text_pattern: draft.text_pattern?.trim() || null, text_field: draft.text_field?.trim() || null };
    try {
      if (editing === "new") await paging.createLabelRule(body);
      else if (editing) await paging.updateLabelRule(editing, body);
      setEditing(null);
      rules.refresh();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const move = async (idx: number, dir: -1 | 1) => {
    const list = [...(rules.data ?? [])];
    const j = idx + dir;
    if (j < 0 || j >= list.length) return;
    [list[idx], list[j]] = [list[j], list[idx]];
    try {
      await paging.orderLabelRules(list.map((r) => r.id));
      rules.refresh();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const remove = async (r: LabelRule) => {
    if (!window.confirm(`Delete label rule "${r.name}"? Open alerts lose its labels the next time they are sent.`)) return;
    try {
      await paging.deleteLabelRule(r.id);
      rules.refresh();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const list = rules.data ?? [];

  return (
    <div className="page">
      <div className="section-head">
        <h2>Label rules</h2>
        {manage && editing === null && <button className="btn-primary" onClick={() => startEdit(null)}><Icon name="plus" size={15} /> New rule</button>}
      </div>
      <p className="glance">Add or change labels on incoming alerts — from their labels and from their text — before routing and silences see them. Every matching rule applies, top to bottom, so a later rule overrides an earlier one: put the general rule first and the exception below it.</p>
      <ErrorNotice>{rules.error || error}</ErrorNotice>

      {editing !== null && (
        <form className="card pad form-stack" onSubmit={(e) => { e.preventDefault(); save(); }}>
          <h3 className="sub">{editing === "new" ? "New label rule" : `Edit ${draft.name}`}</h3>
          <Field label="Name"><input required maxLength={200} value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} placeholder="silent socket is P1" /></Field>
          <fieldset>
            <legend>When the alert's labels match</legend>
            <MatcherEditor value={draft.matchers} allowEmpty onChange={(m: Matcher[]) => setDraft({ ...draft, matchers: m })}
              emptyHint="No label matchers: every alert (narrow it with the text below)." />
          </fieldset>
          <div className="form-grid">
            <Field label="…and its text contains" hint="A regular expression, case-insensitive, searched anywhere in the text. Empty = no text condition.">
              <input className="mono" value={draft.text_pattern ?? ""} onChange={(e) => setDraft({ ...draft, text_pattern: e.target.value })} placeholder="silent socket|connection reset" />
            </Field>
            <Field label="Search in" hint="Which annotation. Empty = all of them (summary, description, message…).">
              <input value={draft.text_field ?? ""} onChange={(e) => setDraft({ ...draft, text_field: e.target.value })} placeholder="all annotations" />
            </Field>
          </div>
          <Field label="Then set labels" hint="One per line, name=value. Overwrites a label of the same name; alertname cannot be changed.">
            <textarea className="mono" rows={3} value={setText} onChange={(e) => setSetText(e.target.value)} placeholder="level=P1" />
          </Field>
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
          <label className="check"><input type="checkbox" checked={draft.stop} onChange={(e) => setDraft({ ...draft, stop: e.target.checked })} /> Stop here — rules below are not applied to an alert this rule matched</label>
          <label className="check"><input type="checkbox" checked={draft.enabled} onChange={(e) => setDraft({ ...draft, enabled: e.target.checked })} /> Enabled</label>
          <div className="detail-actions">
            <button className="btn-primary">Save rule</button>
            <button type="button" className="btn-ghost" onClick={() => setEditing(null)}>Cancel</button>
          </div>
        </form>
      )}

      {rules.data && list.length === 0 && (
        <Empty icon="tag" title="No label rules yet">Alerts keep the labels their sender gave them. A rule can, for example, set <code>level=P2</code> on every warning.</Empty>
      )}
      <ol className="route-list">
        {list.map((r, idx) => (
          <li key={r.id} className={`card route-row${r.enabled ? "" : " row-muted"}`}>
            <span className="route-pos">{idx + 1}</span>
            <div className="route-main">
              <div className="alert-title">
                <span className="alert-name">{r.name}</span>
                {!r.enabled && <span className="badge neutral">disabled</span>}
                {r.stop && <span className="badge info">stop</span>}
              </div>
              <div className="silence-matchers">
                {r.matchers.map((m, k) => <code key={k} className="matcher">{matcherText(m)}</code>)}
                {r.text_pattern && <code className="matcher">{r.text_field ?? "text"} ~ /{r.text_pattern}/i</code>}
                {!r.matchers.length && !r.text_pattern && <span className="muted small">every alert</span>}
              </div>
              <div className="alert-meta">
                <span className="meta-item"><span className="muted">sets</span> {Object.entries(r.set_labels).map(([k, v]) => <code key={k} className="set-label">{k}={v}</code>)}</span>
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
  const [labels, setLabels] = useState("alertname=AisFeedStale\nseverity=warning");
  const [text, setText] = useState("summary=AIS feed: silent socket for 15 min");
  const [result, setResult] = useState<LabelRuleTest | null>(null);
  const [error, setError] = useState<string | null>(null);

  const run = async () => {
    setError(null);
    const [l, bad] = parsePairs(labels);
    if (bad) { setError(`Labels: ${bad}`); return; }
    const [a, bad2] = parsePairs(text);
    if (bad2) { setError(`Annotations: ${bad2}`); return; }
    try {
      setResult(await paging.testLabelRules(l, a));
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const route = result?.routing.routes.find((r) => r.chosen);
  return (
    <section className="card pad">
      <h3 className="sub">Test the rules</h3>
      <div className="detail-grid">
        <form className="form-stack" onSubmit={(e) => { e.preventDefault(); run(); }}>
          <label>Labels, one per line
            <textarea rows={4} className="mono" value={labels} onChange={(e) => setLabels(e.target.value)} />
          </label>
          <label>Annotations (the text), one per line
            <textarea rows={3} className="mono" value={text} onChange={(e) => setText(e.target.value)} />
          </label>
          <div><button className="btn-primary"><Icon name="search" size={15} /> Apply the rules</button></div>
          <ErrorNotice>{error}</ErrorNotice>
        </form>
        <div>
          {!result && <p className="muted small">Shows what every rule does to an alert like this, the labels it ends up with, and which route then takes it.</p>}
          {result && (
            <>
              <p className="history-glance">{Object.keys(result.changed).length
                ? <>Sets {Object.entries(result.changed).map(([k, v]) => <code key={k} className="set-label">{k}={v}</code>)}</>
                : <strong>No rule changes this alert.</strong>}</p>
              <ol className="verdicts">
                {result.rules.map((v) => (
                  <li key={v.id} className={v.applied ? "chosen" : ""}>
                    <Icon name={v.applied ? "check" : "x"} size={14} />
                    <span><strong>{v.name}</strong> — {v.applied ? `sets ${pairsText(v.set).replace(/\n/g, ", ")}` : v.why}</span>
                  </li>
                ))}
              </ol>
              <p className="small">Then routed to: {route ? <strong>{route.name}</strong> : <strong>no route — recorded, nobody paged</strong>}</p>
            </>
          )}
        </div>
      </div>
    </section>
  );
}
