import { describe, expect, it } from "vitest";
import {
  buildEmailSrcDoc,
  emailDocumentStyles,
  escapeHtmlText,
  isSafeInlineImageUrl,
  splitTextParagraphs,
} from "./SafeEmailHtml";

describe("SafeEmailHtml helpers", () => {
  it("uses a visible fallback when sanitization leaves no markup", () => {
    expect(buildEmailSrcDoc("")).toContain("This message has no display content.");
    expect(buildEmailSrcDoc("   ")).toContain("<p>This message has no display content.</p>");
  });

  it("includes responsive overflow rules for long email content", () => {
    const styles = emailDocumentStyles();
    expect(styles).toContain("overflow-wrap:anywhere");
    expect(styles).toContain(".safe-email-table-wrap");
    expect(styles).toContain("pre{max-width:100%;overflow:auto");
  });

  it("accepts only local image URLs for cid replacements", () => {
    expect(isSafeInlineImageUrl("blob:https://mail.invalid/image")).toBe(true);
    expect(isSafeInlineImageUrl("data:image/png;base64,abc")).toBe(true);
    expect(isSafeInlineImageUrl("https://tracker.invalid/image.png")).toBe(false);
    expect(isSafeInlineImageUrl("javascript:alert(1)")).toBe(false);
  });

  it("escapes text before placing it in fallback markup", () => {
    expect(escapeHtmlText(`<img src="x"> & 'quoted'`)).toBe(
      "&lt;img src=&quot;x&quot;&gt; &amp; &#39;quoted&#39;",
    );
  });

  it("keeps long plain-text paragraphs separate", () => {
    expect(splitTextParagraphs(" first\n\n second ")).toEqual(["first", "second"]);
  });
});
