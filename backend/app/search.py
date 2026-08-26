"""搜索客户端（可选增强）。"""

import asyncio
import logging

import httpx2 as httpx

from . import cost_control, db
from .asr import http_client_kwargs


def _http_kwargs() -> dict:
    return http_client_kwargs(httpx.Timeout(15.0))


logger = logging.getLogger(__name__)


def _get_active_search_config() -> dict | None:
    conn = db.get_db()
    try:
        return db.get_active_config(conn, "search")
    finally:
        conn.close()


def _clean_results(
    items: list[dict], title_key: str, link_key: str, snippet_key: str
) -> list[dict]:
    """限制外部搜索内容的形状和长度，避免把任意对象直接交给 LLM。"""
    results = []
    for item in items[:5]:
        if not isinstance(item, dict):
            continue
        title = str(item.get(title_key, "")).strip()[:300]
        link = str(item.get(link_key, "")).strip()[:2000]
        snippet = str(item.get(snippet_key, "")).strip()[:1000]
        if title or snippet:
            results.append({"title": title, "link": link, "snippet": snippet})
    return results


async def search_web(query: str) -> list[dict]:
    """搜索网络，返回结果列表。未配置搜索 API 时返回空列表。

    Bing Web Search API 已于 2025-08 退役：新配置在保存时被拒绝
    （models.SearchConfigData），历史遗留的 bing 配置在此按不可用降级，
    避免对已下线端点白白消耗一次搜索预算。
    """
    try:
        config = await asyncio.to_thread(_get_active_search_config)
    except (RuntimeError, ValueError):
        logger.exception("搜索配置不可用，降级为纯 LLM")
        return []
    if not config:
        return []

    data = config["data"]
    engine = data.get("engine", "")
    api_key = data.get("api_key", "")

    if engine != "google" or not api_key:
        return []

    async with cost_control.paid_call_slot("search"):
        await cost_control.reserve_search_request(api_key)
        try:
            return await _search_google(query, data)
        except (httpx.HTTPError, ValueError, TypeError):
            # 搜索是可选增强；失败时必须安全降级为纯 LLM。
            return []
    return []


async def _search_google(query: str, data: dict) -> list[dict]:
    """Google Custom Search JSON API。"""
    cx = data.get("cx", "")
    api_key = data.get("api_key", "")
    url = "https://www.googleapis.com/customsearch/v1"
    params = {"key": api_key, "cx": cx, "q": query, "num": 5}
    async with httpx.AsyncClient(
        **_http_kwargs()
    ) as client:
        resp = await client.get(url, params=params)
        if resp.status_code != 200:
            return []
        payload = resp.json()
        if not isinstance(payload, dict):
            return []
        items = payload.get("items", [])
        return _clean_results(
            items if isinstance(items, list) else [], "title", "link", "snippet"
        )
