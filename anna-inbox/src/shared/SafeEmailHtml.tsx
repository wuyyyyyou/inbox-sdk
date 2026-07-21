import { useEffect, useMemo, useRef, useState } from "react";
import DOMPurify from "dompurify";

const TEXT_FLOW_BLOCK_SELECTOR = [
  "address", "article", "aside", "blockquote", "details", "dl", "fieldset",
  "figure", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "main", "nav",
  "ol", "p", "pre", "section", "table", "ul",
].join(",");
function normalizeLinks(root: DocumentFragment | HTMLElement) {
  root.querySelectorAll("a[href]").forEach((node) => {
    node.setAttribute("target", "_blank");
    node.setAttribute("rel", "noopener noreferrer");
  });
}

function normalizeDocumentMarkup(root: HTMLElement) {
  root.querySelectorAll("meta, title, base, link").forEach((node) => node.remove());

  const htmlNode = root.querySelector("html");
  if (htmlNode) {
    while (htmlNode.firstChild) {
      root.appendChild(htmlNode.firstChild);
    }
    htmlNode.remove();
  }

  const bodyNode = root.querySelector("body");
  if (bodyNode) {
    const bodyWrapper = document.createElement("div");
    copyAttributes(bodyNode, bodyWrapper);
    while (bodyNode.firstChild) {
      bodyWrapper.appendChild(bodyNode.firstChild);
    }
    bodyNode.replaceWith(bodyWrapper);
  }

  const styles = Array.from(root.querySelectorAll("head style"));
  styles.reverse().forEach((node) => root.prepend(node));
  root.querySelectorAll("head").forEach((node) => node.remove());
}

function isWhitespaceNode(node: ChildNode) {
  return node.nodeType === Node.TEXT_NODE && !(node.textContent || "").replace(/\u00a0/g, " ").trim();
}

function isLineBreak(node: ChildNode) {
  return node.nodeType === Node.ELEMENT_NODE && (node as Element).tagName.toLowerCase() === "br";
}

function hasReadableContent(nodes: ChildNode[]) {
  return nodes.some((node) => {
    if (isLineBreak(node) || isWhitespaceNode(node)) return false;
    if (node.nodeType === Node.TEXT_NODE) return Boolean((node.textContent || "").replace(/\u00a0/g, " ").trim());
    return Boolean((node.textContent || "").replace(/\u00a0/g, " ").trim()) || (node as Element).children.length > 0;
  });
}

function copyAttributes(from: Element, to: Element) {
  Array.from(from.attributes).forEach((attribute) => {
    to.setAttribute(attribute.name, attribute.value);
  });
}

function paragraphFrom(nodes: ChildNode[], attributesFrom?: Element) {
  const paragraph = document.createElement("p");
  if (attributesFrom) copyAttributes(attributesFrom, paragraph);
  nodes.forEach((node) => paragraph.appendChild(node));
  return paragraph;
}

function splitNodesIntoParagraphs(nodes: ChildNode[], attributesFrom?: Element) {
  const paragraphs: HTMLParagraphElement[] = [];
  let current: ChildNode[] = [];
  let pendingBreaks = 0;

  const flushCurrent = () => {
    if (!hasReadableContent(current)) {
      current = [];
      return;
    }
    paragraphs.push(paragraphFrom(current, attributesFrom));
    current = [];
  };

  const flushPendingBreaks = () => {
    if (pendingBreaks >= 2) {
      flushCurrent();
    } else if (pendingBreaks === 1 && current.length) {
      current.push(document.createElement("br"));
    }
    pendingBreaks = 0;
  };

  nodes.forEach((node) => {
    if (isLineBreak(node)) {
      pendingBreaks += 1;
      return;
    }
    if (pendingBreaks && isWhitespaceNode(node)) return;
    flushPendingBreaks();
    if (!isWhitespaceNode(node) || current.length) {
      current.push(node);
    }
  });

  flushCurrent();
  return paragraphs;
}

function splitParagraphBreaks(root: HTMLElement) {
  Array.from(root.querySelectorAll("p")).forEach((node) => {
    if (!node.querySelector("br")) return;
    const paragraphs = splitNodesIntoParagraphs(Array.from(node.childNodes), node);
    if (!paragraphs.length) {
      node.replaceChildren();
      return;
    }
    if (paragraphs.length <= 1) {
      node.replaceChildren(...Array.from(paragraphs[0].childNodes));
      return;
    }
    node.replaceWith(...paragraphs);
  });

  if (!root.querySelector(TEXT_FLOW_BLOCK_SELECTOR) && root.querySelector("br")) {
    const paragraphs = splitNodesIntoParagraphs(Array.from(root.childNodes));
    if (paragraphs.length) {
      root.replaceChildren(...paragraphs);
    }
  }
}

function normalizeTextEmailBlocks(root: HTMLElement) {
  // Gmail commonly uses adjacent divs as visual lines. Keeping those divs
  // preserves a single line break; converting each one to a paragraph adds
  // browser paragraph margins that are not present in Gmail (notably in
  // signatures such as "Best," followed by a name).
  splitParagraphBreaks(root);
}

function splitTextParagraphs(text: string) {
  return text
    .replace(/\r\n?/g, "\n")
    .split(/\n{2,}/)
    .map((paragraph) => paragraph.trim())
    .filter(Boolean);
}

export function SafeEmailHtml({ html, className = "" }: { html: string; className?: string; scaleToFit?: boolean }) {
  const frameRef = useRef<HTMLIFrameElement | null>(null);
  const [frameHeight, setFrameHeight] = useState(160);
  const [documentRevision, setDocumentRevision] = useState(0);
  const sanitized = useMemo(() => {
    const value = DOMPurify.sanitize(html, {
      USE_PROFILES: { html: true },
      FORBID_TAGS: ["script", "iframe", "object", "embed", "form"],
      FORBID_ATTR: ["onerror", "onload", "onclick", "onmouseover"],
      ALLOW_UNKNOWN_PROTOCOLS: false,
      RETURN_DOM_FRAGMENT: true,
    });
    normalizeLinks(value as DocumentFragment);
    const container = document.createElement("div");
    container.appendChild(value as DocumentFragment);
    normalizeDocumentMarkup(container);
    return container.innerHTML;
  }, [html]);
  const srcDoc = useMemo(() => `<!doctype html>
<html><head><meta name="viewport" content="width=device-width, initial-scale=1"><style>html,body{margin:0;padding:0}img{max-width:100%;height:auto}</style></head><body>${sanitized}</body></html>`, [sanitized]);

  useEffect(() => {
    const frame = frameRef.current;
    const document = frame?.contentDocument;
    if (!frame || !document?.body) return;

    let raf = 0;
    // 不用 ResizeObserver：跨文档 observe body 并改 iframe 高度会触发 Chrome
    // “ResizeObserver loop …” 并被 Anna host 当成 iframe error。
    const measure = () => {
      window.cancelAnimationFrame(raf);
      raf = window.requestAnimationFrame(() => {
        const nextHeight = Math.max(
          document.body.scrollHeight,
          document.documentElement.scrollHeight,
          1,
        );
        setFrameHeight((current) => (current === nextHeight ? current : nextHeight));
      });
    };

    measure();
    const mutationObserver = new MutationObserver(measure);
    mutationObserver.observe(document.body, {
      attributes: true,
      characterData: true,
      childList: true,
      subtree: true,
    });
    const images = Array.from(document.images);
    images.forEach((image) => {
      image.addEventListener("load", measure);
      image.addEventListener("error", measure);
    });
    window.addEventListener("resize", measure);
    void document.fonts?.ready?.then(measure);
    return () => {
      window.cancelAnimationFrame(raf);
      mutationObserver.disconnect();
      images.forEach((image) => {
        image.removeEventListener("load", measure);
        image.removeEventListener("error", measure);
      });
      window.removeEventListener("resize", measure);
    };
  }, [documentRevision, srcDoc]);


  return (
    <iframe
      className={`${className} safe-email-frame safe-email-html`}
      ref={frameRef}
      srcDoc={srcDoc}
      sandbox="allow-same-origin allow-popups"
      title="Email content"
      style={{ height: frameHeight }}
      onLoad={() => setDocumentRevision((current) => current + 1)}
    />
  );
}

export function SafeEmailText({ text, className = "" }: { text: string; className?: string }) {
  const paragraphs = useMemo(() => splitTextParagraphs(text), [text]);

  return (
    <div className={`${className} safe-email-text safe-email-plain`}>
      {paragraphs.length ? paragraphs.map((paragraph, index) => <p key={index}>{paragraph}</p>) : <p>This message has no display content.</p>}
    </div>
  );
}
