// Document workspace: thumbnails · page with region overlay · text panel.
import { h, clear, debounce, fmtDuration } from "../lib/dom.js";
import { icon } from "../lib/icons.js";
import { api } from "../lib/api.js";
import { t } from "../lib/i18n.js";
import { state, KIND_COLOR, upsertPage } from "../state.js";
import { toast, menu, bar } from "../components/ui.js";

const SVGNS = "http://www.w3.org/2000/svg";
const SIDE = { right: "right margin", left: "left margin", top: "top margin", bottom: "bottom margin" };
// region types a user can ask for when re-reading part of a page or correcting a block
const AREA_KINDS = ["auto", "body", "title", "footnote", "margin", "header"];
const BLOCK_KINDS = ["body", "title", "footnote", "margin", "header", "footer", "page_number"];
const EDGE = /^([\s.,،؛:;!?؟"'«»()[\]{}<>\-–—…*/\\|]*)(.*?)([\s.,،؛:;!?؟"'«»()[\]{}<>\-–—…*/\\|]*)$/su;

// Region text with the words the model was unsure of underlined for review.
function markedText(text, unsure) {
  if (!unsure || !unsure.length) return [text];
  const words = new Set(unsure);
  const out = [];
  for (const part of text.split(/(\s+)/)) {
    const m = part && EDGE.exec(part);
    if (m && m[2] && words.has(m[2])) out.push(m[1], h("mark.unsure", { text: m[2], title: t("unsure") }), m[3]);
    else if (part) out.push(part);
  }
  return out.filter(Boolean);
}

export class WorkspaceView {
  constructor(app, jobId) {
    this.app = app;
    this.jobId = jobId;
    this.pageIndex = Number(sessionStorage.getItem(`warraq.page.${jobId}`) || 0);
    this.zoom = Number(localStorage.getItem("warraq.zoom") || 0); // 0 = fit
    this.tab = localStorage.getItem("warraq.tab") || "page";
    this.selected = null;
    this.areaMode = false; // dragging on the page selects an area to read again
    this.drag = null; // area being drawn (page px)
    this.selection = null; // area drawn, waiting for "Re-read"
    this.pending = null; // {page, bbox}: area being re-read now
    this.areaKind = localStorage.getItem("warraq.areaKind") || "auto";
    this.assembled = null;
    this.translation = null;
    this.el = h("div.view");
    this.grid = h("div.workspace");
    this.thumbs = h("div.thumbs");
    this.stage = h("div.stage");
    this.panel = h("div.panel");
    this.grid.append(this.thumbs, this.stage, this.panel);
    this.el.appendChild(this.grid);
    this.buildStage();
    this.buildPanel();
    this.load();
    this.refreshAssembled = debounce(() => this.loadAssembled(), 500);
  }

  get job() {
    return state.jobs.find((j) => j.id === this.jobId);
  }
  get doc() {
    return state.docs[this.jobId];
  }
  get page() {
    const d = this.doc;
    return d && d.pages[this.pageIndex];
  }

  async load() {
    try {
      const res = await api.job(this.jobId);
      if (res.job) {
        const i = state.jobs.findIndex((j) => j.id === res.job.id);
        if (i >= 0) state.jobs[i] = res.job; else state.jobs.unshift(res.job);
      }
      state.docs[this.jobId] = res.document || { pages: [] };
      if (this.pageIndex >= (state.docs[this.jobId].pages || []).length) this.pageIndex = 0;
      this.renderAll();
      this.loadAssembled();
      this.app.renderTopbar();
    } catch (e) {
      toast(e.message, "err");
    }
  }

  async loadAssembled() {
    try {
      this.assembled = await api.assembled(this.jobId);
      if (this.tab === "page") this.renderPanel();
    } catch { /* not ready */ }
  }

  renderAll() {
    this.renderThumbs();
    this.renderStage();
    this.renderPanel();
  }

  /* ------------------------------------------------------------ thumbs */
  renderThumbs() {
    clear(this.thumbs);
    const pages = (this.doc && this.doc.pages) || [];
    const job = this.job;
    pages.forEach((p, i) => {
      const flagged = p.regions && p.regions.some((r) => r.flagged);
      const st = p.status === "done" ? (flagged ? h("span.flag", { title: t("lowConf") }) : null) : p.status === "error" ? icon("alert") : h("span.spinner", { style: { width: "12px", height: "12px" } });
      const item = h(`div.thumb-item${i === this.pageIndex ? ".on" : ""}`, { onclick: () => this.goto(i), title: `${t("page")} ${i + 1}` },
        h("img", { src: api.pageUrl(this.jobId, p.thumb_path), loading: "lazy", alt: "" }),
        h("span.no", { text: String(i + 1) }),
        h("span.st", {}, st),
      );
      this.thumbs.appendChild(item);
    });
    // placeholders for pages still to come
    if (job && job.total_pages && ["running", "queued", "paused"].includes(job.status)) {
      const remaining = Math.max(0, job.total_pages - (job.done_pages || 0) - (pages.some((p) => p.status === "running") ? 1 : 0));
      for (let k = 0; k < Math.min(remaining, 40); k++) {
        this.thumbs.appendChild(h("div.thumb-item.pending", {}, h("div", { style: { aspectRatio: "0.72", background: "var(--surface)" } })));
      }
    }
  }

  goto(i) {
    const pages = (this.doc && this.doc.pages) || [];
    if (!pages.length) return;
    this.pageIndex = Math.max(0, Math.min(pages.length - 1, i));
    sessionStorage.setItem(`warraq.page.${this.jobId}`, String(this.pageIndex));
    this.selected = null;
    this.selection = null;
    this.closeAreaPop();
    this.renderThumbs();
    this.renderStage();
    this.renderPanel();
    const on = this.thumbs.querySelector(".thumb-item.on");
    on && on.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  /* ------------------------------------------------------------ stage */
  buildStage() {
    this.canvas = h("div.canvas");
    this.paper = h("div.paper");
    this.canvas.appendChild(this.paper);
    this.zoomLabel = h("div.zoom");
    const tools = h("div.stage-tools", {},
      h("button.icon-btn", { title: "Previous page (←)", onclick: () => this.goto(this.pageIndex - 1) }, icon("back")),
      h("button.icon-btn", { title: "Zoom out", onclick: () => this.setZoom(this.currentZoom() / 1.2) }, icon("zoomOut")),
      this.zoomLabel,
      h("button.icon-btn", { title: "Zoom in", onclick: () => this.setZoom(this.currentZoom() * 1.2) }, icon("zoomIn")),
      h("button.icon-btn", { title: "Fit", onclick: () => this.setZoom(0) }, icon("fit")),
      (this.overlayBtn = h("button.icon-btn.on", { title: "Show regions", onclick: () => { this.overlayOn = !this.overlayOn; this.overlayBtn.classList.toggle("on", this.overlayOn); this.renderOverlay(); } }, icon("layers"))),
      (this.areaBtn = h("button.icon-btn", { title: `${t("rereadArea")} (A)`, onclick: () => this.setAreaMode(!this.areaMode) }, icon("scan"))),
      h("button.icon-btn", { title: "Next page (→)", onclick: () => this.goto(this.pageIndex + 1) }, icon("chevron")),
    );
    this.overlayOn = true;
    const legend = h("div.legend", {},
      ...[["body", "region_body"], ["margin", "region_margin"], ["footnote", "region_footnote"], ["header", "region_header"]].map(([k, key]) => h("span", {}, h("i", { style: { background: KIND_COLOR[k] } }), t(key))),
    );
    this.areaHint = h("div.area-hint", {}, icon("scan"), h("span", { text: t("rereadAreaHint") }));
    this.stage.append(legend, this.areaHint, this.canvas, tools);
    this.paper.addEventListener("pointerdown", (e) => this.areaDown(e));
    this.paper.addEventListener("pointermove", (e) => this.areaMove(e));
    this.paper.addEventListener("pointerup", (e) => this.areaUp(e));
    this.paper.addEventListener("pointercancel", () => { this.drag = null; this.drawAreas(); });
    this.canvas.addEventListener("scroll", () => this.placeAreaPop(), { passive: true });
    this.canvas.addEventListener("wheel", (e) => {
      if (e.ctrlKey || e.metaKey) {
        e.preventDefault();
        this.setZoom(this.currentZoom() * (e.deltaY < 0 ? 1.08 : 1 / 1.08));
      }
    }, { passive: false });
    new ResizeObserver(() => this.applyZoom()).observe(this.canvas);
  }

  currentZoom() {
    const p = this.page;
    if (!p) return 1;
    return this.paper.offsetWidth / p.width;
  }

  setZoom(z) {
    this.zoom = z;
    localStorage.setItem("warraq.zoom", String(z));
    this.applyZoom();
  }

  applyZoom() {
    const p = this.page;
    if (!p) return;
    const avail = this.canvas.clientWidth - 80;
    const availH = this.canvas.clientHeight - 150;
    let width;
    if (!this.zoom) width = Math.max(240, Math.min(avail, (availH * p.width) / p.height, p.width));
    else width = Math.max(200, Math.min(p.width * 3, p.width * this.zoom));
    const changed = this.paper.style.width !== `${Math.round(width)}px`;
    this.paper.style.width = `${Math.round(width)}px`;
    this.zoomLabel.textContent = `${Math.round((width / p.width) * 100)}%`;
    if (changed && this.svg) this.renderOverlay();
    this.placeAreaPop();
  }

  renderStage() {
    clear(this.paper);
    const p = this.page;
    const empty = this.stage.querySelector(".stage-empty");
    if (empty) empty.remove();
    if (!p) {
      this.stage.appendChild(h("div.stage-empty", {}, h("div", {}, h("div.big", { text: "اقرأ" }), h("div", { text: t("waiting") }))));
      return;
    }
    const img = h("img", { src: api.pageUrl(this.jobId, p.image_path), alt: "", draggable: false });
    this.paper.appendChild(img);
    this.svg = document.createElementNS(SVGNS, "svg");
    this.svg.setAttribute("class", "overlay");
    this.svg.setAttribute("viewBox", `0 0 ${p.width} ${p.height}`);
    this.svg.setAttribute("preserveAspectRatio", "none");
    this.paper.appendChild(this.svg);
    this.applyZoom();
    this.renderOverlay();
  }

  renderOverlay() {
    const svg = this.svg;
    const p = this.page;
    if (!svg || !p) return;
    svg.innerHTML = `<defs><linearGradient id="scan-grad" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="var(--accent)" stop-opacity="0"/><stop offset=".85" stop-color="var(--accent)" stop-opacity=".28"/><stop offset="1" stop-color="var(--accent)" stop-opacity=".7"/></linearGradient></defs>`;
    if (!this.overlayOn) return this.drawAreas();
    const reading = state.reading[this.jobId];
    const readingIds = reading && reading.page === p.index ? new Set(reading.regions) : new Set();
    const scale = Math.max(0.05, (this.paper.offsetWidth || p.width) / p.width);
    const labelSize = 10.5 / scale;
    for (const r of p.regions || []) {
      const [x0, y0, x1, y1] = r.bbox;
      const g = document.createElementNS(SVGNS, "g");
      g.style.setProperty("--c", KIND_COLOR[r.kind] || "var(--k-body)");
      const rect = document.createElementNS(SVGNS, "rect");
      rect.setAttribute("x", x0); rect.setAttribute("y", y0);
      rect.setAttribute("width", Math.max(1, x1 - x0)); rect.setAttribute("height", Math.max(1, y1 - y0));
      rect.setAttribute("rx", 6);
      rect.setAttribute("data-region", r.id);
      rect.setAttribute("class", `region${this.selected === r.id ? " sel" : ""}${this.selected && this.selected !== r.id ? " dim" : ""}${r.flagged ? " flagged" : ""}`);
      rect.addEventListener("click", (e) => { e.stopPropagation(); if (!this.areaMode) this.select(r.id, true); });
      rect.addEventListener("mouseenter", () => this.hoverBlock(r.id, true));
      rect.addEventListener("mouseleave", () => this.hoverBlock(r.id, false));
      const title = document.createElementNS(SVGNS, "title");
      title.textContent = `${t("region_" + r.kind)}${r.confidence != null ? ` · ${Math.round(r.confidence * 100)}%` : ""}`;
      rect.appendChild(title);
      g.appendChild(rect);
      const label = document.createElementNS(SVGNS, "text");
      label.setAttribute("x", x0 + 2 / scale);
      label.setAttribute("y", y0 - 5 / scale);
      label.setAttribute("class", "region-label");
      label.setAttribute("font-size", labelSize);
      label.style.strokeWidth = `${3 / scale}px`;
      label.textContent = t("region_" + r.kind);
      g.appendChild(label);
      if (readingIds.has(r.id)) {
        const glow = rect.cloneNode();
        glow.setAttribute("class", "reading-glow");
        g.appendChild(glow);
        const scan = document.createElementNS(SVGNS, "rect");
        const hh = Math.max(20, (y1 - y0) * 0.22);
        scan.setAttribute("x", x0); scan.setAttribute("width", x1 - x0); scan.setAttribute("height", hh); scan.setAttribute("class", "scanline");
        scan.innerHTML = `<animate attributeName="y" from="${y0 - hh}" to="${y1 - hh * 0.2}" dur="1.6s" repeatCount="indefinite"/>`;
        g.appendChild(scan);
      }
      svg.appendChild(g);
    }
    this.drawAreas();
  }

  /* ------------------------------------------------------------ re-read an area */
  setAreaMode(on) {
    this.areaMode = on;
    this.areaBtn.classList.toggle("on", on);
    this.paper.classList.toggle("selecting", on);
    this.stage.classList.toggle("area-mode", on);
    if (!on) {
      this.drag = null;
      this.selection = null;
      this.closeAreaPop();
    }
    this.drawAreas();
  }

  toPage(e) {
    const p = this.page;
    const r = this.paper.getBoundingClientRect();
    const x = ((e.clientX - r.left) / Math.max(1, r.width)) * p.width;
    const y = ((e.clientY - r.top) / Math.max(1, r.height)) * p.height;
    return [Math.max(0, Math.min(p.width, x)), Math.max(0, Math.min(p.height, y))];
  }

  areaDown(e) {
    if (!this.areaMode || !this.page || e.button !== 0) return;
    e.preventDefault();
    e.stopPropagation();
    this.closeAreaPop();
    this.selection = null;
    const [x, y] = this.toPage(e);
    this.drag = { x0: x, y0: y, x1: x, y1: y };
    this.paper.setPointerCapture(e.pointerId);
    this.drawAreas();
  }

  areaMove(e) {
    if (!this.drag) return;
    [this.drag.x1, this.drag.y1] = this.toPage(e);
    this.drawAreas();
  }

  areaUp(e) {
    if (!this.drag) return;
    [this.drag.x1, this.drag.y1] = this.toPage(e);
    const box = normBox(this.drag);
    this.drag = null;
    const scale = this.paper.offsetWidth / this.page.width;
    if ((box[2] - box[0]) * scale < 8 || (box[3] - box[1]) * scale < 8) { this.drawAreas(); return; }
    this.selection = box;
    this.drawAreas();
    this.openAreaPop();
  }

  drawAreas() {
    const svg = this.svg;
    const p = this.page;
    if (!svg || !p) return;
    svg.querySelectorAll(".area").forEach((n) => n.remove());
    const scale = Math.max(0.05, (this.paper.offsetWidth || p.width) / p.width);
    const box = this.drag ? normBox(this.drag) : this.selection;
    if (box) svg.appendChild(svgRect(box, "area area-sel", 4 / scale));
    if (this.pending && this.pending.page === p.index && this.pending.bbox) {
      const [x0, y0, x1, y1] = this.pending.bbox;
      const g = document.createElementNS(SVGNS, "g");
      g.setAttribute("class", "area");
      g.appendChild(svgRect(this.pending.bbox, "area-pending", 4 / scale));
      const scan = document.createElementNS(SVGNS, "rect");
      const hh = Math.max(12, (y1 - y0) * 0.3);
      scan.setAttribute("x", x0); scan.setAttribute("width", Math.max(1, x1 - x0)); scan.setAttribute("height", hh); scan.setAttribute("class", "scanline");
      scan.innerHTML = `<animate attributeName="y" from="${y0 - hh}" to="${y1 - hh * 0.2}" dur="1.2s" repeatCount="indefinite"/>`;
      g.appendChild(scan);
      svg.appendChild(g);
    }
  }

  openAreaPop() {
    this.closeAreaPop();
    const seg = h("div.seg");
    const pick = (k) => {
      this.areaKind = k;
      localStorage.setItem("warraq.areaKind", k);
      for (const b of seg.children) b.classList.toggle("on", b.dataset.kind === k);
    };
    for (const k of AREA_KINDS) seg.appendChild(h("button", { dataset: { kind: k }, onclick: () => pick(k), text: k === "auto" ? t("rereadAreaAuto") : t("region_" + k) }));
    pick(AREA_KINDS.includes(this.areaKind) ? this.areaKind : "auto");
    const go = () => this.rereadArea({ bbox: this.selection.map((v) => Math.round(v)), kind: this.areaKind }, this.selection);
    this.areaPop = h("div.area-pop", { role: "dialog", onpointerdown: (e) => e.stopPropagation() },
      h("div.t", { text: t("rereadAreaAs") }),
      seg,
      h("div.row", {},
        h("button.btn.sm.ghost", { onclick: () => { this.selection = null; this.closeAreaPop(); this.drawAreas(); } }, t("cancel")),
        h("button.btn.sm.primary", { onclick: go }, icon("scan"), t("rereadAreaGo")),
      ),
    );
    this.areaPop.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); go(); } });
    this.stage.appendChild(this.areaPop);
    this.placeAreaPop();
    this.areaPop.querySelector(".btn.primary").focus();
  }

  placeAreaPop() {
    const pop = this.areaPop;
    const p = this.page;
    if (!pop || !this.selection || !p) return;
    const pr = this.paper.getBoundingClientRect();
    const sr = this.stage.getBoundingClientRect();
    const k = pr.width / p.width;
    const [x0, , x1, y1] = this.selection;
    const cx = pr.left - sr.left + ((x0 + x1) / 2) * k;
    let top = pr.top - sr.top + y1 * k + 10;
    if (top + pop.offsetHeight > sr.height - 70) top = Math.max(10, pr.top - sr.top + this.selection[1] * k - pop.offsetHeight - 10);
    const left = Math.max(10, Math.min(sr.width - pop.offsetWidth - 10, cx - pop.offsetWidth / 2));
    pop.style.left = `${left}px`;
    pop.style.top = `${top}px`;
  }

  closeAreaPop() {
    if (this.areaPop) this.areaPop.remove();
    this.areaPop = null;
  }

  async rereadArea(body, bbox) {
    const p = this.page;
    if (!p) return;
    const job = this.job;
    if (job && ["running", "queued"].includes(job.status) && job.kind !== "area") {
      toast(t("waiting"), "info");
      return;
    }
    try {
      await api.action(this.jobId, "area", { page: p.index, ...body });
      this.pending = { page: p.index, bbox: bbox ? [...bbox] : null };
      this.app.markDirty(this.jobId);
      toast(`${t("rereading")}…`, "info", 1600);
    } catch (e) {
      toast(e.message, "err");
    }
    this.selection = null;
    this.closeAreaPop();
    if (this.areaMode) this.setAreaMode(false);
    this.drawAreas();
  }

  kindMenu(anchor, r) {
    menu(anchor, BLOCK_KINDS.filter((k) => k !== r.kind).map((k) => ({ label: t("region_" + k), run: () => this.changeKind(r, k) })));
  }

  async changeKind(r, kind) {
    try {
      const page = await api.editRegion(this.jobId, r.id, { kind });
      upsertPage(this.jobId, page);
      this.app.markDirty(this.jobId);
      this.renderStage();
      this.renderPanel();
      this.refreshAssembled();
    } catch (e) {
      toast(e.message, "err");
    }
  }

  select(id, fromCanvas = false) {
    this.selected = this.selected === id && fromCanvas ? null : id;
    this.renderOverlay();
    for (const b of this.panel.querySelectorAll(".block")) b.classList.toggle("sel", b.dataset.region === this.selected);
    if (this.selected && fromCanvas) {
      if (this.tab !== "page") this.setTab("page");
      const b = this.panel.querySelector(`.block[data-region="${CSS.escape(this.selected)}"]`);
      b && b.scrollIntoView({ block: "center", behavior: "smooth" });
    }
  }

  hoverBlock(id, on) {
    const b = this.panel.querySelector(`.block[data-region="${CSS.escape(id)}"]`);
    if (b) b.style.background = on ? "var(--surface)" : "";
  }

  /* ------------------------------------------------------------ panel */
  buildPanel() {
    this.tabsEl = h("div.tabs");
    this.body = h("div.panel-body");
    this.actions = h("div.panel-actions");
    this.panel.append(this.tabsEl, this.body, this.actions);
    this.renderTabs();
  }

  renderTabs() {
    clear(this.tabsEl);
    const tabs = [["page", t("text"), "text"], ["document", t("txt"), "page"], ["translation", t("translation"), "globe"]];
    for (const [k, label, ic] of tabs) {
      this.tabsEl.appendChild(h(`button.tab${this.tab === k ? ".on" : ""}`, { onclick: () => this.setTab(k) }, icon(ic), label));
    }
    this.tabsEl.appendChild(h("div.spacer"));
    const size = Number(localStorage.getItem("warraq.arSize") || 21);
    this.panel.style.setProperty("--ar-size", `${size}px`);
    this.tabsEl.append(
      h("button.icon-btn", { title: "Smaller text", onclick: () => this.textSize(-1) }, h("span", { style: { fontSize: "12px", fontWeight: 700 } }, "A−")),
      h("button.icon-btn", { title: "Larger text", onclick: () => this.textSize(1) }, h("span", { style: { fontSize: "14px", fontWeight: 700 } }, "A+")),
    );
  }

  textSize(d) {
    const size = Math.max(14, Math.min(34, Number(localStorage.getItem("warraq.arSize") || 21) + d));
    localStorage.setItem("warraq.arSize", String(size));
    this.panel.style.setProperty("--ar-size", `${size}px`);
  }

  setTab(k) {
    this.tab = k;
    localStorage.setItem("warraq.tab", k);
    this.renderTabs();
    this.renderPanel();
  }

  renderPanel() {
    clear(this.body);
    clear(this.actions);
    if (this.tab === "document") return this.renderDocument();
    if (this.tab === "translation") return this.renderTranslation();
    return this.renderPage();
  }

  renderPage() {
    const p = this.page;
    if (!p) {
      this.body.append(h("div.skeleton", { style: { width: "70%" } }), h("div.skeleton"), h("div.skeleton", { style: { width: "85%" } }));
      return;
    }
    const printed = p.printed_number ? ` · ${p.printed_number}` : "";
    this.body.appendChild(h("div.page-head", {}, h("span", { text: `${t("page")} ${p.index + 1}${printed}` }), h("span.line"), p.elapsed ? h("span", { text: fmtDuration(p.elapsed) }) : null));
    if (p.status === "error") {
      this.body.appendChild(h("div.notice", {}, h("div.ico", {}, icon("alert")), h("div", {}, h("div.t", { text: t("error") }), h("div.d", { text: p.error }))));
    }
    const regions = p.regions || [];
    const asm = this.assembled && this.assembled.pages ? this.assembled.pages.find((x) => x.index === p.index) : null;
    const headers = regions.filter((r) => ["header", "page_number", "footer"].includes(r.kind));
    for (const r of headers.filter((r) => r.kind === "header")) this.body.appendChild(this.block(r, "header"));
    const flow = regions.filter((r) => ["body", "title", "table", "formula", "caption"].includes(r.kind));
    for (const r of flow) this.body.appendChild(this.block(r, r.kind === "title" ? "title" : ""));
    const margins = regions.filter((r) => r.kind === "margin");
    if (margins.length) {
      this.body.appendChild(this.sectionHead(this.assembled?.margin_header || "------marginal notes-------", "var(--k-margin)"));
      margins.forEach((r, i) => {
        const m = asm && asm.margins.find((x) => x.region_id === r.id);
        this.body.appendChild(this.block(r, "small", m ? m.label : `[M${i + 1}]`, r.side ? SIDE[r.side] || r.side : ""));
      });
    }
    const notes = regions.filter((r) => r.kind === "footnote");
    if (notes.length) {
      this.body.appendChild(this.sectionHead(this.assembled?.footnote_header || "------footnotes-------", "var(--k-footnote)"));
      if (asm && asm.footnotes.length) {
        for (const f of asm.footnotes) {
          const rid = (f.regions && f.regions[0]) || notes[0].id;
          const r = regions.find((x) => x.id === rid) || notes[0];
          this.body.appendChild(this.noteLine(f, r));
        }
        const raw = h("details", { style: { marginTop: "14px" } }, h("summary.faint", { style: { cursor: "pointer", fontSize: "12px" } }, "Edit the footnote area as read"));
        for (const r of notes) raw.appendChild(this.block(r, "small"));
        this.body.appendChild(raw);
      } else {
        for (const r of notes) this.body.appendChild(this.block(r, "small"));
      }
    }
    for (const r of headers.filter((r) => r.kind !== "header")) this.body.appendChild(this.block(r, "header"));
    if (!regions.some((r) => r.text)) this.body.appendChild(h("div.cta", {}, p.status === "done" ? t("noText") : t("waiting")));
    this.actions.append(
      h("button.btn.sm", { onclick: () => this.copyPage() }, icon("copy"), t("copy")),
      h("div.spacer"),
      h("button.btn.sm", { title: `${t("rereadAreaHint")} (A)`, onclick: () => this.setAreaMode(!this.areaMode) }, icon("scan"), t("rereadArea")),
      h("button.btn.sm", { onclick: () => this.rereadPage() }, icon("refresh"), t("rereadPage")),
    );
  }

  sectionHead(text, color) {
    const el = h("div.sect-head", {}, h("span.lbl", { text }), h("span.line"));
    el.style.setProperty("--c", color);
    return el;
  }

  noteLine(f, region) {
    const el = h("div.block", { dataset: { region: region.id } },
      h("div.note", {},
        h("span.lbl", { text: f.label }),
        h("div", {}, h("span.ar-text.small", { text: f.text }), f.merged_from_page ? h("div", {}, h("span.merged", { text: `+ continued on page ${f.merged_from_page}` })) : null, f.continued_from_previous ? h("div", {}, h("span.merged", { text: "continuation" })) : null),
      ),
    );
    el.style.setProperty("--c", "var(--k-footnote)");
    el.addEventListener("click", () => this.select(region.id));
    return el;
  }

  block(r, variant = "", label = "", side = "") {
    const text = h(`div.ar-text${variant ? "." + variant : ""}`, { contenteditable: "true", spellcheck: false, dir: "rtl", lang: "ar" }, markedText(r.text || "", r.uncertain));
    const save = debounce(async () => {
      const value = text.innerText.replace(/ /g, " ").replace(/\n{3,}/g, "\n\n").trim();
      if (value === (r.text || "").trim()) return;
      try {
        const page = await api.editRegion(this.jobId, r.id, { text: value });
        r.text = value;
        upsertPage(this.jobId, page);
        this.app.markDirty(this.jobId);
        toast(t("edited"), "ok", 1800);
        this.refreshAssembled();
      } catch (e) {
        toast(e.message, "err");
      }
    }, 700);
    text.addEventListener("input", save);
    text.addEventListener("focus", () => this.select(r.id));
    text.addEventListener("keydown", (e) => { if (e.key === "Escape") text.blur(); });
    const content = label ? h("div.note", {}, h("span.lbl", { text: label }), h("div", { style: { flex: 1 } }, side ? h("div.side", { text: side }) : null, text)) : text;
    const tools = h("div.block-tools", {},
      h("button.icon-btn", { title: t("rereadRegion"), onclick: (e) => { e.stopPropagation(); this.rereadArea({ region_id: r.id }, r.bbox); } }, icon("refresh")),
      h("button.icon-btn", { title: t("changeType"), onclick: (e) => { e.stopPropagation(); this.kindMenu(e.currentTarget, r); } }, icon("tag")),
    );
    const el = h("div.block", { dataset: { region: r.id }, onmouseenter: () => this.hoverRegion(r.id, true), onmouseleave: () => this.hoverRegion(r.id, false) },
      tools,
      content,
      r.flagged ? h("span.conf", { text: `${t("lowConf")}${r.confidence != null ? " · " + Math.round(r.confidence * 100) + "%" : ""}` }) : null,
    );
    el.style.setProperty("--c", KIND_COLOR[r.kind] || "var(--accent)");
    if (this.selected === r.id) el.classList.add("sel");
    return el;
  }

  hoverRegion(id, on) {
    if (!this.svg) return;
    const rect = this.svg.querySelector(`rect.region[data-region="${CSS.escape(id)}"]`);
    if (rect) rect.style.fill = on ? "color-mix(in oklab, var(--c) 24%, transparent)" : "";
  }

  async renderDocument() {
    const pre = h("div.txt-preview", {}, h("div.skeleton"), h("div.skeleton", { style: { width: "80%" } }));
    this.body.appendChild(pre);
    try {
      const text = await api.preview(this.jobId);
      clear(pre);
      for (const line of text.split("\n")) {
        const isSep = /^[=\-]{4,}|^-{3,}.*-{3,}$|^={3,}.*={3,}$/.test(line.trim());
        pre.appendChild(isSep ? h("div.sep", { text: line }) : h("div", { dir: "auto", text: line || " " }));
      }
      this.docText = text;
    } catch (e) {
      clear(pre);
      pre.appendChild(h("div.cta", { text: e.message }));
    }
    this.actions.append(
      h("button.btn.sm", { onclick: async () => { await navigator.clipboard.writeText(this.docText || ""); toast(t("copied"), "ok"); } }, icon("copy"), t("copy")),
      h("div.spacer"),
      h("button.btn.sm.primary", { onclick: () => this.app.exportJob(this.jobId) }, icon("download"), t("export")),
    );
  }

  async renderTranslation() {
    const job = this.job;
    const box = h("div");
    this.body.appendChild(box);
    let data = null;
    try { data = await api.translation(this.jobId); } catch { /* none */ }
    const pages = (data && data.pages) || [];
    const tr = pages[this.pageIndex];
    const progress = state.translate[this.jobId];
    if (progress && progress.done < progress.total && job && job.status === "running") {
      box.appendChild(h("div.cta", {}, h("div.ico", {}, icon("globe")), h("div", { text: `${t("translation")}… ${progress.done}/${progress.total}` }), h("div", { style: { maxWidth: "260px", margin: "14px auto 0" } }, bar(progress.done / Math.max(1, progress.total)))));
      return;
    }
    if (!tr || !Object.keys(tr).length) {
      box.appendChild(h("div.cta", {},
        h("div.ico", {}, icon("globe")),
        h("div", { style: { fontWeight: 600, color: "var(--text)" }, text: t("translateNow") }),
        h("div.faint", { style: { margin: "6px auto 16px", maxWidth: "300px", fontSize: "12.5px" }, text: t("translateHint") }),
        h("button.btn.primary", { onclick: () => this.app.translateJob(this.jobId) }, icon("sparkles"), t("translateNow")),
      ));
      return;
    }
    const lang = (state.settings && state.settings.translation.target_language) || "";
    box.appendChild(h("div.page-head", {}, h("span", { text: `${t("page")} ${this.pageIndex + 1} · ${lang}` }), h("span.line")));
    if (tr.body) box.appendChild(h("div.translation", { dir: "auto", text: tr.body }));
    const notes = Object.entries(tr).filter(([k]) => k.startsWith("footnote|"));
    const margins = Object.entries(tr).filter(([k]) => k.startsWith("margin|"));
    if (margins.length) {
      box.appendChild(this.sectionHead(this.assembled?.margin_header || "------marginal notes-------", "var(--k-margin)"));
      margins.forEach(([k, v], i) => box.appendChild(h("div.note", { style: { direction: "ltr", marginBottom: "8px" } }, h("span.lbl", { text: `[M${i + 1}]` }), h("div.translation", { style: { fontSize: "15px" }, text: v }))));
    }
    if (notes.length) {
      box.appendChild(this.sectionHead(this.assembled?.footnote_header || "------footnotes-------", "var(--k-footnote)"));
      for (const [k, v] of notes) box.appendChild(h("div.note", { style: { direction: "ltr", marginBottom: "8px" } }, h("span.lbl", { text: k.split("|")[1] }), h("div.translation", { style: { fontSize: "15px" }, text: v })));
    }
    this.actions.append(h("div.spacer"), h("button.btn.sm", { onclick: () => this.app.translateJob(this.jobId) }, icon("refresh"), t("retry")));
  }

  copyPage() {
    const parts = [...this.body.querySelectorAll(".ar-text")].map((el) => el.innerText.trim()).filter(Boolean);
    navigator.clipboard.writeText(parts.join("\n\n"));
    toast(t("copied"), "ok");
  }

  async rereadPage() {
    const p = this.page;
    if (!p) return;
    await api.action(this.jobId, "reocr", { pages: [p.source_index] });
    toast(`${t("rereadPage")} ${p.index + 1}`, "info");
  }

  /* ------------------------------------------------------------ events */
  update(kind, e) {
    if (!e || e.job_id !== this.jobId) {
      if (kind === "settings") this.refreshAssembled();
      return;
    }
    if (kind === "page_done" && this.pending && this.pending.page === e.page.index) this.pending = null;
    if (kind === "job" && e.job && e.job.kind === "area" && ["done", "error", "cancelled"].includes(e.status)) {
      this.pending = null;
      this.drawAreas();
    }
    if (kind === "page_image" || kind === "layout" || kind === "page_done") {
      upsertPage(this.jobId, e.page);
      const pages = this.doc.pages;
      const idx = pages.findIndex((p) => p.source_index === e.page.source_index && (p.split_part ?? -1) === (e.page.split_part ?? -1));
      // follow the reader while the user hasn't navigated away from the newest page
      const following = this.pageIndex >= pages.length - 2 || this.pageIndex === idx - 1;
      if (kind !== "page_done" && following && idx >= 0 && idx !== this.pageIndex && !this.app.userBusy()) this.pageIndex = idx;
      this.renderThumbs();
      if (idx === this.pageIndex) {
        this.renderStage();
        if (kind === "page_done") { this.renderPanel(); this.refreshAssembled(); }
      }
    } else if (kind === "reading") {
      const p = this.page;
      if (p && e.page === p.index) this.renderOverlay();
    } else if (kind === "job") {
      this.renderThumbs();
      if (e.status === "done") { this.refreshAssembled(); if (this.tab !== "page") this.renderPanel(); }
    } else if (kind === "translate" && this.tab === "translation") {
      this.renderPanel();
    }
  }

  onKey(e) {
    if (e.target.closest && e.target.closest("[contenteditable], input, textarea, select")) return false;
    if (e.key === "ArrowRight") { this.goto(this.pageIndex + (document.dir === "rtl" ? -1 : 1)); return true; }
    if (e.key === "ArrowLeft") { this.goto(this.pageIndex + (document.dir === "rtl" ? 1 : -1)); return true; }
    if (e.key === "Escape" && this.areaMode) { this.setAreaMode(false); return true; }
    if (e.key === "Escape" && this.selected) { this.select(null); return true; }
    if ((e.key === "a" || e.key === "A") && !e.ctrlKey && !e.metaKey && !e.altKey && this.page) { this.setAreaMode(!this.areaMode); return true; }
    return false;
  }
}

function normBox(d) {
  return [Math.min(d.x0, d.x1), Math.min(d.y0, d.y1), Math.max(d.x0, d.x1), Math.max(d.y0, d.y1)];
}

function svgRect([x0, y0, x1, y1], cls, rx = 4) {
  const rect = document.createElementNS(SVGNS, "rect");
  rect.setAttribute("x", x0); rect.setAttribute("y", y0);
  rect.setAttribute("width", Math.max(1, x1 - x0)); rect.setAttribute("height", Math.max(1, y1 - y0));
  rect.setAttribute("rx", rx);
  rect.setAttribute("class", cls);
  return rect;
}
