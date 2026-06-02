import { createContext, useContext } from "react";
import type { AppState } from "../types/mail";
import type { AppActions } from "./useAppController";

export interface AppContextValue {
  state: AppState;
  actions: AppActions;
}

export const AppContext = createContext<AppContextValue | null>(null);

export function useApp() {
  const ctx = useContext(AppContext);
  if (!ctx) throw new Error("useApp must be used inside AppContext");
  return ctx;
}
