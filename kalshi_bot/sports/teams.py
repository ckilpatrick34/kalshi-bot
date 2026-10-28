"""Match the venues' team names to The Odds API's.

The Odds API uses full names ("Kansas City Chiefs"). Kalshi often shows only the city,
with a letter to split shared cities ("Los Angeles C"), and Polymarket US may show the
nickname alone. A game is only matched when both teams line up one way clearly better
than the other, so an ambiguous pairing is skipped rather than traded.
"""

import re
from difflib import SequenceMatcher

# Nicknames longer than one word, so "Red Sox" isn't reduced to "Sox".
MULTI_WORD_NICKNAMES = (
    "red sox", "white sox", "blue jays", "maple leafs", "golden knights", "trail blazers",
    "red wings", "blue jackets", "golden state",
)

MIN_TEAM_SCORE = 0.6


def normalize(name: str) -> str:
    name = name.lower().replace("&", " and ").replace(".", "")
    name = re.sub(r"[^a-z0-9 ]", " ", name)
    name = re.sub(r"^la ", "los angeles ", name)
    name = re.sub(r"^ny ", "new york ", name)
    return " ".join(name.split())


def nickname(full: str) -> str:
    full = normalize(full)
    for nick in MULTI_WORD_NICKNAMES:
        if full.endswith(nick):
            return nick
    return full.split()[-1] if full else ""


def city(full: str) -> str:
    full = normalize(full)
    nick = nickname(full)
    return full[: -len(nick)].strip() if nick and full != nick else full


def team_score(venue_name: str, odds_name: str) -> float:
    v, o = normalize(venue_name), normalize(odds_name)
    if not v or not o:
        return 0.0
    if v == o:
        return 1.0
    nick, town = nickname(o), city(o)
    if re.search(rf"\b{re.escape(nick)}\b", v):
        return 0.9
    # "Los Angeles C" -> city plus the nickname's first letter
    m = re.fullmatch(r"(.+) ([a-z])", v)
    if m and m.group(1) == town:
        return 0.85 if nick.startswith(m.group(2)) else 0.0
    if v == town:
        return 0.75
    return 0.6 * SequenceMatcher(None, v, o).ratio()


def match_pair(venue_names: tuple[str, str], odds_names: tuple[str, str]) -> dict[str, str] | None:
    """Map each venue team name to an Odds API team name, or None if unclear."""
    a, b = venue_names
    h, w = odds_names
    straight = (team_score(a, h), team_score(b, w))
    crossed = (team_score(a, w), team_score(b, h))
    s_straight, s_crossed = sum(straight), sum(crossed)
    if s_straight > s_crossed and min(straight) >= MIN_TEAM_SCORE:
        return {a: h, b: w}
    if s_crossed > s_straight and min(crossed) >= MIN_TEAM_SCORE:
        return {a: w, b: h}
    return None
