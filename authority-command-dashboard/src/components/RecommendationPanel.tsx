import { useState } from "react";
import { acknowledgeRecommendation } from "../api/gateway";
import type { Recommendation } from "../types";

export function RecommendationPanel({ recommendations, onChanged }: { recommendations: Recommendation[]; onChanged: () => void }) {
  const [working, setWorking] = useState<number | null>(null); const [error, setError] = useState<string | null>(null);
  const acknowledge = async (id: number) => { setWorking(id); setError(null); try { await acknowledgeRecommendation(id); onChanged(); } catch { setError("Unable to acknowledge recommendation. The Gateway may be unavailable."); } finally { setWorking(null); } };
  return <section className="recommendations panel"><div className="eyebrow">RESPONSE DECISION ENGINE</div><h2>RECOMMENDED ACTIONS</h2>{error && <p className="error">{error}</p>}{recommendations.length === 0 ? <p className="empty">RECOMMENDATIONS PENDING</p> : recommendations.map((rec, index) => <article className="recommendation" key={rec.id}><div className="recommendation-top"><span className={`priority ${rec.priority.toLowerCase()}`}>PRIORITY {index + 1} · {rec.priority}</span><span className="status">{rec.status}</span></div><p>{rec.recommendation}</p>{rec.rationale && <small>{rec.rationale}</small>}<div className="action-buttons">{rec.status === "acknowledged" ? <span className="acknowledged">ACKNOWLEDGED</span> : <button disabled={working === rec.id} onClick={() => acknowledge(rec.id)}>{working === rec.id ? "ACKNOWLEDGING…" : "ACKNOWLEDGE"}</button>}<button className="unsupported" title="The current backend has no recommendation assignment endpoint" disabled>ASSIGN UNAVAILABLE</button></div></article>)}</section>;
}
