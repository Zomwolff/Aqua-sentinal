import React, { useEffect, useState } from "react";
import { SAR_ARTIFACT_FILENAMES, sarArtifactUrls, type SarArtifactKey } from "../lib/api";
import { SARArtifactPreview } from "./SARArtifactPreview";
import "./SARTaskingPipeline.css";

type SARStep = "sar_tasking" | "sar_fetching" | "sar_despeckling" | "sar_cfar" | "sar_morphology" | "sar_polygonize" | "sar_complete";

const steps: SARStep[] = [
  "sar_tasking",
  "sar_fetching",
  "sar_despeckling",
  "sar_cfar",
  "sar_morphology",
  "sar_polygonize",
  "sar_complete"
];

const stepLabels: Record<SARStep, string> = {
  sar_tasking: "Acquiring SAR",
  sar_fetching: "Downloading Scene",
  sar_despeckling: "Despeckling Filter",
  sar_cfar: "CFAR Object Detection",
  sar_morphology: "Morphological Cleaning",
  sar_polygonize: "Polygon Extraction",
  sar_complete: "Processing Complete"
};

interface Props {
  liveEvent?: any;
  historicalSceneId?: string | null;
}

export function SARTaskingPipeline({ liveEvent, historicalSceneId }: Props) {
  const [activeScene, setActiveScene] = useState<string | null>(null);
  const [currentStep, setCurrentStep] = useState<SARStep | null>(null);
  const [images, setImages] = useState<Partial<Record<SarArtifactKey, string>>>({});
  
  // Track open state
  const [isOpen, setIsOpen] = useState(false);

  useEffect(() => {
    if (historicalSceneId) {
      setIsOpen(true);
      setActiveScene(historicalSceneId);
      setCurrentStep("sar_complete");
      setImages(sarArtifactUrls(historicalSceneId));
    }
  }, [historicalSceneId]);

  useEffect(() => {
    if (!liveEvent || liveEvent.type !== "sar_tasking") return;

    const data = liveEvent.data;
    if (data && data.scene_id) {
      setIsOpen(true);
      setActiveScene(data.scene_id);
      setCurrentStep(data.step);

      if (data.step === "sar_complete") {
        setImages(sarArtifactUrls(data.scene_id));
      } else if (data.step === "sar_tasking") {
        // reset images on new tasking
        setImages({});
      }
    }
  }, [liveEvent]);

  if (!isOpen) return null;

  const currentIndex = currentStep ? steps.indexOf(currentStep) : -1;

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
        {activeScene && (
          <div className="scene-id">Scene ID: {activeScene.substring(0, 8)}</div>
        )}
        
        <div className="steps-container">
          {steps.map((step, index) => {
            const isCompleted = index < currentIndex || currentStep === "sar_complete";
            const isActive = index === currentIndex && currentStep !== "sar_complete";
            
            return (
              <div key={step} className={`step-item ${isCompleted ? 'completed' : isActive ? 'active' : 'pending'}`}>
                <div className="step-circle"></div>
                <span className="step-label">{stepLabels[step]}</span>
              </div>
            );
          })}
        </div>

        {currentStep === "sar_complete" && Object.keys(images).length > 0 && (
          <div className="artifacts-grid">
            <div className="artifact-item">
              <div className="artifact-label">Raw SAR</div>
              <SARArtifactPreview url={images.raw} alt="Raw SAR" filename={SAR_ARTIFACT_FILENAMES.raw} />
            </div>
            <div className="artifact-item">
              <div className="artifact-label">Despeckled</div>
              <SARArtifactPreview url={images.filtered} alt="Despeckled" filename={SAR_ARTIFACT_FILENAMES.filtered} />
            </div>
            <div className="artifact-item">
              <div className="artifact-label">CFAR Mask</div>
              <SARArtifactPreview url={images.cfar} alt="CFAR" filename={SAR_ARTIFACT_FILENAMES.cfar} />
            </div>
            <div className="artifact-item">
              <div className="artifact-label">Final Polygon</div>
              <SARArtifactPreview url={images.final} alt="Polygon" filename={SAR_ARTIFACT_FILENAMES.final} />
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
