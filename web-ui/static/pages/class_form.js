// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
// LLM: Anthropic Opus
import { apiGet, apiPost } from '/static/core/api.js';
import { showError } from '/static/core/auth.js';

const urlParams = new URLSearchParams(window.location.search);
const editName = urlParams.get('edit');
const isEdit = !!editName;

document.getElementById('page-title').textContent = isEdit ? 'Edit Class' : 'Add Class';
if (isEdit) {
    loadClass(editName);
}

async function loadClass(name) {
    try {
        const data = await apiGet('/classes/' + encodeURIComponent(name));
        document.getElementById('f-class-name').value = data.name;
        document.getElementById('f-class-name').readOnly = true;
        document.getElementById('f-class-keyword').value = data.keyword;
        document.getElementById('f-class-role').value = data.role || '';
        document.getElementById('f-class-sort').value = data.sort || 100;
    } catch (e) {
        showError('Failed to load class: ' + e.message);
    }
}

document.getElementById('form-class').addEventListener('submit', async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const data = Object.fromEntries(fd);
    data.name = data.name.toUpperCase();
    data.keyword = data.keyword.toLowerCase();
    data.sort = parseInt(data.sort) || 100;

    try {
        await apiPost('/classes', data);
        window.location.href = '/';
    } catch (e) {
        showError('Failed to save class: ' + e.message);
    }
});
