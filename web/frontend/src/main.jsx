import React from "react";
import { createRoot } from "react-dom/client";
import "@xyflow/react/dist/style.css";
import "./styles.css";
import BusinessApp from "./business/BusinessApp.jsx";

createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <BusinessApp />
  </React.StrictMode>,
);