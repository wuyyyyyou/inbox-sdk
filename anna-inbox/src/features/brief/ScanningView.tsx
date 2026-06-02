import { useApp } from "../../app/AppContext";
import { scanProgressLabel, SCAN_STEPS } from "./runHelpers";

export function ScanningView() {
  const { state } = useApp();
  const stepIndex = Math.min(state.scanStepIndex, SCAN_STEPS.length - 1);
  const currentStep = SCAN_STEPS[stepIndex] || SCAN_STEPS[0];
  return (
    <div className="scanning-layout">
      <section className="scanning-panel" aria-label="Anna is scanning">
        <div>
          <div className="scanning-orb">A</div>
          <h1 className="scanning-title">{currentStep.title}</h1>
          <p className="scanning-copy">Anna is taking a first pass through your mailbox and turning the inbox into a short action brief.</p>
        </div>
        <div className="scan-flow">
          {SCAN_STEPS.map((step, index) => (
            <div key={step.title} className={`scan-flow-step ${index < stepIndex ? "is-done" : ""} ${index === stepIndex ? "is-active" : ""}`}>
              <span className="scan-flow-dot">{index < stepIndex ? "✓" : ""}</span>
              <span>{step.title}</span>
            </div>
          ))}
        </div>
        <div className="scanning-microcopy">{currentStep.microcopy}</div>
        <div className="scanning-count">{scanProgressLabel(state.scanStage, state.scanProgress) || "Starting..."}</div>
      </section>
    </div>
  );
}
