import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import type { ComposeContact } from "../types/mail";

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export function isValidEmail(value: string): boolean {
  return EMAIL_RE.test(value.trim().toLowerCase());
}

/** 将文本按查询词切分，用于结果中的搜索词高亮（不区分大小写）。 */
function highlightMatch(text: string, query: string): ReactNode {
  const q = query.trim();
  if (!text || !q) return text;
  const lowerText = text.toLowerCase();
  const lowerQuery = q.toLowerCase();
  const nodes: ReactNode[] = [];
  let cursor = 0;
  let matchIndex = lowerText.indexOf(lowerQuery, cursor);
  let key = 0;
  while (matchIndex >= 0) {
    if (matchIndex > cursor) {
      nodes.push(
        <span key={`t-${key++}`}>{text.slice(cursor, matchIndex)}</span>,
      );
    }
    nodes.push(
      <mark className="compose-contact-highlight" key={`m-${key++}`}>
        {text.slice(matchIndex, matchIndex + q.length)}
      </mark>,
    );
    cursor = matchIndex + q.length;
    matchIndex = lowerText.indexOf(lowerQuery, cursor);
  }
  if (cursor < text.length) {
    nodes.push(<span key={`t-${key++}`}>{text.slice(cursor)}</span>);
  }
  return nodes.length ? nodes : text;
}

type MenuOption =
  | { kind: "contact"; contact: ComposeContact }
  | { kind: "raw"; email: string };

export function RecipientChipInput({
  label,
  emails,
  onChange,
  mailbox,
  searchContacts,
  placeholder = "Enter a name or email",
  trailing,
  onEmptyBlur,
  onInputValueChange,
  readOnly = false,
  autoFocus = false,
  inputAriaLabel,
  /** to | cc | bcc：用于失焦收起时识别是否仍在 Cc/Bcc 相关区域 */
  fieldRole = "to",
}: {
  label: string;
  emails: string[];
  onChange: (emails: string[]) => void;
  mailbox: string;
  searchContacts: (
    mailbox: string,
    query: string,
  ) => Promise<{ contacts: ComposeContact[] }>;
  placeholder?: string;
  trailing?: ReactNode;
  /** 失焦且无 chip、输入为空时回调（用于收起 Cc/Bcc 行） */
  onEmptyBlur?: () => void;
  /** 将当前未提交的输入同步给父级，用于处理点击编辑器其他区域的收起行为。 */
  onInputValueChange?: (value: string) => void;
  /** 只展示收件人，不允许编辑或移除。 */
  readOnly?: boolean;
  autoFocus?: boolean;
  inputAriaLabel?: string;
  fieldRole?: "to" | "cc" | "bcc";
}) {
  const [input, setInput] = useState("");
  const [contacts, setContacts] = useState<ComposeContact[]>([]);
  const [menuOpen, setMenuOpen] = useState(false);
  const [selectedIndex, setSelectedIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const rootRef = useRef<HTMLLabelElement | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);

  const candidate = input.trim().toLowerCase();
  const canAddRaw =
    EMAIL_RE.test(candidate) && !emails.includes(candidate);

  const options = useMemo<MenuOption[]>(() => {
    const items: MenuOption[] = contacts
      .filter((contact) => !emails.includes(contact.email.toLowerCase()))
      .map((contact) => ({ kind: "contact" as const, contact }));
    if (canAddRaw) items.push({ kind: "raw", email: candidate });
    return items;
  }, [canAddRaw, candidate, contacts, emails]);

  const showMenu = menuOpen && options.length > 0;

  useEffect(() => {
    if (autoFocus) inputRef.current?.focus();
  }, [autoFocus]);

  useEffect(() => {
    if (!input.trim()) {
      setContacts([]);
      setSelectedIndex(0);
      return;
    }
    const timer = window.setTimeout(() => {
      void searchContacts(mailbox, input)
        .then((result) => {
          setContacts(result.contacts);
          setMenuOpen(true);
          setSelectedIndex(0);
        })
        .catch(() => {
          setContacts([]);
          setSelectedIndex(0);
        });
    }, 220);
    return () => window.clearTimeout(timer);
  }, [input, mailbox, searchContacts]);

  useEffect(() => {
    if (!showMenu) return;
    setSelectedIndex((index) =>
      Math.min(index, Math.max(options.length - 1, 0)),
    );
  }, [options.length, showMenu]);

  useEffect(() => {
    if (!showMenu || !listRef.current) return;
    const item = listRef.current.querySelector<HTMLElement>(
      `[data-option-index="${selectedIndex}"]`,
    );
    item?.scrollIntoView({ block: "nearest" });
  }, [selectedIndex, showMenu]);

  const addEmail = (email: string) => {
    const normalized = email.trim().toLowerCase();
    if (!EMAIL_RE.test(normalized) || emails.includes(normalized)) return;
    onChange([...emails, normalized]);
    setInput("");
    onInputValueChange?.("");
    setContacts([]);
    setMenuOpen(false);
    setSelectedIndex(0);
    inputRef.current?.focus();
  };

  const confirmSelected = () => {
    const option = options[selectedIndex];
    if (!option) return false;
    if (option.kind === "contact") addEmail(option.contact.email);
    else addEmail(option.email);
    return true;
  };

  const handleBlur = () => {
    window.setTimeout(() => {
      setMenuOpen(false);
      const active = document.activeElement;
      if (!(active instanceof Element)) {
        if (!emails.length && !input.trim()) onEmptyBlur?.();
        return;
      }
      // 本行内、另一条 Cc/Bcc 行、或 To 行上的 Cc/Bcc 按钮：不收起
      if (
        rootRef.current?.contains(active)
        || active.closest('[data-recipient-role="cc"], [data-recipient-role="bcc"]')
        || active.closest(".compose-cc-bcc-toggle")
        || active.closest(".compose-contact-menu")
      ) {
        return;
      }
      if (!emails.length && !input.trim()) onEmptyBlur?.();
    }, 120);
  };

  return (
    <label
      className="compose-field compose-recipient-field"
      data-recipient-role={fieldRole}
      ref={rootRef}
    >
      <span>{label}</span>
      <div className="compose-recipient-row">
        <div className="compose-recipient-chips">
          {emails.map((email) => (
            <span className="compose-chip" key={email}>
              {email}
              {!readOnly ? (
                <button
                  type="button"
                  aria-label={`Remove ${email}`}
                  onClick={() =>
                    onChange(emails.filter((item) => item !== email))
                  }
                >
                  ×
                </button>
              ) : null}
            </span>
          ))}
          <input
            ref={inputRef}
            aria-label={inputAriaLabel || label}
            aria-autocomplete="list"
            aria-expanded={showMenu}
            aria-controls={showMenu ? `compose-contact-list-${fieldRole}` : undefined}
            role="combobox"
            value={input}
            readOnly={readOnly}
            onFocus={() => {
              if (options.length) setMenuOpen(true);
            }}
            onBlur={handleBlur}
            onChange={(event) => {
              if (readOnly) return;
              const value = event.target.value;
              setInput(value);
              onInputValueChange?.(value);
              setMenuOpen(true);
              setSelectedIndex(0);
            }}
            onKeyDown={(event) => {
              if (readOnly) return;
              if (event.key === "Escape" && showMenu) {
                event.preventDefault();
                setMenuOpen(false);
                return;
              }
              if (event.key === "ArrowDown" && showMenu) {
                event.preventDefault();
                setSelectedIndex((index) => (index + 1) % options.length);
                return;
              }
              if (event.key === "ArrowUp" && showMenu) {
                event.preventDefault();
                setSelectedIndex(
                  (index) =>
                    (index - 1 + options.length) % options.length,
                );
                return;
              }
              if (event.key === "Enter") {
                if (showMenu && options.length) {
                  event.preventDefault();
                  confirmSelected();
                  return;
                }
                if (canAddRaw) {
                  event.preventDefault();
                  addEmail(candidate);
                }
                return;
              }
              if (
                event.key === "Backspace" &&
                !input &&
                emails.length
              ) {
                onChange(emails.slice(0, -1));
              }
            }}
            placeholder={emails.length ? undefined : placeholder}
          />
          {showMenu ? (
            <div
              className="compose-contact-menu"
              id={`compose-contact-list-${fieldRole}`}
              role="listbox"
              ref={listRef}
            >
              {options.map((option, index) => {
                const selected = index === selectedIndex;
                if (option.kind === "contact") {
                  const contact = option.contact;
                  const title = contact.name || contact.email;
                  const subtitle = contact.name ? contact.email : "";
                  return (
                    <button
                      type="button"
                      role="option"
                      aria-selected={selected}
                      data-option-index={index}
                      className={selected ? "is-selected" : ""}
                      key={contact.email}
                      onMouseDown={(event) => event.preventDefault()}
                      onMouseMove={() => setSelectedIndex(index)}
                      onClick={() => addEmail(contact.email)}
                    >
                      {contact.avatar_url ? (
                        <img src={contact.avatar_url} alt="" />
                      ) : (
                        <span className="compose-contact-avatar" aria-hidden="true">
                          {(title.charAt(0) || "?").toUpperCase()}
                        </span>
                      )}
                      <span className="compose-contact-text">
                        <strong>{highlightMatch(title, input)}</strong>
                        {subtitle ? (
                          <small>{highlightMatch(subtitle, input)}</small>
                        ) : null}
                      </span>
                      {selected ? <kbd>Enter</kbd> : null}
                    </button>
                  );
                }
                return (
                  <button
                    type="button"
                    role="option"
                    aria-selected={selected}
                    data-option-index={index}
                    className={selected ? "is-selected" : ""}
                    key={`raw:${option.email}`}
                    onMouseDown={(event) => event.preventDefault()}
                    onMouseMove={() => setSelectedIndex(index)}
                    onClick={() => addEmail(option.email)}
                  >
                    <span className="compose-contact-avatar" aria-hidden="true">
                      +
                    </span>
                    <span className="compose-contact-text">
                      <strong>
                        Add {highlightMatch(option.email, input)}
                      </strong>
                    </span>
                    {selected ? <kbd>Enter</kbd> : null}
                  </button>
                );
              })}
            </div>
          ) : null}
        </div>
        {trailing ? (
          <div className="compose-recipient-trailing">{trailing}</div>
        ) : null}
      </div>
    </label>
  );
}
