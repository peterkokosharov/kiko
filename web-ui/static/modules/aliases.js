// modules/aliases.js - Aliases management module
// SPDX-License-Identifier: MIT
// Author: Peter Kokosharov <peter.kokosharov@gmail.com>

import { apiGet, apiPost, apiDelete } from '../core/api.js';
import { Table } from '../components/table.js';
import { Modal, confirmDialog } from '../components/modal.js';
import { escapeHtml } from '../core/utils.js';

let aliasesTable = null;
let aliasModal = null;

export async function initAliases() {
    aliasesTable = new Table(
        document.querySelector('#aliases-table tbody'),
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

    aliasModal = new Modal('modal-alias', {
        title: 'Add Alias',
        size: 'sm',
        content: getModalContent(),
        onClose: () => { document.getElementById('form-alias').reset(); }
    });
    aliasModal.render();

    document.getElementById('form-alias').addEventListener('submit', handleAliasSubmit);
    document.getElementById('btn-add-alias').addEventListener('click', () => openAliasModal());

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

function openAliasModal() {
    document.getElementById('form-alias').reset();
    aliasModal.show();
}

async function deleteAlias(glob) {
    confirmDialog('Delete alias "' + glob + '"?', async () => {
        try {
            await apiDelete('/aliases/' + encodeURIComponent(glob));
            await loadAliases();
        } catch (e) {
            showError('Failed to delete alias: ' + e.message);
        }
    });
}

async function handleAliasSubmit(e) {
    e.preventDefault();
    const fd = new FormData(e.target);
    const data = Object.fromEntries(fd);

    try {
        await apiPost('/aliases', data);
        aliasModal.hide();
        await loadAliases();
    } catch (e) {
        showError('Failed to save alias: ' + e.message);
    }
}

function getModalContent() {
    return `
        <form id="form-alias">
            <div class="form-row">
                <label>Glob Pattern <span class="required">*</span></label>
                <input type="text" name="glob" id="f-alias-glob" placeholder="claude-*" required>
            </div>
            <div class="form-row">
                <label>Target Model <span class="required">*</span></label>
                <input type="text" name="target" id="f-alias-target" placeholder="gemini-flash" required>
            </div>
            <div class="modal-actions">
                <button type="button" class="btn-secondary" onclick="Modal.closeAll()">Cancel</button>
                <button type="submit" class="btn-primary">Save</button>
            </div>
        </form>
    `;
}