// modules/text_logs.js - Text Logs module for LLM request/response logs
// SPDX-License-Identifier: MIT
// Author: Peter Kokosharov <peter.kokosharov@gmail.com>

import { apiGet } from '../core/api.js';
import { Table } from '../components/table.js';
import { escapeHtml } from '../core/utils.js';

let textLogsTable = null;
let currentPage = 1;
let pageSize = 30;
let totalEntries = 0;

export async function initTextLogs() {
    // Create table
    textLogsTable = new Table(
        document.querySelector('#text-logs-table'),
        [
            {
                key: 'created_at',
                label: 'Време',
                render: r => escapeHtml(new Date(r.created_at).toLocaleString())
            },
            {
                key: 'id',
                label: 'Request ID',
                render: r => `<code>${escapeHtml(r.id)}</code>`
            },
            {
                key: 'input_json',
                label: 'Request (JSON)',
                render: r => formatJson(r.input_json),
                maxWidth: '500px'
            },
            {
                key: 'output_json',
                label: 'Response (JSON)',
                render: r => formatJson(r.output_json),
                maxWidth: '500px'
            }
        ],
        {
            rowKey: 'id',
            sortable: true,
            defaultSort: { key: 'created_at', desc: true }
        }
    );

    // Pagination controls
    setupPagination();

    // Load first page
    await loadTextLogs();
}

function formatJson(jsonStr) {
    try {
        const obj = JSON.parse(jsonStr);
        const formatted = JSON.stringify(obj, null, 2);
        return `<pre class="json-pre">${escapeHtml(formatted)}</pre>`;
    } catch (e) {
        return `<pre class="json-pre">${escapeHtml(jsonStr)}</pre>`;
    }
}

function setupPagination() {
    const container = document.getElementById('text-logs-pagination');
    if (!container) return;

    container.innerHTML = `
        <div class="pagination-info" id="text-logs-pagination-info"></div>
        <div class="pagination-controls">
            <button class="btn-secondary btn-sm" id="btn-text-logs-prev" disabled>← Назад</button>
            <span id="text-logs-page-indicator">Страница 1</span>
            <button class="btn-secondary btn-sm" id="btn-text-logs-next" disabled>Напред →</button>
        </div>
        <div class="pagination-size">
            <label>На страница:
                <select id="text-logs-page-size" style="margin-left: 0.5rem;">
                    <option value="10">10</option>
                    <option value="30" selected>30</option>
                    <option value="50">50</option>
                    <option value="100">100</option>
                </select>
            </label>
        </div>
    `;

    document.getElementById('btn-text-logs-prev').addEventListener('click', () => {
        if (currentPage > 1) {
            currentPage--;
            loadTextLogs();
        }
    });

    document.getElementById('btn-text-logs-next').addEventListener('click', () => {
        const totalPages = Math.ceil(totalEntries / pageSize);
        if (currentPage < totalPages) {
            currentPage++;
            loadTextLogs();
        }
    });

    document.getElementById('text-logs-page-size').addEventListener('change', (e) => {
        pageSize = parseInt(e.target.value, 10);
        currentPage = 1;
        loadTextLogs();
    });
}

async function loadTextLogs() {
    try {
        // Get total count first
        const countRes = await apiGet('/responses/count');
        totalEntries = countRes.count || 0;

        // Get paginated data (newest first)
        const offset = (currentPage - 1) * pageSize;
        const data = await apiGet(`/responses?limit=${pageSize}&offset=${offset}&order=desc`);

        textLogsTable.setData(data.responses || []);

        // Update pagination UI
        updatePaginationUI();
    } catch (e) {
        console.error('Failed to load text logs:', e);
    }
}

function updatePaginationUI() {
    const totalPages = Math.ceil(totalEntries / pageSize);

    const infoEl = document.getElementById('text-logs-pagination-info');
    if (infoEl) {
        const start = totalEntries > 0 ? (currentPage - 1) * pageSize + 1 : 0;
        const end = Math.min(currentPage * pageSize, totalEntries);
        infoEl.textContent = `Показване на ${start}–${end} от ${totalEntries} записа`;
    }

    const pageIndicator = document.getElementById('text-logs-page-indicator');
    if (pageIndicator) {
        pageIndicator.textContent = `Страница ${currentPage} от ${totalPages || 1}`;
    }

    const btnPrev = document.getElementById('btn-text-logs-prev');
    const btnNext = document.getElementById('btn-text-logs-next');
    if (btnPrev) btnPrev.disabled = currentPage <= 1;
    if (btnNext) btnNext.disabled = currentPage >= totalPages;
}