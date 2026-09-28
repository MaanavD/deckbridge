#!/usr/bin/env python3
"""Model id -> provider mark and key-sized name."""
import unittest

import logos
import models


class ModelNameTests(unittest.TestCase):
    CASES = {
        "claude-opus-5-5": ("anthropic", "Opus 5.5"),
        "claude-opus-5-5[1m]": ("anthropic", "Opus 5.5"),
        "claude-3-5-sonnet-20241022": ("anthropic", "Sonnet 3.5"),
        "anthropic/claude-fable-5": ("anthropic", "Fable 5"),
        "gpt-6-astra-900k": ("openai", "GPT-6 Astra"),
        "openai-codex/gpt-6-astra": ("openai", "GPT-6 Astra"),
        "gpt-5.6-terra": ("openai", "5.6 Terra"),
        "cursor-grok-4.6-high-fast": ("xai", "Grok 4.6"),
        "composer-2.5": ("cursor", "Composer 2.5"),
        "deepseek-v4.1-flash": ("deepseek", "V4.1 Flash"),
        "opencode-go/glm-5.3-flash": ("zai", "5.3 Flash"),
        "moonshotai/kimi-k3": ("moonshot", "Kimi K3"),
        "gemini-3.8-flash": ("google", "3.8 Flash"),
        "qwen3-coder-480b": ("qwen", "Qwen3 Coder"),
        "union-alpha": ("", "Union Alpha"),
        "": ("", ""),
    }

    def test_known_models(self):
        for model, expected in self.CASES.items():
            with self.subTest(model=model):
                self.assertEqual((models.provider(model), models.short_name(model)), expected)

    def test_names_fit_a_key(self):
        for model in self.CASES:
            self.assertLessEqual(len(models.short_name(model)), models.MAX_CHARS)

    def test_every_provider_has_a_white_mark(self):
        for provider in set(models.FAMILY_PROVIDER.values()) | set(models.VENDOR_PROVIDER.values()):
            with self.subTest(provider=provider):
                path = logos.provider_path(provider)
                self.assertTrue(path, f"no logos/providers/{provider}.svg")
                with open(path) as handle:
                    svg = handle.read()
                self.assertIn('fill="#ffffff"', svg.split(">")[0])
                self.assertNotIn("currentColor", svg)


if __name__ == "__main__":
    unittest.main()
