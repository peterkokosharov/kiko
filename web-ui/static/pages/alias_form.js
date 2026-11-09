// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
// LLM: Anthropic Opus
import { apiPost } from '/static/core/api.js';
import { showError } from '/static/core/auth.js';

document.getElementById('form-alias').addEventListener('submit', async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const data = Object.fromEntries(fd);

    try {
        await apiPost('/aliases', data);
        window.location.href = '/';
    } catch (e) {
        showError('Failed to save alias: ' + e.message);
    }
});
