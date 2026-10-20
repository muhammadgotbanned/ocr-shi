// Warraq GUI bootstrap: state, routing, top bar, live events.
import { h, clear } from "./lib/dom.js";
import { icon } from "./lib/icons.js";
import { api, subscribe } from "./lib/api.js";
import { t, setLang } from "./lib/i18n.js";
import { state, notify, subscribeState, upsertJob, upsertPage } from "./state.js";
import { toast, menu, palette, bar } from "./components/ui.js";
import { HomeView } from "./views/home.js";
import { WorkspaceView } from "./views/workspace.js";
import { SettingsSheet } from "./views/settings.js";

class App {
  constructor(root) {
    this.root = root;
    this.topbar = h("header.topbar");
    this.main = h("main", { style: { flex: 1, minHeight: 0, position: "relative", display: "flex" } });
    root.append(this.topbar, this.main);
    this.view = null;
    this.settingsSheet = null;
    this.dirty = new Set(JSON.parse(localStorage.getItem("warraq.dirty") || "[]"));
    this.lastInteraction = 0;
    for (const ev of ["mousedown", "keydown", "wheel"]) window.addEventListener(ev, () => (this.lastInteraction = Date.now()), { passive: true });
  }

  async start() {
    try {
      const [settings, system, jobs] = await Promise.all([api.settings(), api.system(), api.jobs()]);
      state.schema = settings.schema;
      state.presets = settings.presets;
      state.system = system;
      state.jobs = jobs;
      state.desktop = !!system.desktop;
      this.applySettings(settings.values);
    } catch (e) {
      this.root.appendChild(h("div.cta", {}, `Could not reach the Warraq engine: ${e.message}`));
      return;
    }
    this.preloadThumbs();
    subscribe((e) => this.onEvent(e), (ok) => { state.connected = ok; this.renderStatus(); });
    window.addEventListener("hashchange", () => this.route());
    this.installDragDrop();
    this.installKeys();
    window.matchMedia("(prefers-color-scheme: light)").addEventListener("change", () => this.applyTheme());
    this.route();
  }

  async preloadThumbs() {
    // fetch documents for the newest jobs so cards get thumbnails
    for (const job of state.jobs.slice(0, 24)) {
      if (state.docs[job.id]) continue;
      try {
        const r = await api.job(job.id);
        state.docs[job.id] = r.document || { pages: [] };
      } catch { /* ignore */ }
    }
    if (this.view instanceof HomeView) this.view.renderLibrary();
  }

  /* ------------------------------------------------------------ settings & theme */
  applySettings(values) {
    state.settings = values;
    setLang(values.general.ui_language);
    this.applyTheme();
    notify("settings");
  }

  applyTheme() {
    const g = state.settings ? state.settings.general : { theme: "system", accent: "gold" };
    const light = g.theme === "light" || (g.theme === "system" && window.matchMedia("(prefers-color-scheme: light)").matches);
    document.documentElement.dataset.theme = light ? "light" : "dark";
    document.documentElement.dataset.accent = g.accent || "gold";
  }

  async saveSettings(patch, quiet = false) {
    try {
      const r = await api.patchSettings(patch);
      const langBefore = state.settings.general.ui_language;
      this.applySettings(r.values);
      if (!quiet) toast(t("saved"), "ok", 1200);
      if (langBefore !== r.values.general.ui_language) this.route(true);
      if (patch.models || patch.translation) this.refreshSystem();
    } catch (e) {
      toast(e.message, "err");
    }
  }

  async refreshSystem() {
    try {
      state.system = await api.system();
      notify("system");
      this.renderStatus();
      if (this.view && this.view.update) this.view.update("system");
    } catch { /* ignore */ }
  }

  /* ------------------------------------------------------------ routing */
  route(force = false) {
    const hash = location.hash || "#/";
    const m = hash.match(/^#\/doc\/([^/]+)/);
    if (hash.startsWith("#/settings")) {
      if (!this.view || force) this.show(new HomeView(this));
      if (!this.settingsSheet) this.settingsSheet = new SettingsSheet(this, hash.split("/")[2]);
      this.renderTopbar();
      return;
    }
    if (this.settingsSheet) { const s = this.settingsSheet; this.settingsSheet = null; s.scrim.remove(); s.el.remove(); }
    if (m) {
      if (force || !(this.view instanceof WorkspaceView) || this.view.jobId !== m[1]) this.show(new WorkspaceView(this, m[1]));
    } else if (force || !(this.view instanceof HomeView)) {
      this.show(new HomeView(this));
    }
    this.renderTopbar();
  }

  show(view) {
    clear(this.main);
    this.view = view;
    this.main.appendChild(view.el);
  }

  /* ------------------------------------------------------------ top bar */
  renderTopbar() {
    clear(this.topbar);
    const brand = h("div.brand", { onclick: () => (location.hash = "#/") },
      h("div.mark", {}, icon("logo")),
      h("div", {}, h("div.name", { text: "Warraq" })),
      h("div.name-ar", { text: "ورّاق" }),
    );
    this.topbar.appendChild(brand);
    const v = this.view;
    if (v instanceof WorkspaceView) {
      const job = v.job;
      this.topbar.appendChild(h("div.crumbs", {}, h("span.sep", { text: "/" }), h("span.doc-name", { text: job ? job.name : "" })));
      this.topbar.appendChild((this.jobbar = h("div.jobbar")));
      this.renderJobbar();
    }
    this.topbar.appendChild(h("div.spacer"));
    this.topbar.appendChild((this.statusEl = h("div")));
    this.renderStatus();
    if (v instanceof WorkspaceView) {
      this.topbar.appendChild(h("button.btn.sm", { onclick: (e) => this.exportMenu(e.currentTarget, v.jobId) }, icon("download"), t("export"), icon("chevronDown")));
    }
    this.topbar.append(
      h("button.icon-btn", { title: "Command palette (Ctrl/⌘ K)", onclick: () => this.openPalette() }, icon("command")),
      h("button.icon-btn", { title: t("toggleTheme"), onclick: () => this.toggleTheme() }, icon(document.documentElement.dataset.theme === "light" ? "moon" : "sun")),
      h("button.icon-btn", { title: `${t("settings")} (Ctrl/⌘ ,)`, onclick: () => this.openSettings() }, icon("settings")),
    );
  }

  renderJobbar() {
    const v = this.view;
    if (!(v instanceof WorkspaceView) || !this.jobbar) return;
    clear(this.jobbar);
    const job = v.job;
    if (!job) return;
    const running = ["running", "queued", "paused"].includes(job.status);
    const frac = job.total_pages ? (job.done_pages || 0) / job.total_pages : 0;
    if (running) {
      this.jobbar.append(
        bar(frac, !job.total_pages),
        h("span.msg", { text: `${job.message || t(job.status)}${job.total_pages ? ` · ${job.done_pages || 0}/${job.total_pages}` : ""}` }),
        job.status === "paused"
          ? h("button.icon-btn", { title: t("resume"), onclick: () => api.action(job.id, "resume") }, icon("play"))
          : h("button.icon-btn", { title: t("pause"), onclick: () => api.action(job.id, "pause") }, icon("pause")),
        h("button.icon-btn", { title: t("cancel"), onclick: () => api.action(job.id, "cancel") }, icon("stop")),
      );
    } else {
      const cls = job.status === "done" ? "ok" : job.status === "error" ? "err" : "warn";
      this.jobbar.append(h(`span.pill.${cls}`, {}, h("span.dot"), job.status === "error" ? job.error || t("error") : job.message || t(job.status)));
      if (["error", "interrupted", "cancelled"].includes(job.status)) this.jobbar.append(h("button.btn.sm", { onclick: () => api.action(job.id, "resume") }, icon("refresh"), t("retry")));
      if (this.dirty.has(job.id)) this.jobbar.append(h("button.btn.sm.primary", { onclick: () => this.exportJob(job.id) }, icon("download"), t("export")));
    }
  }

  renderStatus() {
    const el = this.statusEl;
    if (!el) return;
    clear(el);
    const sys = state.system;
    const m = state.model || {};
    if (!state.connected) {
      el.appendChild(h("span.pill.err", {}, h("span.dot"), "Offline"));
      return;
    }
    if (m.stage === "download" && !m.done) {
      el.appendChild(h("span.pill.busy", {}, h("span.dot"), `${t("downloading")} ${m.model || ""} ${Math.round((m.fraction || 0) * 100)}%`));
      return;
    }
    if (m.stage === "model" && m.message) {
      el.appendChild(h("span.pill.busy", {}, h("span.dot"), m.message));
      return;
    }
    const best = sys && sys.hardware && sys.hardware.devices && sys.hardware.devices[0];
    const ocr = sys && sys.models && sys.models.ocr;
    const cpuLabel = best && best.kind === "cpu" ? `CPU · ${sys.hardware.cpu_threads} threads` : "";
    const label = ocr && ocr.loaded && ocr.device_label ? ocr.device_label : best ? cpuLabel || `${best.name}${best.kind === "cuda" && best.memory_gb ? " · " + best.memory_gb + " GB" : ""}` : "";
    const kind = best ? best.kind : "cpu";
    el.appendChild(h(`span.pill.${kind === "cpu" ? "warn" : "ok"}`, { title: kind === "cpu" ? "No GPU detected — running on the CPU" : "GPU acceleration active" }, h("span.dot"), icon("chip"), label || "CPU"));
  }

  toggleTheme() {
    const now = document.documentElement.dataset.theme === "light" ? "dark" : "light";
    this.saveSettings({ general: { theme: now } }, true).then(() => this.renderTopbar());
  }

  openSettings(section) {
    if (this.settingsSheet) return;
    location.hash = section ? `#/settings/${section}` : "#/settings";
  }

  /* ------------------------------------------------------------ jobs */
  options() {
    const preset = localStorage.getItem("warraq.preset") || null;
    return { preset: preset || undefined };
  }

  async openFiles(fallbackInput) {
    if (state.desktop) {
      try {
        const r = await api.dialog("open");
        if (r.paths && r.paths.length) {
          const jobs = await api.fromPaths(r.paths, this.options());
          this.afterCreate(jobs);
        }
        return;
      } catch (e) { /* fall back to the browser picker */ }
    }
    fallbackInput && fallbackInput.click();
  }

  async openFolder() {
    if (!state.desktop) return this.openFiles();
    const r = await api.dialog("folder");
    if (r.paths && r.paths.length) this.afterCreate(await api.fromPaths(r.paths, this.options()));
  }

  async upload(files) {
    files = files.filter((f) => /\.(pdf|png|jpe?g|tiff?|bmp|webp|gif|heic|heif|jp2)$/i.test(f.name));
    if (!files.length) { toast("Please choose PDF or image files.", "err"); return; }
    const big = files.reduce((a, f) => a + f.size, 0) > 40e6;
    let note;
    if (big) note = toast(`Uploading ${files.length} file(s)…`, "info", 60000);
    try {
      const jobs = await api.upload(files, this.options());
      this.afterCreate(jobs);
    } catch (e) {
      toast(e.message, "err");
    }
  }

  afterCreate(jobs) {
    for (const j of jobs) upsertJob(j);
    notify("jobs");
    if (this.view && this.view.update) this.view.update("jobs");
    if (jobs.length === 1) location.hash = `#/doc/${jobs[0].id}`;
    else toast(`${jobs.length} documents queued`, "ok");
  }

  markDirty(jobId) {
    this.dirty.add(jobId);
    localStorage.setItem("warraq.dirty", JSON.stringify([...this.dirty]));
    this.renderJobbar();
  }

  async exportJob(jobId, formats) {
    await api.action(jobId, "export", formats ? { formats } : {});
    this.dirty.delete(jobId);
    localStorage.setItem("warraq.dirty", JSON.stringify([...this.dirty]));
    toast(`${t("export")}…`, "info");
  }

  async translateJob(jobId) {
    await api.action(jobId, "translate");
    toast(`${t("translation")}…`, "info");
  }

  exportMenu(anchor, jobId) {
    const job = state.jobs.find((j) => j.id === jobId) || {};
    const outputs = job.outputs || {};
    const names = { txt: "Text (.txt)", pdf: "Searchable PDF", docx: "Word (.docx)", md: "Markdown", json: "JSON", html: "Reading view (.html)", translation_txt: "Translation (.txt)", bilingual_txt: "Bilingual (.txt)", bilingual_html: "Bilingual reading view", bilingual_docx: "Bilingual Word" };
    const items = Object.keys(outputs).map((fmt) => ({ label: names[fmt] || fmt, icon: "download2", run: () => { window.open(api.fileUrl(jobId, fmt), "_blank"); } }));
    if (items.length) items.push("-");
    items.push({ label: t("openFolder"), icon: "folder", run: () => api.action(jobId, "open-folder").catch((e) => toast(e.message, "err")) });
    items.push({ label: t("exportAll"), icon: "layers", run: () => this.exportJob(jobId, ["txt", "pdf", "docx", "md", "json", "html"]) });
    items.push({ label: `${t("export")} (${(state.settings.output.formats || []).join(", ").toUpperCase()})`, icon: "refresh", run: () => this.exportJob(jobId) });
    items.push("-");
    items.push({ label: t("translateNow"), icon: "globe", run: () => this.translateJob(jobId) });
    items.push({ label: t("retry"), icon: "refresh", run: () => api.action(jobId, "reocr") });
    menu(anchor, items);
  }

  userBusy() {
    return Date.now() - this.lastInteraction < 4000;
  }

  /* ------------------------------------------------------------ events */
  onEvent(e) {
    const type = e.type;
    if (type === "model") {
      state.model = e;
      if (e.stage === "downloaded" || e.stage === "ready" || e.stage === "unloaded" || e.stage === "error") {
        if (e.stage === "error" && !e.replay) toast(e.message || "Model error", "err", 6000);
        state.model = { stage: "idle" };
        this.refreshSystem();
      }
      this.renderStatus();
      if (this.view && this.view.update) this.view.update("model", e);
      return;
    }
    if (type === "job" && e.job) {
      upsertJob(e.job);
      if (e.replay) { /* history replayed on connect: update state silently */ }
      else if (e.status === "done") {
        if (state.settings.general.notify_when_done) chime();
        const job = e.job;
        toast(`${job.name} — ${t("done")}`, "ok", 3600);
        this.refreshSystem();
      } else if (e.status === "error") {
        toast(e.error || t("error"), "err", 7000);
      }
    }
    if (type === "job_deleted") state.jobs = state.jobs.filter((j) => j.id !== e.job_id);
    if (type === "progress" || type === "page_start" || type === "status") {
      const job = state.jobs.find((j) => j.id === e.job_id);
      if (job) {
        if (type === "progress") { job.done_pages = e.done; job.total_pages = e.total; }
        if (type === "page_start") job.message = `${t("running")} ${t("page").toLowerCase()} ${e.source_index + 1}`;
        if (type === "status") job.message = e.message;
      }
    }
    if (type === "page_done" || type === "layout" || type === "page_image") upsertPage(e.job_id, e.page);
    if (type === "reading") state.reading[e.job_id] = { page: e.page, regions: e.regions };
    if (type === "page_done") delete state.reading[e.job_id];
    if (type === "translate") state.translate[e.job_id] = { done: e.done, total: e.total };
    if (type === "log" && e.level === "error" && !e.replay) toast(e.message, "err", 6000);
    if (type === "export" && !this._exportToast) {
      this._exportToast = setTimeout(() => { this._exportToast = null; }, 1500);
    }
    if (this.view && this.view.update) this.view.update(type, e);
    if (["job", "progress", "page_start", "status"].includes(type)) this.renderJobbar();
  }

  /* ------------------------------------------------------------ input */
  installDragDrop() {
    let depth = 0;
    let overlay = null;
    const show = (on) => {
      if (on && !overlay) {
        overlay = h("div.drop-overlay", {}, h("div.box", {}, h("div.big", { text: "اقرأ" }), h("div", { style: { marginTop: "10px", fontSize: "16px", fontWeight: 600 }, text: t("dropHere") })));
        document.body.appendChild(overlay);
      } else if (!on && overlay) { overlay.remove(); overlay = null; }
      if (this.view instanceof HomeView) this.view.setDragging(on);
    };
    window.addEventListener("dragenter", (e) => { if ([...(e.dataTransfer?.types || [])].includes("Files")) { depth++; show(true); e.preventDefault(); } });
    window.addEventListener("dragover", (e) => { if ([...(e.dataTransfer?.types || [])].includes("Files")) e.preventDefault(); });
    window.addEventListener("dragleave", () => { depth = Math.max(0, depth - 1); if (!depth) show(false); });
    window.addEventListener("drop", (e) => {
      e.preventDefault(); depth = 0; show(false);
      const files = [...(e.dataTransfer?.files || [])];
      if (files.length) this.upload(files);
    });
  }

  installKeys() {
    window.addEventListener("keydown", (e) => {
      const mod = e.metaKey || e.ctrlKey;
      if (mod && e.key.toLowerCase() === "k") { e.preventDefault(); this.openPalette(); return; }
      if (mod && e.key === ",") { e.preventDefault(); this.openSettings(); return; }
      if (mod && e.key.toLowerCase() === "o") { e.preventDefault(); this.openFiles(document.querySelector(".dropzone input[type=file]")); return; }
      if (mod && e.key.toLowerCase() === "e" && this.view instanceof WorkspaceView) { e.preventDefault(); this.exportJob(this.view.jobId); return; }
      if (this.view && this.view.onKey && !this.settingsSheet && this.view.onKey(e)) e.preventDefault();
    });
  }

  openPalette() {
    const cmds = [
      { label: t("openFiles"), icon: "upload", hint: "Ctrl O", run: () => { location.hash = "#/"; setTimeout(() => this.openFiles(document.querySelector(".dropzone input[type=file]")), 50); } },
      { label: t("openFolderCmd"), icon: "folder", run: () => this.openFolder() },
      { label: t("goLibrary"), icon: "pages", run: () => (location.hash = "#/") },
      { label: t("settings"), icon: "settings", hint: "Ctrl ,", run: () => this.openSettings() },
      { label: t("toggleTheme"), icon: "moon", run: () => this.toggleTheme() },
      ...state.schema.map((s) => ({ label: `${t("settings")} › ${s.title}`, icon: "sliders", keywords: s.fields.map((f) => f.title.toLowerCase()).join(" "), run: () => this.openSettings(s.key) })),
      ...Object.entries(state.presets).map(([k, p]) => ({ label: `Preset › ${p.title}`, icon: "wand", run: async () => { const r = await api.applyPreset(k); this.applySettings(r.values); toast(`${p.title} ✓`, "ok"); } })),
      { label: t("unload"), icon: "chip", run: async () => { await api.unloadModels(); toast(t("unload"), "ok"); } },
    ];
    if (this.view instanceof WorkspaceView) {
      const id = this.view.jobId;
      cmds.unshift(
        { label: t("export"), icon: "download", hint: "Ctrl E", run: () => this.exportJob(id) },
        { label: t("translateNow"), icon: "globe", run: () => this.translateJob(id) },
        { label: t("openFolder"), icon: "folder", run: () => api.action(id, "open-folder") },
        { label: t("rereadPage"), icon: "refresh", run: () => this.view.rereadPage() },
      );
    }
    for (const job of state.jobs.slice(0, 12)) cmds.push({ label: job.name, icon: "page", keywords: "open document", run: () => (location.hash = `#/doc/${job.id}`) });
    palette(cmds);
  }
}

function chime() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const now = ctx.currentTime;
    [659.25, 987.77].forEach((f, i) => {
      const o = ctx.createOscillator();
      const g = ctx.createGain();
      o.type = "sine";
      o.frequency.value = f;
      g.gain.setValueAtTime(0.0001, now + i * 0.12);
      g.gain.exponentialRampToValueAtTime(0.08, now + i * 0.12 + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, now + i * 0.12 + 0.9);
      o.connect(g).connect(ctx.destination);
      o.start(now + i * 0.12);
      o.stop(now + i * 0.12 + 1);
    });
  } catch { /* audio unavailable */ }
}

const app = new App(document.getElementById("app"));
window.warraq = app;
subscribeState((kind) => {
  if (kind === "settings" && app.view && app.view.update) app.view.update("settings");
});
app.start();
