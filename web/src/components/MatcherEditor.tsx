import type { Matcher } from "../api";
import { Icon } from "./Icon";

/** Alertmanager-style matcher rows, shared by silences, routes and inhibitions so the three
 *  never disagree about what `=~` means. */
export const OPS: { value: string; label: string; isRegex: boolean; isEqual: boolean }[] = [
  { value: "=", label: "=", isRegex: false, isEqual: true },
  { value: "!=", label: "!=", isRegex: false, isEqual: false },
  { value: "=~", label: "=~ (regex)", isRegex: true, isEqual: true },
  { value: "!~", label: "!~ (regex)", isRegex: true, isEqual: false },
];

export const opOf = (m: Matcher) => OPS.find((o) => o.isRegex === m.isRegex && o.isEqual === m.isEqual)!.value;

export function matcherText(m: Matcher) {
  return `${m.name}${opOf(m)}"${m.value}"`;
}

export const blankMatcher = (): Matcher => ({ name: "", value: "", isRegex: false, isEqual: true });

export function MatcherEditor({ value, onChange, allowEmpty = false, emptyHint }: {
  value: Matcher[]; onChange: (m: Matcher[]) => void; allowEmpty?: boolean; emptyHint?: string;
}) {
  const update = (i: number, patch: Partial<Matcher>) =>
    onChange(value.map((m, j) => (j === i ? { ...m, ...patch } : m)));
  return (
    <div className="matcher-editor">
      {value.length === 0 && emptyHint && <p className="muted small">{emptyHint}</p>}
      {value.map((m, i) => (
        <div key={i} className="matcher-row">
          <input aria-label="Label name" placeholder="label" value={m.name}
            onChange={(e) => update(i, { name: e.target.value })} />
          <select aria-label="Operator" value={opOf(m)} onChange={(e) => {
            const op = OPS.find((o) => o.value === e.target.value)!;
            update(i, { isRegex: op.isRegex, isEqual: op.isEqual });
          }}>
            {OPS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
          </select>
          <input aria-label="Value" placeholder="value" value={m.value}
            onChange={(e) => update(i, { value: e.target.value })} />
          <button type="button" className="btn-ghost btn-sm" aria-label="Remove matcher"
            disabled={!allowEmpty && value.length === 1}
            onClick={() => onChange(value.filter((_, j) => j !== i))}>
            <Icon name="x" size={14} />
          </button>
        </div>
      ))}
      <div>
        <button type="button" className="btn-ghost btn-sm" onClick={() => onChange([...value, blankMatcher()])}>
          <Icon name="plus" size={14} /> Add matcher
        </button>
      </div>
    </div>
  );
}

/** Rows with no label name are dropped before sending — a half-typed row is not a matcher. */
export const usable = (ms: Matcher[]) => ms.filter((m) => m.name.trim());
