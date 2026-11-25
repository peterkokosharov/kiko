# Author: Peter Kokosharov <peter.kokosharov@gmail.com>
# AI models: claude-sonnet-4-6, claude-sonnet-5-5
"""Tests for tray/logs.py — tail, redaction, and combined log helpers."""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tray.logs import (
    copy_last_n,
    get_server_log,
    get_tray_log,
    newest_first,
    redact,
    server_log_path,
    tail_file,
    tray_log_path,
)


class TestRedact(unittest.TestCase):

    # L1 — Authorization header
    def test_authorization_bearer_redacted(self):
        result = redact('Authorization: Bearer sk-abc123def')
        self.assertIn('[REDACTED]', result)
        self.assertIn('Bearer', result)
        self.assertNotIn('sk-abc123def', result)

    # L2 — x-api-key header
    def test_x_api_key_redacted(self):
        result = redact('x-api-key: mykey12345')
        self.assertIn('x-api-key:', result)
        self.assertNotIn('mykey12345', result)
        self.assertIn('[REDACTED]', result)

    # L3 — JSON api_key field
    def test_json_api_key_redacted(self):
        result = redact('{"api_key": "sk-secret"}')
        self.assertIn('api_key', result)
        self.assertNotIn('sk-secret', result)
        self.assertIn('[REDACTED]', result)

    # L4 — normal line is unchanged
    def test_normal_line_unchanged(self):
        line = 'normal log line without keys'
        self.assertEqual(redact(line), line)

    # Extra — x-goog-api-key
    def test_x_goog_api_key_redacted(self):
        result = redact('x-goog-api-key: AIzaSyABCDEF')
        self.assertIn('x-goog-api-key:', result)
        self.assertNotIn('AIzaSyABCDEF', result)

    # Extra — query string api_key
    def test_query_string_api_key_redacted(self):
        result = redact('GET /v1/models?api_key=secret123')
        self.assertIn('api_key=', result)
        self.assertNotIn('secret123', result)

    # Extra — bare OpenAI-style key
    def test_bare_sk_key_redacted(self):
        result = redact('key is sk-abcdefgh1234 in the log')
        self.assertNotIn('sk-abcdefgh1234', result)
        self.assertIn('[REDACTED]', result)

    # Extra — case insensitivity
    def test_case_insensitive(self):
        result = redact('X-API-KEY: MySecretKey')
        self.assertIn('X-API-KEY:', result)
        self.assertNotIn('MySecretKey', result)


class TestTailFile(unittest.TestCase):

    # L5 — 10-line file, tail 5
    def test_tail_returns_last_n_lines(self, tmp_path=None):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'test.log'
            lines = [f'line{i}\n' for i in range(10)]
            path.write_text(''.join(lines), encoding='utf-8')
            result = tail_file(path, 5)
        self.assertEqual(len(result), 5)
        self.assertEqual(result[0], 'line5')
        self.assertEqual(result[-1], 'line9')

    # L6 — missing file → empty list
    def test_missing_file_returns_empty(self):
        result = tail_file(Path('/nonexistent/path/to/file.log'))
        self.assertEqual(result, [])

    def test_tail_strips_newlines(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'test.log'
            path.write_text('a\nb\nc\n', encoding='utf-8')
            result = tail_file(path)
        self.assertNotIn('\n', result[0])


class TestGetServerLog(unittest.TestCase):

    # L7 — server log with key is redacted
    def test_server_log_redacts_keys(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            log_file = Path(td) / 'kiko-server.log'
            log_file.write_text(
                'INFO starting\nAuthorization: Bearer sk-real\nINFO done\n',
                encoding='utf-8',
            )
            with patch.dict(os.environ, {'KIKO_LOG_DIR': td}):
                result = get_server_log()
        combined = '\n'.join(result)
        self.assertIn('[REDACTED]', combined)
        self.assertNotIn('sk-real', combined)
        self.assertIn('Bearer', combined)


class TestCopyLastN(unittest.TestCase):

    # L8 — combined logs with keys → redacted
    def test_copy_last_n_redacts_both_logs(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            server_log = Path(td) / 'kiko-server.log'
            tray_log_f = Path(td) / 'kiko-tray.log'
            server_log.write_text('x-api-key: server-secret\n', encoding='utf-8')
            tray_log_f.write_text('x-goog-api-key: tray-secret\n', encoding='utf-8')
            with patch.dict(os.environ, {'KIKO_LOG_DIR': td}):
                result = copy_last_n()
        self.assertIn('[REDACTED]', result)
        self.assertNotIn('server-secret', result)
        self.assertNotIn('tray-secret', result)


class TestNewestFirst(unittest.TestCase):

    def test_reverses_single_line_entries(self):
        lines = ['2026-11-24 10:00:00,001 a INFO one', '2026-11-24 10:00:00,002 a INFO two']
        self.assertEqual(newest_first(lines), [lines[1], lines[0]])

    def test_keeps_traceback_with_its_entry(self):
        lines = ['cut-off tail', '2026-11-24 10:00:01,000 a ERROR boom', 'Traceback:', '  File x', 'ValueError',
                 '2026-11-24 10:00:02,000 a INFO after', 'kiko-lite on http://127.0.0.1:4000',
                 '2026-11-24 10:00:03,000 a INFO newest']
        self.assertEqual(newest_first(lines), [
            lines[7], lines[6], lines[5], lines[1], lines[2], lines[3], lines[4], lines[0]])

    def test_iso_t_format(self):
        lines = ['2026-11-24T08:16:25.482056+00:00 haiku probe ok',
                 '2026-11-24T08:16:26.100000+00:00 sonnet probe ok',
                 '2026-11-24T08:16:27.200000+00:00 opus probe failed']
        self.assertEqual(newest_first(lines), lines[::-1])

    def test_mixed_formats(self):
        lines = ['2026-11-24 10:00:00,001 a INFO one', '2026-11-24T10:00:01+00:00 b boom', 'Traceback:',
                 '2026-11-24 10:00:02,000 a INFO three']
        self.assertEqual(newest_first(lines), [lines[3], lines[1], lines[2], lines[0]])

    def test_no_entry_start_falls_back_to_plain_reversal(self):
        self.assertEqual(newest_first(['a', 'b', 'c']), ['c', 'b', 'a'])

    def test_empty(self):
        self.assertEqual(newest_first([]), [])


if __name__ == '__main__':
    unittest.main()
