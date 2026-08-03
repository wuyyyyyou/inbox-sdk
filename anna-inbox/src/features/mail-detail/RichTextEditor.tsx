import DOMPurify from "dompurify";
import {
  forwardRef,
  useEffect,
  useImperativeHandle,
  useLayoutEffect,
  useRef,
  useState,
  type ClipboardEvent,
  type KeyboardEvent,
  type MouseEvent,
} from "react";
import { useI18n } from "../../i18n/I18nContext";

const FONT_SIZES = [12, 14, 16, 18, 20, 24] as const;
const HEADING_FONT_SIZES: Record<string, number> = { p: 14, h1: 24, h2: 20, h3: 18 };
const COLORS = ["#1f2937", "#dc2626", "#d97706", "#16a34a", "#2563eb", "#7c3aed"] as const;
const ALLOWED_TAGS = ["p", "br", "strong", "em", "u", "s", "span", "ul", "ol", "li", "a", "h1", "h2", "h3", "blockquote"];
const COLOR_NAMES: Record<(typeof COLORS)[number], string> = {
  "#1f2937": "detail.color.darkGray",
  "#dc2626": "detail.color.red",
  "#d97706": "detail.color.orange",
  "#16a34a": "detail.color.green",
  "#2563eb": "detail.color.blue",
  "#7c3aed": "detail.color.purple",
};
const ALLOWED_ATTR = ["href", "style", "align"];
const ALLOWED_ALIGNMENTS = ["left", "center", "right", "justify"] as const;
const FONT_SIZE_BY_LEGACY_VALUE: Record<string, number> = {
  "1": 12,
  "2": 14,
  "3": 16,
  "4": 18,
  "5": 20,
  "6": 24,
  "7": 24,
};

export type RichTextValue = {
  html: string;
  text: string;
};

type RichTextEditorProps = {
  value: string;
  placeholder: string;
  disabled?: boolean;
  onChange: (value: RichTextValue) => void;
};

function escapeHtml(value: string) {
  return value.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/\"/g, "&quot;").replace(/'/g, "&#039;");
}

export function plainTextToEditorHtml(value: string) {
  const lines = String(value || "").replace(/\r\n?/g, "\n").split("\n");
  if (!lines.length || (lines.length === 1 && !lines[0])) return "";
  return lines.map((line) => `<p>${escapeHtml(line) || "<br>"}</p>`).join("");
}

function replaceTag(element: Element, tagName: string) {
  const replacement = element.ownerDocument.createElement(tagName);
  for (const attribute of Array.from(element.attributes)) replacement.setAttribute(attribute.name, attribute.value);
  while (element.firstChild) replacement.appendChild(element.firstChild);
  element.replaceWith(replacement);
  return replacement;
}

function normalizeColor(value: string) {
  const normalized = value.trim().toLowerCase().replace(/\s+/g, "");
  const rgbMatches: Record<string, string> = {
    "rgb(31,41,55)": "#1f2937",
    "rgb(220,38,38)": "#dc2626",
    "rgb(217,119,6)": "#d97706",
    "rgb(22,163,74)": "#16a34a",
    "rgb(37,99,235)": "#2563eb",
    "rgb(124,58,237)": "#7c3aed",
  };
  const resolved = rgbMatches[normalized] || normalized;
  return COLORS.includes(resolved as (typeof COLORS)[number]) ? resolved : "";
}

export function sanitizeEditorStyle(value: string) {
  let color = "";
  let fontSize = "";
  let textAlign = "";
  for (const declaration of value.split(";")) {
    const [property, rawValue] = declaration.split(":", 2);
    if (!property || rawValue === undefined) continue;
    if (property.trim().toLowerCase() === "color") color = normalizeColor(rawValue);
    if (property.trim().toLowerCase() === "font-size") {
      const size = Number.parseInt(rawValue, 10);
      if (FONT_SIZES.includes(size as (typeof FONT_SIZES)[number])) fontSize = `${size}px`;
    }
    if (property.trim().toLowerCase() === "text-align") {
      const alignment = rawValue.trim().toLowerCase();
      if (ALLOWED_ALIGNMENTS.includes(alignment as (typeof ALLOWED_ALIGNMENTS)[number])) textAlign = alignment;
    }
  }
  return [fontSize && `font-size: ${fontSize}`, color && `color: ${color}`, textAlign && `text-align: ${textAlign}`]
    .filter(Boolean)
    .join("; ");
}

function isSafeHref(value: string) {
  try {
    const url = new URL(value);
    return url.protocol === "https:" || url.protocol === "http:" || url.protocol === "mailto:";
  } catch {
    return false;
  }
}

function normalizeHref(value: string) {
  const raw = value.trim();
  if (!raw || /^(?:https?:|mailto:)/i.test(raw)) return raw;
  return `https://${raw}`;
}

export function sanitizeEditorHtml(value: string) {
  const document = new DOMParser().parseFromString(String(value || ""), "text/html");
  for (const element of Array.from(document.body.querySelectorAll("b"))) replaceTag(element, "strong");
  for (const element of Array.from(document.body.querySelectorAll("i"))) replaceTag(element, "em");
  for (const element of Array.from(document.body.querySelectorAll("div"))) replaceTag(element, "p");
  for (const element of Array.from(document.body.querySelectorAll("font"))) {
    const span = replaceTag(element, "span");
    const size = FONT_SIZE_BY_LEGACY_VALUE[span.getAttribute("size") || ""];
    const color = normalizeColor(span.getAttribute("color") || "");
    const style = [size && `font-size: ${size}px`, color && `color: ${color}`].filter(Boolean).join("; ");
    span.removeAttribute("size");
    span.removeAttribute("color");
    if (style) span.setAttribute("style", style);
  }

  // Browsers and office apps commonly paste alignment as an HTML attribute.
  for (const element of Array.from(document.body.querySelectorAll("[align]"))) {
    const alignment = element.getAttribute("align")?.trim().toLowerCase() || "";
    element.removeAttribute("align");
    if (ALLOWED_ALIGNMENTS.includes(alignment as (typeof ALLOWED_ALIGNMENTS)[number])) {
      const existing = element.getAttribute("style") || "";
      element.setAttribute("style", `${existing}${existing.trim() ? ";" : ""} text-align: ${alignment}`);
    }
  }

  const clean = DOMPurify.sanitize(document.body.innerHTML, {
    ALLOWED_TAGS,
    ALLOWED_ATTR,
    ALLOW_UNKNOWN_PROTOCOLS: false,
  });
  const cleanDocument = new DOMParser().parseFromString(clean, "text/html");
  for (const styledElement of Array.from(cleanDocument.body.querySelectorAll("[style]"))) {
    const style = sanitizeEditorStyle(styledElement.getAttribute("style") || "");
    if (style) styledElement.setAttribute("style", style);
    else styledElement.removeAttribute("style");
  }
  for (const link of Array.from(cleanDocument.body.querySelectorAll("a"))) {
    const href = link.getAttribute("href") || "";
    if (isSafeHref(href)) link.setAttribute("href", href);
    else link.removeAttribute("href");
  }
  return cleanDocument.body.innerHTML;
}

export function editorHtmlToPlainText(value: string) {
  const source = sanitizeEditorHtml(value)
    .replace(/<br\s*\/?>(?=)/gi, "\n")
    .replace(/<\/(?:p|li|ul|ol|h1|h2|h3)>/gi, "\n");
  const document = new DOMParser().parseFromString(source, "text/html");
  return (document.body.textContent || "").replace(/\n{3,}/g, "\n\n").trimEnd();
}

function findLinkAtSelection(selection: Selection | null) {
  if (!selection?.rangeCount) return null;
  const node = selection.getRangeAt(0).commonAncestorContainer;
  const element = node.nodeType === Node.ELEMENT_NODE ? node as Element : node.parentElement;
  return element?.closest("a") || null;
}

export const RichTextEditor = forwardRef<HTMLDivElement, RichTextEditorProps>(function RichTextEditor(
  { value, placeholder, disabled = false, onChange },
  forwardedRef,
) {
  const { t } = useI18n();
  const editorRef = useRef<HTMLDivElement | null>(null);
  const selectionRef = useRef<Range | null>(null);
  const historyRef = useRef<string[]>([]);
  const historyIndexRef = useRef(-1);
  const lastValueRef = useRef("");
  const [revision, setRevision] = useState(0);
  const [linkOpen, setLinkOpen] = useState(false);
  const [linkText, setLinkText] = useState("");
  const [linkUrl, setLinkUrl] = useState("");
  const [linkError, setLinkError] = useState("");
  const [currentFontSize, setCurrentFontSize] = useState<number>(14);
  const [currentHeading, setCurrentHeading] = useState("p");
  const openedLinkRef = useRef<HTMLAnchorElement | null>(null);
  const linkPopoverRef = useRef<HTMLDivElement | null>(null);
  const linkButtonRef = useRef<HTMLButtonElement | null>(null);

  useImperativeHandle(forwardedRef, () => editorRef.current as HTMLDivElement);

  const emit = (nextHtml: string, addHistory = true) => {
    const html = sanitizeEditorHtml(nextHtml);
    if (editorRef.current && editorRef.current.innerHTML !== html) editorRef.current.innerHTML = html;
    lastValueRef.current = html;
    if (addHistory && historyRef.current[historyIndexRef.current] !== html) {
      historyRef.current = [...historyRef.current.slice(0, historyIndexRef.current + 1), html].slice(-100);
      historyIndexRef.current = historyRef.current.length - 1;
    }
    onChange({ html, text: editorHtmlToPlainText(html) });
    setRevision((current) => current + 1);
  };

  useLayoutEffect(() => {
    const html = sanitizeEditorHtml(value);
    if (html === lastValueRef.current) return;
    lastValueRef.current = html;
    if (editorRef.current) editorRef.current.innerHTML = html;
    historyRef.current = [html];
    historyIndexRef.current = 0;
    setRevision((current) => current + 1);
  }, [value]);

  const saveSelection = () => {
    const selection = window.getSelection();
    if (!selection?.rangeCount || !editorRef.current?.contains(selection.anchorNode)) return;
    selectionRef.current = selection.getRangeAt(0).cloneRange();
  };

  const updateSelectionFontSize = () => {
    const selection = window.getSelection();
    if (!selection?.rangeCount || !editorRef.current?.contains(selection.anchorNode)) return;
    let element = selection.anchorNode?.nodeType === Node.ELEMENT_NODE
      ? selection.anchorNode as Element
      : selection.anchorNode?.parentElement;
    let fontSize = 14;
    let heading = "p";
    while (element && element !== editorRef.current) {
      const tagName = element.tagName.toLowerCase();
      if (tagName === "h1" || tagName === "h2" || tagName === "h3") heading = tagName;
      const size = Number.parseInt((element as HTMLElement).style.fontSize, 10);
      if (FONT_SIZES.includes(size as (typeof FONT_SIZES)[number])) fontSize = size;
      element = element.parentElement;
    }
    setCurrentFontSize(fontSize === 14 ? (HEADING_FONT_SIZES[heading] || 14) : fontSize);
    setCurrentHeading(heading);
  };

  const showLinkPopover = (link: HTMLAnchorElement, selectedText = link.textContent || "") => {
    openedLinkRef.current = link;
    setLinkText(selectedText);
    setLinkUrl(link.getAttribute("href") || "");
    setLinkError("");
    setLinkOpen(true);
  };

  useEffect(() => {
    const listener = () => {
      saveSelection();
      updateSelectionFontSize();
      const link = findLinkAtSelection(window.getSelection());
      if (link && link !== openedLinkRef.current) showLinkPopover(link);
    };
    document.addEventListener("selectionchange", listener);
    return () => document.removeEventListener("selectionchange", listener);
  });

  useEffect(() => {
    if (!linkOpen) return;
    const closeOnEscape = (event: globalThis.KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      openedLinkRef.current = null;
      setLinkOpen(false);
      linkButtonRef.current?.focus();
    };
    const closeOnOutsidePointerDown = (event: globalThis.MouseEvent) => {
      if (linkPopoverRef.current?.contains(event.target as Node)) return;
      openedLinkRef.current = null;
      setLinkOpen(false);
      linkButtonRef.current?.focus();
    };
    document.addEventListener("keydown", closeOnEscape);
    document.addEventListener("mousedown", closeOnOutsidePointerDown);
    return () => {
      document.removeEventListener("keydown", closeOnEscape);
      document.removeEventListener("mousedown", closeOnOutsidePointerDown);
    };
  }, [linkOpen]);

  const restoreSelection = () => {
    const selection = window.getSelection();
    const range = selectionRef.current;
    if (!selection || !range || !editorRef.current) return;
    editorRef.current.focus();
    selection.removeAllRanges();
    selection.addRange(range);
  };

  const runCommand = (command: string, commandValue?: string) => {
    if (disabled) return;
    restoreSelection();
    document.execCommand(command, false, commandValue);
    emit(editorRef.current?.innerHTML || "");
  };

  const applyFontSize = (size: (typeof FONT_SIZES)[number]) => {
    // fontSize 的兼容命令输出 <font size="1"-"7">，sanitizeEditorHtml 会立即规范为受控 span 样式。
    runCommand("fontSize", String(FONT_SIZES.indexOf(size) + 1));
  };

  const undo = () => {
    if (historyIndexRef.current <= 0) return;
    historyIndexRef.current -= 1;
    emit(historyRef.current[historyIndexRef.current], false);
  };

  const redo = () => {
    if (historyIndexRef.current >= historyRef.current.length - 1) return;
    historyIndexRef.current += 1;
    emit(historyRef.current[historyIndexRef.current], false);
  };

  const openLink = () => {
    saveSelection();
    const link = findLinkAtSelection(window.getSelection());
    const selectedText = window.getSelection()?.toString() || "";
    if (link) {
      showLinkPopover(link, selectedText || link.textContent || "");
      return;
    }
    openedLinkRef.current = null;
    setLinkText(selectedText);
    setLinkUrl("");
    setLinkError("");
    setLinkOpen(true);
  };

  const applyLink = () => {
    const href = normalizeHref(linkUrl);
    if (!isSafeHref(href)) {
      setLinkError(t("detail.linkUrlError"));
      return;
    }
    restoreSelection();
    const link = findLinkAtSelection(window.getSelection());
    if (link) {
      link.setAttribute("href", href);
    } else if (window.getSelection()?.toString()) {
      document.execCommand("createLink", false, href);
    } else if (linkText.trim()) {
      document.execCommand("insertHTML", false, `<a href="${escapeHtml(href)}">${escapeHtml(linkText.trim())}</a>`);
    } else {
      setLinkError(t("detail.linkTextError"));
      return;
    }
    emit(editorRef.current?.innerHTML || "");
    openedLinkRef.current = null;
    setLinkOpen(false);
  };

  const removeLink = () => {
    restoreSelection();
    const link = findLinkAtSelection(window.getSelection());
    if (link) {
      const text = document.createTextNode(link.textContent || "");
      link.replaceWith(text);
      emit(editorRef.current?.innerHTML || "");
    }
    openedLinkRef.current = null;
    setLinkOpen(false);
  };

  const handlePaste = (event: ClipboardEvent<HTMLDivElement>) => {
    event.preventDefault();
    const pastedHtml = event.clipboardData.getData("text/html");
    const pastedText = event.clipboardData.getData("text/plain");
    const safeHtml = pastedHtml ? sanitizeEditorHtml(pastedHtml) : "";
    if (safeHtml) document.execCommand("insertHTML", false, safeHtml);
    else document.execCommand("insertText", false, pastedText);
    emit(editorRef.current?.innerHTML || "");
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (!(event.metaKey || event.ctrlKey)) return;
    const key = event.key.toLowerCase();
    if (key === "b" || key === "i" || key === "u") {
      event.preventDefault();
      runCommand(key === "b" ? "bold" : key === "i" ? "italic" : "underline");
    }
    if (key === "z") {
      event.preventDefault();
      if (event.shiftKey) redo();
      else undo();
    }
  };

  const active = (command: string) => {
    void revision;
    return document.queryCommandState(command);
  };

  const toolbarMouseDown = (event: MouseEvent<HTMLButtonElement>) => event.preventDefault();
  const canUndo = historyIndexRef.current > 0;
  const canRedo = historyIndexRef.current >= 0 && historyIndexRef.current < historyRef.current.length - 1;

  return (
    <div className="rich-text-editor" data-disabled={disabled || undefined}>
      <div className="rich-text-editor-toolbar" role="toolbar" aria-label={t("detail.textFormatting")}>
        <div className="rich-text-editor-group">
          <button type="button" aria-label={t("detail.undo")} data-tooltip={t("detail.undoTooltip")} disabled={disabled || !canUndo} onMouseDown={toolbarMouseDown} onClick={undo}>↶</button>
          <button type="button" aria-label={t("detail.redo")} data-tooltip={t("detail.redoTooltip")} disabled={disabled || !canRedo} onMouseDown={toolbarMouseDown} onClick={redo}>↷</button>
        </div>
        <div className="rich-text-editor-group">
          <button type="button" className={active("bold") ? "is-active" : ""} aria-label={t("detail.bold")} data-tooltip={t("detail.boldTooltip")} disabled={disabled} onMouseDown={toolbarMouseDown} onClick={() => runCommand("bold")}><strong>B</strong></button>
          <button type="button" className={active("italic") ? "is-active" : ""} aria-label={t("detail.italic")} data-tooltip={t("detail.italicTooltip")} disabled={disabled} onMouseDown={toolbarMouseDown} onClick={() => runCommand("italic")}><em>I</em></button>
          <button type="button" className={active("underline") ? "is-active" : ""} aria-label={t("detail.underline")} data-tooltip={t("detail.underlineTooltip")} disabled={disabled} onMouseDown={toolbarMouseDown} onClick={() => runCommand("underline")}><u>U</u></button>
        </div>
        <div className="rich-text-editor-group rich-text-editor-select-group">
          <label className="sr-only" htmlFor="rich-text-font-size">{t("detail.fontSize")}</label>
          <select id="rich-text-font-size" aria-label={t("detail.fontSize")} title={t("detail.fontSize")} disabled={disabled} defaultValue="" onMouseDown={saveSelection} onChange={(event) => {
            if (event.target.value) applyFontSize(Number(event.target.value) as (typeof FONT_SIZES)[number]);
            event.currentTarget.value = "";
          }}>
            <option value="">{currentFontSize}</option>
            {FONT_SIZES.map((size) => <option key={size} value={size}>{size}</option>)}
          </select>
        </div>
        <div className="rich-text-editor-group rich-text-editor-colors">
          {COLORS.map((color) => (
            <button key={color} type="button" className="rich-text-editor-color" aria-label={t("detail.textColor", { color: t(COLOR_NAMES[color] as "detail.color.darkGray") })} data-tooltip={t(COLOR_NAMES[color] as "detail.color.darkGray")} disabled={disabled} onMouseDown={toolbarMouseDown} onClick={() => runCommand("foreColor", color)}>
              <span style={{ backgroundColor: color }} />
            </button>
          ))}
        </div>
        <div className="rich-text-editor-group rich-text-editor-select-group">
          <label className="sr-only" htmlFor="rich-text-heading">{t("detail.heading")}</label>
          <select id="rich-text-heading" aria-label={t("detail.heading")} title={t("detail.heading")} disabled={disabled} value={currentHeading} onMouseDown={saveSelection} onChange={(event) => {
            runCommand("formatBlock", `<${event.target.value}>`);
            setCurrentHeading(event.target.value);
          }}>
            <option value="p">{t("detail.normal")}</option>
            <option value="h1">H1</option>
            <option value="h2">H2</option>
            <option value="h3">H3</option>
          </select>
        </div>
        <div className="rich-text-editor-group">
          <button type="button" className={active("insertUnorderedList") ? "is-active" : ""} aria-label={t("detail.bulletedList")} data-tooltip={t("detail.bulletedList")} disabled={disabled} onMouseDown={toolbarMouseDown} onClick={() => runCommand("insertUnorderedList")}>•≡</button>
          <button type="button" className={active("insertOrderedList") ? "is-active" : ""} aria-label={t("detail.numberedList")} data-tooltip={t("detail.numberedList")} disabled={disabled} onMouseDown={toolbarMouseDown} onClick={() => runCommand("insertOrderedList")}>1≡</button>
        </div>
        <div className="rich-text-editor-group">
          <button type="button" className={active("justifyLeft") ? "is-active" : ""} aria-label={t("detail.alignLeft")} data-tooltip={t("detail.alignLeft")} disabled={disabled} onMouseDown={toolbarMouseDown} onClick={() => runCommand("justifyLeft")}>L</button>
          <button type="button" className={active("justifyCenter") ? "is-active" : ""} aria-label={t("detail.alignCenter")} data-tooltip={t("detail.alignCenter")} disabled={disabled} onMouseDown={toolbarMouseDown} onClick={() => runCommand("justifyCenter")}>C</button>
          <button type="button" className={active("justifyRight") ? "is-active" : ""} aria-label={t("detail.alignRight")} data-tooltip={t("detail.alignRight")} disabled={disabled} onMouseDown={toolbarMouseDown} onClick={() => runCommand("justifyRight")}>R</button>
          <button type="button" className={active("justifyFull") ? "is-active" : ""} aria-label={t("detail.justify")} data-tooltip={t("detail.justify")} disabled={disabled} onMouseDown={toolbarMouseDown} onClick={() => runCommand("justifyFull")}>J</button>
        </div>
        <div className="rich-text-editor-group">
          <button ref={linkButtonRef} type="button" className={active("createLink") ? "is-active" : ""} aria-label={t("detail.link")} data-tooltip={t("detail.link")} disabled={disabled} onMouseDown={toolbarMouseDown} onClick={openLink}>↗</button>
        </div>
      </div>
      {linkOpen ? (
        <div ref={linkPopoverRef} className="rich-text-editor-link-popover" role="dialog" aria-label={t("detail.editLink")}>
          <input value={linkText} onChange={(event) => setLinkText(event.target.value)} placeholder={t("detail.linkText")} aria-label={t("detail.linkText")} />
          <input value={linkUrl} onChange={(event) => setLinkUrl(event.target.value)} onBlur={(event) => setLinkUrl(normalizeHref(event.target.value))} placeholder="https://" aria-label={t("detail.linkUrl")} aria-describedby={linkError ? "rich-text-link-error" : undefined} autoFocus />
          {linkError ? <p id="rich-text-link-error" role="alert">{linkError}</p> : null}
          <div>
            <button type="button" onClick={applyLink}>{t("detail.apply")}</button>
            <button type="button" onClick={removeLink}>{t("detail.remove")}</button>
            <button type="button" onClick={() => { openedLinkRef.current = null; setLinkOpen(false); }}>{t("detail.cancel")}</button>
          </div>
        </div>
      ) : null}
      <div
        ref={editorRef}
        className="rich-text-editor-surface"
        contentEditable={!disabled}
        role="textbox"
        aria-multiline="true"
        aria-label={placeholder}
        data-placeholder={placeholder}
        suppressContentEditableWarning
        onInput={() => emit(editorRef.current?.innerHTML || "")}
        onPaste={handlePaste}
        onKeyDown={handleKeyDown}
        onBlur={saveSelection}
      />
    </div>
  );
});
