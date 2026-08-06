export async function triggerBrowserDownload(
  url: string,
  filename: string,
  ownerDocument: Document = document,
): Promise<void> {
  const isBlobUrl = url.startsWith("blob:");
  let downloadUrl = url;
  let materializedUrl = "";
  if (!isBlobUrl) {
    const response = await fetch(url, { cache: "no-store" });
    if (!response.ok) throw new Error(`Attachment fetch failed with ${response.status}`);
    const blob = await response.blob();
    materializedUrl = URL.createObjectURL(blob);
    downloadUrl = materializedUrl;
  }
  const link = ownerDocument.createElement("a");
  link.href = downloadUrl;
  link.download = filename || "attachment";
  link.rel = "noopener noreferrer";
  link.style.display = "none";
  ownerDocument.body.appendChild(link);
  try {
    link.click();
  } finally {
    link.remove();
    if (materializedUrl) {
      const schedule = ownerDocument.defaultView?.setTimeout || globalThis.setTimeout;
      schedule(() => URL.revokeObjectURL(materializedUrl), 30_000);
    }
  }
}
