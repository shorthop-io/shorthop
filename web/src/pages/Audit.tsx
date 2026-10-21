import { api } from "../api";
import { useNow, usePoll } from "../app";
import { ErrorNotice } from "../components/ui";
import { relative, wall } from "../time";

export function Audit() {
  const now = useNow(60000);
  const { data, error } = usePoll(() => api.audit(), []);
  return (
    <div className="page">
      <div className="section-head"><h2>Audit log</h2></div>
      <p className="glance">Who did what — sign-ins, acknowledgements, silences, tokens, and every MCP tool call. The latest 500.</p>
      <ErrorNotice>{error}</ErrorNotice>
      <div className="table-wrap">
        <table>
          <thead><tr><th>When</th><th>Who</th><th>Action</th><th>Target</th><th>Detail</th></tr></thead>
          <tbody>
            {(data ?? []).map((e) => (
              <tr key={e.id}>
                <td title={wall(e.at)}>{relative(e.at, now)}</td>
                <td>{e.actor}</td>
                <td><code>{e.action}</code>{e.status !== "ok" && <span className="badge danger">{e.status}</span>}</td>
                <td className="cell-wrap">{e.target}</td>
                <td className="cell-wrap muted small">{e.detail}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
