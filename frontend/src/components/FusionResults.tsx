import React from 'react';

export function FusionResults({ result }: { result: any }) {
  if (!result) return null;
  return <section className="idp-card">
    <h3>Satellite Detection · {result.mode}</h3>
    <p>{result.oil_spill_detected ? 'Possible oil spill detected' : 'No oil pixels detected'}</p>
    <dl>
      <dt>Scene ID</dt><dd>{result.scene_id}</dd>
      <dt>Acquisition time</dt><dd>{result.acquisition_time ? new Date(result.acquisition_time).toLocaleString() : 'Unavailable'} · {result.acquisition_time_source}</dd>
      <dt>Total scene detection area</dt><dd>{result.area_km2 == null ? 'Unavailable without Sentinel-1 georeferencing' : `${Number(result.area_km2).toFixed(4)} km²`}</dd>
      <dt>Georeference</dt><dd>{result.georeference_source || 'None (optical evidence only)'}</dd>
      <dt>Model threshold</dt><dd>{result.threshold}</dd>
    </dl>
    {result.weights && <p>SAR {Math.round(result.weights.sar * 100)}% · EO {Math.round(result.weights.eo * 100)}%</p>}
    <div className="idp-sar-grid">
      {([['Sentinel-1', 's1'], ['Sentinel-2', 's2'], ['Final detection mask', 'final'], ['Interpreted oil-slick view', 'visualization']] as const).filter(([,key]) => result.artifacts?.[key]).map(([label,key]) =>
        <div className={`sar-artifact ${key === 'visualization' ? 'sar-artifact-interpretation' : ''}`} key={key}><div className="sar-label">{label}{key === 'visualization' && <span>Illustrative colourisation</span>}</div><div className="sar-img-wrapper"><img src={result.artifacts[key]} alt={label} /></div>{key === 'visualization' && <small className="sar-interpretation-note">Ocean colour and dark slick styling are derived for operator review. Detection geometry comes from the final model mask.</small>}</div>)}
    </div>
    {result.artifacts?.metadata && <a href={result.artifacts.metadata} target="_blank" rel="noreferrer">View fusion metadata</a>}
  </section>;
}
