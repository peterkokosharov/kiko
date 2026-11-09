// core/auth.js - Authentication handling
// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
// LLM: Anthropic Opus

import { apiPost, apiGet } from './api.js';

let currentUser = null;
let isDefaultPassword = false;

export async function login(username, password) {
    const res = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }),
        credentials: 'include'
    });

    if (!res.ok) {
        const err = await res.json();
        throw new Error(err.error || 'Login failed');
    }

    const data = await res.json();
    isDefaultPassword = data.default_password;
    return data;
}

export async function logout() {
    try {
        await apiPost('/auth/logout', {});
    } catch (e) {
        // Ignore
    }
    currentUser = null;
    isDefaultPassword = false;
    window.dispatchEvent(new CustomEvent('auth:logout'));
}

export async function changePassword(currentPassword, newPassword) {
    const res = await apiPost('/auth/change-password', {
        current_password: currentPassword,
        new_password: newPassword
    });
    isDefaultPassword = false;
    return res;
}

export async function checkAuth() {
    try {
        const data = await apiGet('/auth/status');
        if (data.authenticated) {
            currentUser = data.user;
            isDefaultPassword = data.default_password;
            return true;
        }
    } catch (e) {
        // Not authenticated
    }
    return false;
}

export function getCurrentUser() {
    return currentUser;
}

export function getIsDefaultPassword() {
    return isDefaultPassword;
}

export function setDefaultPassword(value) {
    isDefaultPassword = value;
}

// Login form handler
export function initLoginForm(onSuccess) {
    const form = document.getElementById('form-login');
    if (!form) return;

    form.addEventListener('submit', async (e) => {
        e.preventDefault();
        const fd = new FormData(form);
        const data = Object.fromEntries(fd);

        try {
            await login(data.username, data.password);
            onSuccess();
        } catch (err) {
            showError('Login failed: ' + err.message);
        }
    });
}

// Change password form handler
export function initChangePasswordForm(onSuccess) {
    const form = document.getElementById('form-change-pass');
    if (!form) return;

    form.addEventListener('submit', async (e) => {
        e.preventDefault();
        const fd = new FormData(form);
        const data = Object.fromEntries(fd);

        if (data.new_password !== data.confirm_password) {
            showError('Passwords do not match');
            return;
        }

        try {
            await changePassword(data.current_password, data.new_password);
            onSuccess();
        } catch (err) {
            showError('Failed to change password: ' + err.message);
        }
    });
}

// Expose for onclick handlers
window.showChangePasswordModal = function() {
    const modal = document.getElementById('modal-change-pass');
    if (modal) modal.classList.remove('hidden');
};

window.handleLogout = async function() {
    try {
        await fetch('/api/auth/logout', { method: 'POST', credentials: 'include' });
    } catch (e) {
        // Ignore
    }
    location.reload();
};

window.closeModal = function(id) {
    const modal = document.getElementById(id);
    if (modal) modal.classList.add('hidden');
};

// CSP forbids inline onclick: pages mark buttons with data-logout / data-close-modal
document.addEventListener('click', (e) => {
    const out = e.target.closest('[data-logout]');
    if (out) window.handleLogout();
    const close = e.target.closest('[data-close-modal]');
    if (close) window.closeModal(close.dataset.closeModal);
});

// Global error display
export function showError(msg) {
    // Create or reuse toast
    let toast = document.getElementById('toast');
    if (!toast) {
        toast = document.createElement('div');
        toast.id = 'toast';
        toast.className = 'toast';
        document.body.appendChild(toast);
    }
    toast.textContent = msg;
    toast.style.display = 'block';
    setTimeout(() => { toast.style.display = 'none'; }, 5000);
}