// modules/models.js - Models management module
// SPDX-License-Identifier: MIT
// Author: Peter Kokosharov <peter.kokosharov@gmail.com>

import { apiGet, apiPost, apiDelete } from '../core/api.js';
import { Table, badge, keyStatusBadge } from '../components/table.js';
import { confirmDialog } from '../components/modal.js';
import { escapeHtml } from '../core/utils.js';

let modelsTable = null;

export async function initModels() {
    // Create table
    modelsTable = new Table(
        document.querySelector('#models-table'),
        [
            { key: 'alias', label: 'Alias', render: r => `<code>${escapeHtml(r.alias)}</code>` },
            { key: 'provider', label: 'Provider', render: r => `<code>${escapeHtml(r.provider)}</code>` },
            { key: 'model_class', label: 'Class' },
            { key: 'priority', label: 'Priority', sortable: true },
            { key: 'status', label: 'Status', render: r => badge(r.status, r.status) },
            { key: 'enabled', label: 'Enabled', render: r => badge(r.enabled ? 'Yes' : 'No', r.enabled ? 'enabled' : 'disabled') },
            { key: 'key_status', label: 'Key', render: r => keyStatusBadge(r.key_source || 'checking...') }
        ],
        {
            rowKey: 'alias',
            actions: [
                { key: 'edit', label: 'Edit', className: 'btn-secondary', handler: editModel },
                { key: 'delete', label: 'Delete', className: 'btn-danger', handler: deleteModel }
            ]
        }
    );

    // Add button - navigate to form page
    document.getElementById('btn-add-model').addEventListener('click', () => {
        window.location.href = '/models/new';
    });

    // Load data
    await loadModels();
}

async function loadModels() {
    try {
        const data = await apiGet('/models');
        const models = data.models.map(m => ({
            ...m,
            key_source: 'checking...'
        }));
        modelsTable.setData(models);

        // Load key status for each
        for (const m of models) {
            loadKeyStatus(m.alias);
        }
    } catch (e) {
        showError('Failed to load models: ' + e.message);
    }
}

async function loadKeyStatus(alias) {
    try {
        const data = await apiGet('/key-status/' + encodeURIComponent(alias));
        // Update table row
        const row = modelsTable.container.querySelector(`tr[data-key="${alias}"]`);
        if (row) {
            const cell = row.cells[6]; // Key column
            if (cell) cell.innerHTML = keyStatusBadge(data.source);
        }
    } catch (e) {
        const row = modelsTable.container.querySelector(`tr[data-key="${alias}"]`);
        if (row) {
            const cell = row.cells[6];
            if (cell) cell.innerHTML = keyStatusBadge('error');
        }
    }
}

async function editModel(alias) {
    window.location.href = '/models/edit/' + encodeURIComponent(alias);
}

async function deleteModel(alias) {
    if (confirm('Delete model "' + alias + '"?')) {
        try {
            await apiDelete('/models/' + encodeURIComponent(alias));
            await loadModels();
        } catch (e) {
            showError('Failed to delete model: ' + e.message);
        }
    }
}

// Global for onclick handlers
window.editModel = editModel;
window.deleteModel = deleteModel;