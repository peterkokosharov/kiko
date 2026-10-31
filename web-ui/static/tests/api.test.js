// tests/api.test.js - API integration tests (no external deps)
// Run with: node --test tests/api.test.js

import { test, describe, before } from 'node:test';
import assert from 'node:assert';

const BASE_URL = 'http://127.0.0.1:4008';
let cookieHeader = '';

async function fetchWithAuth(path, options = {}) {
    const headers = { 'Content-Type': 'application/json', ...options.headers };
    if (cookieHeader) {
        headers.Cookie = cookieHeader;
    }

    const res = await fetch(`${BASE_URL}${path}`, {
        ...options,
        headers,
    });

    // Store response cookies
    const setCookie = res.headers.get('set-cookie');
    if (setCookie) {
        // Extract session cookie
        const cookies = setCookie.split(',').map(c => c.trim());
        for (const c of cookies) {
            if (c.startsWith('session=')) {
                cookieHeader = c.split(';')[0];
                break;
            }
        }
    }

    return res;
}

async function login() {
    const res = await fetch(`${BASE_URL}/api/auth/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: 'admin', password: 'admin' }),
    });
    assert.strictEqual(res.status, 200);
    const data = await res.json();
    assert.ok(data.ok);

    // Store login cookie
    const setCookie = res.headers.get('set-cookie');
    if (setCookie) {
        const cookies = setCookie.split(',').map(c => c.trim());
        for (const c of cookies) {
            if (c.startsWith('session=')) {
                cookieHeader = c.split(';')[0];
                break;
            }
        }
    }

    return data.token;
}

describe('web-ui API', () => {
    before(async () => {
        await login();
    });

    describe('auth', () => {
        test('GET /api/auth/status returns authenticated', async () => {
            const res = await fetchWithAuth('/api/auth/status');
            assert.strictEqual(res.status, 200);
            const data = await res.json();
            assert.strictEqual(data.authenticated, true);
            assert.strictEqual(data.user, 'admin');
        });

        test('POST /api/auth/logout works', async () => {
            const res = await fetchWithAuth('/api/auth/logout', { method: 'POST' });
            assert.strictEqual(res.status, 200);
            const data = await res.json();
            assert.strictEqual(data.ok, true);

            // Re-login for remaining tests
            await login();
        });
    });

    describe('models', () => {
        test('GET /api/models returns array', async () => {
            const res = await fetchWithAuth('/api/models');
            assert.strictEqual(res.status, 200);
            const data = await res.json();
            assert.ok(Array.isArray(data.models));
            assert.ok(data.models.length > 0);
        });

        test('GET /api/models/{alias} returns model', async () => {
            const res = await fetchWithAuth('/api/models/gemini-flash');
            assert.strictEqual(res.status, 200);
            const data = await res.json();
            assert.strictEqual(data.alias, 'gemini-flash');
        });

        test('POST /api/models creates model', async () => {
            const alias = `test-model-${Date.now()}`;
            const res = await fetchWithAuth('/api/models', {
                method: 'POST',
                body: JSON.stringify({
                    alias,
                    provider: 'test/provider',
                    api_key_env: 'TEST_API_KEY',
                    model_class: 'SONNET',
                    priority: 100
                })
            });
            assert.strictEqual(res.status, 200);
            const data = await res.json();
            assert.strictEqual(data.alias, alias);
            assert.strictEqual(data.created, true);

            // Clean up
            await fetchWithAuth(`/api/models/${alias}`, { method: 'DELETE' });
        });

        test('DELETE /api/models/{alias} deletes model', async () => {
            const alias = `test-delete-${Date.now()}`;
            await fetchWithAuth('/api/models', {
                method: 'POST',
                body: JSON.stringify({
                    alias,
                    provider: 'test/provider',
                    api_key_env: 'TEST_API_KEY',
                    model_class: 'SONNET',
                    priority: 100
                })
            });
            const res = await fetchWithAuth(`/api/models/${alias}`, { method: 'DELETE' });
            assert.strictEqual(res.status, 200);
            const data = await res.json();
            assert.strictEqual(data.deleted, alias);
        });
    });

    describe('classes', () => {
        test('GET /api/classes returns array', async () => {
            const res = await fetchWithAuth('/api/classes');
            assert.strictEqual(res.status, 200);
            const data = await res.json();
            assert.ok(Array.isArray(data.classes));
        });

        test('POST /api/classes creates class', async () => {
            const name = `TEST_CLASS_${Date.now()}`;
            const res = await fetchWithAuth('/api/classes', {
                method: 'POST',
                body: JSON.stringify({
                    name,
                    keyword: name.toLowerCase(),
                    role: 'Test role'
                })
            });
            assert.strictEqual(res.status, 200);
            const data = await res.json();
            assert.strictEqual(data.name, name);

            // Clean up
            await fetchWithAuth(`/api/classes/${name}`, { method: 'DELETE' });
        });

        test('DELETE /api/classes/{name} deletes class', async () => {
            const name = `TEST_DELETE_${Date.now()}`;
            await fetchWithAuth('/api/classes', {
                method: 'POST',
                body: JSON.stringify({
                    name,
                    keyword: name.toLowerCase(),
                    role: 'Test role'
                })
            });
            const res = await fetchWithAuth(`/api/classes/${name}`, { method: 'DELETE' });
            assert.strictEqual(res.status, 200);
            const data = await res.json();
            assert.strictEqual(data.deleted, name);
        });
    });

    describe('billing', () => {
        test('GET /api/billing returns rows', async () => {
            const res = await fetchWithAuth('/api/billing?days=30');
            assert.strictEqual(res.status, 200);
            const data = await res.json();
            assert.ok(Array.isArray(data.rows));
            assert.strictEqual(data.days, 30);
        });
    });

    describe('settings', () => {
        test('GET /api/settings returns object', async () => {
            const res = await fetchWithAuth('/api/settings');
            assert.strictEqual(res.status, 200);
            const data = await res.json();
            assert.ok(typeof data.settings === 'object');
        });

        test('POST /api/settings sets value', async () => {
            const key = `test_setting_${Date.now()}`;
            const res = await fetchWithAuth('/api/settings', {
                method: 'POST',
                body: JSON.stringify({ key, value: 'test-value' })
            });
            assert.strictEqual(res.status, 200);
            const data = await res.json();
            assert.strictEqual(data.key, key);
            assert.strictEqual(data.value, 'test-value');
        });
    });

    describe('aliases', () => {
        test('GET /api/aliases returns array', async () => {
            const res = await fetchWithAuth('/api/aliases');
            assert.strictEqual(res.status, 200);
            const data = await res.json();
            assert.ok(Array.isArray(data.aliases));
        });

        test('POST /api/aliases creates alias', async () => {
            const glob = `test-glob-${Date.now()}*`;
            const res = await fetchWithAuth('/api/aliases', {
                method: 'POST',
                body: JSON.stringify({ glob, target: 'gemini-flash' })
            });
            assert.strictEqual(res.status, 200);
            const data = await res.json();
            assert.strictEqual(data.glob, glob);
            assert.strictEqual(data.target, 'gemini-flash');

            // Clean up
            await fetchWithAuth(`/api/aliases/${encodeURIComponent(glob)}`, { method: 'DELETE' });
        });

        test('DELETE /api/aliases/{glob} deletes alias', async () => {
            const glob = `test-del-${Date.now()}*`;
            await fetchWithAuth('/api/aliases', {
                method: 'POST',
                body: JSON.stringify({ glob, target: 'gemini-flash' })
            });
            const res = await fetchWithAuth(`/api/aliases/${encodeURIComponent(glob)}`, { method: 'DELETE' });
            assert.strictEqual(res.status, 200);
            const data = await res.json();
            assert.strictEqual(data.deleted, glob);
        });
    });

    describe('key-status', () => {
        test('GET /api/key-status/{alias} returns source', async () => {
            const res = await fetchWithAuth('/api/key-status/gemini-flash');
            assert.strictEqual(res.status, 200);
            const data = await res.json();
            assert.ok(['env', 'dotenv', 'store', 'NOT_FOUND'].includes(data.source));
        });
    });
});