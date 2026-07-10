import { describe, expect, it } from "vitest";
import { mailAvatarFallback, senderParts, splitAddresses } from "./mailIdentity";

describe("mailIdentity", () => {
  it("parses display names and addresses", () => {
    expect(senderParts("Jane Doe <jane@example.com>")).toEqual({ name: "Jane Doe", email: "jane@example.com" });
  });

  it("splits address lists without breaking display names", () => {
    expect(splitAddresses('"Anna Team" <team@anna.ai>, Bob <bob@example.com>')).toEqual([
      '"Anna Team" <team@anna.ai>',
      "Bob <bob@example.com>",
    ]);
  });

  it("keeps fallback avatars stable for the same displayed sender", () => {
    const digest = mailAvatarFallback("noreply@luma-mail.com", "Luma");
    const support = mailAvatarFallback("support@luma.com", "Luma");
    expect(digest.initial).toBe("L");
    expect(support).toEqual(digest);
  });

  it("uses the sender name for ordinary contacts", () => {
    const avatar = mailAvatarFallback("Alice Example <alice@example.com>");
    expect(avatar.initial).toBe("A");
    expect(avatar.key).toBe("aliceexample");
  });
});
