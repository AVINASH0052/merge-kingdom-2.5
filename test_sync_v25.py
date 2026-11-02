"""2.5 save/sync: the three tickets with nothing lost, holes 1 and 2, coins
earned on every device kept, the split, and spending on old and new devices.

The ticket tests use only old clients: the fix needs no app update.
"""
import json
import unittest

from sync_today import SCHEMA_22_24, Device, World
from sync_v25 import DeviceV25, ServerV25

HOUR = 3600
DAY = 24 * HOUR
START = 1761700000  # saved_at from Fact 1
PLAYER = "p_8812"


def spend_coins(device, coins):
    """Buy something in the coin store. "Coins are spent on the phone." (Fact 4)"""
    device.current["coins"] -= coins
    device.save()


class V25Test(unittest.TestCase):
    def setUp(self):
        self.world = World(START)
        self.server = ServerV25()

    def device(self, name, schema=SCHEMA_22_24):
        return Device(name, PLAYER, self.world, self.server, schema)

    def device_v25(self, name):
        return DeviceV25(name, PLAYER, self.world, self.server)

    def seed(self, devices, **fields):
        """The server takes over a save that every device also holds."""
        save = dict(player_id=PLAYER, saved_at=self.world.now, level=0, coins=0,
                    inventory={"hammer": 0, "shuffle": 0}, no_ads=False)
        save.update(fields)
        self.seed_text = json.dumps(save)
        self.server.import_save(PLAYER, self.seed_text)
        for d in devices:
            d.local_file = self.seed_text

    def account(self):
        return self.server.account(PLAYER)


class Ticket1(V25Test):
    """Tablet to 160, then the old phone plays offline from 150. Same steps as
    sync_today's ticket 1."""

    def test_tablet_keeps_160_and_the_sons_coins_count(self):
        tablet, phone = self.device("tablet"), self.device("old_phone")
        self.seed([tablet, phone], level=150)

        tablet.open_app()
        for _ in range(10):
            self.world.advance(2 * HOUR)
            tablet.play_level()  # 50 coins each

        self.world.advance(3 * HOUR)
        phone.set_online(False)
        phone.open_app()
        phone.play_level()       # level 151 again, 50 more coins

        self.world.advance(10 * HOUR)
        phone.set_online(True)

        self.assertEqual((self.account().level, self.account().coins), (160, 550))
        self.assertEqual((tablet.current["level"], tablet.current["coins"]), (160, 550))
        self.assertEqual((phone.current["level"], phone.current["coins"]), (160, 550))


class Ticket2(V25Test):
    """Date set to tomorrow for one level, then bus levels offline."""

    def setUp(self):
        super().setUp()
        self.phone = self.device("phone")
        self.seed([self.phone], level=50)
        self.phone.open_app()

        self.world.advance(HOUR)
        self.phone.clock_offset = DAY
        self.phone.play_level()       # 51, stamped a day ahead
        self.phone.clock_offset = 0

        self.phone.set_online(False)
        for _ in range(3):
            self.world.advance(20 * 60)
            self.phone.play_level()   # 52, 53, 54, queued

        self.world.advance(HOUR)
        self.phone.set_online(True)

    def test_bus_levels_kept(self):
        self.assertEqual((self.account().level, self.account().coins), (54, 200))
        self.assertEqual(self.phone.current["level"], 54)
        self.assertEqual(self.phone.queue, [])

    def test_next_save_is_kept_without_waiting_for_the_fake_date(self):
        self.world.advance(HOUR)
        self.phone.play_level()
        self.assertEqual((self.account().level, self.account().coins), (55, 250))
        self.assertEqual(self.phone.current["level"], 55)


class Ticket3(V25Test):
    """No Ads bought on the phone; the old tablet would erase it."""

    def test_reading_b_old_app_without_no_ads_field(self):
        schema_21 = {k: v for k, v in SCHEMA_22_24.items() if k != "no_ads"}
        phone, tablet = self.device("phone"), self.device("old_tablet", schema_21)
        self.seed([phone, tablet], level=100)
        phone.open_app()
        tablet.open_app()

        self.world.advance(30 * DAY)
        phone.buy_no_ads()
        self.world.advance(DAY)
        tablet.play_level()  # its upload has no no_ads field at all

        self.assertIn("no_ads", self.account().entitlements)
        self.assertTrue(phone.current["no_ads"])
        self.assertEqual((phone.current["level"], phone.current["coins"]), (101, 50))

    def test_reading_a_tablet_missed_the_purchase(self):
        phone, tablet = self.device("phone"), self.device("old_tablet")
        self.seed([phone, tablet], level=100)
        phone.open_app()

        self.world.advance(30 * DAY)
        phone.buy_no_ads()
        for _ in range(20):
            self.world.advance(DAY)
            phone.play_level()

        self.world.advance(DAY)
        tablet.open_app()
        tablet.play_level()  # level 101, no_ads false, newest saved_at

        for d in (phone, tablet):
            self.assertTrue(d.current["no_ads"])
            self.assertEqual((d.current["level"], d.current["coins"]), (120, 1050))


class Hole1PushBeforeQueue(V25Test):
    """An old device is back online with saves still queued, and a push
    reaches it before CloudSync sends them."""

    def test_push_does_not_overwrite_unsent_progress(self):
        tablet, phone = self.device("tablet"), self.device("old_phone")
        self.seed([tablet, phone], level=150)
        tablet.open_app()

        phone.set_online(False)
        phone.open_app()
        for _ in range(5):
            self.world.advance(10 * 60)
            phone.play_level()       # 151..155, queued
        for _ in range(2):
            self.world.advance(HOUR)
            tablet.play_level()      # 151, 152

        phone.online = True          # connection is up, queue not sent yet
        self.world.advance(HOUR)
        tablet.play_level()          # 153, pushed to the phone

        # Stamped from the phone's own history (R8), so the push is older than
        # the phone's last save. An account-wide max + 1 would have won here.
        self.assertEqual(phone.current["level"], 155)
        self.assertEqual(json.loads(phone.local_file)["level"], 155)

        phone.flush()
        self.assertEqual((self.account().level, self.account().coins), (155, 400))
        self.assertEqual(tablet.current["level"], 155)
        self.assertEqual(phone.current["level"], 155)


class Hole2PreSplitSave(V25Test):
    """A save made before the split reaches the server after it, with a higher
    level. Player A: 5400 coins, 2000 bought, so the split gives 2000 gems."""

    def setUp(self):
        super().setUp()
        self.phone = self.device_v25("phone_25")
        self.drawer = self.device("drawer_phone")
        self.seed([self.phone, self.drawer], level=150, coins=5400)
        self.server.record_coin_pack(PLAYER, 1000)
        self.server.record_coin_pack(PLAYER, 1000)
        self.drawer.set_online(False)
        self.drawer.open_app()

    def test_paid_part_is_not_counted_twice(self):
        for _ in range(10):
            self.world.advance(HOUR)
            self.drawer.play_level()  # 160, 5900 combined, queued
        self.phone.open_app()         # split: 2000 gems, 3400 coins
        self.drawer.set_online(True)

        acct = self.account()
        self.assertEqual((acct.level, acct.coins, acct.gems), (160, 3900, 2000))
        self.assertEqual(self.drawer.current["coins"], 5900)  # not 7900
        self.assertEqual((self.phone.current["coins"], self.phone.current["gems"]), (3900, 2000))

    def test_paid_spent_before_the_split_comes_off_gems(self):
        self.world.advance(HOUR)
        spend_coins(self.drawer, 3900)  # all 3400 earned + 500 of the paid part
        self.world.advance(HOUR)
        self.drawer.play_level()        # 151, 50 more
        self.phone.open_app()
        self.drawer.set_online(True)

        acct = self.account()
        self.assertEqual((acct.coins, acct.gems, acct.overdraft), (50, 1500, 0))


class EarnedCoinsOnEveryDevice(V25Test):
    """R2: nothing earned is dropped, and nothing adopted is counted twice."""

    def test_coins_earned_on_two_offline_devices_both_count(self):
        tablet, phone = self.device("tablet"), self.device("old_phone")
        self.seed([tablet, phone], level=150)
        for d in (tablet, phone):
            d.set_online(False)
            d.open_app()
        for _ in range(3):
            self.world.advance(HOUR)
            tablet.play_level()
        for _ in range(2):
            self.world.advance(HOUR)
            phone.play_level()

        tablet.set_online(True)
        phone.set_online(True)

        self.assertEqual((self.account().level, self.account().coins), (153, 250))
        for d in (tablet, phone):
            self.assertEqual((d.current["level"], d.current["coins"]), (153, 250))

    def test_coins_from_an_adopted_push_are_not_counted_again(self):
        tablet, phone = self.device("tablet"), self.device("old_phone")
        self.seed([tablet, phone], level=150)
        tablet.open_app()
        phone.open_app()

        self.world.advance(HOUR)
        tablet.play_level()  # 50, pushed to the phone
        self.world.advance(HOUR)
        phone.play_level()   # the phone now shows 100

        # Measured against its own last upload instead of the pushed save,
        # the phone's 100 would count as 100 new coins and give 150.
        self.assertEqual(self.account().coins, 100)


class Split(V25Test):
    """R5 on the three example players."""

    def split(self, coins, packs):
        phone = self.device_v25("phone_25")
        self.seed([phone], level=150, coins=coins)
        for pack in packs:
            self.server.record_coin_pack(PLAYER, pack)
        phone.open_app()
        return phone

    def assert_split(self, phone, gems, coins):
        acct = self.account()
        self.assertEqual((acct.gems, acct.coins), (gems, coins))
        self.assertEqual((phone.current["gems"], phone.current["coins"]), (gems, coins))
        self.assertTrue(phone.current["split_done"])

    def test_player_a_balance_above_purchases(self):
        self.assert_split(self.split(5400, [1000, 1000]), gems=2000, coins=3400)

    def test_player_b_balance_below_purchases(self):
        self.assert_split(self.split(1200, [2500, 2500]), gems=1200, coins=0)

    def test_player_c_never_paid(self):
        self.assert_split(self.split(9000, []), gems=0, coins=9000)

    def test_split_runs_once(self):
        phone = self.split(5400, [1000, 1000])
        phone.set_online(False)
        phone.set_online(True)
        second = self.device_v25("tablet_25")  # updated from 2.4, still holds the old save
        second.local_file = self.seed_text
        second.open_app()

        self.assert_split(phone, gems=2000, coins=3400)
        self.assertEqual((second.current["gems"], second.current["coins"]), (2000, 3400))

    def test_old_device_still_sees_one_combined_number(self):
        phone = self.split(5400, [1000, 1000])
        old = self.device("old_tablet")
        old.local_file = self.seed_text
        old.open_app()
        self.world.advance(HOUR)
        old.play_level()  # 5450 combined

        self.assertEqual(old.current["coins"], 5450)
        self.assert_split(phone, gems=2000, coins=3450)


class OldClientDropsGems(V25Test):
    """The same old client as sync_today's last test, now against ServerV25."""

    def test_old_save_without_gems_or_new_items_changes_nothing(self):
        phone, tablet = self.device_v25("phone_25"), self.device("old_tablet")
        self.seed([phone, tablet], level=150, coins=5400)
        self.server.record_coin_pack(PLAYER, 2000)
        tablet.open_app()
        phone.open_app()  # split: 2000 gems
        self.assertTrue(phone.spend_gems(500, "golden_frame"))

        self.world.advance(HOUR)
        tablet.play_level()  # its upload has no gems and no golden_frame

        acct = self.account()
        self.assertEqual(acct.gems, 1500)
        self.assertIn("golden_frame", acct.entitlements)
        self.assertEqual(phone.current["gems"], 1500)
        self.assertIn("golden_frame", phone.current["entitlements"])
        self.assertEqual(phone.current["level"], 151)


class SpendOnOldAndNew(V25Test):
    """Player A after the split: 3400 coins, 2000 gems. The old tablet is
    closed through the split and the 2.5 purchase or spend, so it still
    holds 5400."""

    def setUp(self):
        super().setUp()
        self.phone = self.device_v25("phone_25")
        self.tablet = self.device("old_tablet")
        self.seed([self.phone, self.tablet], level=150, coins=5400)
        self.server.record_coin_pack(PLAYER, 2000)
        self.phone.open_app()
        self.world.advance(DAY)

    def test_old_spend_past_earned_takes_only_what_it_spent_from_gems(self):
        self.assertTrue(self.phone.buy_gems(1000))  # 3000 gems
        self.tablet.open_app()
        spend_coins(self.tablet, 4000)  # 3400 earned + 600 from gems

        acct = self.account()
        self.assertEqual((acct.coins, acct.gems, acct.overdraft), (0, 2400, 0))
        self.assertEqual(self.tablet.current["coins"], 2400)
        self.assertEqual(self.phone.current["gems"], 2400)

    def test_gems_spent_on_25_do_not_come_back_as_coins(self):
        self.assertTrue(self.phone.spend_gems(800, "golden_frame"))  # 1200 gems
        self.tablet.open_app()
        self.tablet.play_level()

        acct = self.account()
        self.assertEqual((acct.coins, acct.gems), (3450, 1200))
        self.assertEqual(self.tablet.current["coins"], 4650)  # the spent 800 is not shown
        self.assertEqual(self.phone.current["coins"], 3450)

    def test_online_old_device_cannot_spend_gems_twice(self):
        self.assertTrue(self.phone.spend_gems(800, "golden_frame"))
        self.tablet.open_app()
        self.tablet.play_level()
        spend_coins(self.tablet, 4650)

        acct = self.account()
        self.assertEqual((acct.coins, acct.gems, acct.overdraft), (0, 0, 0))

    def test_known_limit_offline_double_spend_is_recorded(self):
        self.assertTrue(self.phone.spend_gems(800, "golden_frame"))  # 1200 gems
        self.tablet.set_online(False)
        self.tablet.open_app()          # still shows 5400
        spend_coins(self.tablet, 5400)
        self.tablet.set_online(True)

        acct = self.account()
        self.assertEqual((acct.coins, acct.gems, acct.overdraft), (0, 0, 800))


if __name__ == "__main__":
    unittest.main()
