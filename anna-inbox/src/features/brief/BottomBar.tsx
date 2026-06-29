import { useApp } from "../../app/AppContext";
import { scanProgressLabel, SCAN_STEPS } from "./runHelpers";
import { visibleCards } from "./cardHelpers";

export function BottomBar() {
  const { state, actions } = useApp();
  if (state.view === "ask" || (state.view === "start" && state.originalOpen)) return null;
  const cards = visibleCards(state.cards);
  const scanInProgress = state.isScanning || state.isPreparingScan;
  if (scanInProgress) {
    const step = SCAN_STEPS[Math.min(state.scanStepIndex, SCAN_STEPS.length - 1)] || SCAN_STEPS[0];
    return (
      <footer className="bottom-bar">
        <div className="bar-status">
          <span className="bar-status-dot is-scanning" />
          <div>
          <p className="bar-title">Anna is scanning your inbox.</p>
          <p className="bar-copy">{step.title} · {scanProgressLabel(state.scanStage, state.scanProgress) || "Starting..."}</p>
          </div>
        </div>
        <div className="bar-actions">
          <button className="soft-btn" disabled>Scanning...</button>
          <button className="soft-btn scan-settings-btn" disabled>Scan Settings</button>
        </div>
      </footer>
    );
  }
  const hasScanned = Number(state.scanState?.total_scans || 0) > 0 || state.cards.length > 0;
  const scanButtonLabel = hasScanned ? "Continue Scan" : "Scan now";
  return (
    <footer className="bottom-bar">
      <div className="bar-status">
        <span className="bar-status-dot" />
        <div>
        <p className="bar-title">{cards.length} active item{cards.length === 1 ? "" : "s"}</p>
        <p className="bar-copy">{state.loading ? "Connecting to Anna runtime..." : "Ready."}</p>
        </div>
      </div>
      <div className="bar-actions">
        <button className="primary-btn" disabled={scanInProgress || !state.runtime.connected} onClick={() => void actions.startScan("manual")}>{scanButtonLabel}</button>
        <button className="soft-btn scan-settings-btn" disabled={scanInProgress || !state.runtime.connected} onClick={actions.openSourcesWithConfig}>Scan Settings</button>
      </div>
    </footer>
  );
}
