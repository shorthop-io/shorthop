/** Time helpers.
 *
 *  ⚠️ Every timestamp goes through `parseTs`. The server emits zone-aware ISO from Postgres,
 *  but SQLite (local dev, tests) hands back NAIVE UTC — and ECMAScript parses a bare
 *  date-time as LOCAL time, which shifts every age by the device's UTC offset. Invisible on
 *  a UTC laptop, which is exactly why it has bitten this platform twice. */

export function parseTs(s: string): Date {
  return new Date(/[zZ]$|[+-]\d{2}:?\d{2}$/.test(s) ? s : s + "Z");
}

/** "3 min ago", "2 h ago", "4 d ago" — or "in 20 min" for a future time. */
export function relative(s: string | null | undefined, now: number = Date.now()): string {
  if (!s) return "—";
  const diff = now - parseTs(s).getTime();
  const future = diff < 0;
  const sec = Math.abs(diff) / 1000;
  let text: string;
  // Floored, not rounded: "2 h ago" beside "for 1 h 35 min" reads as a contradiction.
  if (sec < 60) text = `${Math.floor(sec)} s`;
  else if (sec < 3600) text = `${Math.floor(sec / 60)} min`;
  else if (sec < 86400 * 2) text = `${Math.floor(sec / 3600)} h`;
  else text = `${Math.floor(sec / 86400)} d`;
  return future ? `in ${text}` : `${text} ago`;
}

/** Duration between two instants, e.g. how long an alert fired. */
export function duration(from: string, to: string | null, now: number = Date.now()): string {
  const end = to ? parseTs(to).getTime() : now;
  // Floored like relative(), so "started 1 min ago" never sits beside "for 2 min".
  const sec = Math.max(0, (end - parseTs(from).getTime()) / 1000);
  if (sec < 60) return `${Math.floor(sec)} s`;
  if (sec < 3600) return `${Math.floor(sec / 60)} min`;
  if (sec < 86400) {
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    return m ? `${h} h ${m} min` : `${h} h`;
  }
  const d = Math.floor(sec / 86400);
  const h = Math.floor((sec % 86400) / 3600);
  return h ? `${d} d ${h} h` : `${d} d`;
}

/** Wall clock in the viewer's own locale and zone, e.g. "15 Oct, 20:18". */
export function wall(s: string | null | undefined): string {
  if (!s) return "—";
  return parseTs(s).toLocaleString(undefined, {
    day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
  });
}

/** For <input type="datetime-local">: a local wall time, and back to an ISO instant. */
export function toLocalInput(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function fromLocalInput(v: string): string {
  return new Date(v).toISOString();
}
