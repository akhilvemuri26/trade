"""Match Kalshi market team labels to ESPN score-feed team names."""

from __future__ import annotations

from rapidfuzz import process as fuzz_process

FUZZY_THRESHOLD = 70
FUZZY_FALLBACK = 55

# sport -> ticker suffix -> name fragments (lowercase)
TICKER_FRAGMENTS: dict[str, dict[str, tuple[str, ...]]] = {
    "mlb": {
        "ARI": ("arizona", "diamondbacks"), "ATL": ("atlanta", "braves"),
        "BAL": ("baltimore", "orioles"), "BOS": ("boston", "red sox"),
        "CHC": ("chicago cubs", "cubs"), "CWS": ("chicago white sox", "white sox"),
        "CIN": ("cincinnati", "reds"), "CLE": ("cleveland", "guardians"),
        "COL": ("colorado", "rockies"), "DET": ("detroit", "tigers"),
        "HOU": ("houston", "astros"), "KC": ("kansas city", "royals"),
        "LAA": ("los angeles angels", "angels"), "LAD": ("los angeles dodgers", "dodgers"),
        "MIA": ("miami", "marlins"), "MIL": ("milwaukee", "brewers"),
        "MIN": ("minnesota", "twins"), "NYM": ("new york mets", "mets"),
        "NYY": ("new york yankees", "yankees"), "OAK": ("oakland", "athletics"),
        "PHI": ("philadelphia", "phillies"), "PIT": ("pittsburgh", "pirates"),
        "SD": ("san diego", "padres"), "SEA": ("seattle", "mariners"),
        "SF": ("san francisco", "giants"), "STL": ("st. louis", "cardinals"),
        "TB": ("tampa bay", "rays"), "TEX": ("texas", "rangers"),
        "TOR": ("toronto", "blue jays"), "WSH": ("washington", "nationals"),
    },
    "nba": {
        "ATL": ("atlanta", "hawks"), "BKN": ("brooklyn", "nets"),
        "BOS": ("boston", "celtics"), "CHA": ("charlotte", "hornets"),
        "CHI": ("chicago", "bulls"), "CLE": ("cleveland", "cavaliers"),
        "DAL": ("dallas", "mavericks"), "DEN": ("denver", "nuggets"),
        "DET": ("detroit", "pistons"), "GSW": ("golden state", "warriors"),
        "HOU": ("houston", "rockets"), "IND": ("indiana", "pacers"),
        "LAC": ("la clippers", "clippers"), "LAL": ("los angeles lakers", "lakers"),
        "MEM": ("memphis", "grizzlies"), "MIA": ("miami", "heat"),
        "MIL": ("milwaukee", "bucks"), "MIN": ("minnesota", "timberwolves"),
        "NOP": ("new orleans", "pelicans"), "NYK": ("new york", "knicks"),
        "OKC": ("oklahoma city", "thunder"), "ORL": ("orlando", "magic"),
        "PHI": ("philadelphia", "76ers", "sixers"), "PHX": ("phoenix", "suns"),
        "POR": ("portland", "trail blazers"), "SAC": ("sacramento", "kings"),
        "SAS": ("san antonio", "spurs"), "TOR": ("toronto", "raptors"),
        "UTA": ("utah", "jazz"), "WAS": ("washington", "wizards"),
    },
    "nhl": {
        "ANA": ("anaheim", "ducks"), "BOS": ("boston", "bruins"),
        "BUF": ("buffalo", "sabres"), "CAR": ("carolina", "hurricanes"),
        "CBJ": ("columbus", "blue jackets"), "CGY": ("calgary", "flames"),
        "CHI": ("chicago", "blackhawks"), "COL": ("colorado", "avalanche"),
        "DAL": ("dallas", "stars"), "DET": ("detroit", "red wings"),
        "EDM": ("edmonton", "oilers"), "FLA": ("florida", "panthers"),
        "LAK": ("los angeles", "kings"), "MIN": ("minnesota", "wild"),
        "MTL": ("montreal", "canadiens"), "NSH": ("nashville", "predators"),
        "NYI": ("new york islanders", "islanders"), "NYR": ("new york rangers", "rangers"),
        "OTT": ("ottawa", "senators"), "PHI": ("philadelphia", "flyers"),
        "PIT": ("pittsburgh", "penguins"), "SEA": ("seattle", "kraken"),
        "SJS": ("san jose", "sharks"), "STL": ("st. louis", "blues"),
        "TBL": ("tampa bay", "lightning"), "TOR": ("toronto", "maple leafs"),
        "VAN": ("vancouver", "canucks"), "VGK": ("vegas", "golden knights"),
        "WPG": ("winnipeg", "jets"), "WSH": ("washington", "capitals"),
    },
}


def _abbrev(display_name: str) -> str:
    parts = display_name.upper().replace(".", "").split()
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0][:3]
    return "".join(p[0] for p in parts if p)


def _by_ticker_code(code: str, feed_names: list[str], sport: str) -> str | None:
    if not code:
        return None
    code = code.upper()
    frags = TICKER_FRAGMENTS.get(sport, {}).get(code, ())
    for name in feed_names:
        low = name.lower()
        if frags and any(f in low for f in frags):
            return name
        if _abbrev(name) == code:
            return name
    return None


def match_feed_team(
    label: str,
    ticker_code: str,
    feed_names: list[str],
    *,
    sport: str = "",
) -> str | None:
    """Return the ESPN feed team name that best matches a Kalshi side label."""
    if not feed_names:
        return None

    queries: list[str] = []
    for q in (label, ticker_code):
        if q and q not in queries:
            queries.append(q)

    for threshold in (FUZZY_THRESHOLD, FUZZY_FALLBACK):
        for q in queries:
            hit = fuzz_process.extractOne(q, feed_names, score_cutoff=threshold)
            if hit:
                return hit[0]

    hit = _by_ticker_code(ticker_code, feed_names, sport)
    if hit:
        return hit

    if label:
        first = label.split()[0]
        for name in feed_names:
            if name.lower().startswith(first.lower()):
                return name

    return None
