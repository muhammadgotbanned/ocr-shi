// Shared UI components: toasts, modal, menu, command palette, form controls.
import { h, $, clear } from "../lib/dom.js";
import { icon } from "../lib/icons.js";
import { t } from "../lib/i18n.js";

/* ---------------- toasts ---------------- */
let toastRoot;
export function toast(message, kind = "info", ms = 2800) {
  if (!toastRoot) {
    toastRoot = h("div.toasts");
    document.body.appendChild(toastRoot);
  }
  const ic = kind === "ok" ? "check" : kind === "err" ? "alert" : "info";
  const el = h(`div.toast.${kind}`, {}, icon(ic), h("span", { text: message }));
  toastRoot.appendChild(el);
  setTimeout(() => {
    el.classList.add("out");
    setTimeout(() => el.remove(), 300);
  }, ms);
}

/* ---------------- modal ---------------- */
export function confirmDialog(title, message, okLabel = "OK", danger = false) {
  return new Promise((resolve) => {
    const close = (v) => { scrim.remove(); box.remove(); resolve(v); };
    const scrim = h("div.scrim", { onclick: () => close(false) });
    const box = h(
      "div.modal",
      { role: "dialog" },
      h("h3", { text: title }),
      h("div.muted", { text: message }),
      h("div.actions", {},
        h("button.btn.ghost", { onclick: () => close(false) }, t("cancel")),
        h(`button.btn${danger ? ".danger" : ".primary"}`, { onclick: () => close(true) }, okLabel),
      ),
    );
    document.body.append(scrim, box);
    const onKey = (e) => {
      if (e.key === "Escape") { close(false); document.removeEventListener("keydown", onKey); }
      if (e.key === "Enter") { close(true); document.removeEventListener("keydown", onKey); }
    };
    document.addEventListener("keydown", onKey);
  });
}

/* ---------------- dropdown menu ---------------- */
export function menu(anchor, items) {
  document.querySelectorAll(".menu").forEach((m) => m.remove());
  const r = anchor.getBoundingClientRect();
  const el = h("div.menu");
  for (const it of items) {
    if (it === "-") { el.appendChild(h("hr")); continue; }
    el.appendChild(
      h("button", { onclick: () => { el.remove(); it.run(); }, disabled: it.disabled }, it.icon ? icon(it.icon) : null, h("span", { text: it.label }), it.hint ? h("span.hint", { text: it.hint }) : null),
    );
  }
  document.body.appendChild(el);
  const w = el.offsetWidth;
  const left = Math.min(window.innerWidth - w - 10, Math.max(10, r.right - w));
  el.style.left = `${left}px`;
  el.style.top = `${r.bottom + 6}px`;
  setTimeout(() => {
    const off = (e) => { if (!el.contains(e.target)) { el.remove(); document.removeEventListener("mousedown", off); } };
    document.addEventListener("mousedown", off);
  });
  return el;
}

/* ---------------- command palette ---------------- */
export function palette(commands) {
  document.querySelectorAll(".palette, .scrim.pal").forEach((m) => m.remove());
  const scrim = h("div.scrim.pal", { onclick: () => close() });
  const input = h("input.input", { placeholder: t("palette"), autofocus: true });
  const list = h("ul");
  const box = h("div.palette", {}, input, list);
  let sel = 0;
  let shown = commands;
  const close = () => { scrim.remove(); box.remove(); };
  const render = () => {
    const q = input.value.trim().toLowerCase();
    shown = commands.filter((c) => !q || c.label.toLowerCase().includes(q) || (c.keywords || "").includes(q));
    sel = Math.min(sel, Math.max(0, shown.length - 1));
    clear(list);
    shown.forEach((c, i) => {
      list.appendChild(
        h(`li${i === sel ? ".on" : ""}`, { onmouseenter: () => { sel = i; render(); }, onclick: () => { close(); c.run(); } }, icon(c.icon || "chevron"), h("span", { text: c.label }), c.hint ? h("span.kbd", { text: c.hint }) : null),
      );
    });
  };
  input.addEventListener("input", () => { sel = 0; render(); });
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { sel = Math.min(shown.length - 1, sel + 1); render(); e.preventDefault(); }
    else if (e.key === "ArrowUp") { sel = Math.max(0, sel - 1); render(); e.preventDefault(); }
    else if (e.key === "Enter" && shown[sel]) { close(); shown[sel].run(); }
    else if (e.key === "Escape") close();
  });
  document.body.append(scrim, box);
  render();
  input.focus();
}

/* ---------------- form controls ---------------- */
export function toggle(value, onChange) {
  const input = h("input", { type: "checkbox", checked: !!value, onchange: (e) => onChange(e.target.checked) });
  return h("label.toggle", {}, input, h("span.track"), h("span.knob"));
}

export function segmented(options, value, onChange) {
  const el = h("div.seg", { role: "radiogroup" });
  const render = (v) => {
    clear(el);
    for (const o of options) {
      el.appendChild(h(`button${o.value === v ? ".on" : ""}`, { type: "button", onclick: () => { render(o.value); onChange(o.value); } }, o.label));
    }
  };
  render(value);
  return el;
}

export function select(options, value, onChange) {
  const el = h("select.select", { onchange: (e) => { const o = options[e.target.selectedIndex]; onChange(o.value); } });
  options.forEach((o) => {
    const opt = h("option", { text: o.label });
    if (o.value === value || String(o.value) === String(value)) opt.selected = true;
    el.appendChild(opt);
  });
  return el;
}

export function chips(options, values, onChange) {
  const el = h("div.chips");
  const set = new Set(values);
  const render = () => {
    clear(el);
    for (const o of options) {
      el.appendChild(
        h(`button.chip${set.has(o.value) ? ".on" : ""}`, {
          type: "button",
          onclick: () => {
            if (set.has(o.value)) { if (set.size > 1) set.delete(o.value); } else set.add(o.value);
            render();
            onChange(options.map((x) => x.value).filter((v) => set.has(v)));
          },
        }, set.has(o.value) ? icon("check") : null, o.label),
      );
    }
  };
  render();
  return el;
}

export function range(min, max, step, value, onChange) {
  const num = h("input.input.num", { type: "number", min, max, step, value });
  const slider = h("input", { type: "range", min, max, step, value });
  const paint = () => slider.style.setProperty("--p", `${((slider.value - min) / (max - min)) * 100}%`);
  slider.addEventListener("input", () => { num.value = slider.value; paint(); });
  slider.addEventListener("change", () => onChange(Number(slider.value)));
  num.addEventListener("change", () => {
    let v = Math.min(max, Math.max(min, Number(num.value)));
    num.value = v; slider.value = v; paint(); onChange(v);
  });
  paint();
  return h("div.range", {}, slider, num);
}

export function textInput(value, onChange, opts = {}) {
  const el = h(opts.multiline ? "textarea.input" : "input.input", { value: value ?? "", placeholder: opts.placeholder || "", type: opts.secret ? "password" : "text", spellcheck: false });
  if (opts.multiline) el.value = value ?? "";
  if (opts.suggestions && opts.suggestions.length) {
    const id = `dl-${Math.random().toString(36).slice(2)}`;
    el.setAttribute("list", id);
    const dl = h("datalist", { id }, ...opts.suggestions.map((s) => h("option", { value: s })));
    el.addEventListener("focus", () => { if (!document.getElementById(id)) document.body.appendChild(dl); });
  }
  el.addEventListener("change", () => onChange(el.value));
  return el;
}

export function pairsEditor(value, onChange, placeholders = ["", ""]) {
  const rows = (value || []).map((p) => [p[0] || "", p[1] || ""]);
  const el = h("div.pairs");
  const emit = () => onChange(rows.filter((r) => r[0] || r[1]));
  const render = () => {
    clear(el);
    rows.forEach((r, i) => {
      const a = h("input.input", { value: r[0], placeholder: placeholders[0], dir: "auto", onchange: (e) => { rows[i][0] = e.target.value; emit(); } });
      const b = h("input.input", { value: r[1], placeholder: placeholders[1], dir: "auto", onchange: (e) => { rows[i][1] = e.target.value; emit(); } });
      const del = h("button.icon-btn", { type: "button", title: t("delete"), onclick: () => { rows.splice(i, 1); render(); emit(); } }, icon("close"));
      el.appendChild(h("div.pair", {}, a, b, del));
    });
    el.appendChild(h("button.btn.sm", { type: "button", onclick: () => { rows.push(["", ""]); render(); } }, icon("plus"), "Add"));
  };
  render();
  return el;
}

export function ring(fraction, size = 38) {
  const r = 15;
  const c = 2 * Math.PI * r;
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 36 36");
  svg.setAttribute("class", "ring");
  svg.style.setProperty("--size", `${size}px`);
  svg.innerHTML = `<circle class="bg" cx="18" cy="18" r="${r}"/><circle class="fg" cx="18" cy="18" r="${r}" stroke-dasharray="${c}" stroke-dashoffset="${c * (1 - Math.max(0, Math.min(1, fraction || 0)))}"/>`;
  return svg;
}

export function bar(fraction, indeterminate = false) {
  const i = h("i", { style: { width: `${Math.round((fraction || 0) * 100)}%` } });
  return h(`div.bar${indeterminate ? ".indeterminate" : ""}`, {}, i);
}
