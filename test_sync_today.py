"""Replays the three support tickets (Fact 6) against today's save/sync."""
import json
import unittest

from sync_today import SCHEMA_22_24, Device, Server, World

HOUR = 3600
DAY = 24 * HOUR
START = 1761700000  # saved_at from Fact 1
PLAYER = "p_8812"


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.world = World(START)
        self.server = Server()

    def device(self, name, schema=SCHEMA_22_24):
        return Device(name, PLAYER, self.world, self.server, schema)

    def seed(self, devices, **fields):
        """Server and every device hold the same save, written now."""
        save = dict(player_id=PLAYER, saved_at=self.world.now, level=0, coins=0,
                    inventory={"hammer": 0, "shuffle": 0}, no_ads=False)
        save.update(fields)
        text = json.dumps(save)
        self.server.saves[PLAYER] = text
        for d in devices:
            d.local_file = text

    def server_save(self):
        return json.loads(self.server.saves[PLAYER])


class Ticket1TabletLevelsLost(SyncTest):
    """"I played on my tablet all weekend and got to level 160. My son played on
    my old phone in airplane mode on Sunday night, after I stopped. Today I am
    back at level 151."

    Assumed: both devices were synced at level 150 before the weekend, and the
    tablet app was still open in the background when the phone came back
    online. Every clock is correct.
    """

    def test_offline_phone_overwrites_tablet_progress(self):
        tablet, phone = self.device("tablet"), self.device("old_phone")
        self.seed([tablet, phone], level=150)

        # The weekend: ten levels on the tablet, each one uploaded and kept.
        tablet.open_app()
        for _ in range(10):
            self.world.advance(2 * HOUR)
            tablet.play_level()
        self.assertEqual(self.server_save()["level"], 160)

        # Sunday night, after the parent stopped: the son plays in airplane mode.
        self.world.advance(3 * HOUR)
        phone.set_online(False)
        phone.open_app()
        self.assertEqual(phone.current["level"], 150)  # Load() reads only the local file
        phone.play_level()
        self.assertEqual(len(phone.queue), 1)

        # Next morning airplane mode goes off and the queue is sent.
        self.world.advance(10 * HOUR)
        phone.set_online(True)

        self.assertEqual(self.server_save()["level"], 151)
        self.assertEqual(tablet.current["level"], 151)
        self.assertEqual(json.loads(tablet.local_file)["level"], 151)


class Ticket2BusLevelsLost(SyncTest):
    """"I changed the date on my phone to tomorrow, opened the free chest early
    and played one level. Then I changed the date back. Later I played more
    levels on the bus with no internet. When I got home, those levels were gone."

    Assumed: one device, online while the date was ahead. The chest is not
    modelled; the brief does not say how it uses the clock.
    """

    def setUp(self):
        super().setUp()
        self.phone = self.device("phone")
        self.seed([self.phone], level=50)
        self.phone.open_app()

        self.world.advance(HOUR)
        self.phone.clock_offset = DAY  # date set to tomorrow
        self.phone.play_level()        # level 51, stamped a day ahead, kept
        self.fake_time = self.server_save()["saved_at"]
        self.phone.clock_offset = 0    # date set back

        self.phone.set_online(False)   # the bus
        for _ in range(3):
            self.world.advance(20 * 60)
            self.phone.play_level()    # 52, 53, 54, queued
        self.assertEqual(self.phone.current["level"], 54)

    def test_bus_levels_rolled_back_when_online(self):
        self.world.advance(HOUR)
        self.phone.set_online(True)

        self.assertEqual(self.server_save()["level"], 51)
        self.assertEqual(self.phone.current["level"], 51)
        self.assertEqual(self.phone.queue, [])

    def test_every_save_is_rejected_until_real_time_passes_the_fake_date(self):
        self.world.advance(HOUR)
        self.phone.set_online(True)
        self.phone.play_level()
        self.assertEqual(self.phone.current["level"], 51)

        self.world.advance(DAY)
        self.assertGreater(self.world.now, self.fake_time)
        self.phone.play_level()
        self.assertEqual(self.server_save()["level"], 52)


class Ticket3NoAdsLost(SyncTest):
    """"I paid for No Ads in March. Last week the ads came back. I play on my
    phone and on my old tablet."

    The brief allows two readings; both are replayed.
    """

    def test_tablet_that_missed_the_purchase_uploads_no_ads_false(self):
        """Assumed: bought on the phone; the tablet app was closed in March, so
        it missed the push (A1), and was next opened last week."""
        phone, tablet = self.device("phone"), self.device("old_tablet")
        self.seed([phone, tablet], level=100)
        phone.open_app()

        self.world.advance(30 * DAY)  # March
        phone.buy_no_ads()
        self.assertTrue(self.server_save()["no_ads"])
        for _ in range(20):
            self.world.advance(DAY)
            phone.play_level()

        self.world.advance(DAY)       # last week
        tablet.open_app()
        self.assertFalse(tablet.current["no_ads"])
        tablet.play_level()

        self.assertFalse(phone.current["no_ads"])
        self.assertEqual(phone.current["level"], 101)  # the 20 phone levels went too

    def test_old_app_without_no_ads_field_strips_it(self):
        """Assumed: the old tablet is stuck on 2.0-2.1 (Android 7) and that
        version's SaveData has no no_ads field. Levels sync; No Ads does not.
        This fits the ticket better: the player reports no lost levels."""
        schema_21 = {k: v for k, v in SCHEMA_22_24.items() if k != "no_ads"}
        phone, tablet = self.device("phone"), self.device("old_tablet", schema_21)
        self.seed([phone, tablet], level=100)
        phone.open_app()
        tablet.open_app()

        self.world.advance(30 * DAY)
        phone.buy_no_ads()            # pushed to the tablet, which drops the field
        self.world.advance(DAY)
        tablet.play_level()           # uploads a save with no no_ads at all

        self.assertNotIn("no_ads", self.server_save())
        self.assertFalse(phone.current["no_ads"])
        self.assertEqual(phone.current["level"], tablet.current["level"])


class OldClientDropsUnknownFields(SyncTest):
    """Not a ticket: what a 2.2-2.4 client does with fields it does not know (A3).
    This is why 2.5 cannot keep gems or new items inside the save."""

    def test_gems_and_new_items_are_dropped_by_the_next_old_save(self):
        schema_new = dict(SCHEMA_22_24, gems=0,
                          inventory={"hammer": 0, "shuffle": 0, "rocket": 0})
        new, old = self.device("new_phone", schema_new), self.device("old_tablet")
        self.seed([new, old], level=10)
        new.open_app()
        old.open_app()

        self.world.advance(HOUR)
        new.current["gems"] = 500
        new.current["inventory"]["rocket"] = 2
        new.save()
        self.assertEqual(self.server_save()["gems"], 500)  # stored as-is, never read

        self.world.advance(HOUR)
        old.play_level()

        stored = self.server_save()
        self.assertNotIn("gems", stored)
        self.assertNotIn("rocket", stored["inventory"])
        self.assertEqual(new.current["gems"], 0)
        self.assertEqual(new.current["inventory"]["rocket"], 0)


if __name__ == "__main__":
    unittest.main()
