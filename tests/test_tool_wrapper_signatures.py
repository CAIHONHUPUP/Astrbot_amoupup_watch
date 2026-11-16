# -*- coding: utf-8 -*-
"""LLM 工具签名守卫：AstrBot 某些调用路径不会传 event，event 必须可缺省。"""
import inspect
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.tool_wrappers import ToolWrappersMixin  # noqa: E402


class ToolWrapperSignatureTests(unittest.TestCase):
    def test_event_parameter_is_optional_everywhere(self):
        bad = []
        for name, func in vars(ToolWrappersMixin).items():
            if not inspect.isfunction(func):
                continue
            params = inspect.signature(func).parameters
            if "event" in params and params["event"].default is inspect.Parameter.empty:
                bad.append(name)
        self.assertEqual(bad, [], "这些工具在没传 event 的调用路径会 TypeError: %s" % bad)

    def test_confirm_arrival_callable_without_event(self):
        params = inspect.signature(ToolWrappersMixin.confirm_arrival).parameters
        self.assertIsNot(params["event"].default, inspect.Parameter.empty)


if __name__ == "__main__":
    unittest.main()
