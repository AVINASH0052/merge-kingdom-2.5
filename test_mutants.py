"""Breaks ServerV25 on purpose and checks the random runs catch each break.

Each mutant must fail a random run within MAX_SEEDS seeds; the output names
the seed and the check that failed. The last test goes the other way: a change
the random runs are not built to catch.
"""
import unittest

from sync_today import SCHEMA_22_24, from_json
from sync_v25 import ServerV25
from test_sync_v25_random import run_seed

MAX_SEEDS = 300


class LastWriteWins(ServerV25):
    """Fact 3 again: the newest saved_at replaces level, coins and items."""

    def upload(self, device, text):
        super().upload(device, text)
        acct = self.accounts[device.player_id]
        save = from_json(text, SCHEMA_22_24)
        if save["saved_at"] > getattr(acct, "newest", acct.floor):
            acct.newest = save["saved_at"]
            acct.level = save["level"]
            acct.coins = save["coins"]
            acct.inventory = dict(save["inventory"])


class CountPushedCoinsAgain(ServerV25):
    """Ignores the mark and measures each upload against the device's own last
    upload, as if it never took a push."""

    def _base(self, acct, st, mark):
        return st.line if st.line is not None else acct.origin


class AssumeEveryPushTaken(ServerV25):
    """Ignores the mark and measures each upload against the last save sent to
    the device, as if it always took it."""

    def _view(self, acct, device):
        text = super()._view(acct, device)
        self.state[device].last_sent = acct.views[acct.last_mark]
        return text

    def _base(self, acct, st, mark):
        sent, st.last_sent = getattr(st, "last_sent", None), None
        if sent is not None:
            return sent
        return st.line if st.line is not None else acct.origin


class SplitOnEveryContact(ServerV25):
    """Runs the split again each time a 2.5 device calls."""

    def _split(self, acct):
        acct.split_done = False
        super()._split(acct)


class NoAdsFromLatestSave(ServerV25):
    """Takes No Ads from each upload instead of never removing it."""

    def upload(self, device, text):
        super().upload(device, text)
        if not from_json(text, SCHEMA_22_24)["no_ads"]:
            self.accounts[device.player_id].entitlements.discard("no_ads")


class SpendGemsFirst(ServerV25):
    """Old-device spends take gems before earned coins. Only the oracle's
    spending order can catch this; value is still conserved."""

    def _add_coins(self, acct, change, combined):
        if change < 0 and combined:
            from_gems = min(-change, acct.gems)
            acct.gems -= from_gems
            change += from_gems
        super()._add_coins(acct, change, combined)


class AccountWideStamp(ServerV25):
    """Hole 1's first design: every save sent is stamped above anything seen
    on the account, so a device takes it even with saves still queued."""

    def _view(self, acct, device):
        states = [s for d, s in self.state.items() if d.player_id == acct.player_id]
        self.state[device].sent = max([acct.floor] + [max(s.seen, s.sent) for s in states])
        return super()._view(acct, device)


def first_failure(server_cls):
    """(seed, failed check) for the first failing seed, or None."""
    for seed in range(MAX_SEEDS):
        try:
            run_seed(seed, server_cls)
        except AssertionError as e:
            return seed, str(e).splitlines()[-1]
    return None


class MutantsAreCaught(unittest.TestCase):
    def assert_caught(self, server_cls):
        found = first_failure(server_cls)
        self.assertIsNotNone(found, "%s passed %d seeds" % (server_cls.__name__, MAX_SEEDS))
        print("\n    %s: seed %d, %s" % ((server_cls.__name__,) + found))

    def test_last_write_wins(self):
        self.assert_caught(LastWriteWins)

    def test_count_pushed_coins_again(self):
        self.assert_caught(CountPushedCoinsAgain)

    def test_assume_every_push_taken(self):
        self.assert_caught(AssumeEveryPushTaken)

    def test_split_on_every_contact(self):
        self.assert_caught(SplitOnEveryContact)

    def test_no_ads_from_latest_save(self):
        self.assert_caught(NoAdsFromLatestSave)

    def test_spend_gems_first(self):
        self.assert_caught(SpendGemsFirst)

    def test_known_gap_account_wide_stamp_is_not_caught(self):
        """With the mark, R8 only changes what a device shows while its queue
        is still going up. The server's numbers stay exact, and the random
        runs check those plus every device after the final sync, not what a
        device shows mid-run."""
        self.assertIsNone(first_failure(AccountWideStamp))


if __name__ == "__main__":
    unittest.main()
