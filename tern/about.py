"""What each setting is, in words, with examples worked from the values on screen: the right side of the settings box and
of a variant's editor. Lines are (style, text): "text", "head", "note" (grey) or "fact" (a worked example)."""

from __future__ import annotations

from .book import taker_fee
from .config import coin

cents = lambda p: f"{p * 100:.0f}¢"


def usd(v: float) -> str:
    return f"${v:,.2f}".removesuffix(".00")


def few(n, one: str, more: str | None = None) -> str:
    return f"{n:,g} {one if n == 1 else more or one + 's'}"


def _clock(m) -> str:
    return f":{60 - m:02g}" if 0 < m < 60 else f"{m:g} min before"


def rule(key: str, r: dict, c: dict) -> list[tuple[str, str]]:
    """A variant's setting. r: the draft's values. c: coins' volatility now (vol), and whether the variant the draft starts
    from keeps to its limits on paper (limits) and its name (name)."""
    T, F, N, H = "text", "fact", "note", "head"
    k, hour, slots, coins = r["contracts"], r["max_hour_cost"], sorted(r["minutes"], reverse=True), r["series"]
    p = min(0.99, max(0.01, (r["band_lo"] + r["band_hi"]) / 2))          # a price in the middle of the band
    buy = k * p
    fee = taker_fee(k, p)
    if key == "band_lo":
        return [(T, "Every strike has a favorite: the side, YES or NO, that costs more than 50¢. Its mid is halfway between "
                    "its bid and its ask. At each slot the rule looks at every favorite, and buys none whose mid is under this."),
                (T, "Lower lets in more strikes. They are cheaper, so they pay more when they win, and they lose more often. "
                    "Higher keeps only the surer, dearer ones."),
                (H, "EXAMPLE"), (F, f"With {cents(r['band_lo'])}: a favorite at {cents(r['band_lo'])} can be bought, one at "
                                    f"{cents(r['band_lo'] - .01)} can't. A contract bought at {cents(r['band_lo'])} makes "
                                    f"{cents(1 - r['band_lo'])} if it wins and loses {cents(r['band_lo'])} if not, before the fee.")]
    if key == "band_hi":
        return [(T, "A favorite at this mid or over is not bought: so near certain that a win pays only a few cents."),
                (T, "Higher lets in surer strikes that pay little. One loss there takes many wins to earn back."),
                (H, "EXAMPLE"), (F, f"At {cents(r['band_hi'] - .01)} a contract makes {cents(1.01 - r['band_hi'])} if it wins and loses "
                                    f"{cents(r['band_hi'] - .01)} if not: one loss undoes {(r['band_hi'] - .01) / max(.01, 1.01 - r['band_hi']):.0f} wins.")]
    if key == "minutes":
        return [(T, "The minutes before the hourly close at which it looks at the market and buys. It does nothing between them. "
                    "At a slot it buys every favorite that fits: in the band, tight enough, with money left to spend."),
                (T, "At an early slot there is more time for the price to move, either way. At a late one the hour is nearly "
                    "decided, and favorites cost more."),
                (H, "NOW"), (F, f"{few(len(slots), 'slot')} an hour, at " + " ".join(_clock(m) for m in slots) +
                                f". A strike in the band all that time can be bought {few(len(slots) if not r['cap'] else min(len(slots), r['cap']), 'time')}.")]
    if key == "max_spread":
        return [(T, "The spread is the gap between a strike's bid and ask. It buys at the ask, so it pays about half the spread "
                    "over the mid. Strikes with a wider gap than this are passed over."),
                (T, "Tight spreads are on the busy strikes near the coin's price. A wider limit lets in thinner ones, where each "
                    "buy costs more than the price on the chart."),
                (H, "EXAMPLE"), (F, f"At a {cents(r['max_spread'])} spread it pays about {r['max_spread'] * 50:.1f}¢ over the mid: "
                                    f"{usd(k * r['max_spread'] / 2)} on {few(k, 'contract')}.")]
    if key == "series":
        return [(T, "The coins it trades. Each has its own hourly market, its own strikes and its own budget (most an hour)."),
                (T, "More coins means more entries and more money in the market at once. They often move together, so it "
                    "is not as spread out as it looks."),
                (H, "NOW"), (F, f"{few(len(coins), 'coin')}, up to {usd(hour)} each an hour: {usd(hour * len(coins))} an hour in all. "
                                f"Its most at risk, {usd(r['max_risk'])}, holds over all of them together.")]
    if key == "cap":
        n = len(slots) if not r["cap"] else min(len(slots), r["cap"])
        return [(T, "A strike is one price level, such as \"BTC above $60,500\". The rule looks again at every slot it has on, and a "
                    "strike that is still in the band can be bought again at each one. This is the most times it may buy "
                    "the same strike in one hour."),
                (T, "Off: once at every slot. 1: only at the first slot it fits, and later slots buy other strikes. Every buy on a "
                    "strike wins or loses together, so more of them puts more money on one outcome."),
                (H, "EXAMPLE"), (F, f"{coin(coins[0])} has a favorite at {cents(p)} at all of {' '.join(f'{m:g}' for m in slots)} minutes. "
                                    f"It is bought {few(n, 'time')}: {few(n * k, 'contract')} for {usd(n * buy)}, which makes {usd(n * k * (1 - p))} or loses "
                                    f"{usd(n * buy)} together."),
                (N, f"With entries a strike at 1 that is {few(k, 'contract')} for {usd(buy)}.")]
    if key == "max_vol":
        vols = {s: v for s, v in (c.get("vol") or {}).items() if s in coins}
        px = c.get("price")
        out = [(T, "Volatility is how hard the coin is moving: the usual size of its one-minute moves over the last hour, in "
                   "basis points (bp). 1 bp is 0.01%. At each slot, a coin over this is skipped for that slot. So is one "
                   "without a full hour of prices to measure."),
               (T, "In a wild hour a favorite can turn in a minute. Lower skips more of those slots, and more of the calm ones "
                   "with them. Off never skips. The built-in calm skips over 7.6."),
               (H, "NOW"), (F, " · ".join(f"{coin(s)} {v:.1f}" + (" (skipped)" if r["max_vol"] and v > r["max_vol"] else "")
                                          for s, v in vols.items() if v is not None) + " bp a minute"
                            if any(v is not None for v in vols.values()) else "No coin has a full hour of prices yet.")]
        if px:
            out.append((F, f"10 bp on {coin(coins[0])} at {usd(px)} is a move of {usd(px * 0.001)} in a minute."))
        gaps = [coin(s) for s, v in vols.items() if v is None]
        if gaps and len(gaps) < len(vols):
            out.append((N, f"{' '.join(gaps)}: a gap in the last hour's prices" + (", skipped until it has passed." if r["max_vol"] else ".")))
        return out
    if key == "contracts":
        return [(T, "How many contracts it buys of one favorite at one slot. A contract pays $1 if it wins and nothing if it loses, "
                    "so its price is what it costs. Live buys the same number."),
                (T, "Twice the contracts is twice the money, won or lost, as long as the market has them: it never buys more than the "
                    "quote shows, and fewer when a limit, the hour's budget or its cash leaves less room."),
                (H, "EXAMPLE"), (F, f"{few(k, 'contract')} at {cents(p)} cost {usd(buy)} and {usd(fee)} in fees: "
                                    f"+{usd(k - buy - fee)} if it wins, -{usd(buy + fee)} if it loses."),
                (F, f"Won {p + fee / max(k, 1e-9):.0%} of the time, that comes out even.")]
    if key == "max_hour_cost":
        return [(T, "The most it may spend on one coin in one hour, over all its slots and strikes, fees included. When it is "
                    "used up the rest of that hour's favorites are passed over. The next hour starts fresh."),
                (T, "Every coin has its own. It fills from the first slot on, so a low one leaves nothing for the late slots."),
                (H, "EXAMPLE"), (F, f"{usd(hour)} is about {few(int(hour / max(buy + fee, .01)), 'buy')} of {few(k, 'contract')} at {cents(p)}. "
                                    f"{few(len(coins), 'coin')}: up to {usd(hour * len(coins))} an hour.")]
    who = [(N, "Paper and live both keep to it." if c.get("limits", True) else
            f"Saved as a new variant, paper and live both keep to it. {c.get('name')} was there before limits were part of variants: "
            "live keeps to this one, and its paper goes on as it was.")]
    if key == "max_risk":
        reach = hour * len(coins)
        return [(T, "The most money it may have in open bets at one time, over all its coins: what it would lose if every one of them "
                    "lost. Close to it, it buys fewer contracts. At it, it buys none until something settles."),
                (H, "EXAMPLE"), (F, f"{usd(r['max_risk'])} is {few(int(r['max_risk'] / max(buy, .01)), 'open buy')} of {few(k, 'contract')} at {cents(p)}."),
                (F, f"Its hourly budgets reach {usd(reach)} ({few(len(coins), 'coin')} × {usd(hour)}), so " +
                    (f"this stops it first, at {usd(r['max_risk'])}." if r["max_risk"] < reach else "they stop it first, in one hour."))] + who
    if key == "max_order":
        return [(T, "The most one buy may cost. A bigger one is cut to as many contracts as fit."),
                (H, "EXAMPLE"), (F, f"{few(k, 'contract')} at {cents(p)} cost {usd(buy)}: " +
                                    ("they fit." if buy <= r["max_order"] + 1e-9 else f"cut to {few(int(r['max_order'] / p), 'contract')}.")),
                (F, f"The dearest in its band, at {cents(r['band_hi'] - .01)}, cost {usd(k * (r['band_hi'] - .01))}.")] + who
    if key == "day_loss":
        return [(T, "How far it may fall since midnight, settled and open bets together, before it stops. On paper it takes no new "
                    "entries until the next midnight. Live turns off, and stays off until you press L. Open bets settle either way."),
                (H, "EXAMPLE"), (F, f"{usd(r['day_loss'])} is {few(max(1, round(r['day_loss'] / max(buy + fee, .01))), 'lost buy')} of {few(k, 'contract')} at {cents(p)}.")] + who
    if key == "paper_capital":
        return [(T, "The cash its paper record starts with. Wins add to it and losses take from it, and it never buys more than it has "
                    "left, so a small start can run out."),
                (T, "Live doesn't use it: live spends your Kalshi balance."),
                (H, "EXAMPLE"), (F, f"{usd(r['paper_capital'])} pays for {few(int(r['paper_capital'] / max(buy + fee, .01)), 'buy')} of {few(k, 'contract')} at "
                                    f"{cents(p)}, and its hourly budgets can spend {usd(hour * len(coins))} an hour.")]
    return []


DESK = {
    "gate.min_days": [
        ("text", "Live trades a variant only after that variant's own paper record passes all three gate checks. This is the first: how "
                 "long it has traded. A day is 24 hours it actually traded. Hours with the engine or the variant off don't count."),
        ("text", "More days means more kinds of hours in the record: quiet ones, wild ones, nights and weekends.")],
    "gate.min_entries": [
        ("text", "The second check: how many of its entries have settled. One entry is one buy of one strike at one slot."),
        ("text", "Entries on the same strike, and on strikes of the same hour, mostly win or lose together, so a thousand of them say less "
                 "than a thousand separate bets would.")],
    "gate.min_pnl": [
        ("text", "The third check: what it has made on paper since its start, after fees. It can be below zero, which lets a variant that "
                 "has lost up to that much through."),
        ("text", "A variant with more contracts wins and loses more dollars for the same trading, so this is easier for it to reach.")],
    "alerts": [
        ("text", "A pop-up on this computer (notify-send) for what matters with the screen closed: live orders and settlements, live "
                 "turning itself off, errors, and the engine stopping."),
        ("text", "Paper trading never makes one. The same message comes at most once in 10 minutes.")],
    "web.enabled": [
        ("text", "The screen in a browser, for another computer or a phone. It shows everything here and changes nothing that trades: "
                 "no pausing, no live, no edits. Each browser keeps its own theme, coin and selected variant."),
        ("text", "It is a service of its own (tern-web). Turning it off stops that and nothing else.")],
    "web.port": [
        ("text", "The number after the colon in its address. Change it if something else on this computer uses it."),
        ("text", "The firewall and the router are not touched. Off this computer a new port needs its own rules there: "
                 "sudo ufw allow PORT/tcp, and the router's forward.")],
    "web.network": [
        ("text", "Off: only this computer can open it, at localhost. On: any computer that can reach this one can, and it needs the "
                 "password and HTTPS."),
        ("text", "It will not start on the network without a password (tern web-password) and a certificate (tern web-cert).")],
    "screen.log": [
        ("text", "Which of the engine's lines the log box shows. It only hides them here: runtime/tern.log always has every line."),
        ("head", "CHOICES"), ("fact", "everything: every line."),
        ("fact", "no shadow lines: without the hourly results of the variants."),
        ("fact", "live, warnings, commands: without those and the paper slots."),
        ("fact", "trades only: what was bought, on paper and live, and live's settlements.")],
}
