import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const styles = readFileSync(new URL("./global.css", import.meta.url), "utf8");

describe("mail tabs manage tooltip", () => {
  it("resets the tab underline positioning before applying tooltip positioning", () => {
    const tooltipRule = styles.match(
      /\.mail-tabs \.mail-tabs-manage::after,[\s\S]*?\r?\n}\r?\n/,
    )?.[0];

    expect(tooltipRule).toContain("right: auto;");
    expect(tooltipRule).toContain("bottom: auto;");
    expect(tooltipRule).toContain("height: auto;");
  });
});

describe("mail feed overflow", () => {
  it("prevents row action overlays from creating a horizontal scrollbar", () => {
    const feedRule = styles.match(/\.mail-feed \{[\s\S]*?\r?\n}\r?\n/)?.[0];

    expect(feedRule).toContain("overflow-x: clip;");
    expect(feedRule).toContain("overflow-y: auto;");
    expect(feedRule).toContain("scrollbar-gutter: stable;");
  });

  it("keeps webkit scrollbar chrome stable for the mail feed", () => {
    expect(styles).toContain(".mail-feed::-webkit-scrollbar {");
    expect(styles).toContain(".mail-feed::-webkit-scrollbar-thumb {");
    expect(styles).toContain(".mail-feed::-webkit-scrollbar-track {");
  });
});

describe("inbox selection toolbar", () => {
  it("uses a consistent icon-button size and exposes its tooltips", () => {
    const clearRule = styles.match(/\.inbox-selection-clear \{[\s\S]*?\r?\n}\r?\n/)?.[0];
    const tooltipRule = styles.match(
      /\.inbox-selection-clear::after,[\s\S]*?\r?\n}\r?\n/,
    )?.[0];

    expect(clearRule).toContain("width: 32px;");
    expect(clearRule).toContain("height: 32px;");
    expect(tooltipRule).toContain("content: attr(data-tooltip);");
    expect(tooltipRule).toContain("top: calc(100% + 8px);");
  });
});

describe("AI sidebar collapse", () => {
  it("hides collapsed chrome with display:none so the expand logo stays visible", () => {
    expect(styles).toContain(".ai-sidebar.is-collapsed .new-chat-btn");
    expect(styles).toMatch(
      /\.ai-sidebar\.is-collapsed \.new-chat-btn,[\s\S]*?display:\s*none;/,
    );
    expect(styles).toContain(".ai-sidebar.is-collapsed .anna-wordmark > button");
    expect(styles).toContain("z-index: 21;");
  });
});

describe("mail detail composer controls", () => {
  it("uses icon reply and forward actions with visible tooltips", () => {
    expect(styles).toContain(".mail-detail-reply-action {");
    expect(styles).toContain(".mail-detail-reply-action::after {");
    expect(styles).toContain(".mail-detail-footer {");
    expect(styles).toContain("--mail-detail-footer-size: 51px;");
    expect(styles).toContain("height: var(--mail-detail-footer-size);");
    expect(styles).toContain("  overflow: visible;");
    expect(styles).toContain("bottom: calc(100% + 8px);");
  });

  it("keeps forward recipients aligned and gives the contact menu enough width", () => {
    expect(styles).toMatch(
      /\.mail-detail-forward-to \.compose-field \{\r?\n\s*align-items: center;/,
    );
    expect(styles).toContain(".mail-detail-forward-to .compose-contact-menu {");
    expect(styles).toContain("width: min(420px, max(260px, calc(100vw - 48px)));");
  });
});
