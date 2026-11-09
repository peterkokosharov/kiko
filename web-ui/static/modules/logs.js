// modules/logs.js - Logs/Billing management module with pagination
// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
// LLM: Anthropic Opus

import { apiGet } from '../core/api.js';
import { Table } from '../components/table.js';
import { escapeHtml, formatNumber } from '../core/utils.js';

let logsTable = null;
let currentPage = 1;
let pageSize = 30;
let totalEntries = 0;

export async function initLogs() {
    // Create table
    logsTable = new Table(
        document.querySelector('#logs-table'),
        [
            { key: 'ts', label: 'Timestamp', render: r => escapeHtml(new Date(r.ts).toLocaleString()) },
            { key: 'model', label: 'Model', render: r => `<code>${escapeHtml(r.model)}</code>` },
            { key: 'route', label: 'Route', render: r => escapeHtml(r.route || '-') },
            { key: 'tokens_in', label: 'Tokens In', render: r => formatNumber(r.tokens_in), align: 'right' },
            { key: 'tokens_out', label: 'Tokens Out', render: r => formatNumber(r.tokens_out), align: 'right' },
            { key: 'cost_usd', label: 'Cost ($)', render: r => r.cost_usd ? '$' + r.cost_usd.toFixed(6) : '-', align: 'right' },
            { key: 'latency_ms', label: 'Latency (ms)', render: r => r.latency_ms ? formatNumber(r.latency_ms) : '-', align: 'right' },
            { key: 'attempts', label: 'Attempts', render: r => r.attempts ? formatNumber(r.attempts) : '-', align: 'center' },
            { key: 'upstream_model', label: 'Upstream', render: r => escapeHtml(r.upstream_model || '-') },
            { key: 'error', label: 'Error', render: r => r.error ? `<span class="badge badge-error">${escapeHtml(r.error)}</span>` : '-' }
        ],
        {
            rowKey: 'id',
            sortable: true,
            defaultSort: { key: 'ts', desc: true }
        }
    );

    // Pagination controls
    setupPagination();

    // Load first page
    await loadLogs();
}

function setupPagination() {
    const container = document.getElementById('logs-pagination');
    if (!container) return;

    container.innerHTML = `
        <div class="pagination-info" id="pagination-info"></div>
        <div class="pagination-controls">
            <button class="btn-secondary btn-sm" id="btn-prev" disabled>← Prev</button>
            <span id="page-indicator">Page 1</span>
            <button class="btn-secondary btn-sm" id="btn-next" disabled>Next →</button>
        </div>
        <div class="pagination-size">
            <label>Per page:
                <select id="page-size" class="page-size-select">
                    <option value="10">10</option>
                    <option value="30" selected>30</option>
                    <option value="50">50</option>
                    <option value="100">100</option>
                </select>
            </label>
        </div>
    `;

    document.getElementById('btn-prev').addEventListener('click', () => {
        if (currentPage > 1) {
            currentPage--;
            loadLogs();
        }
    });

    document.getElementById('btn-next').addEventListener('click', () => {
        const totalPages = Math.ceil(totalEntries / pageSize);
        if (currentPage < totalPages) {
            currentPage++;
            loadLogs();
        }
    });

    document.getElementById('page-size').addEventListener('change', (e) => {
        pageSize = parseInt(e.target.value, 10);
        currentPage = 1;
        loadLogs();
    });
}

async function loadLogs() {
    try {
        // Get total count first
        const countRes = await apiGet('/logs/count');
        totalEntries = countRes.count || 0;

        // Get paginated data (newest first)
        const offset = (currentPage - 1) * pageSize;
        const data = await apiGet(`/logs?limit=${pageSize}&offset=${offset}&order=desc`);

        logsTable.setData(data.logs || []);

        // Update pagination UI
        updatePaginationUI();
    } catch (e) {
        console.error('Failed to load logs:', e);
        // Fallback to billing API if logs endpoint not available
        await loadLogsFallback();
    }
}

async function loadLogsFallback() {
    try {
        // Use billing API as fallback
        const data = await apiGet('/billing?days=365'); // Get all available
        const allLogs = (data.rows || []).map((r, idx) => ({
            id: idx + 1,
            ts: new Date().toISOString(), // billing doesn't have per-entry timestamp
            model: r.model,
            tokens_in: r.tokens_in,
            tokens_out: r.tokens_out,
            cost_usd: r.cost_usd,
            latency_ms: null,
            attempts: null,
            upstream_model: r.model,
            error: r.errors > 0 ? 'error' : null,
            route: 'messages'
        }));

        // Sort by timestamp desc (newest first) - we don't have real timestamps
        // Just use the order from API
        const start = (currentPage - 1) * pageSize;
        const end = start + pageSize;
        const pageLogs = allLogs.slice(start, end);

        totalEntries = allLogs.length;
        logsTable.setData(pageLogs);
        updatePaginationUI();
    } catch (e) {
        console.error('Failed to load logs fallback:', e);
    }
}

function updatePaginationUI() {
    const totalPages = Math.ceil(totalEntries / pageSize);

    const infoEl = document.getElementById('pagination-info');
    if (infoEl) {
        const start = totalEntries > 0 ? (currentPage - 1) * pageSize + 1 : 0;
        const end = Math.min(currentPage * pageSize, totalEntries);
        infoEl.textContent = `Showing ${start}–${end} of ${totalEntries} entries`;
    }

    const pageIndicator = document.getElementById('page-indicator');
    if (pageIndicator) {
        pageIndicator.textContent = `Page ${currentPage} of ${totalPages || 1}`;
    }

    const btnPrev = document.getElementById('btn-prev');
    const btnNext = document.getElementById('btn-next');
    if (btnPrev) btnPrev.disabled = currentPage <= 1;
    if (btnNext) btnNext.disabled = currentPage >= totalPages;
}