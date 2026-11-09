// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
// LLM: Anthropic Opus
import { login, showError } from '/static/core/auth.js';

document.getElementById('form-login').addEventListener('submit', async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const data = Object.fromEntries(fd);

    try {
        await login(data.username, data.password);
        window.location.href = '/';
    } catch (err) {
        const errorEl = document.getElementById('login-error');
        errorEl.textContent = err.message;
        errorEl.style.display = 'block';
    }
});
