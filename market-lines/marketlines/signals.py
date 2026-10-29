"""Line-movement signals, evaluated at each poll for one event and market.

* steam: several books move the same way between two close polls, none the other way.
* key: the consensus spread lands on or crosses a key number (college football).
* drift: the consensus has moved a set amount since the market opened.
* rlm: the line moves against the side most tickets are on (needs betting splits).
* rlm_proxy: with no splits, a move toward the underdog or the under. Public money
  leans to favourites and overs, so this stands in for RLM. It is logged as its own
  type so the CLV ledger can judge it separately.
* divergence: one book's number is off the sharp consensus by enough to be +EV.
* model: the ratings model (model.py) prices a book's number as +EV. Logged in the
  same ledger, so the model is judged on CLV like every other signal.

Each signal names the book and number a user could bet at signal time, and the
fair probability the consensus gave that number. CLV is computed later against
the close (see clv.py).
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .model import cover_prob, over_prob, win_prob
from .lines import SIDES, Line, Timeline, best_line, consensus, equivalent, ref_point, shift_prob, side_fair
from .sports import Sport


@dataclass
class Signal:
    event_id: str
    sport: str
    market: str
    kind: str
    side: str          # "a" (home / over) or "b" (away / under)
    book: str
    line: float | None  # the book's home spread or total, as quoted
    price: float        # decimal price on the side
    fair: float         # consensus fair probability of the side at that line
    cons_point: float | None
    cons_fair_a: float
    score: float
    fired_at: datetime
    detail: dict = field(default_factory=dict)
    key: str = ""

    @property
    def side_name(self) -> str:
        return SIDES[self.market][0 if self.side == "a" else 1]

    @property
    def ev(self) -> float:
        return self.fair * self.price - 1


def _clip(score: float) -> float:
    return round(min(max(score, 0.0), 100.0), 1)


def _signal(sport, market, event_id, kind, side, lines, cons, when, score, key_suffix, detail) -> Signal:
    line, _ev = best_line(market, lines, side, sport, cons)
    return Signal(
        event_id=event_id,
        sport=sport.name,
        market=market,
        kind=kind,
        side=side,
        book=line.book,
        line=line.point,
        price=line.price(side),
        fair=side_fair(sport, market, cons, side, line.point),
        cons_point=cons.point,
        cons_fair_a=cons.fair_a,
        score=_clip(score),
        fired_at=when,
        detail=detail,
        key=f"{event_id}:{market}:{kind}:{side}:{key_suffix}",
    )


def steam(sport: Sport, market: str, event_id: str, tl: Timeline, t0: datetime | None, t1: datetime) -> list[Signal]:
    if t0 is None or t1 - t0 > timedelta(minutes=sport.steam_window_min):
        return []
    now = tl.at(t1)
    before = {l.book: l for l in tl.at(t0)}
    cons = consensus(sport, market, now)
    if cons is None:
        return []
    moves = {
        l.book: equivalent(sport, market, l, cons.point) - equivalent(sport, market, before[l.book], cons.point)
        for l in now if l.book in before
    }
    up = [b for b, d in moves.items() if d >= sport.steam_move]
    down = [b for b, d in moves.items() if d <= -sport.steam_move]
    out = []
    for side, movers, against in (("a", up, down), ("b", down, up)):
        if len(movers) < sport.steam_books or against:
            continue
        size = sum(abs(moves[b]) for b in movers) / len(movers)
        sharp_led = any(b in sport.sharp_books for b in movers)
        laggers = [l for l in now if l.book not in movers] or now
        score = 40 + 8 * (len(movers) - sport.steam_books) + 500 * (size - sport.steam_move) + (15 if sharp_led else 0)
        detail = {"movers": sorted(movers), "avg_move": round(size, 4), "share": round(len(movers) / len(now), 2),
                  "sharp_led": sharp_led, "minutes": round((t1 - t0).total_seconds() / 60, 1)}
        out.append(_signal(sport, market, event_id, "steam", side, laggers, cons, t1, score,
                           t1.strftime("%Y%m%dT%H%M"), detail))
    return out


def key_numbers(sport: Sport, market: str, event_id: str, tl: Timeline, t0: datetime | None, t1: datetime) -> list[Signal]:
    if market != "spreads" or not sport.key_numbers or t0 is None:
        return []
    before, now = tl.at(t0), tl.at(t1)
    p0, p1 = ref_point(before), ref_point(now)
    cons = consensus(sport, market, now)
    if p0 is None or p1 is None or p0 == p1 or cons is None:
        return []
    side = "a" if p1 < p0 else "b"  # home spread falling means money on home
    lo, hi = min(p0, p1), max(p0, p1)
    out = []
    for k in sport.key_numbers:
        for key in (k, -k):
            if key == p0 or not lo <= key <= hi:
                continue
            through = key != p1
            score = 30 + 50 * sport.margin_mass(key) / sport.margin_mass(sport.key_numbers[0]) + (15 if through else 0)
            detail = {"key": key, "from": p0, "to": p1, "through": through}
            out.append(_signal(sport, market, event_id, "key", side, now, cons, t1, score, str(key), detail))
    return out


def _open(sport: Sport, market: str, tl: Timeline, t1: datetime):
    opened = tl.first_time(sport.min_books)
    if opened is None or opened >= t1:
        return None
    return consensus(sport, market, tl.at(opened))


def drift(sport: Sport, market: str, event_id: str, tl: Timeline, t1: datetime) -> list[Signal]:
    now = tl.at(t1)
    cons, opening = consensus(sport, market, now), _open(sport, market, tl, t1)
    if cons is None or opening is None:
        return []
    move = cons.fair_a - shift_prob(sport, market, opening.fair_a, opening.point, cons.point)
    level = int(abs(move) / sport.drift_step)
    if level == 0:
        return []
    side = "a" if move > 0 else "b"
    detail = {"open_point": opening.point, "open_fair_a": round(opening.fair_a, 4), "move": round(move, 4), "level": level}
    return [_signal(sport, market, event_id, "drift", side, now, cons, t1, 30 + 15 * level, str(level), detail)]


def rlm(sport: Sport, market: str, event_id: str, tl: Timeline, t1: datetime, ticket_share_a: float | None) -> list[Signal]:
    now = tl.at(t1)
    cons, opening = consensus(sport, market, now), _open(sport, market, tl, t1)
    if cons is None or opening is None:
        return []
    move = cons.fair_a - shift_prob(sport, market, opening.fair_a, opening.point, cons.point)
    if abs(move) < sport.rlm_move:
        return []
    side = "a" if move > 0 else "b"
    detail = {"open_point": opening.point, "move": round(move, 4)}
    if ticket_share_a is not None:
        public_on_other = ticket_share_a if side == "b" else 1 - ticket_share_a
        if public_on_other < sport.rlm_public_share:
            return []
        detail["public_share_against"] = round(public_on_other, 3)
        score = 50 + 100 * (public_on_other - sport.rlm_public_share) + 300 * (abs(move) - sport.rlm_move)
        return [_signal(sport, market, event_id, "rlm", side, now, cons, t1, score, "1", detail)]
    if market == "totals":
        dog = "b"
    elif market == "spreads":
        if not opening.point:
            return []
        dog = "a" if opening.point > 0 else "b"
    else:
        dog = "a" if opening.fair_a < 0.5 else "b"
    if side != dog:
        return []
    score = 30 + 300 * (abs(move) - sport.rlm_move)
    return [_signal(sport, market, event_id, "rlm_proxy", side, now, cons, t1, score, "1", detail)]


def divergence(sport: Sport, market: str, event_id: str, tl: Timeline, t1: datetime) -> list[Signal]:
    now = tl.at(t1)
    out = []
    for line in now:
        cons = consensus(sport, market, now, exclude=line.book)
        if cons is None or cons.books < sport.min_books - 1:
            continue
        for side in ("a", "b"):
            fair = side_fair(sport, market, cons, side, line.point)
            ev = fair * line.price(side) - 1
            if ev < sport.divergence_ev:
                continue
            detail = {"ref": cons.source, "ref_point": cons.point, "ev": round(ev, 4)}
            out.append(Signal(
                event_id=event_id, sport=sport.name, market=market, kind="divergence", side=side,
                book=line.book, line=line.point, price=line.price(side), fair=fair,
                cons_point=cons.point, cons_fair_a=cons.fair_a, score=_clip(30 + 1000 * ev),
                fired_at=t1, detail=detail,
                key=f"{event_id}:{market}:divergence:{side}:{line.book}:{line.side_point(market, side)}",
            ))
    return out


def model_prob_a(market: str, point: float | None, projection: tuple) -> float:
    """The model's probability for side A at a book's number."""
    margin, total, sd_margin, sd_total = projection
    if market == "h2h":
        return win_prob(margin, sd_margin)
    if market == "spreads":
        return cover_prob(margin, point, sd_margin)
    return over_prob(total, point, sd_total)


def model_edge(sport: Sport, market: str, event_id: str, tl: Timeline, t1: datetime,
               projection: tuple | None) -> list[Signal]:
    """The best-priced book per side where the model sees enough edge. Fires once per side."""
    if projection is None:
        return []
    now = tl.at(t1)
    cons = consensus(sport, market, now)
    if cons is None:
        return []
    out = []
    for side in ("a", "b"):
        best = None
        for line in now:
            if market != "h2h" and line.point is None:
                continue
            p_a = model_prob_a(market, line.point, projection)
            fair = p_a if side == "a" else 1 - p_a
            ev = fair * line.price(side) - 1
            if ev >= sport.model_ev and (best is None or ev > best[2]):
                best = (line, fair, ev)
        if best is None:
            continue
        line, fair, ev = best
        margin, total = projection[0], projection[1]
        detail = {"model_margin": round(margin, 2), "model_total": round(total, 2), "ev": round(ev, 4),
                  "market_fair": round(side_fair(sport, market, cons, side, line.point), 4)}
        out.append(Signal(
            event_id=event_id, sport=sport.name, market=market, kind="model", side=side,
            book=line.book, line=line.point, price=line.price(side), fair=fair,
            cons_point=cons.point, cons_fair_a=cons.fair_a, score=_clip(30 + 800 * ev),
            fired_at=t1, detail=detail, key=f"{event_id}:{market}:model:{side}",
        ))
    return out


def detect(sport: Sport, market: str, event_id: str, tl: Timeline, t0: datetime | None, t1: datetime,
           ticket_share_a: float | None = None, projection: tuple | None = None) -> list[Signal]:
    """All signals for one event and market at poll time t1 (t0 is the previous poll)."""
    if len(tl.at(t1)) < sport.min_books:
        return []
    return [
        *steam(sport, market, event_id, tl, t0, t1),
        *key_numbers(sport, market, event_id, tl, t0, t1),
        *drift(sport, market, event_id, tl, t1),
        *rlm(sport, market, event_id, tl, t1, ticket_share_a),
        *divergence(sport, market, event_id, tl, t1),
        *model_edge(sport, market, event_id, tl, t1, projection),
    ]
