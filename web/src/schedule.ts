/** On-call schedule views — pure, shared by the web and the native app (synced verbatim by
 *  shorthop-native/scripts/sync-portal.mjs, so the phone draws the same calendar from the same
 *  rules). No DOM, no React: only dates, segments and colours. */
import type { Layer, Schedule, Segment, Shifts } from "./api";
import { parseTs } from "./time";

export const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
export const ROTATIONS = [{ h: 24, label: "Daily" }, { h: 168, label: "Weekly" }, { h: 336, label: "Every 2 weeks" }];

/** A stable colour per person, from a palette that reads on both themes (mid-lightness). */
export const PALETTE = ["#2f7fc1", "#2a9d8f", "#b5651d", "#7b5ea7", "#c0392b", "#3d8b3d", "#a0527a", "#5a6b7c"];
export function colourFor(id: string | undefined): string {
  if (!id) return "transparent";
  let h = 0;
  for (const c of id) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return PALETTE[h % PALETTE.length];
}

/** A segment's WHOLE shift: the server sends shift_start/shift_end when the window it was asked
 *  for cut the shift (a 14-day view opening mid-week), and picking the shift must cover all of
 *  it, not the part on screen. */
export const shiftBounds = (s: Segment) => ({ start: s.shift_start ?? s.start, end: s.shift_end ?? s.end });

export const who = (u: Segment["user"]) => (u ? u.name || u.email : "nobody");
/** First name, or the address's local part — a calendar cell has room for one word. */
export const shortWho = (u: Segment["user"]) => (u ? (u.name || "").trim().split(/\s+/)[0] || u.email.split("@")[0] : "nobody");
export const hhmm = (d: Date) => d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
export const sameDay = (x: Date, y: Date) =>
  x.getFullYear() === y.getFullYear() && x.getMonth() === y.getMonth() && x.getDate() === y.getDate();

/** The 6-week, Monday-first grid that contains `month` (any date in it), in the device's time. */
export function monthGrid(month: Date): { first: Date; days: Date[] } {
  const first = new Date(month.getFullYear(), month.getMonth(), 1);
  const lead = (first.getDay() + 6) % 7;                       // Monday = 0
  const start = new Date(first.getFullYear(), first.getMonth(), 1 - lead);
  // Built from calendar fields, never by adding 86 400 000 ms — a DST day is 23 or 25 hours.
  const days = Array.from({ length: 42 }, (_, i) => new Date(start.getFullYear(), start.getMonth(), start.getDate() + i));
  return { first, days };
}

export interface DayEntry { seg: Segment; from: Date | null; to: Date | null }

/** Who is on call during [day, next day): one entry per segment overlapping it, with the
 *  handoff times that fall INSIDE the day (a person on all day carries no times). */
export function entriesFor(day: Date, final: Segment[]): DayEntry[] {
  const a = day.getTime();
  const b = new Date(day.getFullYear(), day.getMonth(), day.getDate() + 1).getTime();
  const out: DayEntry[] = [];
  for (const s of final) {
    const sa = parseTs(s.start).getTime(), sb = parseTs(s.end).getTime();
    if (sb <= a || sa >= b) continue;
    out.push({ seg: s, from: sa > a ? new Date(sa) : null, to: sb < b ? new Date(sb) : null });
  }
  return out;
}

/** Everyone on call somewhere from `from` on, once — the key to the colours. */
export function peopleIn(final: Segment[], from: Date): NonNullable<Segment["user"]>[] {
  return [...new Map(final.filter((x) => x.user && parseTs(x.end).getTime() > from.getTime())
    .map((x) => [x.user!.id, x.user!])).values()];
}

/** When the schedule begins (its earliest layer start), or null with no layers. Before it
 *  there is no schedule yet, which is not a gap. */
export function scheduleStart(s: Pick<Schedule, "layers">): Date | null {
  return s.layers.length ? new Date(Math.min(...s.layers.map((l) => new Date(l.start).getTime()))) : null;
}

export interface Bar { seg: Segment; left: number; width: number }      // percent of the window
export interface TimelineRow { label: string; final: boolean; bars: Bar[] }

/** The layers view: one row per layer (lowest first) and the final "who is paged" row, each a
 *  list of bars positioned as percentages of the shifts window. */
export function timelineRows(shifts: Shifts): { rows: TimelineRow[]; ticks: { left: number; day: Date }[] } {
  const start = parseTs(shifts.start).getTime();
  const end = parseTs(shifts.end).getTime();
  const span = end - start;
  const days = Math.round(span / 86400e3);
  const bars = (segs: Segment[]): Bar[] => segs.filter((s) => s.user).map((s) => {
    const a = Math.max(parseTs(s.start).getTime(), start);
    const b = Math.min(parseTs(s.end).getTime(), end);
    return { seg: s, left: ((a - start) / span) * 100, width: ((b - a) / span) * 100 };
  });
  return {
    rows: [...shifts.layers.map((l) => ({ label: l.name, final: false, bars: bars(l.shifts) })),
           { label: "On call", final: true, bars: bars(shifts.final) }],
    ticks: dayTicks(start, end, days),
  };
}

/** One tick per local midnight in [start, end), placed by real time — a DST day is 23 or 25
 *  hours, so `start + d * 24 h` would drift an hour off the day line. */
function dayTicks(start: number, end: number, days: number): { left: number; day: Date }[] {
  const s = new Date(start);
  const out: { left: number; day: Date }[] = [];
  for (let d = 0; d <= days + 1; d++) {
    const day = new Date(s.getFullYear(), s.getMonth(), s.getDate() + d);
    if (day.getTime() < start) continue;
    if (day.getTime() >= end) break;
    out.push({ left: ((day.getTime() - start) / (end - start)) * 100, day });
  }
  return out;
}

/** Local midnight of `d`, plus `n` calendar days (never `n * 24 h`). */
export const addDays = (d: Date, n: number) => new Date(d.getFullYear(), d.getMonth(), d.getDate() + n);

export interface WeekBar { seg: Segment; left: number; width: number; cutLeft: boolean; cutRight: boolean }

/** The month view's stretches for one week row: who is on call drawn as bars across the days,
 *  positioned in percent of the week. A time maps into the DAY it falls in, so a 23-hour DST day
 *  is one column wide like the rest. Back-to-back segments of the same person and source are one
 *  stretch; before `since` the schedule did not exist yet, so nothing is drawn (not a gap). */
export function weekBars(week: Date[], final: Segment[], since: Date | null): WeekBar[] {
  const edges = [...week.map((d) => d.getTime()), addDays(week[week.length - 1], 1).getTime()];
  const a = edges[0], b = edges[edges.length - 1];
  const col = (t: number) => {
    let i = 0;
    while (i < week.length - 1 && t >= edges[i + 1]) i++;
    return i + (t - edges[i]) / (edges[i + 1] - edges[i]);
  };
  const merged: Segment[] = [];
  for (const s of final) {
    const last = merged[merged.length - 1];
    if (last && last.end === s.start && last.source === s.source && last.user?.id === s.user?.id) {
      merged[merged.length - 1] = { ...last, end: s.end, shift_end: s.shift_end };
    } else merged.push(s);
  }
  const out: WeekBar[] = [];
  for (const s of merged) {
    let sa = parseTs(s.start).getTime();
    const sb = parseTs(s.end).getTime();
    if (since && sb <= since.getTime()) continue;
    if (since) sa = Math.max(sa, since.getTime());
    if (sb <= a || sa >= b) continue;
    const x = col(Math.max(sa, a)), y = sb >= b ? week.length : col(sb);
    out.push({ seg: s, left: (x / week.length) * 100, width: ((y - x) / week.length) * 100, cutLeft: sa < a, cutRight: sb > b });
  }
  return out;
}

/** `list` with the item at `from` moved to `to`. */
export function moveItem<T>(list: T[], from: number, to: number): T[] {
  const out = [...list];
  const [x] = out.splice(from, 1);
  out.splice(to, 0, x);
  return out;
}

/** "Weekly from 2026-10-19 09:00 · Mon, Tue 09:00–17:00" */
export function layerSummary(l: Layer): string {
  const rot = ROTATIONS.find((r) => r.h === l.rotation_hours)?.label ?? `every ${l.rotation_hours} h`;
  const hours = l.restriction
    ? ` · ${l.restriction.days.map((d) => DAYS[d]).join(", ")} ${l.restriction.start}–${l.restriction.end}`
    : " · all hours";
  return `${rot} from ${l.start.replace("T", " ")}${hours}`;
}
