"""Model of save/sync in 2.5: the server owns gems and purchases, and adds up
what every device did instead of picking one save.

Old clients (2.0-2.4) are sync_today.Device, unchanged. DeviceV25 is the new
client. ServerV25 replaces the Fact 3 server; both kinds of client talk to it
the same way: upload() in, on_cloud_save() out.

How the merge stays exact with old clients:
  Every save the server sends carries a mark in player_id ("p_8812~7"). The
  old client never reads player_id, so every save it builds on that state
  sends the mark back (A8). Each upload is one action on top of a state the
  server knows: the save whose mark it carries, or that device's previous
  upload with the same mark. So the server sees the action, not only the
  result, and can add it once.

Server rules:
  R1  Level: the highest any device reached.
  R2  Coins and inventory: add each upload's change against its base. Coins
      earned on any device are kept.
  R3  Entitlements (no_ads, items bought with gems) only grow.
  R4  Gems change only on the server: purchases add, 2.5 gem spends
      subtract, and so do old-device spends that go past the earned coins.
      The gems field in a save is ignored.
  R5  Split once, at the player's first 2.5 contact:
      gems = min(coins, P), coins -= gems.
  R6  Old devices, and 2.5 saves from before the split, show one number:
      coins + gems. A spend from it uses earned coins first, then gems. A 2.5
      coin spend uses earned coins only.
  R7  Spending more than exists (the same coins spent offline on two
      devices) stops at 0 and the excess goes to Account.overdraft.
  R8  Every save the server sends is stamped
      max(import saved_at, highest saved_at seen from that device,
          highest saved_at already sent to it) + 1,
      so a device with newer unsent saves keeps showing its own progress.

Assumptions on top of sync_today's A1-A4:
  A5  Each upload tells the server which device sent it.
  A6  1 gem = 1 paid coin.
  A7  Only coin packs bought before the split are modelled.
  A8  Nothing in the 2.0-2.4 client or CloudSync reads player_id from the
      save. Fact 2's SaveSystem never does; CloudSync's code is not given.
  A9  At deploy every device holds the server's save, or saves queued on top
      of it. For a device that already differed, the base of its first
      upload never reached any server.
"""
import json

from sync_today import SCHEMA_22_24, Device, from_json

SCHEMA_V25 = dict(SCHEMA_22_24, save_version=2, split_done=False, gems=0, entitlements=[])
ITEMS = ("hammer", "shuffle")


def _mark(player_id):
    """The view id the server wrote into player_id, or None for an unmarked save."""
    _, sep, mark = player_id.partition("~")
    return int(mark) if sep else None


class Account:
    """The server's copy of one player."""

    def __init__(self, player_id, save):
        self.player_id = player_id
        self.level = save["level"]
        self.coins = save["coins"]  # one combined pot before the split, earned coins after
        self.inventory = dict(save["inventory"])
        self.entitlements = {"no_ads"} if save["no_ads"] else set()
        self.gems = 0
        self.purchased = 0          # P: coins from coin-pack records (Fact 4)
        self.split_done = False
        self.overdraft = 0          # coins or gems spent that did not exist (R7)
        self.item_overdraft = 0     # items used that did not exist
        self.floor = save["saved_at"]  # no device held a newer synced save at import
        # Base for a device's first unmarked upload (A9).
        self.origin = {"coins": save["coins"], "inventory": dict(save["inventory"])}
        self.views = {}             # mark -> coins and inventory as sent
        self.last_mark = 0


class DeviceState:
    """What the server remembers about one device."""

    def __init__(self):
        self.seen = 0      # highest saved_at it uploaded
        self.sent = 0      # highest saved_at the server sent it
        self.v25 = False
        self.line = None   # its last upload: mark, coins, inventory


class ServerV25:
    def __init__(self):
        self.accounts = {}  # player_id -> Account
        self.devices = {}   # player_id -> [Device]
        self.state = {}     # Device -> DeviceState

    def register(self, device):
        self.devices.setdefault(device.player_id, []).append(device)
        self.state[device] = DeviceState()

    def import_save(self, player_id, text):
        """Deploy day: take over the save the Fact 3 server had stored."""
        self.accounts[player_id] = Account(player_id, from_json(text, SCHEMA_22_24))

    def record_coin_pack(self, player_id, coins):
        """Fact 4 purchase record (A7)."""
        self.accounts[player_id].purchased += coins

    def account(self, player_id):
        return self.accounts[player_id]

    # Calls from clients

    def upload(self, device, text):
        acct, st = self.accounts[device.player_id], self.state[device]
        raw = json.loads(text)
        save = from_json(text, SCHEMA_22_24)  # the fields every version has
        st.seen = max(st.seen, save["saved_at"])
        before = self._snapshot(acct)
        if raw.get("save_version") == 2:
            st.v25 = True
            self._split(acct)

        mark = _mark(save["player_id"])
        base = self._base(acct, st, mark)
        combined = not raw.get("split_done", False)
        self._add_coins(acct, save["coins"] - base["coins"], combined)  # R2
        for item in ITEMS:
            self._add_item(acct, item, save["inventory"][item] - base["inventory"][item])
        acct.level = max(acct.level, save["level"])  # R1
        if save["no_ads"]:                           # R3
            acct.entitlements.add("no_ads")
        st.line = {"mark": mark, "coins": save["coins"], "inventory": save["inventory"]}

        self._send(acct, device, before)

    def hello(self, device):
        """A 2.5 client is online. Its first contact runs the split (R5)."""
        acct, before = self._v25_contact(device)
        self._send(acct, device, before)

    def buy_gems(self, device, gems):
        acct, before = self._v25_contact(device)
        acct.gems += gems
        self._send(acct, device, before)

    def spend_gems(self, device, gems, item):
        acct, before = self._v25_contact(device)
        ok = acct.gems >= gems
        if ok:
            acct.gems -= gems
            acct.entitlements.add(item)
        self._send(acct, device, before)
        return ok

    # Rules

    def _v25_contact(self, device):
        acct = self.accounts[device.player_id]
        self.state[device].v25 = True
        before = self._snapshot(acct)
        self._split(acct)
        return acct, before

    def _base(self, acct, st, mark):
        """The state an upload was built on: the device's previous upload if it
        carries the same mark, else the save whose mark it carries."""
        if st.line is not None and st.line["mark"] == mark:
            return st.line
        if mark is None:
            return acct.origin  # A9
        return acct.views[mark]

    def _split(self, acct):
        """R5, at most once per player."""
        if acct.split_done:
            return
        acct.gems = min(acct.coins, acct.purchased)
        acct.coins -= acct.gems
        acct.split_done = True

    def _add_coins(self, acct, change, combined):
        """R2, R6, R7."""
        if change >= 0:
            acct.coins += change
            return
        spend = -change
        from_coins = min(spend, acct.coins)
        acct.coins -= from_coins
        spend -= from_coins
        if combined:
            from_gems = min(spend, acct.gems)
            acct.gems -= from_gems
            spend -= from_gems
        acct.overdraft += spend

    def _add_item(self, acct, item, change):
        total = acct.inventory[item] + change
        acct.inventory[item] = max(0, total)
        acct.item_overdraft += max(0, -total)

    def _send(self, acct, device, before):
        """Reply to the device that called; push to the others if anything changed."""
        device.on_cloud_save(self._view(acct, device))
        if self._snapshot(acct) != before:
            for other in self.devices[acct.player_id]:
                if other is not device and other.connected:  # A1
                    other.on_cloud_save(self._view(acct, other))

    def _view(self, acct, device):
        """The save this device gets, in a format its version understands."""
        st = self.state[device]
        st.sent = max(acct.floor, st.seen, st.sent) + 1  # R8
        acct.last_mark += 1
        v25 = st.v25 and acct.split_done
        coins = acct.coins if v25 else acct.coins + acct.gems  # R6; gems are 0 before the split
        acct.views[acct.last_mark] = {"coins": coins, "inventory": dict(acct.inventory)}
        save = {
            "player_id": "%s~%d" % (acct.player_id, acct.last_mark),
            "saved_at": st.sent,
            "level": acct.level,
            "coins": coins,
            "inventory": dict(acct.inventory),
            "no_ads": "no_ads" in acct.entitlements,
        }
        if v25:
            save.update(gems=acct.gems, split_done=True, save_version=2,
                        entitlements=sorted(acct.entitlements))
        return json.dumps(save)

    @staticmethod
    def _snapshot(acct):
        return (acct.level, acct.coins, sorted(acct.inventory.items()),
                sorted(acct.entitlements), acct.gems, acct.split_done)


class DeviceV25(Device):
    """The 2.5 client: the same SaveSystem plus online-only gem calls (R4)."""

    def __init__(self, name, player_id, world, server):
        super().__init__(name, player_id, world, server, SCHEMA_V25)

    def open_app(self):
        super().open_app()
        if self.connected:
            self.server.hello(self)

    def set_online(self, online):
        super().set_online(online)
        if self.connected:
            self.server.hello(self)

    def buy_gems(self, gems):
        if not self.connected:
            return False
        self.server.buy_gems(self, gems)
        return True

    def spend_gems(self, gems, item):
        return self.connected and self.server.spend_gems(self, gems, item)
