import { useEffect, useRef, useState } from "react";
import { api, oncallApi, type Layer, type Schedule, type ScheduleIn, type Segment, type Shifts } from "../api";
import { Link, useMe, useNavigate, useNow, usePoll } from "../app";
import { Icon } from "../components/Icon";
import { Empty, ErrorNotice, Field } from "../components/ui";
import { addDays, colourFor, DAYS, hhmm, shiftBounds, layerSummary, monthGrid, moveItem, peopleIn, ROTATIONS, sameDay, scheduleStart, shortWho, timelineRows, weekBars, who } from "../schedule";
import { parseTs, relative, toLocalInput, wall } from "../time";

const ZONES = ["UTC", "Europe/Stockholm", "Europe/London", "Europe/Malta", "Europe/Berlin", "America/New_York", "America/Los_Angeles", "Asia/Tokyo"];


function OnCallBadge({ s, now }: { s: Pick<Schedule, "on_call">; now: number }) {
  const oc = s.on_call;
  if (!oc.user) return <span className="badge danger">nobody on call</span>;
  return (
    <span className="oncall-now">
      <span className="dot" style={{ background: colourFor(oc.user.id) }} />
      <strong>{who(oc.user)}</strong>
      {oc.source === "override" && <span className="badge info">override</span>}
      {oc.until && <span className="muted small">until {wall(oc.until)} ({relative(oc.until, now).replace("in ", "")} left)</span>}
    </span>
  );
}

export function Schedules() {
  const navigate = useNavigate();
  const { can } = useMe();
  const now = useNow(60000);
  const { data, error } = usePoll(() => oncallApi.list(), [], 60000);
  const mine = usePoll(() => oncallApi.mine(14), []);

  return (
    <div className="page">
      <div className="section-head">
        <h2>On-call</h2>
        {can("config:write") && <button className="btn-primary" onClick={() => navigate("/schedules/new")}><Icon name="plus" size={15} /> New schedule</button>}
      </div>
      <p className="glance">Who answers when an escalation step targets a schedule. Rotations hand off at local wall-clock time; an override beats every rotation.</p>
      <ErrorNotice>{error}</ErrorNotice>
      {data && data.length === 0 && <Empty icon="calendar" title="No schedules yet">Create one, then add it as a target in an escalation policy.</Empty>}
      <ul className="integration-list">
        {(data ?? []).map((s) => (
          <li key={s.id} className="card integration-row">
            <Link to={`/schedules/${s.id}`} className="alert-main">
              <div className="alert-title"><span className="alert-name">{s.name}</span><span className="badge neutral">{s.timezone}</span></div>
              <OnCallBadge s={s} now={now} />
            </Link>
          </li>
        ))}
      </ul>

      <section className="card pad">
        <div className="section-head">
          <h3 className="sub">My next shifts</h3>
          <button className="btn-ghost btn-sm" onClick={() => navigate("/account?tab=calendar")}><Icon name="calendar" size={14} /> Add to my calendar</button>
        </div>
        {mine.data && mine.data.length === 0 && <p className="muted small">You are not on call in the next 14 days.</p>}
        <ul className="shift-list">
          {(mine.data ?? []).map((m, i) => (
            <li key={i}><Link to={`/schedules/${m.schedule_id}`}>{m.schedule}</Link> <span className="muted small">{wall(m.start)} → {wall(m.end)}{m.source === "override" ? " · override" : ""}</span></li>
          ))}
        </ul>
      </section>
    </div>
  );
}

/** A stretch someone picked on the calendar or the timeline: a person's shift (`seg`), a run of
 *  whole days, or — `manual`, from "New override" — the next hours, to be edited in the panel.
 *  It pre-fills the override panel, which is the ONE place an override is made. */
interface Picked { start: Date; end: Date; seg?: Segment; manual?: boolean }
const pickOf = (seg: Segment): Picked => { const b = shiftBounds(seg); return { start: parseTs(b.start), end: parseTs(b.end), seg }; };
const isPicked = (pick: Picked | null, seg: Segment) => !!pick?.seg && pick.seg.start === seg.start && pick.seg.end === seg.end;

function Timeline({ shifts, pick, onPick, now }: { shifts: Shifts; pick: Picked | null; onPick?: (p: Picked) => void; now: number }) {
  const { rows, ticks } = timelineRows(shifts);
  const step = ticks.length > 14 ? 2 : 1;
  const a = parseTs(shifts.start).getTime(), b = parseTs(shifts.end).getTime();
  const nowLeft = now > a && now < b ? ((now - a) / (b - a)) * 100 : null;
  return (
    <div className="timeline-chart">
      <div className="tl-row tl-scale">
        <div className="tl-label" />
        <div className="tl-track">
          {ticks.map((t, d) => (
            <span key={d} className="tl-tick" style={{ left: `${t.left}%` }}>
              {d % step === 0 ? t.day.toLocaleDateString(undefined, { weekday: "short", day: "numeric" }) : ""}
            </span>
          ))}
        </div>
      </div>
      {rows.map((r) => (
        <div className={`tl-row${r.final ? " tl-final" : ""}`} key={r.label}>
          <div className="tl-label">{r.label}</div>
          <div className="tl-track">
            {ticks.map((t, d) => <span key={`g${d}`} className="tl-grid" style={{ left: `${t.left}%` }} />)}
            {r.bars.map((bar, i) => {
              const title = `${who(bar.seg.user)} · ${wall(bar.seg.start)} → ${wall(bar.seg.end)}${bar.seg.source === "override" ? " (override)" : ""}`;
              const cls = `tl-seg${bar.seg.source === "override" ? " tl-override" : ""}${isPicked(pick, bar.seg) ? " tl-picked" : ""}`;
              const style = { left: `${bar.left}%`, width: `${bar.width}%`, background: colourFor(bar.seg.user?.id) };
              const label = bar.width > 6 && <span>{who(bar.seg.user)}</span>;
              return onPick
                ? <button key={i} type="button" className={cls} style={style} title={`${title} — click to override`} aria-label={`${title}. Override this shift`} onClick={() => onPick(pickOf(bar.seg))}>{label}</button>
                : <div key={i} className={cls} style={style} title={title}>{label}</div>;
            })}
            {nowLeft !== null && <span className="tl-now" style={{ left: `${nowLeft}%` }} aria-hidden />}
          </div>
        </div>
      ))}
    </div>
  );
}


/* --- month calendar ------------------------------------------------------------------------ */

/** Six week rows. Who is on call is drawn as coloured stretches ACROSS the days; click a
 *  stretch to override that shift, or click (or drag across) days to override whole days. */
function MonthCalendar({ id, since, pick, onPick, reload }: { id: string; since: Date | null; pick: Picked | null; onPick?: (p: Picked) => void; reload: number }) {
  const [month, setMonth] = useState(() => { const d = new Date(); return new Date(d.getFullYear(), d.getMonth(), 1); });
  const [drag, setDrag] = useState<{ a: number; b: number } | null>(null);
  const { first, days } = monthGrid(month);
  const from = days[0];
  const { data, error } = usePoll(() => oncallApi.shifts(id, 42, from.toISOString()), [id, from.getTime(), reload], 60000);
  const today = new Date();
  // Everyone on call somewhere in this month, once — the key to the colours.
  const people = data ? peopleIn(data.final, days[0]) : [];
  const step = (n: number) => setMonth(new Date(month.getFullYear(), month.getMonth() + n, 1));
  const weeks = Array.from({ length: 6 }, (_, w) => days.slice(w * 7, w * 7 + 7));

  // Day selection: the drag range while dragging, else the picked whole-day range.
  const lo = drag ? Math.min(drag.a, drag.b) : -1, hi = drag ? Math.max(drag.a, drag.b) : -1;
  const selected = (i: number) => drag ? i >= lo && i <= hi
    : !!pick && !pick.seg && !pick.manual && days[i] >= pick.start && days[i] < pick.end;
  const dayAt = (x: number, y: number) => {
    const el = document.elementFromPoint(x, y)?.closest("[data-day]");
    return el ? Number(el.getAttribute("data-day")) : null;
  };
  const finish = () => {
    if (!drag || !onPick) return;
    setDrag(null);
    onPick({ start: days[lo], end: addDays(days[hi], 1) });
  };

  return (
    <div className="mcal">
      <div className="mcal-head">
        <button type="button" className="btn-ghost btn-sm" aria-label="Previous month" onClick={() => step(-1)}><Icon name="chevronLeft" size={15} /></button>
        <strong>{first.toLocaleDateString(undefined, { month: "long", year: "numeric" })}</strong>
        <button type="button" className="btn-ghost btn-sm" aria-label="Next month" onClick={() => step(1)}><Icon name="chevronRight" size={15} /></button>
        {!sameDay(first, new Date(today.getFullYear(), today.getMonth(), 1)) && (
          <button type="button" className="btn-ghost btn-sm" onClick={() => setMonth(new Date(today.getFullYear(), today.getMonth(), 1))}>Today</button>
        )}
      </div>
      <ErrorNotice>{error}</ErrorNotice>
      <div className="mcal-dows">{DAYS.map((d) => <div key={d} className="mcal-dow">{d}</div>)}</div>
      <div className="mcal-weeks"
        onPointerMove={(e) => { if (drag) { const i = dayAt(e.clientX, e.clientY); if (i !== null && i !== drag.b) setDrag({ ...drag, b: i }); } }}
        onPointerUp={finish} onPointerCancel={() => setDrag(null)} onPointerLeave={() => drag && finish()}>
        {weeks.map((week, w) => (
          <div className="mcal-week" key={w}>
            {week.map((day, d) => {
              const i = w * 7 + d;
              const other = day.getMonth() !== first.getMonth();
              const label = day.toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" });
              const cls = `mcal-day${other ? " mcal-other" : ""}${sameDay(day, today) ? " mcal-today" : ""}${selected(i) ? " mcal-sel" : ""}`;
              const date = <span className="mcal-date">{day.getDate()}</span>;
              return onPick
                ? <button key={i} type="button" data-day={i} className={cls} aria-label={`${label} — cover this day`}
                    onPointerDown={(e) => { if (e.button === 0) setDrag({ a: i, b: i }); }}
                    onClick={(e) => { if (e.detail === 0) onPick({ start: day, end: addDays(day, 1) }); }}>{date}</button>
                : <div key={i} className={cls} aria-label={label}>{date}</div>;
            })}
            <div className="mcal-bars">
              {data && weekBars(week, data.final, since).map((bar, k) => {
                const s = bar.seg;
                const start = parseTs(s.start);
                const title = `${who(s.user)} · ${wall(s.start)} → ${wall(s.end)}${s.source === "override" ? " (override)" : ""}`;
                const time = !bar.cutLeft && (start.getHours() || start.getMinutes()) ? hhmm(start) : null;
                // A seventh of the week is a day; under ~half a day there is room for a first name on a
                // desktop only (phones hide `.narrow` names), and under that only for the colour.
                const name = bar.width >= 26 ? who(s.user) : bar.width >= 6 ? shortWho(s.user) : "";
                const cls = `mcal-bar${s.source === "override" ? " mcal-bar-override" : ""}${s.user ? "" : " mcal-bar-nobody"}`
                  + `${bar.width < 11 ? " narrow" : ""}${bar.cutLeft ? " cut-l" : ""}${bar.cutRight ? " cut-r" : ""}${isPicked(pick, s) ? " mcal-bar-picked" : ""}`;
                const style = { left: `${bar.left}%`, width: `${bar.width}%`, background: s.user ? colourFor(s.user.id) : undefined };
                const body = <>{time && bar.width >= 13 && <span className="mcal-bar-time">{time}</span>}<span className="mcal-bar-name">{name}</span></>;
                return onPick
                  ? <button key={k} type="button" className={cls} style={style} title={`${title} — click to override`} aria-label={`${title}. Override this shift`}
                      onPointerDown={(e) => e.stopPropagation()} onClick={() => onPick(pickOf(s))}>{body}</button>
                  : <span key={k} className={cls} style={style} title={title}>{body}</span>;
              })}
            </div>
          </div>
        ))}
      </div>
      {people.length > 0 && (
        <ul className="mcal-legend" aria-label="Who is who">
          {people.map((u) => <li key={u.id}><span className="dot" style={{ background: colourFor(u.id) }} />{who(u)}</li>)}
        </ul>
      )}
      <p className="notice">Times are in your local time. Hatched = override.{onPick ? " Click a shift to override it, or click or drag across days to cover them." : ""}</p>
    </div>
  );
}


/* --- override ------------------------------------------------------------------------------ */

/** Put someone on call from–to, pre-filled from what was picked (a shift, days, or "New
 *  override"). From and To stay editable, which is why no separate by-time form exists. */
function OverrideForm({ scheduleId, from, to, seg, onDone, onCancel }: {
  scheduleId: string; from: Date; to: Date; seg?: Segment; onDone: () => void; onCancel?: () => void;
}) {
  const { me, can } = useMe();
  const users = usePoll(() => api.listUsers(), []);
  const [fromAt, setFromAt] = useState(toLocalInput(from));
  const [toAt, setToAt] = useState(toLocalInput(to));
  const [forUser, setForUser] = useState("");
  const [reason, setReason] = useState(seg?.user && seg.user.id !== me.id ? `covering for ${shortWho(seg.user)}` : "");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const mine = seg?.user?.id === me.id;

  const take = async () => {
    setError(null);
    const a = new Date(fromAt), b = new Date(toAt);
    if (!(b > a)) { setError("The end must be after the start."); return; }
    setBusy(true);
    try {
      await oncallApi.addOverride(scheduleId, { user_id: forUser || undefined, starts_at: a.toISOString(), ends_at: b.toISOString(), reason });
      onDone();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="form-stack" onSubmit={(e) => { e.preventDefault(); take(); }}>
      {seg && (
        <p className="override-context">
          <span className="dot" style={seg.user ? { background: colourFor(seg.user.id) } : { background: "var(--danger)" }} />
          {seg.user ? <>Override <strong>{who(seg.user)}</strong>{mine ? " (you)" : ""}</> : <>Fill the gap</>}
          <span className="muted small"> · {wall(shiftBounds(seg).start)} → {wall(shiftBounds(seg).end)}</span>
        </p>
      )}
      {mine && <p className="notice notice--inline-warn">This is your own shift. {can("config:write") ? "Pick who covers it." : "Only the person covering it can put themselves on call — ask them to take it here."}</p>}
      <div className="form-grid">
        <Field label="From"><input type="datetime-local" value={fromAt} onChange={(e) => setFromAt(e.target.value)} /></Field>
        <Field label="To" hint="At most 62 days. Shorten it to cover part of the shift."><input type="datetime-local" value={toAt} onChange={(e) => setToAt(e.target.value)} /></Field>
        <Field label="Who" hint={can("config:write") ? undefined : "You can only put yourself on call."}>
          <select value={forUser} disabled={!can("config:write")} onChange={(e) => setForUser(e.target.value)}>
            <option value="">Me ({me.name || me.email})</option>
            {(users.data ?? []).filter((u) => u.id !== me.id && !u.disabled).map((u) => <option key={u.id} value={u.id}>{u.name || u.email}</option>)}
          </select>
        </Field>
      </div>
      <Field label="Why (optional)"><input value={reason} maxLength={500} onChange={(e) => setReason(e.target.value)} placeholder="covering for Bob, he is travelling" /></Field>
      <ErrorNotice>{error}</ErrorNotice>
      <div className="detail-actions">
        <button className="btn-primary" disabled={busy}><Icon name="clock" size={15} /> {forUser ? "Put them on call" : "Take it"}</button>
        {onCancel && <button type="button" className="btn-ghost" onClick={onCancel}>Cancel</button>}
      </div>
    </form>
  );
}

/** Local midnight today — where the layer timeline starts, and where "Today" goes back to. */
const todayStart = () => { const d = new Date(); return new Date(d.getFullYear(), d.getMonth(), d.getDate()); };
const TIMELINE_DAYS = 14;

export function ScheduleDetail({ id }: { id: string }) {
  const { me, can } = useMe();
  const navigate = useNavigate();
  const now = useNow(60000);
  const [reload, setReload] = useState(0);
  const [tlStart, setTlStart] = useState(todayStart);
  const sched = usePoll(() => oncallApi.get(id), [id, reload], 60000);
  const shifts = usePoll(() => oncallApi.shifts(id, TIMELINE_DAYS, tlStart.toISOString()), [id, tlStart.getTime(), reload], 60000);
  const users = usePoll(() => api.listUsers(), []);
  const [view, setView] = useState<"month" | "timeline">("month");
  const [pick, setPick] = useState<Picked | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = () => setReload((n) => n + 1);
  const choose = can("oncall:write") ? (p: Picked) => setPick(p) : undefined;

  const back = <Link to="/schedules" className="back-link"><Icon name="chevronLeft" size={15} /> On-call</Link>;
  if (sched.error && !sched.data) return <div className="page">{back}<ErrorNotice>{sched.error}</ErrorNotice></div>;
  if (!sched.data) return <div className="page">{back}<p className="muted">Loading…</p></div>;
  const s = sched.data;
  const userName = (uid: string) => { const u = users.data?.find((x) => x.id === uid); return u ? u.name || u.email : "unknown"; };
  const tlEnd = addDays(tlStart, TIMELINE_DAYS - 1);
  const fmt = (d: Date) => d.toLocaleDateString(undefined, { day: "numeric", month: "short" });

  return (
    <div className="page detail-page">
      {back}
      <div className="detail-head">
        <div>
          <h2>{s.name}</h2>
          <div className="detail-badges"><span className="badge neutral">{s.timezone}</span><OnCallBadge s={s} now={now} /></div>
        </div>
        <div className="detail-links">
          <button className="btn-ghost btn-sm" onClick={() => navigate(`/account?tab=calendar&schedule=${s.id}`)}><Icon name="calendar" size={14} /> Subscribe in calendar</button>
          {can("config:write") && <button className="btn-ghost btn-sm" onClick={() => navigate(`/schedules/${s.id}/edit`)}><Icon name="edit" size={14} /> Edit layers</button>}
        </div>
      </div>
      <ErrorNotice>{error}</ErrorNotice>

      <section className="card pad">
        <div className="section-head">
          <h3 className="sub">{view === "month" ? "Calendar" : "By layer"}</h3>
          <div className="row-actions">
          {choose && (
            <button type="button" className="btn-ghost btn-sm" onClick={() => {
              const start = new Date();
              setPick({ start, end: new Date(start.getTime() + 8 * 3600e3), manual: true });
            }}><Icon name="plus" size={14} /> New override</button>
          )}
          <div className="seg">
            <button type="button" className={`seg-btn${view === "month" ? " active" : ""}`} onClick={() => setView("month")}>Month</button>
            <button type="button" className={`seg-btn${view === "timeline" ? " active" : ""}`} onClick={() => setView("timeline")}>Layers</button>
          </div>
          </div>
        </div>
        {view === "month" ? (
          <MonthCalendar id={s.id} since={scheduleStart(s)} pick={pick} onPick={choose} reload={reload} />
        ) : (
          <div className="mcal">
            <div className="mcal-head">
              <button type="button" className="btn-ghost btn-sm" aria-label={`Previous ${TIMELINE_DAYS} days`} onClick={() => setTlStart(addDays(tlStart, -TIMELINE_DAYS))}><Icon name="chevronLeft" size={15} /></button>
              <strong>{fmt(tlStart)} – {fmt(tlEnd)}</strong>
              <button type="button" className="btn-ghost btn-sm" aria-label={`Next ${TIMELINE_DAYS} days`} onClick={() => setTlStart(addDays(tlStart, TIMELINE_DAYS))}><Icon name="chevronRight" size={15} /></button>
              {!sameDay(tlStart, todayStart()) && <button type="button" className="btn-ghost btn-sm" onClick={() => setTlStart(todayStart())}>Today</button>}
            </div>
            <ErrorNotice>{shifts.error}</ErrorNotice>
            {shifts.data ? <Timeline shifts={shifts.data} pick={pick} onPick={choose} now={now} /> : <p className="muted small">Loading…</p>}
            <p className="notice">Rows are the layers, lowest first; the bottom row is who is actually paged. Hatched = override.{choose ? " Click a shift to override it." : ""}</p>
          </div>
        )}
        {pick && choose && (
          <div className="override-panel" id="override-panel">
            <OverrideForm key={`${pick.start.getTime()}-${pick.end.getTime()}`} scheduleId={s.id} from={pick.start} to={pick.end} seg={pick.seg}
              onDone={() => { setPick(null); refresh(); }} onCancel={() => setPick(null)} />
          </div>
        )}
      </section>

      <section className="card pad">
        <h3 className="sub">Overrides</h3>
        {(s.overrides ?? []).length === 0 && <p className="muted small">None coming up.</p>}
        <ul className="shift-list">
          {(s.overrides ?? []).map((o) => (
            <li key={o.id}>
              <span><strong>{o.user ? o.user.name || o.user.email : "?"}</strong> <span className="muted small">{wall(o.starts_at)} → {wall(o.ends_at)}</span>
                {o.reason && <div className="small">{o.reason}</div>}</span>
              {(o.user_id === me.id || o.created_by === me.email || can("config:write")) && (
                <button className="btn-ghost btn-sm btn-danger" aria-label="Remove override" onClick={async () => {
                  try { await oncallApi.removeOverride(s.id, o.id); refresh(); } catch (e) { setError((e as Error).message); }
                }}><Icon name="trash" size={14} /></button>
              )}
            </li>
          ))}
        </ul>
      </section>

      <section className="card pad">
        <h3 className="sub">Layers</h3>
        <ol className="step-summary">
          {s.layers.map((l, i) => (
            <li key={i}><strong>{l.name}</strong> <span className="muted small">{layerSummary(l)}</span>
              <div className="small">{l.users.map(userName).join(" → ")}</div></li>
          ))}
        </ol>
      </section>
    </div>
  );
}

/** The rotation order as chips you drag to reorder (mouse and touch: pointer events, since
 *  HTML drag-and-drop does not work on phones). Arrow keys on a focused chip move it too. */
function RotationOrder({ users, name, onChange }: { users: string[]; name: (uid: string) => string; onChange: (users: string[]) => void }) {
  const [dragging, setDragging] = useState<number | null>(null);
  const ref = useRef<HTMLDivElement>(null);
  const move = (from: number, to: number) => { if (to >= 0 && to < users.length && to !== from) onChange(moveItem(users, from, to)); };
  const over = (x: number, y: number) => {
    const el = document.elementFromPoint(x, y)?.closest("[data-pos]");
    return el && ref.current?.contains(el) ? Number(el.getAttribute("data-pos")) : null;
  };
  return (
    // The pointer is captured by the CONTAINER: a chip is moved in the DOM as it is reordered,
    // and moving a node releases a capture held on it.
    <div className="rotation-order" ref={ref}
      onPointerMove={(e) => {
        if (dragging === null) return;
        const to = over(e.clientX, e.clientY);
        if (to !== null && to !== dragging) { move(dragging, to); setDragging(to); }
      }}
      onPointerUp={() => setDragging(null)} onPointerCancel={() => setDragging(null)}>
      {users.map((uid, k) => (
        <span key={`${uid}#${users.slice(0, k).filter((x) => x === uid).length}`} data-pos={k} tabIndex={0} className={`filter-chip rot-chip${dragging === k ? " rot-dragging" : ""}`}
          aria-label={`${k + 1}. ${name(uid)} — drag or use the arrow keys to reorder`}
          onKeyDown={(e) => {
            if (e.key === "ArrowLeft" || e.key === "ArrowUp") { e.preventDefault(); move(k, k - 1); }
            if (e.key === "ArrowRight" || e.key === "ArrowDown") { e.preventDefault(); move(k, k + 1); }
          }}>
          <span className="rot-grip" aria-hidden
            onPointerDown={(e) => { e.preventDefault(); try { ref.current?.setPointerCapture(e.pointerId); } catch { /* not an active pointer */ } setDragging(k); }}>⠿</span>
          <span className="dot" style={{ background: colourFor(uid) }} />
          {k + 1}. {name(uid)}
          <button type="button" className="chip-x" aria-label={`Remove ${name(uid)}`} onClick={() => onChange(users.filter((_, j) => j !== k))}><Icon name="x" size={12} /></button>
        </span>
      ))}
    </div>
  );
}

const nextMonday = () => {
  const d = new Date();
  d.setDate(d.getDate() + ((8 - d.getDay()) % 7 || 7));
  return `${toLocalInput(d).slice(0, 10)}T09:00`;
};
const blankLayer = (): Layer => ({ name: "Primary", users: [], rotation_hours: 168, start: nextMonday(), restriction: null });

export function ScheduleEdit({ id }: { id: string | null }) {
  const navigate = useNavigate();
  const users = usePoll(() => api.listUsers(), []);
  const teams = usePoll(() => api.listTeams(), []);
  const existing = usePoll(() => (id ? oncallApi.get(id) : Promise.resolve(null)), [id]);
  const [draft, setDraft] = useState<ScheduleIn | null>(id ? null : { name: "", team_id: null, timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC", layers: [blankLayer()], handover_notify: true });
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (id && existing.data) {
      const { name, team_id, timezone, layers, handover_notify } = existing.data;
      setDraft({ name, team_id, timezone, layers, handover_notify });
    }
  }, [id, existing.data]);
  if (!draft) return <div className="page"><p className="muted">Loading…</p></div>;

  const setLayer = (i: number, l: Layer) => setDraft({ ...draft, layers: draft.layers.map((x, j) => (j === i ? l : x)) });
  const save = async () => {
    setError(null);
    try {
      const s = id ? await oncallApi.update(id, draft) : await oncallApi.create(draft);
      navigate(`/schedules/${s.id}`);
    } catch (e) {
      setError((e as Error).message);
    }
  };
  const userName = (uid: string) => { const u = users.data?.find((x) => x.id === uid); return u ? u.name || u.email : "unknown"; };

  return (
    <div className="page">
      <Link to={id ? `/schedules/${id}` : "/schedules"} className="back-link"><Icon name="chevronLeft" size={15} /> Back</Link>
      <h2>{id ? `Edit ${draft.name}` : "New schedule"}</h2>
      <form className="card pad form-stack" onSubmit={(e) => { e.preventDefault(); save(); }}>
        <div className="form-grid">
          <Field label="Name"><input required maxLength={200} value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} placeholder="platform primary" /></Field>
          <Field label="Timezone" hint="Handoffs happen at this local wall-clock time, across DST.">
            <input list="zones" value={draft.timezone} onChange={(e) => setDraft({ ...draft, timezone: e.target.value })} />
          </Field>
          <Field label="Team">
            <select value={draft.team_id ?? ""} onChange={(e) => setDraft({ ...draft, team_id: e.target.value || null })}>
              <option value="">None</option>
              {(teams.data ?? []).map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
            </select>
          </Field>
        </div>
        <datalist id="zones">{ZONES.map((z) => <option key={z} value={z} />)}</datalist>
        <label className="check"><input type="checkbox" checked={draft.handover_notify} onChange={(e) => setDraft({ ...draft, handover_notify: e.target.checked })} /> Email whoever comes on call</label>

        <ol className="step-list">
          {draft.layers.map((l, i) => (
            <li key={i} className="step-card">
              <div className="step-head">
                <input aria-label={`Layer ${i + 1} name`} value={l.name} onChange={(e) => setLayer(i, { ...l, name: e.target.value })} />
                <span className="muted small">layer {i + 1}{i > 0 ? " — wins over the layers above where it covers" : ""}</span>
                {draft.layers.length > 1 && <button type="button" className="btn-ghost btn-sm" aria-label={`Remove layer ${i + 1}`} onClick={() => setDraft({ ...draft, layers: draft.layers.filter((_, j) => j !== i) })}><Icon name="trash" size={14} /></button>}
              </div>
              <div className="target-chips">
                <RotationOrder users={l.users} name={userName} onChange={(u) => setLayer(i, { ...l, users: u })} />
                <select aria-label="Add a person to the rotation" value="" onChange={(e) => e.target.value && setLayer(i, { ...l, users: [...l.users, e.target.value] })}>
                  <option value="">+ add to rotation…</option>
                  {(users.data ?? []).filter((u) => !u.disabled).map((u) => <option key={u.id} value={u.id}>{u.name || u.email}</option>)}
                </select>
              </div>
              {l.users.length > 1 && <p className="notice">Drag the chips to change who goes first; the rotation runs in this order from the first handoff.</p>}
              <div className="form-grid">
                <Field label="Rotation">
                  <select value={ROTATIONS.some((r) => r.h === l.rotation_hours) ? l.rotation_hours : "custom"}
                    onChange={(e) => setLayer(i, { ...l, rotation_hours: e.target.value === "custom" ? 12 : Number(e.target.value) })}>
                    {ROTATIONS.map((r) => <option key={r.h} value={r.h}>{r.label}</option>)}
                    <option value="custom">Every N hours…</option>
                  </select>
                </Field>
                {!ROTATIONS.some((r) => r.h === l.rotation_hours) && (
                  <Field label="Hours"><input type="number" min={1} max={672} value={l.rotation_hours} onChange={(e) => setLayer(i, { ...l, rotation_hours: Number(e.target.value) })} /></Field>
                )}
                <Field label="First handoff (local)"><input type="datetime-local" value={l.start} onChange={(e) => setLayer(i, { ...l, start: e.target.value })} /></Field>
              </div>
              <label className="check"><input type="checkbox" checked={!!l.restriction}
                onChange={(e) => setLayer(i, { ...l, restriction: e.target.checked ? { days: [0, 1, 2, 3, 4], start: "08:00", end: "18:00" } : null })} /> Only certain hours</label>
              {l.restriction && (
                <div className="form-grid">
                  <div className="day-picks">
                    {DAYS.map((d, n) => (
                      <label key={d} className="check"><input type="checkbox" checked={l.restriction!.days.includes(n)}
                        onChange={(e) => setLayer(i, { ...l, restriction: { ...l.restriction!, days: e.target.checked ? [...l.restriction!.days, n] : l.restriction!.days.filter((x) => x !== n) } })} /> {d}</label>
                    ))}
                  </div>
                  <Field label="From"><input type="time" value={l.restriction.start} onChange={(e) => setLayer(i, { ...l, restriction: { ...l.restriction!, start: e.target.value } })} /></Field>
                  <Field label="To" hint="Earlier than From = overnight."><input type="time" value={l.restriction.end} onChange={(e) => setLayer(i, { ...l, restriction: { ...l.restriction!, end: e.target.value } })} /></Field>
                </div>
              )}
            </li>
          ))}
        </ol>
        <div><button type="button" className="btn-ghost btn-sm" onClick={() => setDraft({ ...draft, layers: [...draft.layers, { ...blankLayer(), name: `Layer ${draft.layers.length + 1}` }] })}><Icon name="plus" size={14} /> Add a layer</button></div>
        <ErrorNotice>{error}</ErrorNotice>
        <div className="detail-actions">
          <button className="btn-primary">Save schedule</button>
          {id && <button type="button" className="btn-ghost btn-danger" onClick={async () => {
            if (!window.confirm(`Delete ${draft.name}?`)) return;
            try { await oncallApi.remove(id); navigate("/schedules"); } catch (e) { setError((e as Error).message); }
          }}>Delete</button>}
        </div>
      </form>
    </div>
  );
}

export { OnCallBadge };
