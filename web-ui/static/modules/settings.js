// modules/settings.js - Settings management module
// SPDX-License-Identifier: MIT
// Author: Peter Kokosharov <peter.kokosharov@gmail.com>

import { apiGet, apiPost } from '../core/api.js';
import { Table } from '../components/table.js';
import { escapeHtml } from '../core/utils.js';

let settingsTable = null;

export async function initSettings() {
    settingsTable = new Table(
        document.querySelector('#settings-table tbody'),
        [
            { key: 'key', label: 'Key', render: r => `<code>${escapeHtml(r.key)}</code>` },
            { key: 'value', label: 'Value', render: r => `<code>${escapeHtml(r.value)}</code>` }
        ],
        {
            rowKey: 'key',
            actions: [
                { key: 'delete', label: 'Delete', className: 'btn-danger', handler: deleteSetting }
            ]
        }
    );

    document.getElementById('btn-add-setting').addEventListener('click', handleAddSetting);

    await loadSettings();
}

async function loadSettings() {
    try {
        const data = await apiGet('/settings');
        const rows = Object.entries(data.settings).map(([key, value]) => ({ key, value }));
        settingsTable.setData(rows);
    } catch (e) {
        showError('Failed to load settings: ' + e.message);
    }
}

async function handleAddSetting() {
    const key = document.getElementById('setting-key').value.trim();
    const value = document.getElementById('setting-value').value.trim();

    if (!key) return showError('Key required');

    try {
        await apiPost('/settings', { key, value });
        document.getElementById('setting-key').value = '';
        document.getElementById('setting-value').value = '';
        await loadSettings();
    } catch (e) {
        showError('Failed to set setting: ' + e.message);
    }
}

async function deleteSetting(key) {
    if (!confirm('Delete setting "' + key + '"?')) return;

    try {
        // Set to empty string to "delete"
        await apiPost('/settings', { key, value: '' });
        await loadSettings();
    } catch (e) {
        showError('Failed to delete setting: ' + e.message);
    }
}