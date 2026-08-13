import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from app import llm


@pytest.mark.asyncio
async def test_generate_answer_without_config():
    """无配置时应抛出明确错误。"""
    with pytest.raises(RuntimeError, match="未配置 LLM"):
        await llm.generate_answer("什么是 FastAPI?")
