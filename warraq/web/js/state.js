// Global application state + change notifications.
export const state = {
  settings: null, // current values
  schema: [],
  presets: {},
  system: null,
  jobs: [],
  docs: {}, // job id -> document {pages: [...]}
  reading: {}, // job id -> {page, regions}
  model: { stage: "idle" },
  translate: {}, // job id -> {done,total}
  connected: true,
};

const listeners = new Set();
export function subscribeState(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}
export function notify(kind, payload) {
  for (const fn of listeners) {
    try { fn(kind, payload); } catch (e) { console.error(e); }
  }
}

export function upsertJob(job) {
  const i = state.jobs.findIndex((j) => j.id === job.id);
  if (i >= 0) state.jobs[i] = { ...state.jobs[i], ...job };
  else state.jobs.unshift(job);
}

export function upsertPage(jobId, page) {
  const doc = state.docs[jobId] || (state.docs[jobId] = { pages: [] });
  const key = (p) => `${p.source_index}:${p.split_part ?? -1}`;
  const i = doc.pages.findIndex((p) => key(p) === key(page));
  if (i >= 0) doc.pages[i] = page;
  else doc.pages.push(page);
  doc.pages.sort((a, b) => a.source_index - b.source_index || (a.split_part ?? -1) - (b.split_part ?? -1));
  doc.pages.forEach((p, k) => (p.index = k));
}

export const KIND_COLOR = {
  body: "var(--k-body)",
  title: "var(--k-title)",
  margin: "var(--k-margin)",
  footnote: "var(--k-footnote)",
  header: "var(--k-header)",
  footer: "var(--k-header)",
  page_number: "var(--k-header)",
  table: "var(--k-table)",
  formula: "var(--k-table)",
  caption: "var(--k-title)",
  figure: "var(--k-figure)",
};

export function getSetting(path) {
  return path.split(".").reduce((o, k) => (o ? o[k] : undefined), state.settings);
}
