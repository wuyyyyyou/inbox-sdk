import { describe, expect, it } from "vitest";
import { connectedAccountsStatusMessage } from "./connectedAccounts";

describe("connectedAccountsStatusMessage", () => {
  it("tells the user to enable Connected accounts when the platform denies the grant", () => {
    expect(connectedAccountsStatusMessage({
      available: false,
      code: "not_granted",
      message: "Enable Google Connected accounts for Anna Inbox, then retry.",
      action: "enable_connected_accounts",
    })).toBe("Enable Google Connected accounts for Anna Inbox, then retry.");
  });

  it("does not show a configuration prompt for a successful account lookup", () => {
    expect(connectedAccountsStatusMessage({
      available: true,
      code: "ok",
      message: "Google Connected accounts are available.",
      action: "none",
    })).toBe("");
  });
});
