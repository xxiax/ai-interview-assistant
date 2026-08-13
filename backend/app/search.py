"""搜索客户端（可选增强）。"""
import httpx

from . import db


async def search_web(query: str) -> list[dict]:
    """搜索网络，返回结果列表。未配置搜索 API 时返回空列表。"""
    conn = db.get_db()
    try:
        config = db.get_active_config(conn, "search")
    finally:
        conn.close()
    if not config:
        return []

    data = config["data"]
    engine = data.get("engine", "")
    api_key = data.get("api_key", "")

    if not engine or not api_key:
        return []

    if engine == "google":
        return await _search_google(query, data)
    elif engine == "bing":
        return await _search_bing(query, data)
    return []


async def _search_google(query: str, data: dict) -> list[dict]:
    """Google Custom Search JSON API。"""
    cx = data.get("cx", "")
    api_key = data.get("api_key", "")
    url = "https://www.googleapis.com/customsearch/v1"
    params = {"key": api_key, "cx": cx, "q": query, "num": 5}
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url, params=params)
        if resp.status_code != 200:
            return []
        items = resp.json().get("items", [])
        return [{"title": i.get("title", ""), "link": i.get("link", ""), "snippet": i.get("snippet", "")} for i in items]


async def _search_bing(query: str, data: dict) -> list[dict]:
    """Bing Web Search API。"""
    api_key = data.get("api_key", "")
    url = "https://api.bing.microsoft.com/v7.0/search"
    params = {"q": query, "count": 5}
    headers = {"Ocp-Apim-Subscription-Key": api_key}
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url, params=params, headers=headers)
        if resp.status_code != 200:
            return []
        items = resp.json().get("webPages", {}).get("value", [])
        return [{"title": i.get("name", ""), "link": i.get("url", ""), "snippet": i.get("snippet", "")} for i in items]
