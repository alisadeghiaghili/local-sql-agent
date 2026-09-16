/* web/js/icons.js — inline SVG icon set for product chrome.
 *
 * No emoji in chrome: render variance, poor contrast on navy, no
 * accessible name story. Icons are 16×16, 1.5px stroke, currentColor,
 * decorative by default (aria-hidden) unless the caller passes a label.
 *
 * Built with DOM APIs so CSP `script-src 'self'` stays clean — no
 * inline event handlers, no innerHTML of untrusted text.
 *
 * See docs/design/DESIGN.md §7.
 */

"use strict";

/** @type {Record<string, string>} path data only — viewBox 0 0 24 24. */
const PATHS = {
  menu: "M4 7h16M4 12h16M4 17h16",
  close: "M6 6l12 12M18 6L6 18",
  sun: "M12 4v2m0 12v2M4 12H2m20 0h-2M6.3 6.3l1.4 1.4m8.6 8.6l1.4 1.4M6.3 17.7l1.4-1.4m8.6-8.6l1.4-1.4M12 8a4 4 0 100 8 4 4 0 000-8z",
  moon: "M20 14.5A8.5 8.5 0 019.5 4a7 7 0 1010.5 10.5z",
  theme: "M12 3a9 9 0 100 18 9 9 0 000-18zm0 0v18",
  memory: "M9 8a3 3 0 016 0v1h1a2 2 0 012 2v6a2 2 0 01-2 2H8a2 2 0 01-2-2v-6a2 2 0 012-2h1V8zm3 4v4m-2-2h4",
  refresh: "M4 12a8 8 0 0114-5.3M20 12a8 8 0 01-14 5.3M18 4v4h-4M6 20v-4h4",
  copy: "M8 8h10v10H8zM6 16H4V4h12v2",
  download: "M12 4v10m0 0l-4-4m4 4l4-4M5 19h14",
  chevronDown: "M6 9l6 6 6-6",
  chevronUp: "M6 15l6-6 6 6",
  check: "M5 12l5 5L20 7",
  alert: "M12 8v5m0 3h.01M10.3 4.3L2.5 18a2 2 0 001.7 3h15.6a2 2 0 001.7-3L13.7 4.3a2 2 0 00-3.4 0z",
  database: "M4 6c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3zm0 0v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6m-16 6c0 1.7 3.6 3 8 3s8-1.3 8-3",
  shield: "M12 3l8 3v6c0 5-3.5 8.5-8 10-4.5-1.5-8-5-8-10V6l8-3z",
  pin: "M12 3v4m0 0l-2 6h4l-2-6zm0 10v8",
  edit: "M4 20h4l10-10-4-4L4 16v4zm10-12l4 4",
  // Lid + body + two score lines on the same 24×24 stroke grid as the rest
  // of the set — kept purely geometric (no emoji glyph variance across
  // platforms) so the delete action reads identically everywhere.
  trash: "M4 7h16M9 7V4h6v3M6 7l1 13a2 2 0 002 2h6a2 2 0 002-2l1-13M10 11v6m4-6v6",
};

/**
 * Create an SVG icon element.
 *
 * @param {keyof typeof PATHS} name
 * @param {{ className?: string, label?: string, size?: number }} [opts]
 * @returns {SVGSVGElement}
 *
 * @example
 * const el = icon("menu", { label: "فهرست" });
 */
export function icon(name, opts = {}) {
  const { className = "", label, size = 16 } = opts;
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", String(size));
  svg.setAttribute("height", String(size));
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.5");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  if (className) svg.setAttribute("class", `icon icon-${name} ${className}`.trim());
  else svg.setAttribute("class", `icon icon-${name}`);
  if (label) {
    svg.setAttribute("role", "img");
    const title = document.createElementNS(NS, "title");
    title.textContent = label;
    svg.appendChild(title);
  } else {
    svg.setAttribute("aria-hidden", "true");
  }
  const path = document.createElementNS(NS, "path");
  path.setAttribute("d", PATHS[name] || "");
  svg.appendChild(path);
  return svg;
}

/**
 * Replace the text content of *host* with a single icon (keeps any
 * trailing text node the caller already put there via textContent split).
 *
 * @param {HTMLElement} host
 * @param {keyof typeof PATHS} name
 * @param {{ className?: string, label?: string, size?: number }} [opts]
 * @returns {SVGSVGElement}
 */
export function mountIcon(host, name, opts = {}) {
  const svg = icon(name, opts);
  host.insertBefore(svg, host.firstChild);
  return svg;
}
