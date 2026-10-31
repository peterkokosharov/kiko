// core/api.js - Fetch wrapper with auth handling
// SPDX-License-Identifier: MIT
// Author: Peter Kokosharov <peter.kokosharov@gmail.com>

const API_BASE = '/api';

let authToken = null;

export function setAuthToken(token) {
    authToken = token;
}

export function clearAuthToken() {
    authToken = null;
}

export async function apiRequest(path, options = {}) {
    const headers = {
        'Content-Type': 'application/json',
        ...options.headers
    };

    if (authToken) {
        headers['Authorization'] = `Bearer ${authToken}`;
    }

    const res = await fetch(`${API_BASE}${path}`, {
        ...options,
        headers,
        credentials: 'include'
    });

    if (res.status === 401) {
        clearAuthToken();
        window.dispatchEvent(new CustomEvent('auth:logout'));
        throw new Error('unauthorized');
    }

    if (!res.ok) {
        const text = await res.text();
        throw new Error(text);
    }

    return res.json();
}

export async function apiGet(path) {
    return apiRequest(path);
}

export async function apiPost(path, data) {
    return apiRequest(path, {
        method: 'POST',
        body: JSON.stringify(data)
    });
}

export async function apiDelete(path) {
    return apiRequest(path, { method: 'DELETE' });
}

// Cookie helpers
export function getCookie(name) {
    const value = `; ${document.cookie}`;
    const parts = value.split(`; ${name}=`);
    if (parts.length === 2) return parts.pop().split(';').shift();
    return null;
}

export function setCookie(name, value, options = {}) {
    let cookie = `${name}=${value}`;
    if (options.maxAge) cookie += `; Max-Age=${options.maxAge}`;
    if (options.path) cookie += `; Path=${options.path}`;
    if (options.httpOnly) cookie += `; HttpOnly`;
    if (options.sameSite) cookie += `; SameSite=${options.sameSite}`;
    document.cookie = cookie;
}

export function deleteCookie(name) {
    document.cookie = `${name}=; Max-Age=0; Path=/`;
}