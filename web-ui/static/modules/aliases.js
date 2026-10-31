// modules/aliases.js - Aliases management module
// SPDX-License-Identifier: MIT
// Author: Peter Kokosharov <peter.kokosharov@gmail.com>

import { apiGet, apiPost, apiDelete } from '../core/api.js';
import { Table } from '../components/table.js';
import { escapeHtml } from '../core/utils.js';

let aliasesTable = null;

export async function initAliases() {
    aliasesTable = new Table(
        document.querySelector('#aliases-table'),
        [
            { key: 'glob', label: 'Glob Pattern', render: r => `<code>${escapeHtml(r.glob)}</code>` },
            { key: 'target', label: 'Target Model', render: r => `<code>${escapeHtml(r.target)}</code>` }
        ],
        {
            rowKey: 'glob',
            actions: [
                { key: 'delete', label: 'Delete', className: 'btn-danger', handler: deleteAlias }
            ]
        }
    );

    // Add button - navigate to form page
    document.getElementById('btn-add-alias').addEventListener('click', () => {
        window.location.href = '/aliases/new';
    });

    await loadAliases();
}

async function loadAliases() {
    try {
        const data = await apiGet('/aliases');
        aliasesTable.setData(data.aliases);
    } catch (e) {
        showError('Failed to load aliases: ' + e.message);
    }
}

// Add button - navigate to form page
document.getElementById('btn-add-alias').addEventListener('click', () => {
    window.location.href = '/aliases/new';
});

async function deleteAlias(glob) {
    if (confirm('Delete alias "' + glob + '"?')) {
        try {
            await apiDelete('/aliases/' + encodeURIComponent(glob));
            await loadAliases();
        } catch (e) {
            showError('Failed to delete alias: ' + e.message);
        }
    }
}

// Global for onclick handlers
window.deleteAlias = deleteAlias;