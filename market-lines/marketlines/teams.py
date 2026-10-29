"""Match team names between the odds feed and the score source.

Both usually say "School Mascot", but spellings drift ("UConn" / "Connecticut",
accents, "St." / "State"). Names are normalised, then known aliases applied, then
a close fuzzy match is accepted. Anything left over is reported, not guessed.
"""

import difflib
import re
import unicodedata

ALIASES = {
    "uconn huskies": "connecticut huskies",
    "app state mountaineers": "appalachian state mountaineers",
    "ul monroe warhawks": "louisiana monroe warhawks",
    "ulm warhawks": "louisiana monroe warhawks",
    "umass minutemen": "massachusetts minutemen",
    "utsa roadrunners": "ut san antonio roadrunners",
    "southern miss golden eagles": "southern mississippi golden eagles",
    "miami hurricanes": "miami fl hurricanes",
    "miami oh redhawks": "miami ohio redhawks",
    "sam houston bearkats": "sam houston state bearkats",
    "ole miss rebels": "mississippi rebels",
    "pitt panthers": "pittsburgh panthers",
    "nc state wolfpack": "north carolina state wolfpack",
    "fiu panthers": "florida international panthers",
    "fau owls": "florida atlantic owls",
    "byu cougars": "brigham young cougars",
    "smu mustangs": "southern methodist mustangs",
    "tcu horned frogs": "texas christian horned frogs",
    "ucf knights": "central florida knights",
    "lsu tigers": "louisiana state tigers",
    "usc trojans": "southern california trojans",
    "unlv rebels": "nevada las vegas rebels",
    "utep miners": "texas el paso miners",
}


def normalise(name: str) -> str:
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    text = text.replace("&", " and ").replace("'", "")
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    text = re.sub(r"\bst\b", "state", text)
    text = re.sub(r"\s+", " ", text).strip()
    return ALIASES.get(text, text)


class TeamMatcher:
    def __init__(self, known: list[str], cutoff: float = 0.88):
        self.by_norm = {normalise(t): t for t in known}
        self.cutoff = cutoff
        self.unmatched: set[str] = set()

    def match(self, name: str) -> str | None:
        key = normalise(name)
        if key in self.by_norm:
            return self.by_norm[key]
        close = difflib.get_close_matches(key, list(self.by_norm), n=1, cutoff=self.cutoff)
        if close:
            return self.by_norm[close[0]]
        self.unmatched.add(name)
        return None
