#!/usr/bin/env python3
"""Vai buscar os dados ao Sorare e à The Odds API e escreve data/data.json.

Uso:  python scripts/fetch_data.py [--odds]
Variáveis de ambiente: SORARE_API_KEY, ODDS_API_KEY (só com --odds).
Sem --odds, as odds do data.json anterior são reaproveitadas (poupa créditos).
"""
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
OUT = os.path.join(ROOT, "data", "data.json")
SORARE = "https://api.sorare.com/graphql"
SKEY = os.environ.get("SORARE_API_KEY", "")
OKEY = os.environ.get("ODDS_API_KEY", "")
ASSETS = "https://assets.sorare.com/"
POS = {"Goalkeeper": "GK", "Defender": "DF", "Midfielder": "MD", "Forward": "FW"}
ODDS_SPORTS = {
    "premier-league-gb-eng": "soccer_epl",
    "laliga-es": "soccer_spain_la_liga",
    "ligue-1-fr": "soccer_france_ligue_one",
    "bundesliga-de": "soccer_germany_bundesliga",
}
LEAGUE_NAME = {
    "premier-league-gb-eng": "Premier League",
    "laliga-es": "LaLiga",
    "ligue-1-fr": "Ligue 1",
    "bundesliga-de": "Bundesliga",
}
# fatia de golos/assistências "a priori" por posição (regularização para quem tem poucos jogos)
PRIOR_G = {"GK": 0.0, "DF": 0.04, "MD": 0.08, "FW": 0.20}
PRIOR_A = {"GK": 0.0, "DF": 0.06, "MD": 0.12, "FW": 0.10}
PRIOR_K = 4.0

# Grupos de ações All-Around. Cada grupo é ajustado pelo que o adversário permite:
#   pass  passes (certos, falhados, longos)                      -> passes que o adversário permite
#   ftp   último terço (passes no último terço, passes para remate, grandes ocasiões, entradas na área)
#                                                                -> passes no último terço que o adversário permite
#   att   remates à baliza, dribles, grandes ocasiões falhadas   -> remates que o adversário permite
#   def   desarmes, interceções, bloqueios, double-double        -> volume de passe do adversário
#   duel  duelos e bolas ganhas/perdidas                         -> neutro
#   gk    defesas, saídas, etc.                                  -> remates à baliza do adversário
#   gen   faltas, cartões, erros                                 -> neutro
GROUPS = ["pass", "ftp", "att", "def", "duel", "gk", "gen"]
CAT_GROUP = {"PASSING": "pass", "ATTACKING": "att", "DEFENDING": "def", "POSSESSION": "duel",
             "GOALKEEPING": "gk", "GENERAL": "gen"}
STAT_GROUP = {"successful_final_third_passes": "ftp", "adjusted_total_att_assist": "ftp",
              "big_chance_created": "ftp", "pen_area_entries": "ftp", "interception_won": "def",
              "won_contest": "att"}
# ações que dependem do resultado do jogo: o simulador trata delas (golos sofridos, baliza a zeros)
EXCLUDE = {"goals_conceded", "mins_played", "penalty_won"}
PRIOR_MIN = 180  # minutos "a priori" que puxam a taxa de cada grupo para a média da posição
TEAM_STATS = ["accuratePass", "totalPass", "totalScoringAtt", "ontargetScoringAtt",
              "possessionPercentageBasisPoints", "wonCorners", "wonTackle", "totalClearance"]


def log(*a):
    print(*a, flush=True)


def chunks(xs, n):
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


def short(url):
    """Guarda só o caminho das imagens do Sorare (o site junta o prefixo)."""
    return url[len(ASSETS):] if url and url.startswith(ASSETS) else url


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def gql(query, variables=None, tries=5):
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    for i in range(tries):
        req = urllib.request.Request(SORARE, data=body, headers={
            "content-type": "application/json", "APIKEY": SKEY, "user-agent": "sorare-set-builder"})
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                d = json.load(r)
            if d.get("errors"):
                raise RuntimeError(d["errors"][0].get("message"))
            return d["data"]
        except (urllib.error.URLError, RuntimeError, TimeoutError) as e:
            if i == tries - 1:
                raise
            log("  nova tentativa", i + 1, "-", e)
            time.sleep(4 * (i + 1))


# ---------------------------------------------------------------- Sorare: jogos

def fetch_set():
    d = gql('{ cardSet { currentSet(sport:FOOTBALL){ slug name customCardEditionNames } } }')
    return d["cardSet"]["currentSet"]


def fetch_games(set_slug, days):
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    team = "... on Club{ slug name shortName pictureUrl }"
    parts = []
    for i in range(0, days, 2):
        a, b = now + timedelta(days=i), now + timedelta(days=i + 2)
        parts.append(f'w{i}: competitionGames(fromDate:"{iso(a)}", toDate:"{iso(b)}") {{ competition{{ slug }} '
                     f'games {{ ... on Game {{ id date statusTyped homeTeam{{ {team} }} awayTeam{{ {team} }} }} }} }}')
    q = '{ cardSet { cardSet(slug:"%s", sport:FOOTBALL) { %s } } }' % (set_slug, " ".join(parts))
    d = gql(q)["cardSet"]["cardSet"]
    games = {}
    for win in d.values():
        for cg in win:
            lg = cg["competition"]["slug"]
            if lg not in ODDS_SPORTS:
                continue
            for g in cg["games"]:
                if not g or g.get("statusTyped") != "scheduled" or not g.get("homeTeam") or not g.get("awayTeam"):
                    continue
                games[g["id"]] = {
                    "id": g["id"], "t": g["date"], "lg": LEAGUE_NAME[lg], "lgSlug": lg,
                    "home": {"s": g["homeTeam"]["slug"], "n": g["homeTeam"]["name"],
                             "sn": g["homeTeam"].get("shortName"), "pic": short(g["homeTeam"].get("pictureUrl"))},
                    "away": {"s": g["awayTeam"]["slug"], "n": g["awayTeam"]["name"],
                             "sn": g["awayTeam"].get("shortName"), "pic": short(g["awayTeam"].get("pictureUrl"))},
                }
    return sorted(games.values(), key=lambda g: g["t"])


# ---------------------------------------------------------------- Sorare: clubes (plantel + estatísticas recentes)

def fetch_clubs(club_slugs):
    stats = "... on FootballTeamGameStats { " + " ".join(TEAM_STATS) + " }"
    players, games = {}, {}
    for ch in chunks(club_slugs, 5):
        q = "{ football { " + " ".join(
            f'c{i}: club(slug:"{s}") {{ slug activePlayers(first:70){{ nodes{{ slug }} }} '
            f'latestGames(first:10){{ nodes{{ id statusTyped homeTeam{{ slug }} awayTeam{{ slug }} '
            f'homeStats{{ {stats} }} awayStats{{ {stats} }} }} }} }}' for i, s in enumerate(ch)) + " } }"
        for v in gql(q)["football"].values():
            players[v["slug"]] = [n["slug"] for n in v["activePlayers"]["nodes"]]
            games[v["slug"]] = [g for g in v["latestGames"]["nodes"]
                                if g and g.get("statusTyped") == "played" and g.get("homeStats") and g.get("awayStats")]
    return players, games


def team_profiles(club_games, club_league, ftp):
    """Para cada clube: o que faz (for) e o que permite aos adversários (against), em média por jogo,
    em proporção da média da sua liga."""
    raw = {}
    for club, gs in club_games.items():
        f, a, n = defaultdict(float), defaultdict(float), 0
        fa, fa_n, ff, ff_n = 0.0, 0, 0.0, 0
        for g in gs:
            home = g["homeTeam"]["slug"] == club
            mine, opp = (g["homeStats"], g["awayStats"]) if home else (g["awayStats"], g["homeStats"])
            opp_slug = g["awayTeam"]["slug"] if home else g["homeTeam"]["slug"]
            if not mine.get("accuratePass") or not opp.get("accuratePass"):
                continue
            n += 1
            for k in TEAM_STATS:
                f[k] += mine.get(k) or 0
                a[k] += opp.get(k) or 0
            if (g["id"], opp_slug) in ftp:
                fa += ftp[(g["id"], opp_slug)]
                fa_n += 1
            if (g["id"], club) in ftp:
                ff += ftp[(g["id"], club)]
                ff_n += 1
        if n:
            raw[club] = {"n": n, "for": {k: f[k] / n for k in TEAM_STATS}, "against": {k: a[k] / n for k in TEAM_STATS},
                         "ftpA": fa / fa_n if fa_n >= 3 else None, "ftpF": ff / ff_n if ff_n >= 3 else None}
    lg_mean = {}
    for lg in set(club_league.values()):
        cs = [raw[c] for c in raw if club_league.get(c) == lg]
        if not cs:
            continue
        m = {k: sum(c["for"][k] for c in cs) / len(cs) for k in TEAM_STATS}
        fs = [c["ftpF"] for c in cs if c["ftpF"]]
        m["ftp"] = sum(fs) / len(fs) if fs else None
        lg_mean[lg] = m
    out = {}
    for club, r in raw.items():
        m = lg_mean.get(club_league.get(club))
        if not m:
            continue

        def ratio(x, k):
            return round(x / m[k], 3) if m.get(k) else 1.0

        pa = ratio(r["against"]["accuratePass"], "accuratePass")
        out[club] = {
            "n": r["n"],
            "pa": pa,                                                              # passes certos que permite
            "fa": ratio(r["ftpA"], "ftp") if r["ftpA"] and m.get("ftp") else pa,  # passes no último terço que permite
            "sa": ratio(r["against"]["totalScoringAtt"], "totalScoringAtt"),      # remates que permite
            "pv": ratio(r["for"]["accuratePass"], "accuratePass"),                # o seu próprio volume de passe
            "sv": ratio(r["for"]["ontargetScoringAtt"], "ontargetScoringAtt"),    # os seus remates à baliza
            "avg": {"passA": round(r["against"]["accuratePass"]), "shotsA": round(r["against"]["totalScoringAtt"], 1),
                    "ftpA": round(r["ftpA"]) if r["ftpA"] else None,
                    "pos": round(r["for"]["possessionPercentageBasisPoints"] / 100),
                    "passF": round(r["for"]["accuratePass"]), "shotsF": round(r["for"]["totalScoringAtt"], 1)},
        }
    return out


# ---------------------------------------------------------------- Sorare: jogadores

PQ = """query($s:[String!]){ players(slugs:$s){ ... on Player {
  slug displayName position avatarPictureUrl activeClub{ slug } playingStatus
  activeInjuries{ __typename } activeSuspensions{ __typename }
  anyFutureGameStats(first:2){ ... on PlayerGameStats { game{ id } footballPlayingStatusOdds{ starterOddsBasisPoints substituteOddsBasisPoints nonPlayingOddsBasisPoints reliability } } }
  a5: averageScore(type:LAST_FIVE_SO5_AVERAGE_SCORE)
  a15: averageScore(type:LAST_FIFTEEN_SO5_AVERAGE_SCORE)
  so5Scores(last:15){ score
    playerGameStats{ minsPlayed gameStarted goals goalAssist assistPenaltyWon anyTeam{ slug } }
    game{ id homeGoals awayGoals homeTeam{ slug } }
    allAroundStats{ category stat statValue totalScore } }
} } }"""


def fetch_players(slugs):
    res = {}
    for i, ch in enumerate(chunks(slugs, 12)):
        if i % 25 == 0:
            log(f"  jogadores {i * 12}/{len(slugs)}")
        for p in gql(PQ, {"s": ch})["players"]:
            if p and p.get("slug"):
                res[p["slug"]] = p
    return res


def rnd(x, n=1):
    return None if x is None else round(x, n)


def group_of(s):
    return STAT_GROUP.get(s["stat"]) or CAT_GROUP.get(s["category"])


def summarize(p, ftp):
    """Resumo do jogador; acumula também os passes no último terço por (jogo, equipa) em ftp."""
    pos = POS.get(p["position"])
    sc = [x for x in (p.get("so5Scores") or []) if x]
    last5 = [round(x["score"] or 0) for x in sc[:5]]
    st10 = sc[:10]
    starts10 = sum(1 for x in st10 if (x.get("playerGameStats") or {}).get("gameStarted"))
    g = a = expo = 0.0
    win = {"5": [defaultdict(float), 0.0], "15": [defaultdict(float), 0.0]}
    for idx, x in enumerate(sc):
        s = x.get("playerGameStats") or {}
        m = s.get("minsPlayed") or 0
        if not m:
            continue
        team = (s.get("anyTeam") or {}).get("slug")
        gm = x.get("game") or {}
        home = (gm.get("homeTeam") or {}).get("slug") == team
        tg = (gm.get("homeGoals") if home else gm.get("awayGoals")) or 0
        expo += tg * m / 90
        g += s.get("goals") or 0
        a += (s.get("goalAssist") or 0) + (s.get("assistPenaltyWon") or 0)
        pts = defaultdict(float)
        for st in x.get("allAroundStats") or []:
            if st["stat"] in EXCLUDE or st["stat"].startswith("clean_sheet"):
                continue
            grp = group_of(st)
            if grp:
                pts[grp] += st.get("totalScore") or 0
            if st["stat"] == "successful_final_third_passes" and gm.get("id") and team:
                ftp[(gm["id"], team)] += st.get("statValue") or 0
        for w in ("5", "15"):
            if w == "5" and idx >= 5:
                continue
            for k, v in pts.items():
                win[w][0][k] += v
            win[w][1] += m
    gs = (g + PRIOR_K * PRIOR_G[pos]) / (expo + PRIOR_K)
    as_ = (a + PRIOR_K * PRIOR_A[pos]) / (expo + PRIOR_K)
    odds = {}
    for f in p.get("anyFutureGameStats") or []:
        if f and f.get("game") and f.get("footballPlayingStatusOdds"):
            o = f["footballPlayingStatusOdds"]
            odds[f["game"]["id"]] = [o["starterOddsBasisPoints"], o["substituteOddsBasisPoints"], o["reliability"]]
    return {
        "s": p["slug"], "n": p["displayName"], "p": pos, "av": short(p.get("avatarPictureUrl")),
        "c": (p.get("activeClub") or {}).get("slug"),
        "ps": p.get("playingStatus"), "inj": bool(p.get("activeInjuries")), "sus": bool(p.get("activeSuspensions")),
        "l5": last5, "a5": rnd(p.get("a5")), "a15": rnd(p.get("a15")),
        "sr": round(starts10 / len(st10), 2) if st10 else 0, "gp": len(st10), "min15": int(win["15"][1]),
        "gs": round(gs, 3), "as": round(as_, 3), "odds": odds,
        "_win": win,
    }


def finalize_rates(players):
    """Taxas All-Around por 90' em cada grupo, puxadas para a média da posição quando há poucos minutos."""
    prior = {}
    for pos in ("GK", "DF", "MD", "FW"):
        ps = [p for p in players if p["p"] == pos and p["_win"]["15"][1] >= 450]
        mins = sum(p["_win"]["15"][1] for p in ps) or 1
        prior[pos] = {k: sum(p["_win"]["15"][0][k] for p in ps) / mins * 90 for k in GROUPS}
    for p in players:
        for w in ("5", "15"):
            pts, mins = p["_win"][w]
            p["g" + w] = [round((pts[k] + prior[p["p"]][k] * PRIOR_MIN / 90) / ((mins + PRIOR_MIN) / 90), 2)
                          for k in GROUPS]
        p["m5"] = int(p["_win"]["5"][1])
        del p["_win"]
    return {pos: [round(prior[pos][k], 2) for k in GROUPS] for pos in prior}


GQ = """query($u:String!,$e:String!,$after:String){ user(slug:$u){ cards(first:100, after:$after, sport:FOOTBALL, customCardEditionName:$e){
  nodes{ slug pictureUrl anyPlayer{ slug } } pageInfo{ endCursor hasNextPage } } } }"""


def fetch_gallery(user, editions):
    cards = []
    for e in editions:
        after = None
        while True:
            c = gql(GQ, {"u": user, "e": e, "after": after})["user"]["cards"]
            cards += [[n["slug"], n["anyPlayer"]["slug"], e, short(n.get("pictureUrl"))]
                      for n in c["nodes"] if n.get("anyPlayer")]
            if not c["pageInfo"]["hasNextPage"]:
                break
            after = c["pageInfo"]["endCursor"]
    return cards


# ---------------------------------------------------------------- odds

def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    toks = [t for t in s.split() if t not in {"fc", "cf", "afc", "sc", "ac", "club", "de", "the", "1"}]
    return " ".join(toks)


def sim(a, b):
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0
    ta, tb = set(a.split()), set(b.split())
    jac = len(ta & tb) / len(ta | tb)
    return max(SequenceMatcher(None, a, b).ratio(), jac, 0.95 if (a in b or b in a) else 0)


def consensus(ev):
    h, d, a, totals = [], [], [], {}
    hn, an = ev["home_team"], ev["away_team"]
    for b in ev.get("bookmakers", []):
        for m in b.get("markets", []):
            o = {x["name"]: x for x in m["outcomes"]}
            if m["key"] == "h2h" and hn in o and an in o and "Draw" in o:
                inv = [1 / o[hn]["price"], 1 / o["Draw"]["price"], 1 / o[an]["price"]]
                s = sum(inv)
                h.append(inv[0] / s), d.append(inv[1] / s), a.append(inv[2] / s)
            elif m["key"] == "totals" and "Over" in o and "Under" in o:
                line = o["Over"].get("point")
                inv = [1 / o["Over"]["price"], 1 / o["Under"]["price"]]
                totals.setdefault(line, []).append(inv[0] / sum(inv))
    if not h:
        return None
    med = lambda xs: sorted(xs)[len(xs) // 2]
    res = {"pH": round(med(h), 4), "pD": round(med(d), 4), "pA": round(med(a), 4), "n": len(h)}
    if totals:
        line = max(totals, key=lambda k: (len(totals[k]), -abs(k - 2.5)))
        res["line"], res["pOver"] = line, round(med(totals[line]), 4)
    return res


def fetch_odds(games):
    by_lg = {}
    for g in games:
        by_lg.setdefault(g["lgSlug"], []).append(g)
    found = {}
    for lg, gs in by_lg.items():
        url = (f"https://api.the-odds-api.com/v4/sports/{ODDS_SPORTS[lg]}/odds?apiKey={OKEY}"
               "&regions=eu&markets=h2h,totals&oddsFormat=decimal")
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"user-agent": "sorare-set-builder"}),
                                        timeout=60) as r:
                events = json.load(r)
                log(f"  odds {lg}: {len(events)} jogos · créditos restantes {r.headers.get('x-requests-remaining')}")
        except urllib.error.URLError as e:
            log("  odds falharam para", lg, e)
            continue
        pairs = []
        for ev in events:
            t = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
            for g in gs:
                tg = datetime.fromisoformat(g["t"].replace("Z", "+00:00"))
                if abs((t - tg).total_seconds()) > 3 * 3600:
                    continue
                sh = max(sim(ev["home_team"], g["home"]["n"]), sim(ev["home_team"], g["home"].get("sn")))
                sa = max(sim(ev["away_team"], g["away"]["n"]), sim(ev["away_team"], g["away"].get("sn")))
                pairs.append((sh + sa, ev, g))
        used_ev, used_g = set(), set()
        for score, ev, g in sorted(pairs, key=lambda x: -x[0]):
            if score < 0.9 or ev["id"] in used_ev or g["id"] in used_g:
                continue
            c = consensus(ev)
            if c:
                found[g["id"]] = c
                used_ev.add(ev["id"]), used_g.add(g["id"])
        missing = [f'{g["home"]["n"]} - {g["away"]["n"]}' for g in gs if g["id"] not in found]
        if missing:
            log("  sem odds:", "; ".join(missing))
    return found


# ---------------------------------------------------------------- main

def main():
    with_odds = "--odds" in sys.argv
    if not SKEY:
        sys.exit("Falta a variável SORARE_API_KEY")
    prev = {}
    if os.path.exists(OUT):
        try:
            prev = json.load(open(OUT, encoding="utf-8"))
        except ValueError:
            prev = {}

    st = fetch_set()
    log("Set atual:", st["name"])
    games = fetch_games(st["slug"], CFG.get("days", 8))
    log("Jogos:", len(games))
    club_league = {}
    for g in games:
        for k in ("home", "away"):
            club_league[g[k]["s"]] = g["lg"]
    club_players, club_games = fetch_clubs(sorted(club_league))
    slugs = sorted({s for v in club_players.values() for s in v})
    log("Jogadores a analisar:", len(slugs))
    raw = fetch_players(slugs)

    ftp = defaultdict(float)
    players = []
    for s in slugs:
        p = raw.get(s)
        if not p or p.get("position") not in POS or (p.get("activeClub") or {}).get("slug") not in club_players:
            continue
        sm = summarize(p, ftp)
        if not sm["min15"] and not sm["odds"] and sm["ps"] not in ("STARTER", "REGULAR"):
            continue  # nunca joga: fica de fora para o ficheiro não crescer
        players.append(sm)
    priors = finalize_rates(players)
    teams = team_profiles(club_games, club_league, ftp)
    log("Jogadores incluídos:", len(players), "· perfis de equipa:", len(teams))

    galleries = {}
    known = {p["s"] for p in players}
    for u in CFG.get("users", []):
        try:
            cards = fetch_gallery(u, st["customCardEditionNames"])
        except RuntimeError as e:
            log("  galeria falhou para", u, e)
            continue
        galleries[u] = [c for c in cards if c[1] in known]
        log(f"Galeria {u}: {len(cards)} cartas do Set, {len(galleries[u])} com jogo nos próximos dias")

    odds_at = prev.get("oddsAt")
    prev_odds = {g["id"]: g.get("odds") for g in prev.get("games", [])}
    new_odds = {}
    if with_odds and OKEY:
        new_odds = fetch_odds(games)
        odds_at = iso(datetime.now(timezone.utc))
    for g in games:
        g["odds"] = new_odds.get(g["id"]) or prev_odds.get(g["id"])
        g.pop("lgSlug", None)

    data = {
        "generatedAt": iso(datetime.now(timezone.utc)),
        "oddsAt": odds_at,
        "set": {"slug": st["slug"], "name": st["name"]},
        "editions": CFG["editions"],
        "groups": GROUPS,
        "priors": priors,
        "rules": {"gc": {"GK": -5, "DF": -4, "MD": -2, "FW": 0}, "csDF": 10},
        "teams": teams,
        "games": games,
        "players": players,
        "galleries": galleries,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    log("Escrito", OUT, f"{os.path.getsize(OUT) // 1024} KB")


if __name__ == "__main__":
    main()
