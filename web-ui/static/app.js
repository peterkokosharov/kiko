// kiko-lite web-ui vanilla JS

const API = '/api';

// ---- Utils -----------------------------------------------------------------------

async function apiGet(path) {
    const res = await fetch(API + path);
    if (!res.ok) throw new Error(await res.text());
    return res.json();
}

async function apiPost(path, data) {
    const res = await fetch(API + path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data)
    });
    if (!res.ok) throw new Error(await res.text());
    return res.json();
}

async function apiDelete(path) {
    const res = await fetch(API + path, { method: 'DELETE' });
    if (!res.ok) throw new Error(await res.text());
    return res.json();
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function formatNumber(n) {
    if (n === null || n === undefined) return '—';
    return Number(n).toLocaleString();
}

function formatMoney(n) {
    if (n === null || n === undefined) return '—';
    return '$' + Number(n).toFixed(6);
}

function showError(msg) {
    alert('Error: ' + msg);
}

// ---- Tabs ------------------------------------------------------------------------

document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
        document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
        btn.classList.add('active');
        document.getElementById('tab-' + btn.dataset.tab).classList.add('active');
        loadTab(btn.dataset.tab);
    });
});

function loadTab(tab) {
    switch (tab) {
        case 'models': loadModels(); break;
        case 'classes': loadClasses(); break;
        case 'billing': loadBilling(); break;
        case 'settings': loadSettings(); break;
        case 'aliases': loadAliases(); break;
    }
}

// ---- DB Path ---------------------------------------------------------------------

async function loadDbPath() {
    try {
        const data = await apiGet('/models');
        document.getElementById('db-path').textContent = 'Connected';
    } catch (e) {
        document.getElementById('db-path').textContent = 'Error loading DB';
    }
}

// ---- Models ----------------------------------------------------------------------

async function loadModels() {
    try {
        const data = await apiGet('/models');
        const tbody = document.querySelector('#models-table tbody');
        tbody.innerHTML = data.models.map(m => `
            <tr>
                <td><code>${escapeHtml(m.alias)}</code></td>
                <td><code>${escapeHtml(m.provider)}</code></td>
                <td>${escapeHtml(m.model_class)}</td>
                <td>${m.priority}</td>
                <td><span class="badge badge-${m.status}">${escapeHtml(m.status)}</span></td>
                <td><span class="badge badge-${m.enabled ? 'enabled' : 'disabled'}">${m.enabled ? 'Yes' : 'No'}</span></td>
                <td>
                    <span class="key-status" id="key-${escapeHtml(m.alias)}">
                        <span class="dot not-found"></span>
                        <span>checking...</span>
                    </span>
                </td>
                <td class="actions">
                    <button class="btn-sm btn-secondary" onclick="editModel('${escapeHtml(m.alias)}')">Edit</button>
                    <button class="btn-sm btn-danger" onclick="deleteModel('${escapeHtml(m.alias)}')">Delete</button>
                </td>
            </tr>
        `).join('');

        // Load key status for each model
        for (const m of data.models) {
            loadKeyStatus(m.alias);
        }
    } catch (e) {
        showError('Failed to load models: ' + e.message);
    }
}

async function loadKeyStatus(alias) {
    try {
        const data = await apiGet('/key-status/' + encodeURIComponent(alias));
        const el = document.getElementById('key-' + alias);
        if (el) {
            const found = data.source !== 'NOT_FOUND';
            el.innerHTML = `
                <span class="dot ${found ? 'found' : 'not-found'}"></span>
                <span>${escapeHtml(data.source)}</span>
            `;
        }
    } catch (e) {
        const el = document.getElementById('key-' + alias);
        if (el) el.innerHTML = '<span class="dot not-found"></span><span>error</span>';
    }
}

function openModelModal(model = null) {
    const modal = document.getElementById('modal-model');
    const form = document.getElementById('form-model');
    form.reset();
    document.getElementById('f-enabled').checked = true;

    if (model) {
        document.getElementById('modal-model-title').textContent = 'Edit Model: ' + model.alias;
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
        document.getElementById('modal-model-title').textContent = 'Add Model';
        document.getElementById('f-alias').readOnly = false;
    }

    modal.classList.remove('hidden');
}

function closeModal(id) {
    document.getElementById(id).classList.add('hidden');
}

document.getElementById('btn-add-model').addEventListener('click', () => openModelModal());

document.getElementById('form-model').addEventListener('submit', async (e) => {
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
        closeModal('modal-model');
        loadModels();
    } catch (e) {
        showError('Failed to save model: ' + e.message);
    }
});

async function editModel(alias) {
    try {
        const model = await apiGet('/models/' + encodeURIComponent(alias));
        openModelModal(model);
    } catch (e) {
        showError('Failed to load model: ' + e.message);
    }
}

async function deleteModel(alias) {
    if (!confirm('Delete model "' + alias + '"?')) return;
    try {
        await apiDelete('/models/' + encodeURIComponent(alias));
        loadModels();
    } catch (e) {
        showError('Failed to delete model: ' + e.message);
    }
}

// ---- Classes ---------------------------------------------------------------------

async function loadClasses() {
    try {
        const data = await apiGet('/classes');
        const tbody = document.querySelector('#classes-table tbody');
        tbody.innerHTML = data.classes.map(c => `
            <tr>
                <td><code>${escapeHtml(c.name)}</code></td>
                <td><code>${escapeHtml(c.keyword)}</code></td>
                <td>${escapeHtml(c.role)}</td>
                <td>${c.sort}</td>
                <td>${c.models}</td>
                <td class="actions">
                    <button class="btn-sm btn-danger" onclick="deleteClass('${escapeHtml(c.name)}')">Delete</button>
                </td>
            </tr>
        `).join('');
    } catch (e) {
        showError('Failed to load classes: ' + e.message);
    }
}

document.getElementById('btn-add-class').addEventListener('click', () => {
    document.getElementById('form-class').reset();
    document.getElementById('modal-class').classList.remove('hidden');
});

document.getElementById('form-class').addEventListener('submit', async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const data = Object.fromEntries(fd);
    data.name = data.name.toUpperCase();
    data.keyword = data.keyword.toLowerCase();
    data.sort = parseInt(data.sort) || 100;

    try {
        await apiPost('/classes', data);
        closeModal('modal-class');
        loadClasses();
    } catch (e) {
        showError('Failed to save class: ' + e.message);
    }
});

async function deleteClass(name) {
    if (!confirm('Delete class "' + name + '"?')) return;
    try {
        await apiDelete('/classes/' + encodeURIComponent(name));
        loadClasses();
    } catch (e) {
        showError('Failed to delete class: ' + e.message);
    }
}

// ---- Billing ---------------------------------------------------------------------

async function loadBilling() {
    try {
        const days = document.getElementById('billing-days').value;
        const data = await apiGet('/billing?days=' + days);
        const tbody = document.querySelector('#billing-table tbody');
        tbody.innerHTML = data.rows.map(r => `
            <tr>
                <td><code>${escapeHtml(r.model)}</code></td>
                <td>${formatNumber(r.calls)}</td>
                <td>${formatNumber(r.errors)}</td>
                <td>${formatNumber(r.tokens_in)}</td>
                <td>${formatNumber(r.tokens_out)}</td>
                <td>${formatMoney(r.cost_usd)}</td>
            </tr>
        `).join('');

        const total = data.rows.reduce((sum, r) => sum + (r.cost_usd || 0), 0);
        document.getElementById('billing-total').textContent = 'Total: ' + formatMoney(total);
    } catch (e) {
        showError('Failed to load billing: ' + e.message);
    }
}

document.getElementById('billing-days').addEventListener('change', loadBilling);

// ---- Settings --------------------------------------------------------------------

async function loadSettings() {
    try {
        const data = await apiGet('/settings');
        const tbody = document.querySelector('#settings-table tbody');
        tbody.innerHTML = Object.entries(data.settings).map(([k, v]) => `
            <tr>
                <td><code>${escapeHtml(k)}</code></td>
                <td><code>${escapeHtml(v)}</code></td>
                <td class="actions">
                    <button class="btn-sm btn-danger" onclick="deleteSetting('${escapeHtml(k)}')">Delete</button>
                </td>
            </tr>
        `).join('');
    } catch (e) {
        showError('Failed to load settings: ' + e.message);
    }
}

document.getElementById('btn-add-setting').addEventListener('click', async () => {
    const key = document.getElementById('setting-key').value.trim();
    const value = document.getElementById('setting-value').value.trim();
    if (!key) return alert('Key required');

    try {
        await apiPost('/settings', { key, value });
        document.getElementById('setting-key').value = '';
        document.getElementById('setting-value').value = '';
        loadSettings();
    } catch (e) {
        showError('Failed to set setting: ' + e.message);
    }
});

async function deleteSetting(key) {
    if (!confirm('Delete setting "' + key + '"?')) return;
    try {
        // No delete endpoint, set to empty
        await apiPost('/settings', { key, value: '' });
        loadSettings();
    } catch (e) {
        showError('Failed to delete setting: ' + e.message);
    }
}

// ---- Aliases ---------------------------------------------------------------------

async function loadAliases() {
    try {
        const data = await apiGet('/aliases');
        const tbody = document.querySelector('#aliases-table tbody');
        tbody.innerHTML = data.aliases.map(a => `
            <tr>
                <td><code>${escapeHtml(a.glob)}</code></td>
                <td><code>${escapeHtml(a.target)}</code></td>
                <td class="actions">
                    <button class="btn-sm btn-danger" onclick="deleteAlias('${escapeHtml(a.glob)}')">Delete</button>
                </td>
            </tr>
        `).join('');
    } catch (e) {
        showError('Failed to load aliases: ' + e.message);
    }
}

document.getElementById('btn-add-alias').addEventListener('click', () => {
    document.getElementById('form-alias').reset();
    document.getElementById('modal-alias').classList.remove('hidden');
});

document.getElementById('form-alias').addEventListener('submit', async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const data = Object.fromEntries(fd);

    try {
        await apiPost('/aliases', data);
        closeModal('modal-alias');
        loadAliases();
    } catch (e) {
        showError('Failed to save alias: ' + e.message);
    }
});

async function deleteAlias(glob) {
    if (!confirm('Delete alias "' + glob + '"?')) return;
    try {
        await apiDelete('/aliases/' + encodeURIComponent(glob));
        loadAliases();
    } catch (e) {
        showError('Failed to delete alias: ' + e.message);
    }
}

// ---- Init -------------------------------------------------------------------------

document.addEventListener('DOMContentLoaded', () => {
    loadDbPath();
    loadTab('models');
});

// Close modal on backdrop click
document.querySelectorAll('.modal').forEach(modal => {
    modal.addEventListener('click', (e) => {
        if (e.target === modal) modal.classList.add('hidden');
    });
});

// Escape key to close modal
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
        document.querySelectorAll('.modal').forEach(m => m.classList.add('hidden'));
    }
});