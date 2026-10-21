import { useState } from "react";
import { paging, type InhibitionIn } from "../api";
import { useMe, usePoll } from "../app";
import { Icon } from "../components/Icon";
import { blankMatcher, MatcherEditor, matcherText, usable } from "../components/MatcherEditor";
import { Empty, ErrorNotice, Field } from "../components/ui";

const blank = (): InhibitionIn => ({
  name: "", source_matchers: [blankMatcher()], target_matchers: [blankMatcher()], equal: ["cluster"], enabled: true,
});

export function Inhibitions() {
  const { can } = useMe();
  const { data, error, refresh } = usePoll(() => paging.listInhibitions(), []);
  const [editing, setEditing] = useState<string | "new" | null>(null);
  const [draft, setDraft] = useState<InhibitionIn>(blank());
  const [equal, setEqual] = useState("cluster");
  const [formError, setFormError] = useState<string | null>(null);
  const manage = can("config:write");

  const save = async () => {
    setFormError(null);
    const body = { ...draft, source_matchers: usable(draft.source_matchers), target_matchers: usable(draft.target_matchers),
      equal: equal.split(",").map((x) => x.trim()).filter(Boolean) };
    try {
      if (editing === "new") await paging.createInhibition(body);
      else if (editing) await paging.updateInhibition(editing, body);
      setEditing(null);
      refresh();
    } catch (e) {
      setFormError((e as Error).message);
    }
  };

  return (
    <div className="page">
      <div className="section-head">
        <h2>Inhibitions</h2>
        {manage && editing === null && <button className="btn-primary" onClick={() => { setDraft(blank()); setEqual("cluster"); setEditing("new"); setFormError(null); }}><Icon name="plus" size={15} /> New inhibition</button>}
      </div>
      <p className="glance">While a <em>source</em> alert fires, matching <em>target</em> alerts that share the listed labels are not paged — still recorded and shown. The classic: "cluster down" muting every per-pod alert on that cluster.</p>
      <ErrorNotice>{error}</ErrorNotice>
      {editing !== null && (
        <form className="card pad form-stack" onSubmit={(e) => { e.preventDefault(); save(); }}>
          <Field label="Name"><input required maxLength={200} value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} placeholder="node down mutes its pods" /></Field>
          <div className="detail-grid">
            <fieldset><legend>While this fires (source)</legend>
              <MatcherEditor value={draft.source_matchers} onChange={(m) => setDraft({ ...draft, source_matchers: m })} /></fieldset>
            <fieldset><legend>Don't page these (target)</legend>
              <MatcherEditor value={draft.target_matchers} onChange={(m) => setDraft({ ...draft, target_matchers: m })} /></fieldset>
          </div>
          <Field label="…when these labels are equal" hint="Comma-separated. Empty = any source mutes every target, anywhere.">
            <input value={equal} onChange={(e) => setEqual(e.target.value)} placeholder="cluster, namespace" />
          </Field>
          <label className="check"><input type="checkbox" checked={draft.enabled} onChange={(e) => setDraft({ ...draft, enabled: e.target.checked })} /> Enabled</label>
          <ErrorNotice>{formError}</ErrorNotice>
          <div className="detail-actions">
            <button className="btn-primary">Save</button>
            <button type="button" className="btn-ghost" onClick={() => setEditing(null)}>Cancel</button>
          </div>
        </form>
      )}
      {data && data.length === 0 && <Empty icon="ban" title="No inhibitions" />}
      <ul className="integration-list">
        {(data ?? []).map((r) => (
          <li key={r.id} className={`card integration-row${r.enabled ? "" : " row-muted"}`}>
            <div className="integration-main">
              <div className="alert-title"><span className="alert-name">{r.name}</span>{!r.enabled && <span className="badge neutral">disabled</span>}</div>
              <p className="small inh-line">while {r.source_matchers.map(matcherText).map((t) => <code key={t} className="matcher">{t}</code>)}
                {" "}fires, don't page {r.target_matchers.map(matcherText).map((t) => <code key={t} className="matcher">{t}</code>)}
                {r.equal.length > 0 && <> with the same {r.equal.join(", ")}</>}</p>
            </div>
            {manage && (
              <div className="row-actions">
                <button className="btn-ghost btn-sm" onClick={() => { const { id: _i, created_at: _c, ...rest } = r; setDraft(rest); setEqual(r.equal.join(", ")); setEditing(r.id); setFormError(null); }}><Icon name="edit" size={14} /> Edit</button>
                <button className="btn-ghost btn-sm btn-danger" aria-label={`Delete ${r.name}`} onClick={async () => {
                  if (!window.confirm(`Delete "${r.name}"?`)) return;
                  try { await paging.deleteInhibition(r.id); refresh(); } catch (e) { setFormError((e as Error).message); }
                }}><Icon name="trash" size={14} /></button>
              </div>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
