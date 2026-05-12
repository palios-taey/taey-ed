// Tiny API layer for the Taey-Ed marketing/account site.
// All requests go to the live Spark API behind the Cloudflare Tunnel.
//
// Token model:
//   access_token   localStorage (short-lived, 15min). Bearer in Authorization.
//   refresh_token  localStorage (long-lived, 30d). Rotated on use.
//
// Note: localStorage is XSS-vulnerable. Acceptable for MVP friends-and-family
// beta. Tighter posture (httponly cookies via /auth/login endpoint) is a
// post-MVP hardening item.

const API_BASE = "https://taey-ed-api.taey.ai";

const LS = {
  ACCESS: "taey.access_token",
  REFRESH: "taey.refresh_token",
  EMAIL: "taey.user_email",
};

function getAccess() { return localStorage.getItem(LS.ACCESS) || null; }
function getRefresh() { return localStorage.getItem(LS.REFRESH) || null; }
function getEmail() { return localStorage.getItem(LS.EMAIL) || null; }

function clearTokens() {
  localStorage.removeItem(LS.ACCESS);
  localStorage.removeItem(LS.REFRESH);
  localStorage.removeItem(LS.EMAIL);
}

function storeTokens({ access_token, refresh_token, expires_in }, email) {
  if (access_token) localStorage.setItem(LS.ACCESS, access_token);
  if (refresh_token) localStorage.setItem(LS.REFRESH, refresh_token);
  if (email) localStorage.setItem(LS.EMAIL, email);
}

async function refreshAccess() {
  const rt = getRefresh();
  if (!rt) return false;
  try {
    const r = await fetch(`${API_BASE}/auth/refresh`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: rt }),
    });
    if (!r.ok) {
      if (r.status === 401 || r.status === 403) clearTokens();
      return false;
    }
    const data = await r.json();
    storeTokens(data, getEmail());
    return true;
  } catch (_) {
    return false;
  }
}

async function api(path, { method = "GET", body = null } = {}) {
  // Single retry on 401 after refresh.
  const doFetch = async () => {
    const headers = { "Content-Type": "application/json" };
    const tok = getAccess();
    if (tok) headers["Authorization"] = `Bearer ${tok}`;
    return fetch(`${API_BASE}${path}`, {
      method,
      headers,
      body: body ? JSON.stringify(body) : undefined,
    });
  };
  let r = await doFetch();
  if (r.status === 401 && getRefresh()) {
    if (await refreshAccess()) r = await doFetch();
  }
  return r;
}

async function signup(email, password) {
  const r = await fetch(`${API_BASE}/auth/signup`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw apiError(r.status, data);
  storeTokens(data, email);
  return data;
}

async function login(email, password) {
  const r = await fetch(`${API_BASE}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw apiError(r.status, data);
  storeTokens(data, email);
  return data;
}

async function logout() {
  try {
    const rt = getRefresh();
    if (rt) {
      await fetch(`${API_BASE}/auth/logout`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refresh_token: rt }),
      });
    }
  } catch (_) { /* best effort */ }
  clearTokens();
}

async function me() {
  const r = await api("/auth/me");
  if (!r.ok) return null;
  return r.json();
}

async function getBalance() {
  const r = await api("/credits/balance");
  if (!r.ok) return null;
  const d = await r.json();
  return typeof d.balance === "number" ? d.balance : null;
}

async function createCheckoutSession() {
  const r = await api("/billing/create-checkout-session", { method: "POST", body: {} });
  if (!r.ok) throw apiError(r.status, await r.json().catch(() => ({})));
  return r.json();
}

function apiError(status, data) {
  let msg = "";
  if (data && data.detail) {
    if (Array.isArray(data.detail) && data.detail[0]?.msg) msg = data.detail[0].msg;
    else if (typeof data.detail === "string") msg = data.detail;
  }
  if (!msg) {
    if (status === 401) msg = "Email or password is incorrect.";
    else if (status === 409) msg = "An account with this email already exists.";
    else if (status >= 500) msg = "Something went wrong on our side. Please try again in a moment.";
    else msg = `Request failed (HTTP ${status}).`;
  }
  const e = new Error(msg);
  e.status = status;
  return e;
}

window.TaeyAPI = {
  getAccess, getRefresh, getEmail, clearTokens,
  signup, login, logout, me,
  getBalance, createCheckoutSession,
};
