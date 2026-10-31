# SPDX-License-Identifier: MIT
"""Tests for providers module."""
import json
import unittest
from kiko import providers as ptable
from kiko.providers import openai_compat as oc
from kiko.providers import gemini


class TestProvidersTable(unittest.TestCase):
    def test_lookup_known_provider(self):
        """Test looking up known providers."""
        result = ptable.lookup("nvidia")
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "nvidia")
        self.assertEqual(result[1]["kind"], "openai")

    def test_lookup_alias(self):
        """Test provider aliases."""
        result = ptable.lookup("qwen")
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "dashscope")

        result = ptable.lookup("google")
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "gemini")

    def test_lookup_unknown(self):
        """Test unknown provider returns None."""
        result = ptable.lookup("unknown_provider_xyz")
        self.assertIsNone(result)

    def test_lookup_case_insensitive(self):
        """Test case-insensitive lookup."""
        result = ptable.lookup("NVIDIA")
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "nvidia")

    def test_providers_structure(self):
        """Test all providers have required fields."""
        for name, entry in ptable.PROVIDERS.items():
            self.assertIn("base_url", entry)
            self.assertIn("kind", entry)
            self.assertIn("key_names", entry)
            self.assertIn("auth", entry)
            self.assertIn("flags", entry)
            self.assertIn(entry["kind"], ("openai", "gemini"))


class TestOpenAICompat(unittest.TestCase):
    def test_endpoint(self):
        """Test endpoint construction."""
        self.assertEqual(oc.endpoint("https://api.example.com"), "https://api.example.com/chat/completions")
        self.assertEqual(oc.endpoint("https://api.example.com/"), "https://api.example.com/chat/completions")

    def test_headers_bearer(self):
        """Test Bearer auth headers."""
        h = oc.headers("test-key", "bearer")
        self.assertEqual(h["authorization"], "Bearer test-key")
        self.assertEqual(h["content-type"], "application/json")

    def test_headers_custom(self):
        """Test custom header auth."""
        h = oc.headers("test-key", "header:x-custom-key")
        self.assertEqual(h["x-custom-key"], "test-key")
        self.assertEqual(h["content-type"], "application/json")

    def test_anthropic_id(self):
        """Test Anthropic ID generation."""
        tid = oc.anthropic_id("abc123")
        self.assertTrue(tid.startswith("toolu_"))
        self.assertIn("abc123", tid)

    def test_upstream_id(self):
        """Test extracting upstream ID."""
        self.assertEqual(oc.upstream_id("toolu_abc123"), "abc123")
        self.assertEqual(oc.upstream_id("plain_id"), "plain_id")

    def test_to_openai_basic(self):
        """Test basic Anthropic -> OpenAI conversion."""
        body = {
            "model": "test-model",
            "messages": [{"role": "user", "content": "Hello"}],
            "max_tokens": 100,
        }
        out = oc.to_openai(body, "upstream-model")
        self.assertEqual(out["model"], "upstream-model")
        self.assertEqual(out["max_tokens"], 100)
        self.assertEqual(len(out["messages"]), 1)
        self.assertEqual(out["messages"][0]["role"], "user")
        self.assertEqual(out["messages"][0]["content"], "Hello")

    def test_to_openai_with_system(self):
        """Test conversion with system prompt."""
        body = {
            "model": "test-model",
            "system": "You are helpful",
            "messages": [{"role": "user", "content": "Hello"}],
        }
        out = oc.to_openai(body, "upstream-model")
        self.assertEqual(out["messages"][0]["role"], "system")
        self.assertEqual(out["messages"][0]["content"], "You are helpful")
        self.assertEqual(out["messages"][1]["role"], "user")

    def test_to_openai_with_tools(self):
        """Test conversion with tools."""
        body = {
            "model": "test-model",
            "messages": [{"role": "user", "content": "Hello"}],
            "tools": [{
                "name": "get_weather",
                "description": "Get weather",
                "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}}
            }],
        }
        out = oc.to_openai(body, "upstream-model")
        self.assertIn("tools", out)
        self.assertEqual(len(out["tools"]), 1)
        self.assertEqual(out["tools"][0]["function"]["name"], "get_weather")

    def test_to_openai_with_temperature(self):
        """Test conversion with temperature."""
        body = {
            "model": "test-model",
            "messages": [{"role": "user", "content": "Hello"}],
            "temperature": 0.7,
            "top_p": 0.9,
        }
        out = oc.to_openai(body, "upstream-model")
        self.assertEqual(out["temperature"], 0.7)
        self.assertEqual(out["top_p"], 0.9)

    def test_to_openai_stream(self):
        """Test conversion with streaming."""
        body = {
            "model": "test-model",
            "messages": [{"role": "user", "content": "Hello"}],
            "stream": True,
        }
        out = oc.to_openai(body, "upstream-model")
        self.assertTrue(out["stream"])
        self.assertIn("stream_options", out)

    def test_parse_arguments(self):
        """Test tool call argument parsing."""
        self.assertEqual(oc.parse_arguments('{"a": 1}'), {"a": 1})
        self.assertEqual(oc.parse_arguments({"a": 1}), {"a": 1})
        self.assertEqual(oc.parse_arguments("not json"), {"_raw": "not json"})
        self.assertEqual(oc.parse_arguments(""), {})

    def test_from_openai_basic(self):
        """Test OpenAI -> Anthropic conversion."""
        resp = {
            "choices": [{
                "message": {"role": "assistant", "content": "Hello!"},
                "finish_reason": "stop"
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}
        }
        out = oc.from_openai(resp, "test-model")
        self.assertEqual(out["role"], "assistant")
        self.assertEqual(out["content"][0]["text"], "Hello!")
        self.assertEqual(out["stop_reason"], "end_turn")
        self.assertEqual(out["usage"]["input_tokens"], 10)
        self.assertEqual(out["usage"]["output_tokens"], 5)

    def test_from_openai_with_tool_call(self):
        """Test conversion with tool calls."""
        resp = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call_123",
                        "function": {"name": "get_weather", "arguments": '{"city": "NYC"}'}
                    }]
                },
                "finish_reason": "tool_calls"
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}
        }
        out = oc.from_openai(resp, "test-model")
        self.assertEqual(out["stop_reason"], "tool_use")
        self.assertEqual(len(out["content"]), 1)
        self.assertEqual(out["content"][0]["type"], "tool_use")
        self.assertEqual(out["content"][0]["name"], "get_weather")

    def test_stream_translator_basic(self):
        """Test basic streaming."""
        tr = oc.StreamTranslator("test-model")
        chunk = {
            "choices": [{
                "delta": {"content": "Hello"}
            }]
        }
        events = tr.feed(chunk)
        # Should have message_start + content_block_start + content_block_delta
        self.assertGreaterEqual(len(events), 3)

    def test_stream_translator_finish(self):
        """Test streaming finish."""
        tr = oc.StreamTranslator("test-model")
        chunk = {
            "choices": [{
                "delta": {"content": "Hello"},
                "finish_reason": "stop"
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}
        }
        events = tr.feed(chunk)
        events += tr.finish()
        # Should end with message_delta and message_stop
        event_names = [e[0] for e in events]
        self.assertIn("message_delta", event_names)
        self.assertIn("message_stop", event_names)


class TestGemini(unittest.TestCase):
    def test_endpoint(self):
        """Test endpoint construction."""
        url = gemini.endpoint("gemini-1.5-pro", False)
        self.assertIn("v1beta", url)
        self.assertIn("generateContent", url)

    def test_endpoint_stream(self):
        """Test streaming endpoint."""
        url = gemini.endpoint("gemini-1.5-pro", True)
        self.assertIn("v1beta", url)
        self.assertIn("streamGenerateContent", url)
        self.assertIn("alt=sse", url)

    def test_headers(self):
        """Test headers."""
        h = gemini.headers("test-key")
        self.assertEqual(h["x-goog-api-key"], "test-key")
        self.assertEqual(h["content-type"], "application/json")

    def test_new_id_split_id(self):
        """Test tool_use ID with thought signature."""
        tid = gemini._new_id("sig123")
        self.assertTrue(tid.startswith("toolu_"))
        self.assertIn("__thought__", tid)

        base, sig = gemini._split_id(tid)
        self.assertTrue(base.startswith("toolu_"))
        # The signature is base64 encoded, so it may have padding
        self.assertIn("sig123", sig)

    def test_split_id_no_signature(self):
        """Test splitting ID without signature."""
        base, sig = gemini._split_id("toolu_abc123")
        self.assertEqual(base, "toolu_abc123")
        self.assertIsNone(sig)

    def test_sanitize_schema_basic(self):
        """Test schema sanitization."""
        schema = {"type": "object", "properties": {"name": {"type": "string"}}}
        out = gemini.sanitize_schema(schema)
        self.assertEqual(out["type"], "object")
        self.assertIn("properties", out)

    def test_sanitize_schema_enum(self):
        """Test enum sanitization."""
        schema = {"type": "string", "enum": ["a", "b", ""]}
        out = gemini.sanitize_schema(schema)
        self.assertIn("enum", out)
        self.assertIn(None, out["enum"])  # empty string -> None

    def test_sanitize_schema_refs(self):
        """Test resolving refs."""
        schema = {
            "type": "object",
            "properties": {
                "user": {"$ref": "#/definitions/User"}
            },
            "definitions": {
                "User": {"type": "object", "properties": {"name": {"type": "string"}}}
            }
        }
        out = gemini.sanitize_schema(schema)
        self.assertEqual(out["type"], "object")
        self.assertIn("properties", out)
        self.assertIn("user", out["properties"])

    def test_to_gemini_basic(self):
        """Test basic Anthropic -> Gemini conversion."""
        body = {
            "model": "gemini-1.5-pro",
            "messages": [{"role": "user", "content": "Hello"}],
        }
        out = gemini.to_gemini(body)
        self.assertIn("contents", out)
        self.assertEqual(len(out["contents"]), 1)
        self.assertEqual(out["contents"][0]["role"], "user")
        self.assertEqual(out["contents"][0]["parts"][0]["text"], "Hello")

    def test_to_gemini_with_system(self):
        """Test conversion with system."""
        body = {
            "model": "gemini-1.5-pro",
            "system": "You are helpful",
            "messages": [{"role": "user", "content": "Hello"}],
        }
        out = gemini.to_gemini(body)
        self.assertIn("systemInstruction", out)
        self.assertEqual(out["systemInstruction"]["parts"][0]["text"], "You are helpful")

    def test_to_gemini_with_tools(self):
        """Test conversion with tools."""
        body = {
            "model": "gemini-1.5-pro",
            "messages": [{"role": "user", "content": "Hello"}],
            "tools": [{
                "name": "get_weather",
                "description": "Get weather",
                "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}}
            }],
        }
        out = gemini.to_gemini(body)
        self.assertIn("tools", out)
        self.assertEqual(len(out["tools"][0]["functionDeclarations"]), 1)

    def test_to_gemini_with_thinking(self):
        """Test conversion with thinking config."""
        body = {
            "model": "gemini-1.5-pro",
            "messages": [{"role": "user", "content": "Hello"}],
            "thinking": {"type": "enabled", "budget_tokens": 1000},
        }
        out = gemini.to_gemini(body)
        self.assertIn("generationConfig", out)
        self.assertIn("thinkingConfig", out["generationConfig"])
        self.assertTrue(out["generationConfig"]["thinkingConfig"]["includeThoughts"])
        self.assertEqual(out["generationConfig"]["thinkingConfig"]["thinkingBudget"], 1000)

    def test_from_gemini_basic(self):
        """Test Gemini -> Anthropic conversion."""
        resp = {
            "candidates": [{
                "content": {"parts": [{"text": "Hello!"}]},
                "finishReason": "STOP"
            }],
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5}
        }
        out = gemini.from_gemini(resp, "test-model")
        self.assertEqual(out["role"], "assistant")
        self.assertEqual(out["content"][0]["text"], "Hello!")
        self.assertEqual(out["stop_reason"], "end_turn")

    def test_from_gemini_with_tool(self):
        """Test conversion with function call."""
        resp = {
            "candidates": [{
                "content": {"parts": [{
                    "functionCall": {"name": "get_weather", "args": {"city": "NYC"}},
                    "thoughtSignature": "sig123"
                }]},
                "finishReason": "STOP"
            }],
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5}
        }
        out = gemini.from_gemini(resp, "test-model")
        self.assertEqual(out["stop_reason"], "tool_use")
        self.assertEqual(out["content"][0]["type"], "tool_use")
        self.assertEqual(out["content"][0]["name"], "get_weather")
        self.assertIn("__thought__", out["content"][0]["id"])

    def test_stream_translator_basic(self):
        """Test basic streaming."""
        tr = gemini.StreamTranslator("test-model")
        chunk = {
            "candidates": [{
                "content": {"parts": [{"text": "Hello"}]}
            }]
        }
        events = tr.feed(chunk)
        self.assertGreaterEqual(len(events), 3)

    def test_stream_translator_tool(self):
        """Test streaming with tool call."""
        tr = gemini.StreamTranslator("test-model")
        chunk = {
            "candidates": [{
                "content": {"parts": [{
                    "functionCall": {"name": "get_weather", "args": {"city": "NYC"}},
                    "thoughtSignature": "sig123"
                }]}
            }]
        }
        events = tr.feed(chunk)
        event_names = [e[0] for e in events]
        self.assertIn("content_block_start", event_names)
        self.assertIn("content_block_delta", event_names)
        self.assertIn("content_block_stop", event_names)

    def test_stream_translator_finish(self):
        """Test streaming finish."""
        tr = gemini.StreamTranslator("test-model")
        chunk = {
            "candidates": [{
                "content": {"parts": [{"text": "Hello"}]},
                "finishReason": "STOP"
            }]
        }
        events = tr.feed(chunk)
        events += tr.finish()
        event_names = [e[0] for e in events]
        self.assertIn("message_delta", event_names)
        self.assertIn("message_stop", event_names)


if __name__ == "__main__":
    unittest.main()