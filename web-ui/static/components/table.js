// components/table.js - Reusable table component
// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
// LLM: Anthropic Opus

import { escapeHtml, formatNumber, formatMoney } from '../core/utils.js';

export { escapeHtml, formatNumber, formatMoney };

export class Table {
    constructor(container, columns, options = {}) {
        this.container = container;
        this.columns = columns; // [{ key, label, render, className, sortable }]
        this.options = {
            emptyMessage: options.emptyMessage || 'No data',
            rowKey: options.rowKey || 'id',
            striped: options.striped !== false,
            hoverable: options.hoverable !== false,
            actions: options.actions || [] // [{ label, className, handler }]
        };
        this.data = [];
        this.sortColumn = null;
        this.sortDirection = 'asc';
    }

    setData(data) {
        this.data = data;
        this.render();
    }

    getCellValue(row, column) {
        if (column.render) {
            return column.render(row, column);
        }
        const value = row[column.key];
        return escapeHtml(value);
    }

    render() {
        if (!this.container) return;

        let html = '<div class="table-container"><table>';

        // Header
        html += '<thead><tr>';
        this.columns.forEach((col, idx) => {
            const sortable = col.sortable ? ' sortable' : '';
            const sortIcon = this.sortColumn === col.key
                ? (this.sortDirection === 'asc' ? ' ▲' : ' ▼')
                : '';
            html += `<th class="${escapeHtml(col.className || '')}${sortable}" data-col="${escapeHtml(col.key)}">${escapeHtml(col.label)}${sortIcon}</th>`;
        });
        if (this.options.actions.length > 0) {
            html += '<th>Actions</th>';
        }
        html += '</tr></thead>';

        // Body
        html += '<tbody>';
        if (this.data.length === 0) {
            const colCount = this.columns.length + (this.options.actions.length > 0 ? 1 : 0);
            html += `<tr><td colspan="${Number(colCount)}" class="empty-cell">${escapeHtml(this.options.emptyMessage)}</td></tr>`;
        } else {
            this.data.forEach(row => {
                const rowClass = this.options.striped && this.data.indexOf(row) % 2 === 1 ? ' class="striped"' : '';
                html += `<tr${rowClass} data-key="${escapeHtml(row[this.options.rowKey])}">`;
                this.columns.forEach(col => {
                    const className = col.className ? ` class="${escapeHtml(col.className)}"` : '';
                    html += `<td${className}>${this.getCellValue(row, col)}</td>`;
                });
                if (this.options.actions.length > 0) {
                    html += '<td class="actions">';
                    this.options.actions.forEach(action => {
                        html += `<button class="btn-sm ${escapeHtml(action.className)}" data-action="${escapeHtml(action.key)}" data-key="${escapeHtml(row[this.options.rowKey])}">${escapeHtml(action.label)}</button>`;
                    });
                    html += '</td>';
                }
                html += '</tr>';
            });
        }
        html += '</tbody></table></div>';

        this.container.innerHTML = html;

        // Attach sort handlers
        if (this.columns.some(c => c.sortable)) {
            this.container.querySelectorAll('th.sortable').forEach(th => {
                th.style.cursor = 'pointer';
                th.addEventListener('click', () => this.handleSort(th.dataset.col));
            });
        }

        // Attach action handlers
        this.container.querySelectorAll('[data-action]').forEach(btn => {
            btn.addEventListener('click', (e) => {
                const actionKey = btn.dataset.action;
                const rowKey = btn.dataset.key;
                const action = this.options.actions.find(a => a.key === actionKey);
                if (action && action.handler) {
                    action.handler(rowKey, e);
                }
            });
        });
    }

    handleSort(columnKey) {
        const col = this.columns.find(c => c.key === columnKey);
        if (!col || !col.sortable) return;

        if (this.sortColumn === columnKey) {
            this.sortDirection = this.sortDirection === 'asc' ? 'desc' : 'asc';
        } else {
            this.sortColumn = columnKey;
            this.sortDirection = 'asc';
        }

        this.data.sort((a, b) => {
            const av = a[columnKey];
            const bv = b[columnKey];
            if (av < bv) return this.sortDirection === 'asc' ? -1 : 1;
            if (av > bv) return this.sortDirection === 'asc' ? 1 : -1;
            return 0;
        });

        this.render();
    }
}

// Badge helper
export function badge(text, type) {
    const types = {
        ok: 'badge-ok',
        error: 'badge-error',
        unknown: 'badge-unknown',
        enabled: 'badge-enabled',
        disabled: 'badge-disabled'
    };
    return `<span class="badge ${escapeHtml(types[type] || '')}">${escapeHtml(text)}</span>`;
}

// Key status badge
export function keyStatusBadge(source) {
    const found = source !== 'NOT_FOUND';
    return `<span class="key-status"><span class="dot ${found ? 'found' : 'not-found'}"></span><span>${escapeHtml(source)}</span></span>`;
}