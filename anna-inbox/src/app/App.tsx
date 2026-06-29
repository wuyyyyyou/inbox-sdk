import { useEffect, type ReactNode } from "react";
import { AskView } from "../features/ask/AskView";
import { BriefView } from "../features/brief/BriefView";
import { BottomBar } from "../features/brief/BottomBar";
import { ScanningView } from "../features/brief/ScanningView";
import { Drawers } from "../features/drawers/Drawers";
import { HandleView } from "../features/handle/HandleView";
import { visibleCards } from "../features/brief/cardHelpers";
import { AppContext } from "./AppContext";
import { useAppController } from "./useAppController";

type UtilityIconName = "sources" | "memory" | "history" | "minimize";

function UtilityIcon({ name }: { name: UtilityIconName }) {
  const paths: Record<UtilityIconName, ReactNode> = {
    sources: <><path d="M4 5.5h12M4 10h12M4 14.5h8" /><circle cx="2.5" cy="5.5" r=".75" /><circle cx="2.5" cy="10" r=".75" /><circle cx="2.5" cy="14.5" r=".75" /></>,
    memory: <><rect x="4" y="4" width="12" height="12" rx="3" /><path d="M7 1.5v2.5M10 1.5v2.5M13 1.5v2.5M7 16v2.5M10 16v2.5M13 16v2.5M1.5 7h2.5M1.5 10h2.5M1.5 13h2.5M16 7h2.5M16 10h2.5M16 13h2.5M8 8h4v4H8z" /></>,
    history: <><path d="M3.3 6.2A7 7 0 1 1 3 12" /><path d="M3.3 2.8v3.4H6.7M10 6v4l2.7 1.6" /></>,
    minimize: <path d="M4 10h12" />,
  };
  return <svg className="utility-icon" viewBox="0 0 20 20" aria-hidden="true">{paths[name]}</svg>;
}

export function App() {
  const controller = useAppController();
  const { state, actions, toast, initialize } = controller;
  const showScanningView = state.view === "start" && (state.isPreparingScan || (state.isScanning && state.cards.length === 0));
  const isDetailView = state.view === "start" && state.originalOpen;

  useEffect(() => {
    void initialize();
    // Initial boot should run once; subsequent state changes are driven by actions.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const mailboxLabel = state.selectedMailboxes.length > 1 ? `${state.selectedMailboxes.length} mailboxes` : state.selectedMailboxes[0] || state.mailbox;
  const subtitle = state.isScanning
    ? `Scanning ${mailboxLabel}`
    : `${visibleCards(state.cards).length} active · ${mailboxLabel}`;

  const llmStatusText = {
    unknown: "unknown",
    checking: "checking",
    connected: "connected",
    unavailable: "unavailable",
    error: "error",
  }[state.llmStatus.status] || "unknown";

  return (
    <AppContext.Provider value={{ state, actions }}>
      <div className="desktop">
        <div className="ambient-window" aria-hidden="true">
          <div className="ambient-lines"><span /><span /><span /></div>
        </div>

        <main className={`app-shell ${state.minimized ? "is-minimized" : ""}`} id="appShell">
          <header className="topbar">
            <div className="brand">
              <div className={`anna-mark anna-avatar ${state.isScanning ? "is-thinking" : ""}`} aria-label="Anna avatar" title="Anna">A</div>
              <div className="brand-copy">
                <div className="brand-title">Anna Inbox</div>
                <div className="brand-meta">
                  <span className="brand-subtitle">{subtitle}</span>
                  <span className={`connection-status ${state.runtime.connected ? "is-live" : "is-offline"}`}>
                    <span className="connection-dot" />
                    {state.runtime.connected ? "Live" : "Offline"}
                  </span>
                  {state.runtime.connected ? (
                    <span className={`llm-status-chip is-${state.llmStatus.status}`} title={state.llmStatus.message || "Anna LLM status"}>
                      <span className="llm-status-dot" />
                      LLM {llmStatusText}
                    </span>
                  ) : null}
                </div>
              </div>
            </div>
            <div className="top-actions">
              <div className="top-segment" aria-label="Main view">
                <button className={`ghost-btn top-segment-btn is-brief ${state.view === "start" ? "is-active" : ""}`} title="Today's brief" onClick={() => actions.setView("start")}>Brief</button>
                <button className={`ghost-btn top-segment-btn is-ask ${state.view === "ask" ? "is-active" : ""}`} title="Run a custom scan" onClick={() => actions.setView("ask")}>Ask</button>
              </div>
              <button className="icon-btn" title="Sources" aria-label="Sources" onClick={() => actions.setDrawer("sources", true)}><UtilityIcon name="sources" /></button>
              <button className="icon-btn" title="Memory" aria-label="Memory" onClick={() => actions.setDrawer("memory", true)}><UtilityIcon name="memory" /></button>
              <button className="icon-btn" title="Briefing history" aria-label="Briefing history" onClick={() => actions.setDrawer("history", true)}><UtilityIcon name="history" /></button>
              <button className="icon-btn" title="Minimize" aria-label="Minimize" onClick={() => actions.minimize(true)}><UtilityIcon name="minimize" /></button>
            </div>
          </header>

          <section className={`app-content ${isDetailView ? "is-detail-view" : ""} ${state.view === "ask" ? "is-ask-view" : ""}`} id="appContent" style={state.snoozeReasonsKey ? { overflow: "hidden" } : undefined}>
            {state.view === "start" && state.originalOpen ? <HandleView /> : null}
            {state.view === "start" && !state.originalOpen && !showScanningView ? <BriefView /> : null}
            {showScanningView ? <ScanningView /> : null}
            {state.view === "ask" ? <AskView /> : null}
          </section>
          <BottomBar />
          <Drawers />
          <div className={`toast ${toast ? "is-visible" : ""}`}>{toast}</div>
        </main>

        <button className={`minimized-pill ${state.minimized ? "is-visible" : ""}`} onClick={() => actions.minimize(false)}>
          <span className="mini-mark">A</span>
          <span>Anna Inbox</span>
        </button>
      </div>
    </AppContext.Provider>
  );
}
