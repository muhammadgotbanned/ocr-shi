// Settings sheet, generated from the server's settings schema.
import { h, clear, escapeHtml } from "../lib/dom.js";
import { icon } from "../lib/icons.js";
import { api } from "../lib/api.js";
import { t } from "../lib/i18n.js";
import { state } from "../state.js";
import { toast, toggle, segmented, select, chips, range, textInput, pairsEditor, confirmDialog } from "../components/ui.js";

const ICONS = { sliders: "sliders", chip: "chip", cube: "cube", image: "image", layout: "layout", eye: "eye", type: "type", footnote: "footnote", margin: "margin", download: "download", globe: "globe" };

export class SettingsSheet {
  constructor(app, section) {
    this.app = app;
    this.section = section || localStorage.getItem("warraq.settingsSection") || "general";
    this.query = "";
    this.advanced = localStorage.getItem("warraq.advanced") === "1";
    this.scrim = h("div.scrim", { onclick: () => this.close() });
    this.nav = h("nav.sheet-nav");
    this.bodyEl = h("div.sheet-body");
    const search = h("div.search", {}, icon("search"), (this.searchInput = h("input.input", { placeholder: t("search"), oninput: (e) => { this.query = e.target.value.trim().toLowerCase(); this.renderBody(); } })));
    const adv = h("label.row", { style: { gap: "8px", fontSize: "12.5px", color: "var(--text-2)" } }, toggle(this.advanced, (v) => { this.advanced = v; localStorage.setItem("warraq.advanced", v ? "1" : "0"); this.renderBody(); this.renderNav(); }), t("advanced"));
    const head = h("div.sheet-head", {}, search, h("div.spacer"), adv, h("button.icon-btn", { title: t("cancel"), onclick: () => this.close() }, icon("close")));
    this.el = h("div.sheet", { role: "dialog" }, this.nav, h("div.sheet-main", {}, head, this.bodyEl));
    document.body.append(this.scrim, this.el);
    this.onKey = (e) => { if (e.key === "Escape") this.close(); };
    document.addEventListener("keydown", this.onKey);
    this.renderNav();
    this.renderBody();
    setTimeout(() => this.searchInput.focus(), 50);
  }

  close() {
    document.removeEventListener("keydown", this.onKey);
    this.scrim.remove();
    this.el.remove();
    this.app.settingsSheet = null;
    if (location.hash.startsWith("#/settings")) history.back();
  }

  visibleFields(section) {
    return section.fields.filter((f) => (this.advanced || !f.advanced || this.query) && this.matches(f) && this.shown(f));
  }

  matches(f) {
    if (!this.query) return true;
    const hay = `${f.title} ${f.description} ${f.key} ${(f.options || []).map((o) => o.label).join(" ")}`.toLowerCase();
    return hay.includes(this.query);
  }

  shown(f) {
    if (!f.show_if) return true;
    return Object.entries(f.show_if).every(([path, v]) => {
      const [s, k] = path.split(".");
      return state.settings[s] && state.settings[s][k] === v;
    });
  }

  renderNav() {
    clear(this.nav);
    this.nav.appendChild(h("div.title", { text: t("settings") }));
    for (const s of state.schema) {
      const count = this.visibleFields(s).length;
      this.nav.appendChild(
        h(`button.nav-item${!this.query && this.section === s.key ? ".on" : ""}`, { onclick: () => { this.section = s.key; localStorage.setItem("warraq.settingsSection", s.key); this.query = ""; this.searchInput.value = ""; this.renderNav(); this.renderBody(); } },
          icon(ICONS[s.icon] || "sliders"), h("span", { text: s.title }), h("span.n", { text: String(count) })),
      );
    }
    this.nav.appendChild(h("div", { style: { height: "18px" } }));
    this.nav.appendChild(h("button.nav-item", { onclick: async () => { if (await confirmDialog(t("resetAll"), "Restore every setting to its default value?", t("resetAll"), true)) { const r = await api.resetSettings(); this.app.applySettings(r.values); this.renderBody(); toast(t("saved"), "ok"); } } }, icon("refresh"), h("span", { text: t("resetAll") })));
  }

  renderBody() {
    clear(this.bodyEl);
    const sections = this.query ? state.schema : state.schema.filter((s) => s.key === this.section);
    if (!this.query && this.section === "general") this.bodyEl.appendChild(this.presetsBlock());
    if (!this.query && this.section === "hardware") this.bodyEl.appendChild(this.hardwareBlock());
    let any = false;
    for (const s of sections) {
      const fields = this.visibleFields(s);
      if (!fields.length) continue;
      any = true;
      const group = h("div.group", {}, h("h3", {}, icon(ICONS[s.icon] || "sliders"), s.title));
      for (const f of fields) group.appendChild(this.field(s, f));
      if (!this.query) {
        group.appendChild(h("div", { style: { marginTop: "12px" } }, h("button.btn.sm.ghost", { onclick: async () => { const r = await api.resetSettings(s.key); this.app.applySettings(r.values); this.renderBody(); toast(`${s.title} · ${t("reset")}`, "ok"); } }, icon("refresh"), `${t("reset")} ${s.title}`)));
      }
      this.bodyEl.appendChild(group);
    }
    if (!any) this.bodyEl.appendChild(h("div.cta", {}, h("div.ico", {}, icon("search")), "No settings match."));
  }

  presetsBlock() {
    const wrap = h("div", {}, h("div.group", { style: { paddingTop: "18px" } }, h("h3", {}, icon("wand"), t("presets"))));
    const grid = h("div.presets");
    for (const [key, p] of Object.entries(state.presets)) {
      grid.appendChild(h("button.preset", { onclick: async () => { const r = await api.applyPreset(key); this.app.applySettings(r.values); this.renderBody(); toast(`${p.title} ✓`, "ok"); } }, h("b", { text: p.title }), h("span", { text: p.description })));
    }
    wrap.appendChild(grid);
    return wrap;
  }

  hardwareBlock() {
    const sys = state.system;
    const hw = sys && sys.hardware;
    const best = hw && hw.devices && hw.devices[0];
    const ocr = sys && sys.models && sys.models.ocr;
    const wrap = h("div.hw", {},
      h("div.box", {}, h("div.k", { text: t("device") }), h("div.v", { text: best ? best.name : "—" })),
      h("div.box", {}, h("div.k", { text: t("memory") }), h("div.v", { text: best && best.memory_gb ? `${best.memory_gb} GB` : hw && hw.ram_gb ? `${hw.ram_gb} GB RAM` : "—" })),
      h("div.box", {}, h("div.k", { text: "PyTorch" }), h("div.v", { text: hw ? `${hw.torch || "—"}${hw.cuda ? " · CUDA " + hw.cuda : ""}` : "—" })),
    );
    const extra = h("div", { style: { margin: "10px 0 0", display: "flex", gap: "8px", alignItems: "center" } },
      h("span.faint", { style: { fontSize: "12.5px" }, text: ocr && ocr.loaded ? `OCR model loaded on ${ocr.device_label || ocr.device}` : "Models load on the first job." }),
      h("div.spacer"),
      ocr && ocr.loaded ? h("button.btn.sm", { onclick: async () => { await api.unloadModels(); toast(t("unload"), "ok"); this.app.refreshSystem(); } }, icon("chip"), t("unload")) : null,
    );
    const warn = (hw && hw.warnings || []).map((w) => h("div.notice", { style: { marginTop: "12px" } }, h("div.ico", {}, icon("alert")), h("div.d", { text: w })));
    return h("div", {}, wrap, extra, ...warn);
  }

  field(section, f) {
    const values = state.settings[section.key] || {};
    const value = values[f.name];
    const save = (v) => this.app.saveSettings({ [section.key]: { [f.name]: v } }).then(() => this.refreshDependent(f));
    let ctl;
    let wide = false;
    switch (f.type) {
      case "bool":
        ctl = toggle(value, save);
        break;
      case "select":
        ctl = f.options.length <= 3 && f.options.every((o) => String(o.label).length < 24) ? segmented(f.options, value, save) : select(f.options, value, save);
        break;
      case "multiselect":
        ctl = chips(f.options, value || [], save);
        wide = true;
        break;
      case "int":
      case "float":
        ctl = f.min !== undefined && f.max !== undefined ? range(f.min, f.max, f.step || (f.type === "int" ? 1 : 0.05), value, save) : textInput(value, (v) => save(Number(v)));
        break;
      case "pairs":
        ctl = pairsEditor(value, save, f.name === "glossary" ? ["Arabic term", "Translation"] : ["Find", "Replace with"]);
        wide = true;
        break;
      case "textarea":
        ctl = textInput(value, save, { multiline: true });
        wide = true;
        break;
      case "secret":
        ctl = textInput(value, save, { secret: true });
        break;
      case "folder":
        ctl = h("div.row", { style: { width: "100%" } }, textInput(value, save, { placeholder: "Default" }), state.desktop ? h("button.btn.sm", { onclick: async () => { const r = await api.dialog("folder"); if (r.paths && r.paths[0]) { await save(r.paths[0]); this.renderBody(); } } }, icon("folder")) : null);
        break;
      default:
        ctl = textInput(value, save, { suggestions: f.suggestions, placeholder: f.placeholder });
    }
    const changed = JSON.stringify(value) !== JSON.stringify(f.default);
    const title = h("div.ft", { html: this.hl(f.title) + (f.advanced ? ` <span class="tag">ADV</span>` : "") });
    const el = h(`div.field${wide ? ".wide" : ""}${changed ? ".changed" : ""}`, {}, h("div", {}, title, f.description ? h("div.fd", { html: this.hl(f.description) }) : null), h("div.ctl", {}, ctl));
    el.dataset.key = f.key;
    return el;
  }

  refreshDependent(f) {
    // fields with show_if may need to appear/disappear
    if (state.schema.some((s) => s.fields.some((x) => x.show_if && Object.keys(x.show_if).includes(f.key)))) this.renderBody();
    this.renderNav();
  }

  hl(text) {
    const safe = escapeHtml(text || "");
    if (!this.query) return safe;
    const i = safe.toLowerCase().indexOf(this.query);
    if (i < 0) return safe;
    return `${safe.slice(0, i)}<mark>${safe.slice(i, i + this.query.length)}</mark>${safe.slice(i + this.query.length)}`;
  }
}
