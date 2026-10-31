// app.js - Main entry point with lazy-loaded modules
// SPDX-License-Identifier: MIT
// Author: Peter Kokosharov <peter.kokosharov@gmail.com>

import { checkAuth, initChangePasswordForm, showError } from './core/auth.js';
import { initModels } from './modules/models.js';
import { initClasses } from './modules/classes.js';
import { initBilling } from './modules/billing.js';
import { initLogs } from './modules/logs.js';
import { initSettings } from './modules/settings.js';
import { initAliases } from './modules/aliases.js';
import { Modal } from './components/modal.js';

// Make Modal globally available for onclick handlers
window.Modal = Modal;
window.showError = showError;

let currentTab = null;
let modulesLoaded = {
    models: false,
    classes: false,
    billing: false,
    logs: false,
    settings: false,
    aliases: false
};

const moduleInit = {
    models: initModels,
    classes: initClasses,
    billing: initBilling,
    logs: initLogs,
    settings: initSettings,
    aliases: initAliases
};

async function init() {
    // Initialize change password form
    initChangePasswordForm(onPasswordChanged);

    // Check auth status
    const authenticated = await checkAuth();
    if (authenticated) {
        showApp();
    } else {
        // Not authenticated - redirect to login page
        window.location.href = '/login';
    }

    // Tab switching
    document.querySelectorAll('.tab-btn').forEach(btn => {
        btn.addEventListener('click', () => switchTab(btn.dataset.tab));
    });

    // Logout handler
    document.getElementById('logout-btn')?.addEventListener('click', handleLogout);

    // Close modals on Escape
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            Modal.closeAll();
        }
    });
}

function onPasswordChanged() {
    // Password changed, modal will close
}

function showApp() {
    document.getElementById('header-right').style.display = 'flex';
    document.body.classList.add('authenticated');

    // Load default tab
    loadTab('models');
}

async function switchTab(tabName) {
    if (currentTab === tabName) return;

    // Update UI
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));

    const btn = document.querySelector(`[data-tab="${tabName}"]`);
    const panel = document.getElementById('tab-' + tabName);

    if (btn) btn.classList.add('active');
    if (panel) panel.classList.add('active');

    currentTab = tabName;

    // Lazy load module
    if (!modulesLoaded[tabName]) {
        try {
            await moduleInit[tabName]();
            modulesLoaded[tabName] = true;
        } catch (e) {
            showError('Failed to load ' + tabName + ': ' + e.message);
        }
    }
}

function loadTab(tabName) {
    switchTab(tabName);
}

async function handleLogout() {
    try {
        const res = await fetch('/api/auth/logout', {
            method: 'POST',
            credentials: 'include'
        });
    } catch (e) {
        // Ignore
    }
    location.reload();
}

// Initialize on DOM ready
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
} else {
    init();
}