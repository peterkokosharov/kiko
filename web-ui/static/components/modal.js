// components/modal.js - Reusable modal component
// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
// LLM: Anthropic Opus

import { escapeHtml } from '../core/utils.js';

export class Modal {
    constructor(id, options = {}) {
        this.id = id;
        this.title = options.title || '';
        this.onClose = options.onClose || (() => {});
        this.content = options.content || '';
        this.showCloseBtn = options.showCloseBtn !== false;
        this.size = options.size || 'md'; // sm, md, lg
        this.closeOnBackdrop = options.closeOnBackdrop !== false;
        this.element = null;
    }

    render() {
        const sizes = { sm: 'modal-sm', md: 'modal-md', lg: 'modal-lg' };

        this.element = document.createElement('div');
        this.element.id = this.id;
        this.element.className = 'modal' + (this.closeOnBackdrop ? '' : ' no-backdrop-close');
        this.element.innerHTML = `
            <div class="modal-content ${sizes[this.size] || 'modal-md'}">
                <h3>${escapeHtml(this.title)}</h3>
                ${this.content}
            </div>
        `;

        if (this.closeOnBackdrop) {
            this.element.addEventListener('click', (e) => {
                if (e.target === this.element) this.hide();
            });
        }

        document.body.appendChild(this.element);
        return this;
    }

    show() {
        if (!this.element) this.render();
        this.element.classList.remove('hidden');
        document.body.style.overflow = 'hidden';
        // Focus first input
        setTimeout(() => {
            const input = this.element.querySelector('input, select, textarea');
            if (input) input.focus();
        }, 50);
    }

    hide() {
        if (this.element) {
            this.element.classList.add('hidden');
            document.body.style.overflow = '';
        }
        this.onClose();
    }

    destroy() {
        if (this.element) {
            this.element.remove();
            this.element = null;
        }
    }

    static closeAll() {
        document.querySelectorAll('.modal:not(.hidden)').forEach(m => m.classList.add('hidden'));
        document.body.style.overflow = '';
    }
}

// Confirm dialog helper
export function confirmDialog(message, onConfirm, onCancel = () => {}) {
    const modal = new Modal('confirm-modal', {
        title: 'Confirm',
        content: `
            <p class="confirm-text">${escapeHtml(message)}</p>
            <div class="modal-actions">
                <button type="button" class="btn-secondary" data-action="cancel">Cancel</button>
                <button type="button" class="btn-danger" data-action="confirm">Confirm</button>
            </div>
        `,
        size: 'sm',
        onClose: () => modal.destroy()
    });

    modal.render();

    modal.element.querySelector('[data-action="confirm"]').addEventListener('click', () => {
        onConfirm();
        modal.hide();
    });

    modal.element.querySelector('[data-action="cancel"]').addEventListener('click', () => {
        onCancel();
        modal.hide();
    });

    modal.show();
    return modal;
}