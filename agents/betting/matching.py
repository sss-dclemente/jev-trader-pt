"""Match free text (a headline, a lineup post) to events by team name. Pure functions."""
from __future__ import annotations

import re
import unicodedata

from agents.common.store import Event

_DROP = {"fc", "cf", "afc", "sc", "ac", "as", "ssc", "us", "rc", "cd", "ud", "sd", "club", "de", "the", "united"}
# `united` dropped on purpose: Manchester United vs Newcastle United vs West Ham United all carry it.

ALIASES: dict[str, set[str]] = {
    "manchester united": {"man utd", "man united", "manutd"},
    "manchester city": {"man city", "mcfc"},
    "tottenham hotspur": {"spurs", "tottenham"},
    "wolverhampton wanderers": {"wolves"},
    "brighton and hove albion": {"brighton"},
    "west ham united": {"west ham", "hammers"},
    "newcastle united": {"newcastle", "magpies"},
    "nottingham forest": {"forest"},
    "sporting cp": {"sporting", "sporting lisbon"},
    "sport lisboa e benfica": {"benfica", "slb"},
    "fc porto": {"porto"},
    "sporting braga": {"braga"},
    "internazionale": {"inter", "inter milan"},
    "paris saint germain": {"psg", "paris sg"},
    "bayern munich": {"bayern"},
    "borussia dortmund": {"dortmund", "bvb"},
    "atletico madrid": {"atletico", "atleti"},
}


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9 ]+", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def team_keys(name: str) -> set[str]:
    """Strings whose presence in text means this team is mentioned."""
    n = norm(name)
    keys = {n}
    words = [w for w in n.split() if w not in _DROP]
    if words:
        keys.add(" ".join(words))
        if len(words) == 1 and len(words[0]) >= 4:
            keys.add(words[0])
    for canon, al in ALIASES.items():
        if canon == n or canon in keys or n in al:
            keys |= al
            keys.add(canon)
    return {k for k in keys if len(k) >= 4}


def mentions(text: str, team: str) -> bool:
    t = f" {norm(text)} "
    return any(f" {k} " in t for k in team_keys(team))


def match_events(text: str, events: list[Event], require_both: bool = False) -> list[str]:
    """Event ids whose home or away team is mentioned. `require_both` for match previews;
    single team is right for injury and lineup news."""
    out = []
    for e in events:
        h, a = mentions(text, e.home), mentions(text, e.away)
        if (h and a) if require_both else (h or a):
            out.append(e.id)
    return out
