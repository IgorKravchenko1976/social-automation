"""Geographic location extraction and map link generation."""
from __future__ import annotations

import json
import logging

from content.ai_client import get_client

logger = logging.getLogger(__name__)


async def extract_location_coordinates(topic: str) -> dict | None:
    """Extract the main location from a topic and return its coordinates.

    Returns {"lat": float, "lon": float, "name": str} or None.
    Picks the most specific location (restaurant > city > country).
    """
    client = get_client()
    try:
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Extract the main geographic location from the text and return its coordinates. "
                        "Find the MOST SPECIFIC place mentioned: a specific restaurant/hotel/stadium "
                        "is better than a city, a city is better than a country. "
                        'Return ONLY valid JSON: {"lat": 48.8566, "lon": 2.3522, "name": "Paris, France"}\n'
                        'If no location can be determined, return: {"lat": null, "lon": null, "name": null}\n'
                        "No explanations, no markdown, ONLY the JSON object."
                    ),
                },
                {"role": "user", "content": topic[:500]},
            ],
            max_tokens=80,
            temperature=0.1,
        )
        raw = response.choices[0].message.content.strip()
        raw = raw.strip("`").removeprefix("json").strip()
        data = json.loads(raw)
        if data.get("lat") is not None and data.get("lon") is not None:
            logger.info(
                "Geo for topic: %s → %s (%.4f, %.4f)",
                topic[:60], data["name"], data["lat"], data["lon"],
            )
            return data
    except Exception:
        logger.warning("Failed to extract coordinates for: %s", topic[:80])
    return None


def build_map_link(lat: float, lon: float, name: str = "") -> str:
    """Build a Google Maps link for given coordinates."""
    label = name.replace(" ", "+") if name else ""
    if label:
        return f"https://maps.google.com/?q={lat},{lon}&label={label}"
    return f"https://maps.google.com/?q={lat},{lon}"
