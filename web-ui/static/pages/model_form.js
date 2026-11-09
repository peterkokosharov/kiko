// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
// LLM: Anthropic Opus
import { apiGet, apiPost } from '/static/core/api.js';
import { showError } from '/static/core/auth.js';

// Check if editing - extract alias from path /models/edit/:alias
const pathParts = window.location.pathname.split('/');
const editAlias = pathParts[3]; // /models/edit/:alias
const isEdit = pathParts[2] === 'edit';

document.getElementById('page-title').textContent = isEdit ? 'Edit Model' : 'Add Model';
if (isEdit) {
    document.getElementById('model-alias').value = editAlias;
    document.getElementById('f-alias').value = editAlias;
    document.getElementById('f-alias').readOnly = true;
    loadModel(editAlias);
}

async function loadModel(alias) {
    try {
        const data = await apiGet('/models/' + encodeURIComponent(alias));
        document.getElementById('f-provider').value = data.provider || '';
        document.getElementById('f-api_base').value = data.api_base || '';
        document.getElementById('f-api_key_env').value = data.api_key_env || '';
        document.getElementById('f-model_class').value = data.model_class || 'SONNET';
        document.getElementById('f-priority').value = data.priority || 100;
        document.getElementById('f-price_in_per_mtok').value = data.price_in_per_mtok || '';
        document.getElementById('f-price_out_per_mtok').value = data.price_out_per_mtok || '';
        document.getElementById('f-price_cache_read_per_mtok').value = data.price_cache_read_per_mtok || '';
        document.getElementById('f-enabled').checked = data.enabled === 1 || data.enabled === true;
        document.getElementById('f-status').value = data.status || 'unknown';
    } catch (e) {
        showError('Failed to load model: ' + e.message);
    }
}

document.getElementById('form-model').addEventListener('submit', async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const data = Object.fromEntries(fd);
    data.enabled = data.enabled ? 1 : 0;
    data.priority = parseInt(data.priority) || 100;
    if (data.price_in_per_mtok === '') data.price_in_per_mtok = null;
    if (data.price_out_per_mtok === '') data.price_out_per_mtok = null;
    if (data.price_cache_read_per_mtok === '') data.price_cache_read_per_mtok = null;

    try {
        const endpoint = isEdit ? '/models/' + encodeURIComponent(editAlias) : '/models';
        const method = isEdit ? 'PUT' : 'POST';
        // Our API uses POST for both create and update
        await apiPost('/models', data);
        window.location.href = '/';
    } catch (e) {
        showError('Failed to save model: ' + e.message);
    }
});
