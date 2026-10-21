import qrcode from "qrcode-generator";
import { useMemo, useState } from "react";
import { devicesApi, type PairCode } from "../api";
import { useNow, usePoll } from "../app";
import { Icon } from "../components/Icon";
import { ErrorNotice } from "../components/ui";
import { relative } from "../time";

/** Pair a phone: the code is minted here, where you are already signed in, so the app never
 *  needs its own Google sign-in. */
export function Phones() {
  const now = useNow(15000);
  const { data, error, refresh } = usePoll(() => devicesApi.list(), [], 20000);
  const [code, setCode] = useState<PairCode | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const expired = code ? new Date(code.expires_at).getTime() <= now : false;
  const active = (data ?? []).filter((d) => !d.revoked);
  // The QR holds the shorthop:// link; a phone camera opens the app with server + code filled
  // in. Rendered as a GIF data URL in an <img>, so nothing is ever injected as markup.
  const qr = useMemo(() => {
    if (!code) return null;
    const q = qrcode(0, "M");
    q.addData(code.link);
    q.make();
    return q.createDataURL(5, 2);
  }, [code]);

  return (
    <section className="card pad">
      <div className="section-head">
        <h3 className="sub">Phones</h3>
        <button className="btn-primary btn-sm" onClick={async () => {
          setErr(null);
          try { setCode(await devicesApi.pairCode()); } catch (e) { setErr((e as Error).message); }
        }}><Icon name="phone" size={14} /> Add a phone</button>
      </div>
      <p className="muted small">The Shorthop app pages you on your phone. Pair it with a one-time code from here — the phone acts as you, can answer pages and take shifts, and never gets admin or configuration rights.</p>
      <ErrorNotice>{error || err}</ErrorNotice>
      {code && !expired && (
        <div className="pair-box">
          {qr && <img className="pair-qr" src={qr} alt="QR code that pairs the Shorthop app" />}
          <div>
            <div className="pair-code" aria-label="Pairing code">{code.code}</div>
            <p className="small">Scan with the phone's camera, or open the Shorthop app and type the code. It works once and expires {relative(code.expires_at, now)}.</p>
          </div>
        </div>
      )}
      {code && expired && <p className="notice notice--warn">That code expired — make a new one.</p>}
      {active.length === 0 ? <p className="muted small">No phones paired.</p> : (
        <ul className="shift-list">
          {active.map((d) => (
            <li key={d.id}>
              <span><strong>{d.name}</strong> <span className="muted small">{d.platform} · paired {relative(d.created_at, now)}{d.last_seen_at ? ` · seen ${relative(d.last_seen_at, now)}` : ""}</span>
                {!d.has_push && <div className="small notice--inline-warn">No push token yet — open the app and allow notifications.</div>}
                {d.has_push && d.relay_status === "refused" && <div className="small notice--inline-warn">The push relay refused this phone: {d.relay_message} Email still pages you.</div>}
                {d.has_push && d.relay_status === "error" && <div className="small muted">Push relay: {d.relay_message}. Tried again on the next page.</div>}
                {d.has_push && d.relay_status === "registered" && d.relay_signs_out_at && <div className="small notice--inline-warn">Unused for a month — it stops receiving pages {relative(d.relay_signs_out_at, now)} unless the Shorthop app is opened on it. Unpair it if it is no longer used.</div>}
                {d.relay_status === "signed_out" && <div className="small notice--inline-warn">Signed out of the push relay after going unused — no pages reach it until the Shorthop app is opened on it again.</div>}</span>
              <button className="btn-ghost btn-sm btn-danger" onClick={async () => {
                if (!window.confirm(`Unpair ${d.name}? It stops receiving pages immediately.`)) return;
                try { await devicesApi.revoke(d.id); refresh(); } catch (e) { setErr((e as Error).message); }
              }}>Unpair</button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
