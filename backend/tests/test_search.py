import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from app import search


@pytest.mark.asyncio
async def test_no_search_config():
    """未配置搜索时应返回空结果。"""
    assert await search.search_web("测试") == []
