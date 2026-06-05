import { useEffect } from "react";
import { AskView } from "../features/ask/AskView";
import { BriefView } from "../features/brief/BriefView";
import { BottomBar } from "../features/brief/BottomBar";
import { ScanningView } from "../features/brief/ScanningView";
import { Drawers } from "../features/drawers/Drawers";
import { HandleView } from "../features/handle/HandleView";
import { visibleCards } from "../features/brief/cardHelpers";
import { AppContext } from "./AppContext";
import { useAppController } from "./useAppController";

export function App() {
  const controller = useAppController();
  const { state, actions, toast, initialize } = controller;

  useEffect(() => {
    void initialize();
    // Initial boot should run once; subsequent state changes are driven by actions.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const planLabel = state.scanPlan && state.scanPlan.time_range !== "auto"
    ? { last_24h: "24h", last_7d: "7d", unread_backlog: "backlog", since_last: "since last" }[state.scanPlan.time_range || ""] || "custom"
    : "auto";
  const scheduleLabel = state.scanPlan && state.scanPlan.schedule !== "manual"
    ? { every_morning: "morning", every_afternoon: "afternoon", twice_daily: "2x/day", workdays: "workdays" }[state.scanPlan.schedule || ""] || "scheduled"
    : "";
  const nextInfo = scheduleLabel ? ` · Next: ${scheduleLabel}` : "";
  const mailboxLabel = state.selectedMailboxes.length > 1 ? `${state.selectedMailboxes.length} mailboxes` : state.selectedMailboxes[0] || state.mailbox;
  const subtitle = state.isScanning
    ? `Scanning ${mailboxLabel}`
    : `${visibleCards(state.cards).length} active · ${planLabel}${nextInfo} · ${mailboxLabel}`;

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
              <div>
                <div className="brand-title">Anna Inbox</div>
                <div className="brand-subtitle">{subtitle}</div>
                <div className={`connection-status ${state.runtime.connected ? "is-live" : "is-offline"}`}>
                  {state.runtime.connected ? `Live · ${mailboxLabel}` : "Runtime not connected"}
                </div>
              </div>
            </div>
            <div className="top-actions">
              <div className="top-segment" aria-label="Main view">
                <button className={`ghost-btn ${state.view === "start" ? "is-active" : ""}`} title="Today's brief" onClick={() => actions.setView("start")}>Brief</button>
                <button className={`ghost-btn ${state.view === "ask" ? "is-active" : ""}`} title="Run a custom scan" onClick={() => actions.setView("ask")}>Ask</button>
              </div>
              <button className="icon-btn" title="Sources" aria-label="Sources" onClick={() => actions.setDrawer("sources", true)}>S</button>
              <button className="icon-btn" title="Briefing history" aria-label="Briefing history" onClick={() => actions.setDrawer("history", true)}>H</button>
              <button className="icon-btn" title="Minimize" onClick={() => actions.minimize(true)}>−</button>
            </div>
          </header>

          <section className="app-content" id="appContent">
            {state.view === "start" && state.originalOpen ? <HandleView /> : null}
            {state.view === "start" && !state.originalOpen && !state.isScanning ? <BriefView /> : null}
            {state.view === "start" && state.isScanning ? <ScanningView /> : null}
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
