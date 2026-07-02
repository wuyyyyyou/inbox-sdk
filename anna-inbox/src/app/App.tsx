import { useEffect } from "react";
import { Drawers } from "../features/drawers/Drawers";
import { HomeView } from "../features/home/HomeView";
import { AppContext } from "./AppContext";
import { useAppController } from "./useAppController";

export function App() {
  const controller = useAppController();
  const { state, actions, toast, dismissToast, accountSwitchNotice, accountSwitchNoticeVisible, closeAccountSwitchNotice, initialize } = controller;
  useEffect(() => {
    void initialize();
    // Initial boot should run once; subsequent state changes are driven by actions.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <AppContext.Provider value={{ state, actions }}>
      <div className="desktop">
        <main className="app-shell home-app-shell" id="appShell">
          <HomeView />
          <Drawers />
          {accountSwitchNotice ? (
            <div className={`account-switch-notice ${accountSwitchNoticeVisible ? "is-visible" : ""}`} role="status">
              {accountSwitchNotice.avatarUrl
                ? <img src={accountSwitchNotice.avatarUrl} alt="" referrerPolicy="no-referrer" />
                : <span className="account-switch-notice-avatar">{(accountSwitchNotice.email.split("@")[0]?.charAt(0) || "A").toUpperCase()}</span>}
              <span><strong>Switched account</strong><small>{accountSwitchNotice.email}</small></span>
              <button type="button" aria-label="Dismiss account switch notice" onClick={closeAccountSwitchNotice}>
                <svg viewBox="0 0 20 20" aria-hidden="true"><path d="m5 5 10 10M15 5 5 15" /></svg>
              </button>
            </div>
          ) : null}
          <div className={`toast ${toast ? "is-visible" : ""}`}>
            {toast ? (
              <>
                <span>{toast.message}</span>
                {toast.actionLabel && toast.onAction ? (
                  <button type="button" className="toast-action" onClick={() => {
                    toast.onAction?.();
                    dismissToast();
                  }}>
                    {toast.actionLabel}
                  </button>
                ) : null}
                {toast.secondaryActionLabel && toast.onSecondaryAction ? (
                  <button type="button" className="toast-action is-secondary" onClick={() => {
                    toast.onSecondaryAction?.();
                    dismissToast();
                  }}>
                    {toast.secondaryActionLabel}
                  </button>
                ) : null}
              </>
            ) : null}
          </div>
        </main>
      </div>
    </AppContext.Provider>
  );
}
