import { useApp } from "../../app/AppContext";
import { scanProgressLabel, SCAN_STEPS } from "./runHelpers";
import { visibleCards } from "./cardHelpers";

export function BottomBar() {
  const { state, actions } = useApp();
  const cards = visibleCards(state.cards);
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
        <button className="primary-btn" disabled={state.isScanning || !state.runtime.connected} onClick={() => void actions.startScan("manual")}>Scan now</button>
      </div>
    </footer>
  );
}
