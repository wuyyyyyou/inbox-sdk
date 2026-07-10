import { useEffect, useMemo, useRef, useState } from "react";
import DOMPurify from "dompurify";

const TEXT_FLOW_BLOCK_SELECTOR = [
  "address", "article", "aside", "blockquote", "details", "dl", "fieldset",
  "figure", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "main", "nav",
  "ol", "p", "pre", "section", "table", "ul",
].join(",");
const MIN_FIXED_EMAIL_SCALE = 0.72;

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

  root.querySelectorAll("head").forEach((node) => node.remove());
}

function hasFixedEmailLayout(root: HTMLElement) {
  if (root.querySelector("table, tbody, thead, tfoot, tr, td, th, colgroup, col, center")) {
    return true;
  }
  if (Array.from(root.querySelectorAll("[width]")).some((node) => {
    const width = Number((node.getAttribute("width") || "").replace(/px$/i, ""));
    return Number.isFinite(width) && width >= 320;
  })) {
    return true;
  }
  return Array.from(root.querySelectorAll("[style]")).some((node) => {
    const style = (node.getAttribute("style") || "").toLowerCase();
    return /(?:^|;)\s*display\s*:\s*(?:table|inline-block|flex|grid)/.test(style)
      || /(?:^|;)\s*(?:width|min-width|max-width)\s*:\s*(?:[3-9]\d{2,}|[1-9]\d{3,})px/.test(style)
      || /(?:^|;)\s*margin(?:-left|-right)?\s*:\s*auto/.test(style);
  });
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

export function SafeEmailHtml({ html, className = "", scaleToFit = false }: { html: string; className?: string; scaleToFit?: boolean }) {
  const frameRef = useRef<HTMLDivElement | null>(null);
  const contentRef = useRef<HTMLDivElement | null>(null);
  const [fit, setFit] = useState({ scale: 1, width: 0, height: 0 });
  const { sanitized, shouldScale } = useMemo(() => {
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
    const fixedLayout = scaleToFit && hasFixedEmailLayout(container);
    if (!fixedLayout) {
      normalizeTextEmailBlocks(container);
    }
    return {
      sanitized: container.innerHTML,
      shouldScale: fixedLayout,
    };
  }, [html, scaleToFit]);

  useEffect(() => {
    if (!shouldScale) {
      setFit({ scale: 1, width: 0, height: 0 });
      return;
    }
    const frame = frameRef.current;
    const content = contentRef.current;
    if (!frame || !content) return;

    let raf = 0;
    const measure = () => {
      window.cancelAnimationFrame(raf);
      raf = window.requestAnimationFrame(() => {
        const frameWidth = Math.max(1, frame.clientWidth);
        const contentWidth = Math.max(content.scrollWidth, content.offsetWidth, frameWidth);
        const rawScale = Math.min(1, frameWidth / contentWidth);
        const scale = rawScale < MIN_FIXED_EMAIL_SCALE ? 1 : rawScale;
        setFit({
          scale,
          width: contentWidth,
          height: Math.ceil(content.scrollHeight * scale),
        });
      });
    };

    measure();
    const ResizeObserverCtor = window.ResizeObserver;
    const observer = ResizeObserverCtor ? new ResizeObserverCtor(measure) : null;
    observer?.observe(frame);
    observer?.observe(content);
    const images = Array.from(content.querySelectorAll("img"));
    images.forEach((image) => image.addEventListener("load", measure));
    window.addEventListener("resize", measure);
    return () => {
      window.cancelAnimationFrame(raf);
      observer?.disconnect();
      images.forEach((image) => image.removeEventListener("load", measure));
      window.removeEventListener("resize", measure);
    };
  }, [sanitized, shouldScale]);

  if (!shouldScale) {
    return <div className={`${className} safe-email-text`} dangerouslySetInnerHTML={{ __html: sanitized }} />;
  }

  return (
    <div
      className={`${className} safe-email-frame`}
      ref={frameRef}
      style={fit.height ? { height: fit.height } : undefined}
    >
      <div
        className="safe-email-scaled"
        ref={contentRef}
        style={{
          transform: `scale(${fit.scale})`,
          width: fit.width ? fit.width : undefined,
        }}
        dangerouslySetInnerHTML={{ __html: sanitized }}
      />
    </div>
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
