"""Random runs against ServerV25: two old devices (2.4 and 2.1) and one on
2.5, going offline, closing the app, changing the date, spending coins and
gems, buying gems and No Ads.

An oracle sees each action itself, in the order the server receives it, and
applies it once. After every step the server must match it. The oracle uses
the server's spending order (R6, R7) because that is a rule, not a fact. The
other checks don't depend on it:
  - coins + gems - overdraft equals everything earned and bought, minus
    everything spent
  - level never goes down, and No Ads is never taken away
  - coins and gems never go down in a step where nothing was spent

run_seed() takes any server class, so test_mutants.py can run broken ones.
SEEDS and STEPS can be set in the environment; SEED=n runs only seed n.
"""
import collections
import json
import os
import random
import unittest

from sync_today import SCHEMA_22_24, Device, World
from sync_v25 import DeviceV25, ServerV25

SEEDS = int(os.environ.get("SEEDS", 3000))
STEPS = int(os.environ.get("STEPS", 100))
HOUR = 3600
DAY = 24 * HOUR
START = 1761700000
PLAYER = "p_8812"
SCHEMA_21 = {k: v for k, v in SCHEMA_22_24.items() if k != "no_ads"}
COINS_PER_LEVEL = 50

Action = collections.namedtuple("Action", "kind amount level combined")


def expect(ok, message):
    if not ok:
        raise AssertionError(message)


class Oracle:
    """What really happened: no saves, no merging."""

    def __init__(self, save, purchased):
        self.level = save["level"]
        self.coins = save["coins"]
        self.hammer = save["inventory"]["hammer"]
        self.no_ads = False
        self.gems = 0
        self.purchased = purchased
        self.split_done = False
        self.overdraft = 0
        self.item_overdraft = 0
        self.total = save["coins"]  # earned + bought - spent; the split does not move it
        self.spends = 0             # spends applied so far

    def split(self):
        if not self.split_done:
            self.gems = min(self.coins, self.purchased)
            self.coins -= self.gems
            self.split_done = True

    def apply(self, action):
        self.level = max(self.level, action.level)
        if action.kind == "play":
            self.coins += action.amount
            self.total += action.amount
        elif action.kind == "buy_hammer":
            self.spend(action.amount, action.combined)
            self.add_hammer(1)
        elif action.kind == "use_hammer":
            self.add_hammer(-1)
        elif action.kind == "no_ads":
            self.no_ads = True

    def spend(self, amount, combined):
        self.spends += 1
        self.total -= amount
        from_coins = min(amount, self.coins)
        self.coins -= from_coins
        amount -= from_coins
        if combined:
            from_gems = min(amount, self.gems)
            self.gems -= from_gems
            amount -= from_gems
        self.overdraft += amount

    def add_hammer(self, change):
        total = self.hammer + change
        self.hammer = max(0, total)
        self.item_overdraft += max(0, -total)

    def buy_gems(self, gems):
        self.gems += gems
        self.total += gems

    def spend_gems(self, gems):
        ok = self.gems >= gems
        if ok:
            self.spends += 1
            self.gems -= gems
            self.total -= gems
        return ok


def checked(server_cls):
    """server_cls, handing each action to the oracle as it arrives."""

    class Checked(server_cls):
        def __init__(self, oracle):
            super().__init__()
            self.oracle = oracle
            self.pending = collections.defaultdict(collections.deque)  # Device -> actions not uploaded yet

        def upload(self, device, text):
            if isinstance(device, DeviceV25):
                self.oracle.split()
            self.oracle.apply(self.pending[device].popleft())
            super().upload(device, text)

        def hello(self, device):
            self.oracle.split()
            super().hello(device)

        def buy_gems(self, device, gems):
            self.oracle.split()
            self.oracle.buy_gems(gems)
            super().buy_gems(device, gems)

        def spend_gems(self, device, gems, item):
            self.oracle.split()
            expected = self.oracle.spend_gems(gems)
            ok = super().spend_gems(device, gems, item)
            expect(ok == expected, "spend_gems(%d): server %s, oracle %s" % (gems, ok, expected))
            return ok

    return Checked


class Run:
    """One seeded run: three devices on one account."""

    def __init__(self, seed, server_cls):
        self.seed = seed
        self.rng = rng = random.Random(seed)
        self.world = World(START)
        start = dict(player_id=PLAYER, saved_at=START, level=rng.randint(1, 200),
                     coins=rng.randint(0, 6000),
                     inventory={"hammer": rng.randint(0, 3), "shuffle": rng.randint(0, 3)},
                     no_ads=False)
        packs = [rng.choice([500, 1000, 2500]) for _ in range(rng.randint(0, 3))]
        self.oracle = Oracle(start, sum(packs))
        self.server = checked(server_cls)(self.oracle)
        self.devices = [Device("tablet_24", PLAYER, self.world, self.server),
                        Device("phone_21", PLAYER, self.world, self.server, SCHEMA_21),
                        DeviceV25("phone_25", PLAYER, self.world, self.server)]
        text = json.dumps(start)
        self.server.import_save(PLAYER, text)
        for pack in packs:
            self.server.record_coin_pack(PLAYER, pack)
        for d in self.devices:
            d.local_file = text
        self.log = ["start %s packs %s" % (text, packs)]

    def go(self):
        try:
            prev = self.state()
            for step in range(STEPS):
                d = self.rng.choice(self.devices)
                self.log.append("%d %s" % (step, d.name))
                self.log[-1] += " " + self.act(d, step)
                prev = self.check(prev)
            self.log.append("final sync")
            self.final_sync()
            self.check(prev)
            self.check_devices()
        except Exception as e:
            raise AssertionError("seed %d, last steps:\n%s\n%s: %s" % (
                self.seed, "\n".join(self.log[-20:]), type(e).__name__, e)) from e

    def act(self, d, step):
        rng = self.rng
        self.world.advance(rng.randint(60, 3 * HOUR))
        if not d.app_open:
            if rng.random() < 0.7:
                d.open_app()
                return "open"
            d.set_online(not d.online)
            return "online" if d.online else "offline"

        choices = ["play"] * 5 + ["buy_hammer"] * 2 + ["use_hammer", "online", "close", "clock"]
        if "no_ads" in d.schema and not d.current["no_ads"]:
            choices.append("no_ads")
        if isinstance(d, DeviceV25) and d.connected:
            choices += ["buy_gems", "spend_gems", "spend_gems"]
        what = rng.choice(choices)

        cur = d.current
        combined = not cur.get("split_done", False)
        pending = self.server.pending[d]
        if what == "play":
            pending.append(Action("play", COINS_PER_LEVEL, cur["level"] + 1, combined))
            d.play_level(COINS_PER_LEVEL)
        elif what == "buy_hammer":
            if cur["coins"] <= 0:
                return "buy_hammer: no coins"
            cost = rng.randint(1, cur["coins"])
            pending.append(Action("buy_hammer", cost, cur["level"], combined))
            cur["coins"] -= cost
            cur["inventory"]["hammer"] += 1
            d.save()
            return "buy_hammer %d" % cost
        elif what == "use_hammer":
            if cur["inventory"]["hammer"] <= 0:
                return "use_hammer: none"
            pending.append(Action("use_hammer", 0, cur["level"], combined))
            cur["inventory"]["hammer"] -= 1
            d.save()
        elif what == "no_ads":
            pending.append(Action("no_ads", 0, cur["level"], combined))
            d.buy_no_ads()
        elif what == "buy_gems":
            gems = rng.choice([100, 500, 1000])
            d.buy_gems(gems)
            return "buy_gems %d" % gems
        elif what == "spend_gems":
            if cur["gems"] <= 0:
                return "spend_gems: none"
            gems = rng.randint(1, cur["gems"])
            d.spend_gems(gems, "item_%d" % step)
            return "spend_gems %d" % gems
        elif what == "online":
            d.set_online(not d.online)
            return "online" if d.online else "offline"
        elif what == "close":
            d.close_app()
        elif what == "clock":
            d.clock_offset = rng.choice([0, 0, DAY, -DAY, 2 * DAY, -3 * HOUR])
            return "clock %+d" % d.clock_offset
        return what

    def final_sync(self):
        """Everyone online with the right date, then one level each so the
        last upload reaches every device."""
        for d in self.devices:
            d.clock_offset = 0
            self.world.advance(60)
            d.set_online(True)
            if not d.app_open:
                d.open_app()
        for d in self.devices:
            self.world.advance(60)
            cur = d.current
            self.server.pending[d].append(Action("play", COINS_PER_LEVEL, cur["level"] + 1,
                                                 not cur.get("split_done", False)))
            d.play_level(COINS_PER_LEVEL)

    def state(self):
        acct = self.server.account(PLAYER)
        return {"level": acct.level, "coins": acct.coins, "gems": acct.gems,
                "no_ads": "no_ads" in acct.entitlements,
                "spends": self.oracle.spends, "split": acct.split_done}

    def check(self, prev):
        acct, o = self.server.account(PLAYER), self.oracle
        got = {"level": acct.level, "coins": acct.coins, "gems": acct.gems,
               "no_ads": "no_ads" in acct.entitlements, "hammer": acct.inventory["hammer"],
               "overdraft": acct.overdraft, "item_overdraft": acct.item_overdraft}
        want = {"level": o.level, "coins": o.coins, "gems": o.gems, "no_ads": o.no_ads,
                "hammer": o.hammer, "overdraft": o.overdraft, "item_overdraft": o.item_overdraft}
        diff = ["%s %s, oracle %s" % (k, got[k], want[k]) for k in want if got[k] != want[k]]
        expect(not diff, "server vs oracle: " + "; ".join(diff))
        value = acct.coins + acct.gems - acct.overdraft
        expect(value == o.total, "value not conserved: %d, should be %d" % (value, o.total))
        expect(acct.level >= prev["level"], "level went down")
        expect(not (prev["no_ads"] and not got["no_ads"]), "No Ads taken away")
        if o.spends == prev["spends"] and acct.split_done == prev["split"]:
            expect(acct.coins >= prev["coins"], "coins went down with no spend")
            expect(acct.gems >= prev["gems"], "gems went down with no spend")
        return self.state()

    def check_devices(self):
        """After the final sync every device shows the server's state."""
        acct = self.server.account(PLAYER)
        for d in self.devices:
            cur = d.current
            if isinstance(d, DeviceV25):
                shown = (cur["coins"], cur["gems"])
                want = (acct.coins, acct.gems)
            else:
                shown = (cur["coins"],)
                want = (acct.coins + acct.gems,)
            shown += (cur["level"], cur["inventory"])
            want += (acct.level, acct.inventory)
            if "no_ads" in cur:
                shown += (cur["no_ads"],)
                want += ("no_ads" in acct.entitlements,)
            expect(shown == want, "%s shows %s, server %s" % (d.name, shown, want))


def run_seed(seed, server_cls=ServerV25):
    """Raises AssertionError, with the seed and its last steps, on any failure."""
    Run(seed, server_cls).go()


class RandomRuns(unittest.TestCase):
    def test_random_runs(self):
        seeds = [int(os.environ["SEED"])] if "SEED" in os.environ else range(SEEDS)
        for seed in seeds:
            run_seed(seed)


if __name__ == "__main__":
    unittest.main()
