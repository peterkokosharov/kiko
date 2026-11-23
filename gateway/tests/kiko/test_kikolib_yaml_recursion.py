# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Claude Sonnet 5.5
"""Deeply nested input must raise YamlError, not RecursionError."""
import pytest

from kikolib import yaml_lite


def test_deep_nesting_raises_yaml_error():
    with pytest.raises(yaml_lite.YamlError):
        yaml_lite.load("[" * 100000)
