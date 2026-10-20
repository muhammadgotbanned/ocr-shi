// REST + Server-Sent Events client for the local Warraq server.

async function request(method, url, body, opts = {}) {
  const init = { method, headers: {} };
  if (body instanceof FormData) init.body = body;
  else if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  const res = await fetch(url, init);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch { /* not json */ }
    const err = new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    err.status = res.status;
    throw err;
  }
  if (opts.raw) return res;
  const type = res.headers.get("content-type") || "";
  return type.includes("json") ? res.json() : res.text();
}

export const api = {
  system: () => request("GET", "/api/system"),
  settings: () => request("GET", "/api/settings"),
  patchSettings: (patch) => request("PATCH", "/api/settings", patch),
  resetSettings: (section) => request("POST", "/api/settings/reset", section ? { section } : {}),
  applyPreset: (name) => request("POST", `/api/settings/preset/${name}`),
  downloadModels: (which) => request("POST", "/api/models/download", which ? { which } : {}),
  unloadModels: () => request("POST", "/api/models/unload"),
  jobs: () => request("GET", "/api/jobs"),
  job: (id) => request("GET", `/api/jobs/${id}`),
  deleteJob: (id) => request("DELETE", `/api/jobs/${id}`),
  action: (id, action, body = {}) => request("POST", `/api/jobs/${id}/${action}`, body),
  editRegion: (id, rid, body) => request("PATCH", `/api/jobs/${id}/regions/${encodeURIComponent(rid)}`, body),
  preview: (id) => request("GET", `/api/jobs/${id}/preview`),
  assembled: (id) => request("GET", `/api/jobs/${id}/assembled`),
  translation: (id) => request("GET", `/api/jobs/${id}/translation`),
  fromPaths: (paths, options = {}) => request("POST", "/api/jobs/from-paths", { paths, ...options }),
  upload(files, options = {}, onProgress) {
    // XHR gives upload progress for big scans
    return new Promise((resolve, reject) => {
      const fd = new FormData();
      for (const f of files) fd.append("files", f, f.name);
      fd.append("options", JSON.stringify(options));
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/jobs");
      xhr.upload.onprogress = (e) => e.lengthComputable && onProgress && onProgress(e.loaded / e.total);
      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) resolve(JSON.parse(xhr.responseText));
        else {
          let msg = xhr.statusText;
          try { msg = JSON.parse(xhr.responseText).detail; } catch { /* ignore */ }
          reject(new Error(msg));
        }
      };
      xhr.onerror = () => reject(new Error("Upload failed"));
      xhr.send(fd);
    });
  },
  dialog: (kind) => request("POST", `/api/dialog/${kind}`),
  pageUrl: (id, name) => `/api/jobs/${id}/pages/${encodeURIComponent(name)}`,
  fileUrl: (id, fmt) => `/api/jobs/${id}/files/${fmt}`,
};

export function subscribe(onEvent, onStatus) {
  let es;
  let retry = 800;
  const connect = () => {
    es = new EventSource("/api/events");
    es.onopen = () => { retry = 800; onStatus && onStatus(true); };
    es.onmessage = (e) => dispatch(e);
    const types = ["job", "model", "page_start", "page_image", "layout", "reading", "page_done", "progress", "status", "log", "export", "translate", "settings", "job_deleted"];
    for (const t of types) es.addEventListener(t, dispatch);
    es.onerror = () => {
      onStatus && onStatus(false);
      es.close();
      setTimeout(connect, retry);
      retry = Math.min(retry * 1.6, 8000);
    };
  };
  const dispatch = (e) => {
    try { onEvent(JSON.parse(e.data)); } catch (err) { console.error(err); }
  };
  connect();
  return () => es && es.close();
}
