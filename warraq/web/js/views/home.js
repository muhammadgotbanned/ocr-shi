// Home: hero, drop zone with quick options, model onboarding, library.
import { h, clear, timeAgo, fmtBytes } from "../lib/dom.js";
import { icon } from "../lib/icons.js";
import { api } from "../lib/api.js";
import { t, getLang } from "../lib/i18n.js";
import { state, notify, getSetting } from "../state.js";
import { toast, chips, select, toggle, bar, ring, confirmDialog } from "../components/ui.js";

const TARGETS = ["English", "French", "German", "Spanish", "Turkish", "Urdu", "Persian", "Malay", "Indonesian", "Russian", "Chinese", "Italian", "Portuguese", "Dutch", "Bengali", "Hindi"];

export class HomeView {
  constructor(app) {
    this.app = app;
    this.el = h("div.view.scroll");
    this.inner = h("div.home");
    this.el.appendChild(this.inner);
    this.render();
  }

  render() {
    clear(this.inner);
    this.inner.append(this.hero(), this.modelsNotice(), this.dropzone(), this.libraryTitle(), (this.lib = h("div.library")));
    this.renderLibrary();
  }

  hero() {
    return h("div.hero", {},
      h("div.calligraphy", { lang: "ar", text: "ورّاق" }),
      h("h1", { text: t("tagline") }),
      h("p", { text: t("taglineSub") }),
    );
  }

  modelsNotice() {
    const wrap = h("div");
    this.noticeWrap = wrap;
    this.renderNotice();
    return wrap;
  }

  renderNotice() {
    const wrap = this.noticeWrap;
    if (!wrap) return;
    clear(wrap);
    const sys = state.system;
    if (!sys) return;
    const m = state.model || {};
    const missing = !sys.models.ocr.downloaded || !sys.models.layout.downloaded;
    const downloading = m.stage === "download" && !m.done;
    if (!missing && !downloading) return;
    const frac = m.fraction || 0;
    const right = downloading
      ? h("div.row", {}, ring(frac), h("div.mono", { text: `${Math.round(frac * 100)}%` }))
      : h("button.btn.primary", { onclick: async () => { await api.downloadModels(); state.model = { stage: "download", fraction: 0 }; this.renderNotice(); } }, icon("download"), t("download"));
    const desc = downloading && m.total ? `${t("downloading")} ${m.model || ""} · ${fmtBytes(m.downloaded)} / ${fmtBytes(m.total)}` : t("modelsDesc");
    wrap.appendChild(
      h("div.notice", {},
        h("div.ico", {}, icon("cube")),
        h("div", { style: { flex: 1, minWidth: 0 } }, h("div.t", { text: t("modelsTitle") }), h("div.d", { text: desc }), downloading ? bar(frac) : null),
        right,
      ),
    );
  }

  dropzone() {
    const input = h("input", { type: "file", multiple: true, accept: ".pdf,.png,.jpg,.jpeg,.tif,.tiff,.bmp,.webp,.gif,.heic,.heif,.jp2", class: "hidden", onchange: (e) => this.app.upload([...e.target.files]) });
    const dz = h("div.dropzone", {
      onclick: (e) => {
        if (e.target.closest(".dz-options")) return;
        this.app.openFiles(input);
      },
      onmousemove: (e) => {
        const r = dz.getBoundingClientRect();
        dz.style.setProperty("--mx", `${e.clientX - r.left}px`);
        dz.style.setProperty("--my", `${e.clientY - r.top}px`);
      },
    },
      h("div.dz-icon", {}, icon("upload")),
      h("div.dz-title", { text: t("drop") }),
      h("div.dz-sub", { html: t("dropSub") }),
      h("div.dz-formats", { text: t("dropFormats") }),
      this.options(),
      input,
    );
    this.dz = dz;
    return dz;
  }

  options() {
    const presets = state.presets || {};
    const presetOptions = [{ value: "", label: getLang() === "ar" ? "إعداداتي الحالية" : "My settings" }, ...Object.entries(presets).map(([k, v]) => ({ value: k, label: v.title }))];
    const current = localStorage.getItem("warraq.preset") || "";
    const presetSel = select(presetOptions, current, (v) => localStorage.setItem("warraq.preset", v));
    const fmtField = (state.schema.find((s) => s.key === "output") || { fields: [] }).fields.find((f) => f.name === "formats");
    const fmtOptions = (fmtField?.options || []).map((o) => ({ value: o.value, label: o.value.toUpperCase() }));
    const fmts = chips(fmtOptions, getSetting("output.formats") || ["txt", "pdf"], (v) => this.app.saveSettings({ output: { formats: v } }, true));
    const trOn = !!getSetting("translation.enabled");
    const lang = getSetting("translation.target_language") || "English";
    const langSel = select(TARGETS.map((x) => ({ value: x, label: x })), lang, (v) => this.app.saveSettings({ translation: { target_language: v } }, true));
    langSel.disabled = !trOn;
    const tr = toggle(trOn, (v) => { langSel.disabled = !v; this.app.saveSettings({ translation: { enabled: v } }, true); });
    return h("div.dz-options", {},
      h("div", {}, h("div.label", { text: t("preset") }), presetSel),
      h("div", {}, h("div.label", { text: t("translate") }), h("div.translate-row", {}, tr, langSel)),
      h("div", { style: { gridColumn: "1 / -1" } }, h("div.label", { text: t("outputs") }), fmts),
    );
  }

  libraryTitle() {
    return h("div.section-title", {}, h("h2", { text: t("recent") }), h("span.faint", { text: state.jobs.length ? `${state.jobs.length}` : "" }));
  }

  renderLibrary() {
    const lib = this.lib;
    if (!lib) return;
    clear(lib);
    if (!state.jobs.length) {
      lib.appendChild(h("div.empty", { style: { gridColumn: "1 / -1" } }, t("emptyLibrary")));
      return;
    }
    for (const job of state.jobs) lib.appendChild(this.card(job));
  }

  card(job) {
    const doc = state.docs[job.id];
    const first = doc && doc.pages && doc.pages[0];
    const thumb = first && first.thumb_path ? h("img", { src: api.pageUrl(job.id, first.thumb_path), loading: "lazy", alt: "" }) : h("div.placeholder", {}, icon("page"));
    const st = job.status;
    const label = t(st) || st;
    const badgeCls = st === "done" ? "done" : st === "running" || st === "queued" || st === "paused" ? "running" : st === "error" ? "error" : "";
    const frac = job.total_pages ? job.done_pages / job.total_pages : 0;
    const del = h("button.icon-btn.del", {
      title: t("delete"),
      onclick: async (e) => {
        e.stopPropagation();
        if (await confirmDialog(t("delete"), t("deleteConfirm"), t("delete"), true)) {
          await api.deleteJob(job.id);
          state.jobs = state.jobs.filter((j) => j.id !== job.id);
          this.renderLibrary();
        }
      },
    }, icon("trash"));
    return h("div.card", { onclick: () => (location.hash = `#/doc/${job.id}`) },
      h("div.thumb", {}, thumb),
      h("div.badge", {}, h(`span.status-badge.${badgeCls}`, {}, st === "running" ? h("span.spinner", { style: { width: "10px", height: "10px", borderWidth: "1.5px" } }) : null, label)),
      del,
      h("div.meta", {},
        h("div.title", { text: job.name, title: job.name }),
        h("div.sub", {}, h("span", { text: job.total_pages ? `${job.total_pages} ${t("pages")}` : "" }), h("span", { text: "·" }), h("span", { text: timeAgo(job.created, getLang()) })),
      ),
      st === "running" ? h("div.progress", {}, bar(frac, !job.total_pages)) : null,
    );
  }

  update(kind) {
    if (kind === "jobs" || kind === "job") this.renderLibrary();
    if (kind === "model" || kind === "system") this.renderNotice();
    if (kind === "settings-reloaded") this.render();
  }

  setDragging(on) {
    if (this.dz) this.dz.classList.toggle("drag", on);
  }
}
