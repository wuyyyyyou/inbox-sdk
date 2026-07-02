import { useMemo, useState } from "react";
import { buildSnoozePresets, formatAbsoluteDateTime } from "../mail-detail/mailDetailHelpers";

export function SnoozePicker({
  open,
  onClose,
  onSubmit,
}: {
  open: boolean;
  onClose: () => void;
  onSubmit: (isoTime: string) => void;
}) {
  const [error, setError] = useState("");
  const [customOpen, setCustomOpen] = useState(false);
  const [customValue, setCustomValue] = useState("");
  const presets = useMemo(() => buildSnoozePresets(new Date()), [open]);

  if (!open) return null;

  const commit = (date: Date | null) => {
    if (!date || Number.isNaN(date.getTime())) {
      setError("Enter a valid time like 4 hours or tomorrow.");
      return;
    }
    setError("");
    onSubmit(date.toISOString());
  };

  return (
    <div className="snooze-picker-layer" role="dialog" aria-modal="true" aria-label="Snooze until">
      <button className="snooze-picker-backdrop" aria-label="Close snooze picker" onClick={onClose} />
      <div className="snooze-picker">
        <header>
          <strong>SNOOZE UNTIL</strong>
        </header>
        {error ? <p className="snooze-picker-error">{error}</p> : null}
        <div className="snooze-picker-list">
          {presets.map((preset) => (
            <button key={preset.id} onClick={() => commit(preset.at)}>
              <span>{preset.label}</span>
              <small>{formatAbsoluteDateTime(preset.at.toISOString())}</small>
            </button>
          ))}
          <button onClick={() => setCustomOpen((value) => !value)}>
            <span>Pick a time</span>
            <small>{customValue ? formatAbsoluteDateTime(new Date(customValue).toISOString()) : "Choose date & time"}</small>
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
            <button onClick={() => commit(customValue ? new Date(customValue) : null)}>Set time</button>
          </div>
        ) : null}
      </div>
    </div>
  );
}
