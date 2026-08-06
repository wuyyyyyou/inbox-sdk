import { describe, expect, it } from "vitest";
import {
  getListEnterAction,
  plainTextToEditorHtml,
  sanitizeEditorStyle,
} from "./RichTextEditor";

describe("RichTextEditor pure helpers", () => {
  it("round-trips plain text lines without allowing markup", () => {
    const html = plainTextToEditorHtml("Hello <script>alert(1)</script>\nSecond");
    expect(html).toContain("&lt;script&gt;alert(1)&lt;/script&gt;");
    expect(html).toBe("<p>Hello &lt;script&gt;alert(1)&lt;/script&gt;</p><p>Second</p>");
  });

  it("allows only the editor's style vocabulary", () => {
    expect(sanitizeEditorStyle("color: red; position: fixed; text-align: justify; font-size: 99px")).toBe("text-align: justify");
    expect(sanitizeEditorStyle("color: #dc2626; font-size: 12px")).toBe("font-size: 12px; color: #dc2626");
  });

  it("normalizes supported pasted style values and drops unsafe CSS", () => {
    expect(sanitizeEditorStyle("color: rgb(37, 99, 235); position: fixed; text-align: justify; font-size: 99px")).toBe("color: #2563eb; text-align: justify");
  });

  it("keeps Enter inside empty list items", () => {
    expect(getListEnterAction(true)).toBe("split");
    expect(getListEnterAction(false)).toBe("split");
  });
});
