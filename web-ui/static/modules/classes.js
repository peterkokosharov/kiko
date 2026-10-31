// modules/classes.js - Classes management module
// SPDX-License-Identifier: MIT
// Author: Peter Kokosharov <peter.kokosharov@gmail.com>

import { apiGet, apiPost, apiDelete } from '../core/api.js';
import { Table, badge } from '../components/table.js';
import { Modal, confirmDialog } from '../components/modal.js';
import { escapeHtml } from '../core/utils.js';

let classesTable = null;
let classModal = null;

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

    classModal = new Modal('modal-class', {
        title: 'Add Class',
        size: 'sm',
        content: getModalContent(),
        onClose: () => { document.getElementById('form-class').reset(); }
    });
    classModal.render();

    document.getElementById('form-class').addEventListener('submit', handleClassSubmit);
    document.getElementById('btn-add-class').addEventListener('click', () => openClassModal());

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

function openClassModal() {
    document.getElementById('form-class').reset();
    classModal.show();
}

async function deleteClass(name) {
    confirmDialog('Delete class "' + name + '"?', async () => {
        try {
            await apiDelete('/classes/' + encodeURIComponent(name));
            await loadClasses();
        } catch (e) {
            showError('Failed to delete class: ' + e.message);
        }
    });
}

async function handleClassSubmit(e) {
    e.preventDefault();
    const fd = new FormData(e.target);
    const data = Object.fromEntries(fd);
    data.name = data.name.toUpperCase();
    data.keyword = data.keyword.toLowerCase();
    data.sort = parseInt(data.sort) || 100;

    try {
        await apiPost('/classes', data);
        classModal.hide();
        await loadClasses();
    } catch (e) {
        showError('Failed to save class: ' + e.message);
    }
}

function getModalContent() {
    return `
        <form id="form-class">
            <div class="form-row">
                <label>Name <span class="required">*</span></label>
                <input type="text" name="name" id="f-class-name" placeholder="VOICE" required>
            </div>
            <div class="form-row">
                <label>Keyword <span class="required">*</span></label>
                <input type="text" name="keyword" id="f-class-keyword" placeholder="voice" required>
            </div>
            <div class="form-row">
                <label>Role</label>
                <input type="text" name="role" id="f-class-role" placeholder="Voice models">
            </div>
            <div class="form-row">
                <label>Sort</label>
                <input type="number" name="sort" id="f-class-sort" value="100" min="1">
            </div>
            <div class="modal-actions">
                <button type="button" class="btn-secondary" onclick="Modal.closeAll()">Cancel</button>
                <button type="submit" class="btn-primary">Save</button>
            </div>
        </form>
    `;
}

window.deleteClass = deleteClass;