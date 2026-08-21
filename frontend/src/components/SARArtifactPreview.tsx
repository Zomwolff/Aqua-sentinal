import { useEffect, useState } from "react";
import "./SARArtifactPreview.css";

interface Props {
  url?: string;
  alt: string;
  filename: string;
}

/** A normal DOM image with an explicit, recoverable unavailable state. */
export function SARArtifactPreview({ url, alt, filename }: Props) {
  const [attempt, setAttempt] = useState(0);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    setAttempt(0);
    setFailed(false);
  }, [url]);

  if (!url || failed) {
    return (
      <div className="sar-artifact-unavailable" role="status">
        <span>Artifact unavailable</span>
        <small>{filename}</small>
        {url && (
          <button type="button" onClick={() => { setFailed(false); setAttempt((value) => value + 1); }}>
            Retry
          </button>
        )}
      </div>
    );
  }

  const imageUrl = attempt === 0 ? url : `${url}${url.includes("?") ? "&" : "?"}retry=${attempt}`;

  return <img src={imageUrl} alt={alt} onError={() => setFailed(true)} />;
}
