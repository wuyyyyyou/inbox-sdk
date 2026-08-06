import { useEffect } from "react";
import { Drawers } from "../features/drawers/Drawers";
import { HomeView } from "../features/home/HomeView";
import { I18nProvider, useI18n } from "../i18n";
import { AppContext } from "./AppContext";
import { useAppController } from "./useAppController";

function AppShell() {
  const controller = useAppController();
  const { t } = useI18n();
  const { state, actions, toasts, dismissToast, accountSwitchNotice, accountSwitchNoticeVisible, closeAccountSwitchNotice, initialize } = controller;
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
              <span><strong>{t("account.switched")}</strong><small>{accountSwitchNotice.email}</small></span>
              <button type="button" aria-label={t("account.dismissSwitch")} onClick={closeAccountSwitchNotice}>
                <svg viewBox="0 0 20 20" aria-hidden="true"><path d="m5 5 10 10M15 5 5 15" /></svg>
              </button>
            </div>
          ) : null}
          <div className="toast-stack" aria-live="polite" aria-atomic="false">
            {toasts.map((toast) => (
              <div className="toast is-visible" key={toast.id} role="status">
                <span>{toast.message}</span>
                {toast.actionLabel && toast.onAction ? (
                  <button type="button" className="toast-action" onClick={() => {
                    dismissToast(toast.id);
                    toast.onAction?.();
                  }}>
                    {toast.actionLabel}
                  </button>
                ) : null}
                {toast.secondaryActionLabel && toast.onSecondaryAction ? (
                  <button type="button" className="toast-action is-secondary" onClick={() => {
                    dismissToast(toast.id);
                    toast.onSecondaryAction?.();
                  }}>
                    {toast.secondaryActionLabel}
                  </button>
                ) : null}
              </div>
            ))}
          </div>
        </main>
      </div>
    </AppContext.Provider>
  );
}

export function App() {
  return (
    <I18nProvider>
      <AppShell />
    </I18nProvider>
  );
}
