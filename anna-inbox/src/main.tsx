import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./app/App";
import "./styles/global.css";

// Chrome 会把 ResizeObserver 投递延迟报告为 error；无功能影响，但 Anna host 会打成 iframe error。
const RESIZE_OBSERVER_LOOP =
  /ResizeObserver loop (?:completed with undelivered notifications|limit exceeded)/i;
window.addEventListener(
  "error",
  (event) => {
    if (!RESIZE_OBSERVER_LOOP.test(event.message || "")) return;
    event.stopImmediatePropagation();
    event.preventDefault();
  },
  true,
);

const root = document.getElementById("root");
if (!root) throw new Error("Missing root element");

createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
