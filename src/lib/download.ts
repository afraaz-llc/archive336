// Shared client-side download helpers.
//
// Used by both VideoDetailPanel (single video) and DownloadPanel
// (bulk channel) to save files / ZIPs from the browser. Browsers
// expose two distinct mechanisms - <a download> for direct URLs and
// Blob URLs for in-memory data - so we wrap both into a single
// surface here.

export function triggerHrefDownload(url: string, filename: string): void {
  const a = document.createElement("a")
  a.href = url
  a.download = filename
  a.rel = "noopener"
  document.body.appendChild(a)
  a.click()
  a.remove()
}

export function triggerBlobDownload(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  triggerHrefDownload(url, filename)
  // Revoke after a tick so the browser has time to start the save.
  window.setTimeout(() => URL.revokeObjectURL(url), 1500)
}
