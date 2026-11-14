# -*- coding: utf-8 -*-
"""关系注入：无 TextPart 时也要把【关系】写进 system_prompt。"""
from __future__ import annotations

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services import social_cmd  # noqa: E402


class InjectTests(unittest.TestCase):
    def test_appends_to_system_prompt(self):
        request = SimpleNamespace(system_prompt="base", extra_user_content_parts=[])
        with mock.patch.object(social_cmd, "prompt_line", return_value="【关系】测试关系"):
            social_cmd.inject_request(object(), request)
        self.assertIn("【关系】测试关系", request.system_prompt)

    def test_empty_line_keeps_prompt(self):
        request = SimpleNamespace(system_prompt="base")
        with mock.patch.object(social_cmd, "prompt_line", return_value=""):
            social_cmd.inject_request(object(), request)
        self.assertEqual(request.system_prompt, "base")


if __name__ == "__main__":
    unittest.main()
