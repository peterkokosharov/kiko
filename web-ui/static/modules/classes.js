// modules/classes.js - Classes management module
// SPDX-License-Identifier: MIT
// Author: Peter Kokosharov <peter.kokosharov@gmail.com>

import { apiGet, apiPost, apiDelete } from '../core/api.js';
import { Table, badge } from '../components/table.js';
import { escapeHtml } from '../core/utils.js';

let classesTable = null;

export async function initClasses() {
    classesTable = new Table(
        document.querySelector('#classes-table'),
        [
            { key: 'name', label: 'Name', render: r => `<code>${escapeHtml(r.name)}</code>` },
            { key: 'keyword', label: 'Keyword', render: r => `<code>${escapeHtml(r.keyword)}</code>` },
            { key: 'role', label: 'Role' },
            { key: 'sort', label: 'Sort', sortable: true },
            { key: 'models', label: 'Models', sortable: true }
        ],
        {
            rowKey: 'name',
            actions: [
                { key: 'delete', label: 'Delete', className: 'btn-danger', handler: deleteClass }
            ]
        }
    );

    // Add button - navigate to form page
    document.getElementById('btn-add-class').addEventListener('click', () => {
        window.location.href = '/classes/new';
    });

    await loadClasses();
}

async function loadClasses() {
    try {
        const data = await apiGet('/classes');
        classesTable.setData(data.classes);
    } catch (e) {
        showError('Failed to load classes: ' + e.message);
    }
}

// Add button - navigate to form page
document.getElementById('btn-add-class').addEventListener('click', () => {
    window.location.href = '/classes/new';
});

async function deleteClass(name) {
    if (confirm('Delete class "' + name + '"?')) {
        try {
            await apiDelete('/classes/' + encodeURIComponent(name));
            await loadClasses();
        } catch (e) {
            showError('Failed to delete class: ' + e.message);
        }
    }
}

// Global for onclick handlers
window.deleteClass = deleteClass;