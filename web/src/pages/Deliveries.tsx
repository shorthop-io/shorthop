import { useState } from "react";
import { paging } from "../api";
import { useNow, usePoll } from "../app";
import { ErrorNotice } from "../components/ui";
import { DeliveryTable } from "./Incidents";

export function Deliveries() {
  const now = useNow();
  const [status, setStatus] = useState("");
  const { data, error } = usePoll(() => paging.listNotifications(status || undefined), [status], 15000);
  const failed = (data ?? []).filter((n) => n.status === "failed").length;
  return (
    <div className="page">
      <div className="section-head">
        <h2>Delivery log</h2>
        <div className="seg">
          {[["", "All"], ["failed", "Failed"], ["pending", "Pending"], ["sent", "Sent"]].map(([v, l]) => (
            <button key={v} className={`seg-btn${status === v ? " active" : ""}`} onClick={() => setStatus(v)}>{l}</button>
          ))}
        </div>
      </div>
      <p className="glance">Every page Shorthop tried to send, where, and whether it arrived. A failure is retried with backoff, then kept here with the reason — "we could not reach you" is the most important line in this list.</p>
      {status === "" && failed > 0 && <p className="notice notice--danger">{failed} notification{failed === 1 ? "" : "s"} failed for good in this list.</p>}
      <ErrorNotice>{error}</ErrorNotice>
      {data && <DeliveryTable rows={data} now={now} showIncident />}
    </div>
  );
}
