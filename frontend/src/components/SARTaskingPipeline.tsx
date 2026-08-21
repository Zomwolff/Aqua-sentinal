import React, { useEffect, useState } from "react";
import "./SARTaskingPipeline.css";
import { ARTIFACTS_BASE } from "../lib/api";

const steps = ["sar_tasking", "sar_fetching", "sar_despeckling", "sar_cfar", "sar_morphology", "sar_polygonize", "sar_complete"];
const stepLabels: Record<string, string> = {
  sar_tasking: "Acquiring SAR",
  sar_fetching: "Downloading Scene",
  sar_despeckling: "Despeckling Filter",
  sar_cfar: "CFAR Object Detection",
  sar_morphology: "Morphological Cleaning",
  sar_polygonize: "Polygon Extraction",
  sar_complete: "Spill Processing Complete",
};

// What each pipeline stage physically does — shown as sub-text so the panel
// explains itself instead of just showing a spinner label.
const stepDetails: Record<string, string> = {
  sar_tasking: "Sentinel-1 scene selected for the vessel's last known position",
  sar_fetching: "Downloading GeoTIFF from Google Earth Engine",
  sar_despeckling: "Lee filter in linear-power domain removes speckle noise",
  sar_cfar: "Constant-false-alarm-rate test finds dark slick pixels",
  sar_morphology: "Opening/closing cleans the candidate mask",
  sar_polygonize: "Connected components become spill candidate polygons",
  sar_complete: "Candidates persisted and fused into incidents",
};

type TaskingInfo = {
  status?: string;
  requested_at?: string;
  completed_at?: string;
  scene_id?: string | null;
  risk_tier?: string;
  risk_score?: number;
  reason?: any;
};

export function SARTaskingPipeline({ liveEvent, historicalSceneId, tasking }: { liveEvent?: any; historicalSceneId?: string | null; tasking?: TaskingInfo | null }) {
  const [isOpen, setIsOpen] = useState(false);
  const [activeScene, setActiveScene] = useState<string | null>(null);
  const [currentStep, setCurrentStep] = useState<string | null>(null);
  const [images, setImages] = useState<Record<string, string>>({});

  // Historical scene (incident detail page) opens the pipeline in review mode.
  useEffect(() => {
    if (!historicalSceneId) return;
    setIsOpen(true);
    setActiveScene(historicalSceneId);
    setCurrentStep("sar_complete");
    const baseUrl = `${ARTIFACTS_BASE}/` + historicalSceneId.replace(/\//g, "_");
    setImages({
      raw: baseUrl + "/raw_image.png",
      filtered: baseUrl + "/filtered_image.png",
      cfar: baseUrl + "/bright_target_mask.png",
      final: baseUrl + "/cleaned_mask.png",
    });
  }, [historicalSceneId]);

  useEffect(() => {
    if (!liveEvent || liveEvent.type !== "sar_tasking") return;

    const data = liveEvent.data;
    if (data && data.scene_id) {
      setIsOpen(true);
      setActiveScene(data.scene_id);
      setCurrentStep(data.step);

      if (data.step === "sar_complete") {
        // Fetch images
        const baseUrl = `${ARTIFACTS_BASE}/` + data.scene_id;
        setImages({
          raw: baseUrl + "/raw_image.png",
          filtered: baseUrl + "/filtered_image.png",
          cfar: baseUrl + "/bright_target_mask.png",
          final: baseUrl + "/cleaned_mask.png"
        });
      } else if (data.step === "sar_tasking") {
        // reset images on new tasking
        setImages({});
      }
    }
  }, [liveEvent]);

  if (!isOpen) return null;

  const currentIndex = currentStep ? steps.indexOf(currentStep) : -1;

  // Derive honest step states when there is no live event stream:
  // fulfilled -> every stage done; pending -> acquisition stage active.
  const isFulfilled = tasking?.status === "fulfilled" || currentStep === "sar_complete";
  const isFailed = tasking?.status === "failed";
  const isPendingTasking = tasking?.status === "pending" && currentIndex < 0;

  const reasonFactors: { factor: string; contribution?: number; description?: string; value?: number }[] =
    Array.isArray(tasking?.reason?.contributing_factors) ? tasking!.reason.contributing_factors : [];

  const elapsedText = (() => {
    if (!tasking?.requested_at) return "";
    const started = new Date(tasking.requested_at).getTime();
    if (Number.isNaN(started)) return "";
    const end = tasking.completed_at ? new Date(tasking.completed_at).getTime() : Date.now();
    const mins = Math.max(0, Math.round((end - started) / 60000));
    return tasking.completed_at ? `completed in ${mins} min` : `elapsed ${mins} min`;
  })();

  return (
    <div className="sar-pipeline-panel">
      <div className="panel-header">
        <h3>
          <span className="live-indicator"></span>
          SAR Processing Pipeline
        </h3>
        <button onClick={() => setIsOpen(false)} className="close-btn">✕</button>
      </div>

      <div className="panel-content">
        {/* WHY this scene was tasked — the risk-engine reasoning, always shown */}
        {reasonFactors.length > 0 && (
          <div className="tasking-reason">
            <div className="reason-title">
              Why SAR was tasked
              {tasking?.risk_tier && (
                <span className={`severity-pill ${String(tasking.risk_tier).toLowerCase()}`}>
                  {String(tasking.risk_tier)}
                  {tasking?.risk_score != null ? ` · ${Math.round(Number(tasking.risk_score))}/100` : ""}
                </span>
              )}
            </div>
            <ul>
              {reasonFactors.map((f: any, i: number) => (
                <li key={i}>
                  <b>{String(f.factor || "").replace(/_/g, " ")}</b>
                  {f.contribution != null && <span className="reason-weight"> · contribution {Number(f.contribution).toFixed(1)}</span>}
                  {f.description && <p>{String(f.description)}</p>}
                </li>
              ))}
            </ul>
          </div>
        )}

        {activeScene && (
          <div className="scene-id">Scene ID: {activeScene.substring(0, 8)}</div>
        )}
        {(tasking?.requested_at || elapsedText) && (
          <div className="scene-id tasking-status">
            Tasking status:{" "}
            <b className={isFailed ? "failed" : isPendingTasking ? "pending" : "ok"}>
              {isFailed ? "FAILED" : isFulfilled ? "FULFILLED" : "PENDING"}
            </b>
            {tasking?.requested_at && <> · requested {new Date(tasking.requested_at).toLocaleTimeString("en-US", { hour12: false, timeZone: "UTC" })} UTC</>}
            {elapsedText && <> · {elapsedText}</>}
          </div>
        )}

        {isPendingTasking && !activeScene && (
          <div className="scene-id">
            Awaiting satellite acquisition — Sentinel-1 revisit windows are orbit-dependent; the scene id appears here the moment processing starts.
          </div>
        )}

        <div className="steps-container">
          {steps.map((step, index) => {
            const isCompleted = index < currentIndex || currentStep === "sar_complete";
            const isActive = index === currentIndex && currentStep !== "sar_complete";

            return (
              <div key={step} className={`step-item ${isCompleted ? 'completed' : isActive ? 'active' : 'pending'}`}>
                <div className="step-circle"></div>
                <div className="step-copy">
                  <span className="step-label">{stepLabels[step]}</span>
                  <small className="step-detail">{stepDetails[step]}</small>
                </div>
              </div>
            );
          })}
        </div>

        {currentStep === "sar_complete" && Object.keys(images).length > 0 && (
          <div className="artifacts-grid">
            <div className="artifact-item">
              <div className="artifact-label">Raw SAR</div>
              <img src={images.raw} alt="Raw SAR" onError={(e) => (e.currentTarget.style.display = 'none')} />
            </div>
            <div className="artifact-item">
              <div className="artifact-label">Despeckled</div>
              <img src={images.filtered} alt="Despeckled" onError={(e) => (e.currentTarget.style.display = 'none')} />
            </div>
            <div className="artifact-item">
              <div className="artifact-label">CFAR Mask</div>
              <img src={images.cfar} alt="CFAR" onError={(e) => (e.currentTarget.style.display = 'none')} />
            </div>
            <div className="artifact-item">
              <div className="artifact-label">Final Polygon</div>
              <img src={images.final} alt="Polygon" onError={(e) => (e.currentTarget.style.display = 'none')} />
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
