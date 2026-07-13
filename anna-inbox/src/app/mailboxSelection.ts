import type { MailboxInfo } from "../types/mail";

function normalizedMailbox(value: string): string {
  return value.trim().toLowerCase();
}

export function resolveMailboxSelection({
  mailboxes,
  selected,
  fallback,
}: {
  mailboxes: MailboxInfo[];
  selected: string[];
  fallback: string;
}): { mailboxes: MailboxInfo[]; selected: string[]; primary: string } {
  const selectedMailboxes = Array.from(new Set(
    selected
      .map(normalizedMailbox)
      .filter(Boolean)
      .filter((email) => mailboxes.find((mailbox) => normalizedMailbox(mailbox.email) === email)?.authorized !== false),
  ));
  const primary = selectedMailboxes[0]
    || normalizedMailbox(mailboxes.find((mailbox) => mailbox.authorized !== false)?.email || fallback);
  const resolvedSelected = selectedMailboxes.length ? selectedMailboxes : (primary ? [primary] : []);
  const selectedSet = new Set(resolvedSelected);

  return {
    mailboxes: mailboxes.map((mailbox) => ({
      ...mailbox,
      selected: selectedSet.has(normalizedMailbox(mailbox.email)),
    })),
    selected: resolvedSelected,
    primary,
  };
}
