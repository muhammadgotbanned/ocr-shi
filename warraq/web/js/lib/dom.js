// Minimal DOM helpers: hyperscript, selection, events.

export function h(tag, props = {}, ...children) {
  const [name, ...classes] = tag.split(".");
  const el = name === "svg" || props.svg ? document.createElementNS("http://www.w3.org/2000/svg", name) : document.createElement(name || "div");
  if (classes.length) el.setAttribute("class", classes.join(" "));
  for (const [k, v] of Object.entries(props || {})) {
    if (v === undefined || v === null || v === false || k === "svg") continue;
    if (k === "class") el.setAttribute("class", [el.getAttribute("class"), v].filter(Boolean).join(" "));
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === "html") el.innerHTML = v;
    else if (k === "text") el.textContent = v;
    else if (v === true) el.setAttribute(k, "");
    else if (k in el && !(el instanceof SVGElement) && typeof v !== "string") el[k] = v;
    else el.setAttribute(k, v);
  }
  append(el, children);
  return el;
}

export function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

export function clear(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
  return el;
}

export function html(str) {
  const t = document.createElement("template");
  t.innerHTML = str.trim();
  return t.content.firstElementChild;
}

export function debounce(fn, ms = 300) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

export function fmtBytes(n) {
  if (!n && n !== 0) return "";
  const u = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i ? 1 : 0)} ${u[i]}`;
}

export function fmtDuration(s) {
  if (!s || s < 1) return "";
  if (s < 60) return `${Math.round(s)}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${Math.round(s % 60)}s`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

export function timeAgo(ts, lang = "en") {
  const d = (Date.now() / 1000 - ts);
  const rtf = new Intl.RelativeTimeFormat(lang, { numeric: "auto" });
  if (d < 60) return rtf.format(-Math.round(d), "second");
  if (d < 3600) return rtf.format(-Math.round(d / 60), "minute");
  if (d < 86400) return rtf.format(-Math.round(d / 3600), "hour");
  return rtf.format(-Math.round(d / 86400), "day");
}

export function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}
