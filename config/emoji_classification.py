"""Classify Telegram reaction emojis into positive/neutral vs negative."""
from __future__ import annotations

NEGATIVE_EMOJIS: set[str] = {
    "👎",
    "🤬",
    "🤮",
    "💩",
    "💔",
    "😡",
    "🖕",
    "😢",
    "😭",
    "🤡",
}

POSITIVE_EMOJIS: set[str] = {
    "👍", "❤️", "🔥", "🥰", "👏", "😁", "🤔", "🤯", "😱", "🎉",
    "🤩", "🙏", "👌", "🕊", "😍", "🐳", "❤️‍🔥", "🌚", "🌭", "💯",
    "🤣", "⚡", "🍌", "🏆", "🤨", "😐", "🍓", "🍾", "💋", "😈",
    "😴", "🤓", "👻", "👨‍💻", "👀", "🎃", "🙈", "😇", "😨", "🤝",
    "✍️", "🤗", "🫡", "🎅", "🎄", "☃️", "💅", "🤪", "🗿", "🆒",
    "💘", "🙉", "🦄", "😘", "💊", "🙊", "😎", "👾", "🤷‍♂️", "🤷",
    "🤷‍♀️", "🥱", "🥴",
}


def classify_emoji(emoji: str) -> str:
    """Return 'negative' for clearly negative emojis, 'positive' otherwise."""
    if emoji in NEGATIVE_EMOJIS:
        return "negative"
    return "positive"
