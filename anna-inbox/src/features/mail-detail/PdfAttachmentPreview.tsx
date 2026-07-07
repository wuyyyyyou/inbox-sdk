import { Component, useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import type {
  PDFDocumentProxy,
  PDFDocumentLoadingTask,
  PDFPageProxy,
  RenderTask,
} from "pdfjs-dist";
import pdfWorkerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";

type PdfPageProps = {
  document: PDFDocumentProxy;
  pageNumber: number;
  width: number;
  onRendered: (pageNumber: number) => void;
  onError: (reason: unknown) => void;
};

function PdfPage({ document, pageNumber, width, onRendered, onError }: PdfPageProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    let disposed = false;
    let page: PDFPageProxy | null = null;
    let renderTask: RenderTask | null = null;

    const render = async () => {
      try {
        page = await document.getPage(pageNumber);
        if (disposed || !canvasRef.current) return;
        const baseViewport = page.getViewport({ scale: 1 });
        const cssScale = width / baseViewport.width;
        // Embedded WebViews have a substantially smaller renderer memory budget than a full browser.
        // Rendering every page at devicePixelRatio=2 can allocate tens of MB per canvas and restart the view.
        const pixelRatio = 1;
        const viewport = page.getViewport({ scale: cssScale * pixelRatio });
        const canvas = canvasRef.current;
        const context = canvas.getContext("2d", { alpha: false });
        if (!context) throw new Error("PDF canvas is unavailable.");
        canvas.width = Math.max(1, Math.floor(viewport.width));
        canvas.height = Math.max(1, Math.floor(viewport.height));
        canvas.style.width = `${Math.floor(viewport.width / pixelRatio)}px`;
        canvas.style.height = `${Math.floor(viewport.height / pixelRatio)}px`;
        renderTask = page.render({ canvas, canvasContext: context, viewport });
        await renderTask.promise;
        if (!disposed) onRendered(pageNumber);
      } catch (reason) {
        if (!disposed && !(reason instanceof Error && reason.name === "RenderingCancelledException")) {
          onError(reason);
        }
      }
    };

    void render();
    return () => {
      disposed = true;
      renderTask?.cancel();
      page?.cleanup();
    };
  }, [document, onError, onRendered, pageNumber, width]);

  return <canvas ref={canvasRef} aria-label={`PDF page ${pageNumber}`} />;
}

function PdfAttachmentPreviewContent({ url, onReady }: { url: string; onReady?: () => void }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [document, setDocument] = useState<PDFDocumentProxy | null>(null);
  const [width, setWidth] = useState(0);
  const [renderedPages, setRenderedPages] = useState<Set<number>>(() => new Set());
  const [error, setError] = useState("");

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const updateWidth = () => {
      const nextWidth = Math.max(1, Math.min(1100, Math.floor(container.clientWidth)));
      setWidth((current) => current === nextWidth ? current : nextWidth);
    };
    updateWidth();
    if (typeof window.ResizeObserver === "function") {
      const observer = new window.ResizeObserver(updateWidth);
      observer.observe(container);
      return () => observer.disconnect();
    }
    // Some Anna Edge runtimes do not expose ResizeObserver. An uncaught constructor error here
    // tears down the whole React tree, which looks like the app jumping back to Home.
    window.addEventListener("resize", updateWidth);
    return () => window.removeEventListener("resize", updateWidth);
  }, []);

  useEffect(() => {
    let disposed = false;
    let loadingTask: PDFDocumentLoadingTask | null = null;
    setDocument(null);
    setRenderedPages(new Set());
    setError("");
    const load = async () => {
      try {
        const { GlobalWorkerOptions, getDocument } = await import("pdfjs-dist");
        if (disposed) return;
        GlobalWorkerOptions.workerSrc = pdfWorkerUrl;
        loadingTask = getDocument({ url });
        const loadedDocument = await loadingTask.promise;
        if (disposed) {
          void loadedDocument.destroy();
          return;
        }
        setDocument(loadedDocument);
      } catch (reason) {
        if (!disposed) setError(reason instanceof Error ? reason.message : String(reason));
      }
    };
    void load();
    return () => {
      disposed = true;
      void loadingTask?.destroy();
    };
  }, [url]);

  const markRendered = useCallback((pageNumber: number) => {
    setRenderedPages((current) => {
      if (current.has(pageNumber)) return current;
      const next = new Set(current);
      next.add(pageNumber);
      return next;
    });
  }, []);

  const reportError = useCallback((reason: unknown) => {
    setError(reason instanceof Error ? reason.message : String(reason));
  }, []);

  useEffect(() => {
    if (document && renderedPages.size === document.numPages) onReady?.();
  }, [document, onReady, renderedPages]);

  return (
    <div className="pdf-attachment-preview" ref={containerRef}>
      {!error && (!document || renderedPages.size < document.numPages) ? (
        <p className="pdf-attachment-progress">
          {document ? `Rendering PDF pages ${renderedPages.size}/${document.numPages}…` : "Loading PDF…"}
        </p>
      ) : null}
      {error ? <p className="pdf-attachment-error">Unable to render PDF: {error}</p> : null}
      {document && width > 0 ? (
        <div className="pdf-attachment-pages">
          {Array.from({ length: document.numPages }, (_, index) => {
            const pageNumber = index + 1;
            return (
              <PdfPage
                key={`${pageNumber}-${width}`}
                document={document}
                pageNumber={pageNumber}
                width={width}
                onRendered={markRendered}
                onError={reportError}
              />
            );
          })}
        </div>
      ) : null}
    </div>
  );
}

type PdfPreviewBoundaryState = { error: string };

class PdfPreviewBoundary extends Component<{ children: ReactNode }, PdfPreviewBoundaryState> {
  state: PdfPreviewBoundaryState = { error: "" };

  static getDerivedStateFromError(reason: unknown): PdfPreviewBoundaryState {
    return { error: reason instanceof Error ? reason.message : String(reason) };
  }

  render() {
    if (this.state.error) {
      return (
        <div className="pdf-attachment-preview">
          <p className="pdf-attachment-error">Unable to render PDF: {this.state.error}</p>
        </div>
      );
    }
    return this.props.children;
  }
}

export function PdfAttachmentPreview({ url, onReady }: { url: string; onReady?: () => void }) {
  return (
    <PdfPreviewBoundary key={url}>
      <PdfAttachmentPreviewContent url={url} onReady={onReady} />
    </PdfPreviewBoundary>
  );
}
