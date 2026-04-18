"""Unified web search client — Tavily + Brave Search fallback.

Provides a single interface for searching the web. Used when Perplexity
is unavailable. Results are fed to GPT-4o-mini for summarization.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import httpx

from config.settings import settings

logger = logging.getLogger(__name__)


@dataclass
class SearchResult:
    title: str = ""
    url: str = ""
    content: str = ""
    score: float = 0.0


@dataclass
class SearchResponse:
    results: list[SearchResult] = field(default_factory=list)
    provider: str = ""


async def search(query: str, max_results: int = 5) -> SearchResponse | None:
    """Search the web using Tavily first, then Brave as fallback.

    Returns SearchResponse with results and provider name, or None if all fail.
    """
    if settings.tavily_api_key:
        result = await _tavily_search(query, max_results)
        if result and result.results:
            return result

    if settings.brave_search_api_key:
        result = await _brave_search(query, max_results)
        if result and result.results:
            return result

    return None


async def _tavily_search(query: str, max_results: int) -> SearchResponse | None:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": settings.tavily_api_key,
                    "query": query,
                    "search_depth": "advanced",
                    "include_raw_content": False,
                    "max_results": max_results,
                },
            )
            resp.raise_for_status()
            data = resp.json()

        results = []
        for item in data.get("results", []):
            results.append(SearchResult(
                title=item.get("title", ""),
                url=item.get("url", ""),
                content=item.get("content", ""),
                score=item.get("score", 0.0),
            ))

        return SearchResponse(results=results, provider="tavily")

    except Exception as e:
        logger.warning("[tavily] search failed: %s", e)
        return None


async def _brave_search(query: str, max_results: int) -> SearchResponse | None:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": query, "count": max_results},
                headers={
                    "Accept": "application/json",
                    "Accept-Encoding": "gzip",
                    "X-Subscription-Token": settings.brave_search_api_key,
                },
            )
            resp.raise_for_status()
            data = resp.json()

        results = []
        for item in data.get("web", {}).get("results", []):
            results.append(SearchResult(
                title=item.get("title", ""),
                url=item.get("url", ""),
                content=item.get("description", ""),
                score=0.0,
            ))

        return SearchResponse(results=results, provider="brave")

    except Exception as e:
        logger.warning("[brave] search failed: %s", e)
        return None


def format_search_context(response: SearchResponse) -> str:
    """Format search results into a context string for GPT summarization."""
    parts = []
    for i, r in enumerate(response.results[:5], 1):
        parts.append(f"[{i}] {r.title}\nURL: {r.url}\n{r.content}\n")
    return "\n".join(parts)
