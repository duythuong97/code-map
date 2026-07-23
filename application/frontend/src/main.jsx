import React from "react";
import { createRoot } from "react-dom/client";
import "@xyflow/react/dist/style.css";
import "./styles.css";
import CodeFlowApp from "./CodeFlowApp";

createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <CodeFlowApp />
  </React.StrictMode>,
);