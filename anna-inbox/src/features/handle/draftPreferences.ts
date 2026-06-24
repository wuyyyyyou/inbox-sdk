import { DEFAULT_DRAFT_PREFERENCES, type DraftPreferenceField, type DraftPreferences } from "../../types/mail";

export const DRAFT_PREFERENCE_LABELS: Record<DraftPreferenceField, string> = {
  length: "Length",
  writingStyle: "Writing style",
  tone: "Tone",
  mood: "Mood",
};

export function resolveDraftPreferences(preferences?: Partial<DraftPreferences> | null): DraftPreferences {
  return {
    ...DEFAULT_DRAFT_PREFERENCES,
    ...(preferences || {}),
  };
}

export function buildDraftPreferencesInstruction(
  preferences?: Partial<DraftPreferences> | null,
  freeformInstruction = "",
  options: { replyGoal?: string; userTake?: string; hasExistingDraft?: boolean } = {},
): string {
  const resolved = resolveDraftPreferences(preferences);
  const customInstruction = freeformInstruction.trim();
  const replyGoal = String(options.replyGoal || "").trim();
  const userTake = String(options.userTake || "").trim();
  const hasExistingDraft = Boolean(options.hasExistingDraft);
  const preferenceLines = (Object.keys(DRAFT_PREFERENCE_LABELS) as DraftPreferenceField[]).map((field) => (
    `- ${DRAFT_PREFERENCE_LABELS[field]}: ${resolved[field]}`
  ));
  const preferenceBlock = [
    "Use these draft preferences:",
    ...preferenceLines,
    "",
    "Keep the reply natural, specific, and ready to send. Avoid generic AI-sounding phrasing.",
  ].join("\n");

  const parts: string[] = [];
  if (!hasExistingDraft && (replyGoal || userTake)) {
    const intentLines = ["User reply intent:"];
    if (replyGoal) intentLines.push(`- Reply goal: ${replyGoal}`);
    if (userTake) intentLines.push(`- Your take: ${userTake}`);
    parts.push(intentLines.join("\n"));
  }
  if (customInstruction) {
    parts.push(customInstruction);
  }
  parts.push(preferenceBlock);
  return parts.join("\n\n");
}
