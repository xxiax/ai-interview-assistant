import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from app import asr


def test_missing_api_key():
    """未设置 GROQ_API_KEY 时应抛出明确错误。"""
    os.environ.pop("GROQ_API_KEY", None)
    with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
        asr._get_api_key()
