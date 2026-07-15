import { useEffect, useMemo, useRef, useState } from "react";
import type { InboxCustomCategory, InboxSettings } from "../../types/mail";
import { applyInboxQuerySuggestion, getInboxQuerySuggestionPlaceholder, getInboxQuerySuggestions, parseInboxQuery, splitInboxQueryTokens } from "../search/inboxQuery";

type SplitDraft = Omit<InboxCustomCategory, "id"> & { id?: string };

const EMPTY_DRAFT: SplitDraft = {
  name: "",
  query: "",
  hide_when_empty: false,
  bundling_behavior: "default",
};

/** 用同一 ID 的最新内容替换一个已保存 Split，并保持其他 Split 的顺序与引用不变。 */
export function replaceInboxSplit(
  splits: InboxCustomCategory[],
  next: InboxCustomCategory,
): InboxCustomCategory[] {
  return splits.map((split) => split.id === next.id ? next : split);
}

/** 管理当前邮箱的本地 Splits；保存动作统一交由上层的设置持久化入口处理。 */
export function SplitsManager({
  open,
  settings,
  onClose,
  onChange,
}: {
  open: boolean;
  settings: InboxSettings;
  onClose: () => void;
  onChange: (patch: Partial<InboxSettings>) => Promise<boolean>;
}) {
  const [draft, setDraft] = useState<SplitDraft>(EMPTY_DRAFT);
  const [screen, setScreen] = useState<"list" | "form" | "query">("list");
  const [deletingId, setDeletingId] = useState("");
  const [queryFocused, setQueryFocused] = useState(false);
  const [querySuggestionIndex, setQuerySuggestionIndex] = useState(0);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");
  const queryInputRef = useRef<HTMLInputElement>(null);
  const categories = Array.isArray(settings.custom_categories) ? settings.custom_categories : [];
  const parsed = useMemo(() => parseInboxQuery(draft.query), [draft.query]);
  const querySuggestions = useMemo(() => getInboxQuerySuggestions(draft.query), [draft.query]);

  useEffect(() => {
    if (open) return;
    setScreen("list");
    setDraft(EMPTY_DRAFT);
    setDeletingId("");
    setQueryFocused(false);
    setQuerySuggestionIndex(0);
  }, [open]);

  if (!open) return null;
  const editing = Boolean(draft.id);
  const canSave = Boolean(draft.name.trim() && parsed.expression && !parsed.error);
  const startCreate = () => {
    setDraft(EMPTY_DRAFT);
    setScreen("form");
  };
  const applyQuerySuggestion = (suggestion: string) => {
    const next = applyInboxQuerySuggestion(draft.query, suggestion);
    setDraft((current) => ({ ...current, query: next }));
    setQueryFocused(true);
    window.requestAnimationFrame(() => queryInputRef.current?.setSelectionRange(next.length, next.length));
  };
  const startEdit = (split: InboxCustomCategory) => {
    setDraft(split);
    setScreen("form");
  };
  const saveQuery = async () => {
    if (!draft.id || !parsed.expression || parsed.error) {
      setScreen("form");
      return;
    }
    setSaving(true);
    setSaveError("");
    const saved = await onChange({ custom_categories: replaceInboxSplit(categories, {
      ...draft,
      id: draft.id,
      name: draft.name.trim(),
      query: draft.query.trim(),
    }) });
    setSaving(false);
    if (saved) setScreen("form");
    else setSaveError("Could not save this Split. Please try again.");
  };
  const save = async () => {
    if (!canSave) return;
    const next: InboxCustomCategory = {
      id: draft.id || crypto.randomUUID(),
      name: draft.name.trim(),
      query: draft.query.trim(),
      hide_when_empty: draft.hide_when_empty,
      bundling_behavior: draft.bundling_behavior,
    };
    setSaving(true);
    setSaveError("");
    const saved = await onChange({ custom_categories: editing
      ? replaceInboxSplit(categories, next)
      : [...categories, next] });
    setSaving(false);
    if (saved) {
      setScreen("list");
      setDraft(EMPTY_DRAFT);
    } else setSaveError("Could not save this Split. Please try again.");
  };

  return <div className="splits-overlay" role="presentation" onMouseDown={onClose}>
    <section className="splits-dialog" role="dialog" aria-modal="true" aria-labelledby="splits-title" onMouseDown={(event) => event.stopPropagation()}>
      <header><h2 id="splits-title">{screen === "query" ? "Edit split query" : screen === "form" ? (editing ? "Edit split" : "Create split") : "Splits"}</h2><button className="icon-btn" type="button" aria-label="Close Splits" onClick={onClose}>×</button></header>
      {screen === "list" ? <>
        <p>Divide your inbox into tabs for different types of emails.</p>
        <h3>Custom splits</h3>
        <ul className="splits-list">{categories.map((split) => <li key={split.id}><div><strong>{split.name}</strong><span>{split.query}</span></div><button type="button" onClick={() => startEdit(split)}>Edit</button><button style={{ color: "#e05c67" }} type="button" onClick={() => setDeletingId(split.id)}>Delete</button></li>)}</ul>
        {!categories.length ? <p className="splits-empty">No custom splits yet.</p> : null}
        <button className="splits-add" type="button" onClick={startCreate}>＋ Add split</button>
      </> : screen === "query" ? <>
        <p>Splits are defined by local search queries.</p>
        <div className="mail-search-wrap splits-query-editor-wrap">
          <label className="mail-search splits-query-editor">
            <span className="splits-query-search-icon" aria-hidden="true">⌕</span>
            <span className="mail-search-highlight" aria-hidden="true">{splitInboxQueryTokens(draft.query).map((token, index, tokens) => <span className={`is-${token.kind}${parsed.error && index === tokens.length - 1 && !/\s$/u.test(draft.query) ? " is-editing" : ""}`} key={`${token.text}-${index}`}>{token.text}</span>)}</span>
            <input ref={queryInputRef} autoFocus value={draft.query} onChange={(event) => { setDraft((current) => ({ ...current, query: event.target.value })); setQueryFocused(true); setQuerySuggestionIndex(0); }} onFocus={() => setQueryFocused(true)} onBlur={() => window.setTimeout(() => setQueryFocused(false), 120)} onKeyDown={(event) => { if (event.key === "ArrowDown" && querySuggestions.length) { event.preventDefault(); setQuerySuggestionIndex((index) => (index + 1) % querySuggestions.length); } else if (event.key === "ArrowUp" && querySuggestions.length) { event.preventDefault(); setQuerySuggestionIndex((index) => (index - 1 + querySuggestions.length) % querySuggestions.length); } else if (event.key === "Enter") { event.preventDefault(); if (querySuggestions.length) applyQuerySuggestion(querySuggestions[querySuggestionIndex]); else if (parsed.expression && !parsed.error && !/\s$/u.test(draft.query)) { const next = `${draft.query} `; setDraft((current) => ({ ...current, query: next })); window.requestAnimationFrame(() => queryInputRef.current?.setSelectionRange(next.length, next.length)); } } }} placeholder="Type your search query" />
          </label>
          {queryFocused ? <div className="mail-search-suggestions splits-query-suggestions" role="listbox">{querySuggestions.length ? querySuggestions.map((suggestion, index) => <button type="button" role="option" aria-selected={index === querySuggestionIndex} className={index === querySuggestionIndex ? "is-selected" : ""} key={suggestion} onMouseDown={(event) => event.preventDefault()} onMouseMove={() => setQuerySuggestionIndex(index)} onClick={() => applyQuerySuggestion(suggestion)}><strong>{suggestion}</strong>{getInboxQuerySuggestionPlaceholder(suggestion) ? <span>{getInboxQuerySuggestionPlaceholder(suggestion)}</span> : null}{index === querySuggestionIndex ? <kbd>Enter</kbd> : null}</button>) : parsed.error ? <p role="alert">{parsed.error}</p> : null}</div> : null}
        </div>
        {saveError ? <p className="splits-error" role="alert">{saveError}</p> : null}
        <div className="splits-actions"><button type="button" disabled={saving} onClick={() => setScreen("form")}>Cancel</button><button type="button" disabled={saving || !parsed.expression || Boolean(parsed.error)} onClick={saveQuery}>{saving ? "Saving…" : "Save"}</button></div>
      </> : <>
        <label>Name<input autoFocus value={draft.name} onChange={(event) => setDraft((current) => ({ ...current, name: event.target.value }))} placeholder='e.g. "High priority"' /></label>
        <label>Query<div className="splits-query-value"><span>{draft.query || "No query set"}</span><button type="button" onClick={() => setScreen("query")}>Edit</button></div></label>
        {getInboxQuerySuggestions(draft.query).length && draft.query ? <p className="splits-hint">Use supported fields such as subject:, from:, to:, is:, has:, before:, or after:.</p> : null}
        <label className="splits-switch"><span>Hide when empty</span><input type="checkbox" checked={draft.hide_when_empty} onChange={(event) => setDraft((current) => ({ ...current, hide_when_empty: event.target.checked }))} /></label>
        <label>Bundling behavior<select value={draft.bundling_behavior} onChange={(event) => setDraft((current) => ({ ...current, bundling_behavior: event.target.value as InboxCustomCategory["bundling_behavior"] }))}><option value="default">Default</option><option value="by_sender">By sender</option><option value="none">None</option></select></label>
        {saveError ? <p className="splits-error" role="alert">{saveError}</p> : null}
        <div className="splits-actions"><button type="button" disabled={saving} onClick={() => setScreen("list")}>Cancel</button><button type="button" disabled={saving || !canSave} onClick={save}>{saving ? "Saving…" : editing ? "Save" : "Create"}</button></div>
      </>}
      {deletingId ? <div className="splits-confirm"><p>Delete this Split? Its saved query will be removed.</p><button type="button" onClick={() => setDeletingId("")}>Cancel</button><button type="button" onClick={() => { onChange({ custom_categories: categories.filter((split) => split.id !== deletingId) }); setDeletingId(""); }}>Delete</button></div> : null}
    </section>
  </div>;
}
