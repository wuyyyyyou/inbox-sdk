export function triggerBrowserDownload(
  url: string,
  filename: string,
  ownerDocument: Document = document,
) {
  const link = ownerDocument.createElement("a");
  link.href = url;
  link.download = filename || "attachment";
  link.rel = "noopener noreferrer";
  link.style.display = "none";
  ownerDocument.body.appendChild(link);
  try {
    link.click();
  } finally {
    link.remove();
  }
}
