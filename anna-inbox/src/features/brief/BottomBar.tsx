import { useApp } from "../../app/AppContext";
import { scanProgressLabel, SCAN_STEPS } from "./runHelpers";
import { visibleCards } from "./cardHelpers";

export function BottomBar() {
  const { state, actions } = useApp();
  const cards = visibleCards(state.cards);
  const samplingResult = state.samplingTestResult;
  const samplingDiagnostics = samplingResult?.diagnostics || {};
  const samplingTitle = samplingResult
    ? `${samplingResult.success ? "Sampling OK" : samplingResult.error || "Sampling failed"} | token=${samplingDiagnostics.context_has_sampling_token ? "yes" : "no"} | invoke=${samplingDiagnostics.context_has_invoke_id ? "yes" : "no"}`
    : "Run test_anna_sampling";
  if (state.isScanning) {
    const step = SCAN_STEPS[Math.min(state.scanStepIndex, SCAN_STEPS.length - 1)] || SCAN_STEPS[0];
    return (
      <footer className="bottom-bar">
        <div>
          <p className="bar-title">Anna is scanning your inbox.</p>
          <p className="bar-copy">{step.title} · {scanProgressLabel(state.scanStage, state.scanProgress) || "Starting..."}</p>
        </div>
        <div className="bar-actions"><button className="soft-btn" disabled>Scanning...</button></div>
      </footer>
    );
  }
  return (
    <footer className="bottom-bar">
      <div>
        <p className="bar-title">{cards.length} active attention card{cards.length === 1 ? "" : "s"}.</p>
        <p className="bar-copy">{state.loading ? "Connecting to Anna runtime..." : "Ready."}</p>
      </div>
      <div className="bar-actions">
        <div className="debug-provider-controls" aria-label="Debug providers">
          <div className="debug-provider-group" role="radiogroup" aria-label="Storage provider">
            <button className={`debug-provider-btn ${state.storageProvider === "aps" ? "is-active" : ""}`} onClick={() => actions.setProvider("storage", "aps")}>APS</button>
            <button className={`debug-provider-btn ${state.storageProvider === "local" ? "is-active" : ""}`} onClick={() => actions.setProvider("storage", "local")}>Local</button>
          </div>
          <button
            className={`debug-provider-btn sampling-test-btn ${samplingResult ? (samplingResult.success ? "is-ok" : "is-error") : ""}`}
            disabled={state.samplingTestRunning || !state.runtime.connected}
            title={samplingTitle}
            onClick={() => void actions.testAnnaSampling()}
          >
            {state.samplingTestRunning ? "Testing..." : samplingResult?.success ? "LLM OK" : samplingResult ? "LLM Fail" : "Test LLM"}
          </button>
          {samplingResult ? (
            <span className={`sampling-test-pill ${samplingResult.success ? "is-ok" : "is-error"}`} title={samplingTitle}>
              {samplingDiagnostics.context_has_sampling_token ? "token yes" : "token no"}
            </span>
          ) : null}
        </div>
        <button className="soft-btn" onClick={() => actions.setDrawer("scanPlan", true)}>Next Scan</button>
        <button className="soft-btn" onClick={() => actions.setView("ask")}>Ask</button>
        <button className="primary-btn" disabled={state.isScanning || !state.runtime.connected} onClick={() => void actions.startScan("manual")}>Scan now</button>
      </div>
    </footer>
  );
}
