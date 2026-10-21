import { useState } from "react";
import { api, SCOPES, type Token } from "../api";
import { useMe, useNow, usePoll } from "../app";
import { Icon } from "../components/Icon";
import { Empty, ErrorNotice, Field, ShowOnce } from "../components/ui";
import { relative } from "../time";

const ROLE_SCOPES: Record<string, string[]> = {
  viewer: ["alerts:read", "oncall:read"],
  responder: ["alerts:read", "alerts:write", "oncall:read", "oncall:write"],
  admin: [...SCOPES],
};

/** API tokens + the API reference and MCP setup — the "API & MCP" tab of My account. */
export function TokensPanel() {
  const { me } = useMe();
  const now = useNow(60000);
  const { data, error, refresh } = usePoll(() => api.listTokens(), []);
  const allowed = ROLE_SCOPES[me.role] ?? [];
  const [name, setName] = useState("");
  const [scopes, setScopes] = useState<string[]>(["alerts:read"]);
  const [days, setDays] = useState("90");
  const [created, setCreated] = useState<Token | null>(null);
  const [formError, setFormError] = useState<string | null>(null);

  const forMcp = scopes.every((s) => !["config:write", "admin"].includes(s));
  const origin = window.location.origin;

  const create = async () => {
    setFormError(null);
    try {
      const t = await api.createToken({ name: name.trim(), scopes, expires_in_days: days ? Number(days) : undefined });
      setCreated(t);
      setName("");
      refresh();
    } catch (e) {
      setFormError((e as Error).message);
    }
  };

  const revoke = async (t: Token) => {
    if (!window.confirm(`Revoke ${t.name}? Anything using it stops working.`)) return;
    try {
      await api.revokeToken(t.id);
      refresh();
    } catch (e) {
      setFormError((e as Error).message);
    }
  };

  return (
    <div className="form-stack">
      <p className="glance">Personal tokens act as you, for scripts and for Claude through MCP. A token never holds more than your role.</p>

      <div className="card pad api-help">
        <div className="api-help-col">
          <h4><Icon name="book" size={15} /> API reference</h4>
          <p className="small">
            <a href="/docs" target="_blank" rel="noreferrer">Swagger UI</a> — every endpoint, the scope it needs and its
            payloads, generated from the code. Press <b>Authorize</b> there and paste a token.
            Machine-readable: <a href="/openapi.json" target="_blank" rel="noreferrer">openapi.json</a>.
          </p>
          <p className="small muted">Send the token as <code>Authorization: Bearer sh_…</code>. ⚠️ "Try it out" calls this
            Shorthop for real — an acknowledge or silence made there is a real one.</p>
        </div>
        <div className="api-help-col">
          <h4><Icon name="plug" size={15} /> MCP (Claude)</h4>
          <p className="small">Claude can read alerts, history and who is on call, and acknowledge, resolve or silence
            (silences capped at 24 h). Endpoint: <code>{origin}/mcp</code></p>
          <p className="small muted">Use a token with only <code>alerts:*</code> / <code>oncall:*</code> scopes — the MCP
            endpoint refuses <code>config:write</code> and <code>admin</code>, because the token sits in a plain
            config file on your laptop.</p>
        </div>
      </div>

      {created?.token && (
        <ShowOnce label={created.name} value={created.token}>
          <p className="muted small">Try it:</p>
          <pre className="snippet">{`curl -H "Authorization: Bearer ${created.token}" \\
  ${origin}/api/v1/me`}</pre>
          {created.scopes.every((s) => !["config:write", "admin"].includes(s)) ? (
            <>
              <p className="muted small">Add it to Claude Code:</p>
              <pre className="snippet">{`claude mcp add --transport http shorthop ${origin}/mcp \\
  --header "Authorization: Bearer ${created.token}"`}</pre>
              <p className="muted small">Or in a project's <code>.mcp.json</code>:</p>
              <pre className="snippet">{JSON.stringify({ mcpServers: { shorthop: { type: "http", url: `${origin}/mcp`,
                headers: { Authorization: `Bearer ${created.token}` } } } }, null, 2)}</pre>
            </>
          ) : (
            <p className="notice notice--warn">This token holds config:write or admin, so it works for the API but the MCP endpoint will refuse it.</p>
          )}
        </ShowOnce>
      )}

      <form className="card pad form-stack narrow" onSubmit={(e) => { e.preventDefault(); create(); }}>
        <Field label="Name"><input required maxLength={200} value={name} onChange={(e) => setName(e.target.value)} placeholder="claude on my laptop" /></Field>
        <fieldset>
          <legend>Scopes</legend>
          <div className="scope-grid">
            {SCOPES.map((s) => (
              <label key={s} className="check" title={allowed.includes(s) ? undefined : "Not part of your role"}>
                <input type="checkbox" disabled={!allowed.includes(s)} checked={scopes.includes(s)}
                  onChange={(e) => setScopes(e.target.checked ? [...scopes, s] : scopes.filter((x) => x !== s))} />
                <code>{s}</code>
              </label>
            ))}
          </div>
          {!forMcp && <p className="notice notice--warn">The MCP endpoint refuses tokens holding config:write or admin.</p>}
        </fieldset>
        <Field label="Expires after (days)" hint="Leave empty for no expiry.">
          <input className="in-num" inputMode="numeric" value={days} onChange={(e) => setDays(e.target.value.replace(/\D/g, ""))} />
        </Field>
        <ErrorNotice>{formError}</ErrorNotice>
        <div><button className="btn-primary" disabled={!name.trim() || !scopes.length}><Icon name="key" size={15} /> Create token</button></div>
      </form>

      <ErrorNotice>{error}</ErrorNotice>
      {data && data.length === 0 && <Empty icon="key" title="No tokens yet" />}
      {data && data.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead><tr><th>Name</th><th>Scopes</th><th>Last used</th><th>Expires</th><th></th></tr></thead>
            <tbody>
              {data.map((t) => (
                <tr key={t.id} className={t.revoked ? "row-muted" : ""}>
                  <td>{t.name}<div className="muted small"><code>{t.prefix}…</code></div></td>
                  <td>{t.scopes.map((s) => <code key={s} className="scope">{s}</code>)}</td>
                  <td>{t.last_used_at ? relative(t.last_used_at, now) : <span className="muted">never</span>}</td>
                  <td>{t.revoked ? <span className="badge neutral">revoked</span> : t.expires_at ? relative(t.expires_at, now) : <span className="muted">never</span>}</td>
                  <td>{!t.revoked && <button className="btn-ghost btn-sm btn-danger" onClick={() => revoke(t)}>Revoke</button>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
