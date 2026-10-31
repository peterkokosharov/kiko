// modules/models.js - Models management module
// SPDX-License-Identifier: MIT
// Author: Peter Kokosharov <peter.kokosharov@gmail.com>

import { apiGet, apiPost, apiDelete } from '../core/api.js';
import { Table, badge, keyStatusBadge } from '../components/table.js';
import { Modal, confirmDialog } from '../components/modal.js';
import { escapeHtml } from '../core/utils.js';

let modelsTable = null;
let modelModal = null;

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

    // Create modal
    modelModal = new Modal('modal-model', {
        title: 'Add Model',
        size: 'lg',
        content: getModalContent(),
        onClose: () => { document.getElementById('form-model').reset(); }
    });
    modelModal.render();

    // Form handler
    document.getElementById('form-model').addEventListener('submit', handleModelSubmit);

    // Add button
    document.getElementById('btn-add-model').addEventListener('click', () => openModelModal());

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

function openModelModal(model = null) {
    const form = document.getElementById('form-model');
    form.reset();
    document.getElementById('f-enabled').checked = true;

    if (model) {
        modelModal.title = 'Edit Model: ' + model.alias;
        document.getElementById('f-alias').value = model.alias;
        document.getElementById('f-alias').readOnly = true;
        document.getElementById('f-provider').value = model.provider;
        document.getElementById('f-api_base').value = model.api_base || '';
        document.getElementById('f-api_key_env').value = model.api_key_env;
        document.getElementById('f-model_class').value = model.model_class;
        document.getElementById('f-priority').value = model.priority;
        document.getElementById('f-price_in_per_mtok').value = model.price_in_per_mtok || '';
        document.getElementById('f-price_out_per_mtok').value = model.price_out_per_mtok || '';
        document.getElementById('f-price_cache_read_per_mtok').value = model.price_cache_read_per_mtok || '';
        document.getElementById('f-enabled').checked = model.enabled;
        document.getElementById('f-status').value = model.status;
    } else {
        modelModal.title = 'Add Model';
        document.getElementById('f-alias').readOnly = false;
    }
    modelModal.show();
}

async function editModel(alias) {
    try {
        const model = await apiGet('/models/' + encodeURIComponent(alias));
        openModelModal(model);
    } catch (e) {
        showError('Failed to load model: ' + e.message);
    }
}

async function deleteModel(alias) {
    confirmDialog('Delete model "' + alias + '"?', async () => {
        try {
            await apiDelete('/models/' + encodeURIComponent(alias));
            await loadModels();
        } catch (e) {
            showError('Failed to delete model: ' + e.message);
        }
    });
}

async function handleModelSubmit(e) {
    e.preventDefault();
    const fd = new FormData(e.target);
    const data = Object.fromEntries(fd);
    data.priority = parseInt(data.priority) || 100;
    data.enabled = data.enabled === 'on';
    if (data.price_in_per_mtok === '') delete data.price_in_per_mtok;
    else data.price_in_per_mtok = parseFloat(data.price_in_per_mtok);
    if (data.price_out_per_mtok === '') delete data.price_out_per_mtok;
    else data.price_out_per_mtok = parseFloat(data.price_out_per_mtok);
    if (data.price_cache_read_per_mtok === '') delete data.price_cache_read_per_mtok;
    else data.price_cache_read_per_mtok = parseFloat(data.price_cache_read_per_mtok);

    const isEdit = document.getElementById('f-alias').readOnly;

    try {
        if (isEdit) {
            await apiPost('/models/' + encodeURIComponent(data.alias), data);
        } else {
            await apiPost('/models', data);
        }
        modelModal.hide();
        await loadModels();
    } catch (e) {
        showError('Failed to save model: ' + e.message);
    }
}

function getModalContent() {
    return `
        <form id="form-model">
            <input type="hidden" id="model-alias" name="alias">
            <div class="form-row">
                <label>Alias <span class="required">*</span></label>
                <input type="text" name="alias" id="f-alias" required>
            </div>
            <div class="form-row">
                <label>Provider <span class="required">*</span></label>
                <input type="text" name="provider" id="f-provider" placeholder="gemini/gemini-3.8-flash" required>
            </div>
            <div class="form-row">
                <label>API Base (optional)</label>
                <input type="text" name="api_base" id="f-api_base" placeholder="https://api.example.com/v1">
            </div>
            <div class="form-row">
                <label>API Key Env Name <span class="required">*</span></label>
                <input type="text" name="api_key_env" id="f-api_key_env" placeholder="GEMINI_API_KEY" required>
            </div>
            <div class="form-row">
                <label>Class</label>
                <select name="model_class" id="f-model_class">
                    <option value="OPUS">OPUS</option>
                    <option value="SONNET" selected>SONNET</option>
                    <option value="HAIKU">HAIKU</option>
                    <option value="FABLE">FABLE</option>
                </select>
            </div>
            <div class="form-row">
                <label>Priority</label>
                <input type="number" name="priority" id="f-priority" value="100" min="1">
            </div>
            <div class="form-row">
                <label>Price In ($/Mtok)</label>
                <input type="number" name="price_in_per_mtok" id="f-price_in_per_mtok" step="0.0001" min="0">
            </div>
            <div class="form-row">
                <label>Price Out ($/Mtok)</label>
                <input type="number" name="price_out_per_mtok" id="f-price_out_per_mtok" step="0.0001" min="0">
            </div>
            <div class="form-row">
                <label>Price Cache ($/Mtok)</label>
                <input type="number" name="price_cache_read_per_mtok" id="f-price_cache_read_per_mtok" step="0.0001" min="0">
            </div>
            <div class="form-row">
                <label>Enabled</label>
                <input type="checkbox" name="enabled" id="f-enabled" checked>
            </div>
            <div class="form-row">
                <label>Status</label>
                <select name="status" id="f-status">
                    <option value="unknown">unknown</option>
                    <option value="ok">ok</option>
                    <option value="error">error</option>
                </select>
            </div>
            <div class="modal-actions">
                <button type="button" class="btn-secondary" onclick="Modal.closeAll()">Cancel</button>
                <button type="submit" class="btn-primary">Save</button>
            </div>
        </form>
    `;
}

// Global for onclick handlers
window.editModel = editModel;
window.deleteModel = deleteModel;
window.Modal = Modal;