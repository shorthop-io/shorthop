import { useState } from "react";
import { calendarApi, oncallApi, type CalendarFeed } from "../api";
import { useNow, usePoll } from "../app";
import { Icon } from "../components/Icon";
import { ErrorNotice, Field, ShowOnce } from "../components/ui";
import { relative, wall } from "../time";

/** My account → Calendar: subscribe to on-call shifts from Google / Apple / Outlook. */
export function CalendarPanel({ preselect }: { preselect?: string | null }) {
  const now = useNow(60000);
  const feeds = usePoll(() => calendarApi.list(), []);
  const scheds = usePoll(() => oncallApi.list(), []);
  const [what, setWhat] = useState<string>(preselect ? `schedule:${preselect}` : "mine");
  const [created, setCreated] = useState<CalendarFeed | null>(null);
  const [error, setError] = useState<string | null>(null);

  const create = async () => {
    setError(null);
    try {
      const [kind, id] = what.split(":") as ["mine" | "schedule", string | undefined];
      setCreated(await calendarApi.create(kind === "mine" ? { kind } : { kind, schedule_id: id }));
      feeds.refresh();
    } catch (e) {
      setError((e as Error).message);
    }
  };
  const revoke = async (f: CalendarFeed) => {
    if (!window.confirm(`Revoke "${f.name}"? Calendars subscribed to it stop updating.`)) return;
    try { await calendarApi.revoke(f.id); feeds.refresh(); } catch (e) { setError((e as Error).message); }
  };
  const scheduleId = what.startsWith("schedule:") ? what.slice(9) : undefined;
  const active = (feeds.data ?? []).filter((f) => !f.revoked);

  return (
    <div className="form-stack">
      <p className="glance">Subscribe to on-call shifts in your calendar — your own, or everyone on a schedule (gaps
        show as "Nobody on call"). The calendar app re-reads it by itself, so overrides and swaps show up there too.</p>

      {created?.url && (
        <ShowOnce label={`Calendar link: ${created.name}`} value={created.url}>
          <div className="cal-actions">
            <a className="btn-primary btn-sm" href={created.webcal_url}><Icon name="calendar" size={14} /> Open in my calendar app</a>
          </div>
          <ul className="cal-howto small">
            <li><strong>Apple Calendar</strong> (Mac, iPhone): "Open in my calendar app", or File → New Calendar Subscription and paste.</li>
            <li><strong>Google Calendar</strong>: Other calendars → + → From URL, paste.</li>
            <li><strong>Outlook</strong>: Add calendar → Subscribe from web, paste.</li>
          </ul>
          <p className="notice notice--warn">Google and Outlook.com fetch the link from <em>their</em> servers, so it only
            works there when this Shorthop is reachable from the internet. Apple Calendar fetches it from your own device
            (the VPN is enough). Anyone with the link can read the shifts it shows — treat it like a password.</p>
        </ShowOnce>
      )}

      <div className="card pad form-stack narrow">
        <Field label="What to show">
          <select value={what} onChange={(e) => setWhat(e.target.value)}>
            <option value="mine">My on-call shifts (every schedule)</option>
            {(scheds.data ?? []).map((s) => <option key={s.id} value={`schedule:${s.id}`}>Everyone on {s.name}</option>)}
          </select>
        </Field>
        <ErrorNotice>{error}</ErrorNotice>
        <div className="detail-actions">
          <button className="btn-primary" onClick={create}><Icon name="link" size={15} /> Create subscription link</button>
          <a className="btn-ghost" href={calendarApi.downloadPath(scheduleId)}><Icon name="download" size={15} /> Download .ics</a>
        </div>
        <p className="muted small">The subscription covers the last 14 days and the next 90, and stays current. The download is a
          snapshot — for a calendar that cannot reach this server — and does not update.</p>
      </div>

      <h3 className="sub">Your calendar links</h3>
      <ErrorNotice>{feeds.error}</ErrorNotice>
      {feeds.data && active.length === 0 && <p className="muted small">None yet.</p>}
      {active.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead><tr><th>Shows</th><th>Link</th><th>Last read by a calendar</th><th></th></tr></thead>
            <tbody>
              {active.map((f) => (
                <tr key={f.id}>
                  <td>{f.name}<div className="muted small">created {wall(f.created_at)}</div></td>
                  <td><code>{f.prefix}…</code></td>
                  <td>{f.last_fetched_at ? relative(f.last_fetched_at, now) : <span className="muted">never — not subscribed yet?</span>}</td>
                  <td><button className="btn-ghost btn-sm btn-danger" onClick={() => revoke(f)}>Revoke</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
