import type { MailboxCredentialsStatus } from "../types/mail";

export function connectedAccountsStatusMessage(status?: MailboxCredentialsStatus): string {
  if (!status || (status.action !== "enable_connected_accounts" && status.action !== "upgrade_runtime")) return "";
  return status.message;
}
