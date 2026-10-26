export class CoreApiError extends Error {
  constructor(status, message, payload = null) {
    super(message || `Core API returned HTTP ${status}`);
    this.name = "CoreApiError";
    this.status = status;
    this.payload = payload;
  }
}

export function createCoreClient(fetchImpl = globalThis.fetch) {
  async function request(path, { method = "GET", body, signal } = {}) {
    const response = await fetchImpl(path, {
      method,
      credentials: "same-origin",
      headers: body === undefined ? {} : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
    });
    let payload = null;
    try {
      payload = await response.json();
    } catch {
      if (response.ok) throw new CoreApiError(response.status, "Core returned an invalid JSON response.");
    }
    if (!response.ok) {
      throw new CoreApiError(response.status, payload?.error || "Не удалось выполнить запрос к Core.", payload);
    }
    return payload;
  }
  const companyPath = (id, suffix = "") => `/api/companies/${encodeURIComponent(id)}${suffix}`;
  const query = (params = {}) => {
    const q = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) if (value) q.set(key, value);
    return q.size ? `?${q}` : "";
  };
  return {
    login: (email, password) => request("/api/auth/login", { method: "POST", body: { email, password } }),
    me: () => request("/api/auth/me"),
    logout: () => request("/api/auth/logout", { method: "POST", body: {} }),
    companies: () => request("/api/companies"),
    company: (id) => request(companyPath(id)),
    context: (id) => request(companyPath(id, "/context")),
    saveContext: (id, expectedVersion, facts) => request(companyPath(id, "/context"), { method: "PUT", body: { expectedVersion, facts } }),
    metrics: (id) => request(companyPath(id, "/metrics")),
    createMetric: (id, payload) => request(companyPath(id, "/metrics"), { method: "POST", body: payload }),
    updateMetric: (id, metricId, payload) => request(companyPath(id, `/metrics/${encodeURIComponent(metricId)}`), { method: "PATCH", body: payload }),
    addObservation: (id, metricId, payload) => request(companyPath(id, `/metrics/${encodeURIComponent(metricId)}/observations`), { method: "POST", body: payload }),
    metricEvents: (id, metricId, period = {}) => request(companyPath(id, `/metrics/${encodeURIComponent(metricId)}/events`) + query(period)),
    dashboard: (id, period = {}) => request(companyPath(id, "/dashboard") + query(period)),
    newsSources: (id) => request(companyPath(id, "/news/sources")),
    newsDocuments: (id, period = {}) => request(companyPath(id, "/news/documents") + query(period)),
    newsAssessment: (id, eventId) => request(companyPath(id, `/news/events/${encodeURIComponent(eventId)}/assessment`)),
    newsRuns: (id) => request(companyPath(id, "/news/runs")),
  };
}
