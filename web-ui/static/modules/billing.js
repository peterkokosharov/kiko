// modules/billing.js - Billing management module
// SPDX-License-Identifier: MIT
// Author: Peter Kokosharov <peter.kokosharov@gmail.com>

import { apiGet } from '../core/api.js';
import { Table, formatNumber, formatMoney } from '../components/table.js';

let billingTable = null;

export async function initBilling() {
    billingTable = new Table(
        document.querySelector('#billing-table tbody'),
        [
            { key: 'model', label: 'Model', render: r => `<code>${escapeHtml(r.model)}</code>` },
            { key: 'calls', label: 'Calls', sortable: true, render: r => formatNumber(r.calls) },
            { key: 'errors', label: 'Errors', sortable: true, render: r => formatNumber(r.errors) },
            { key: 'tokens_in', label: 'Tokens In', sortable: true, render: r => formatNumber(r.tokens_in) },
            { key: 'tokens_out', label: 'Tokens Out', sortable: true, render: r => formatNumber(r.tokens_out) },
            { key: 'cost_usd', label: 'Cost USD', sortable: true, render: r => formatMoney(r.cost_usd) }
        ],
        {
            rowKey: 'model',
            emptyMessage: 'No billing data for this period'
        }
    );

    document.getElementById('billing-days').addEventListener('change', loadBilling);

    await loadBilling();
}

async function loadBilling() {
    try {
        const days = document.getElementById('billing-days').value;
        const data = await apiGet('/billing?days=' + days);

        billingTable.setData(data.rows);

        const total = data.rows.reduce((sum, r) => sum + (r.cost_usd || 0), 0);
        document.getElementById('billing-total').textContent = 'Total: ' + formatMoney(total);
    } catch (e) {
        showError('Failed to load billing: ' + e.message);
    }
}

function escapeHtml(text) {
    if (text === null || text === undefined) return '';
    const div = document.createElement('div');
    div.textContent = String(text);
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