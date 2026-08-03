import { useMemo, useState } from "react";
import { buildSnoozePresets, formatAbsoluteDateTime } from "../mail-detail/mailDetailHelpers";
import { useI18n } from "../../i18n";

export function SnoozePicker({
  open,
  onClose,
  onSubmit,
}: {
  open: boolean;
  onClose: () => void;
  onSubmit: (isoTime: string) => void;
}) {
  const { t, locale } = useI18n();
  const [error, setError] = useState("");
  const [customOpen, setCustomOpen] = useState(false);
  const [customValue, setCustomValue] = useState("");
  const presets = useMemo(() => buildSnoozePresets(new Date()), [open]);

  const presetLabel = (id: string): string => {
    switch (id) {
      case "tomorrow": return t("snooze.tomorrow");
      case "weekend": return t("snooze.weekend");
      case "next_week": return t("snooze.nextWeek");
      default: return id;
    }
  };

  if (!open) return null;

  const commit = (date: Date | null) => {
    if (!date || Number.isNaN(date.getTime())) {
      setError(t("snooze.invalidTime"));
      return;
    }
    setError("");
    onSubmit(date.toISOString());
  };

  return (
    <div className="snooze-picker-layer" role="dialog" aria-modal="true" aria-label={t("snooze.title")}>
      <button className="snooze-picker-backdrop" aria-label={t("snooze.close")} onClick={onClose} />
      <div className="snooze-picker">
        <header>
          <strong>{t("snooze.title")}</strong>
        </header>
        {error ? <p className="snooze-picker-error">{error}</p> : null}
        <div className="snooze-picker-list">
          {presets.map((preset) => (
            <button key={preset.id} onClick={() => commit(preset.at)}>
              <span>{presetLabel(preset.id)}</span>
              <small>{formatAbsoluteDateTime(preset.at.toISOString(), locale)}</small>
            </button>
          ))}
          <button onClick={() => setCustomOpen((value) => !value)}>
            <span>{t("snooze.pickTime")}</span>
            <small>{customValue ? formatAbsoluteDateTime(new Date(customValue).toISOString(), locale) : t("snooze.chooseDateTime")}</small>
          </button>
        </div>
        {customOpen ? (
          <div className="snooze-picker-custom">
            <input
              type="datetime-local"
              value={customValue}
              onChange={(event) => {
                setCustomValue(event.target.value);
                setError("");
              }}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  event.preventDefault();
                  commit(customValue ? new Date(customValue) : null);
                }
              }}
            />
            <button onClick={() => commit(customValue ? new Date(customValue) : null)}>{t("snooze.setTime")}</button>
          </div>
        ) : null}
      </div>
    </div>
  );
}
