"""Model of Merge Kingdom save/sync as it works today (app 2.2-2.4).

Device is SaveSystem from Fact 2 plus the CloudSync queue; Server is Fact 3.
Everything is synchronous and in order: a call to the server returns after
every device it talks to has handled the message.

Assumptions not in the brief:
  A1  The server sends a save only to devices that are online with the app
      open. A device that misses a push never gets it later.
  A2  The CloudSync queue survives the app being closed.
  A3  JsonUtility drops JSON fields the class does not have, and gives missing
      fields their default (0, false, "").
  A4  The server knows the player from the connection, not from the JSON.
"""
import copy
import json

# SaveData and Inventory from Fact 2, as field -> default.
SCHEMA_22_24 = {
    "player_id": "",
    "saved_at": 0,
    "level": 0,
    "coins": 0,
    "inventory": {"hammer": 0, "shuffle": 0},
    "no_ads": False,
}


def _conform(value, schema):
    """Keep only the fields in schema; fill missing ones with defaults (A3)."""
    if not isinstance(schema, dict):
        return value
    src = value if isinstance(value, dict) else {}
    return {key: _conform(src[key], default) if key in src else copy.deepcopy(default)
            for key, default in schema.items()}


def from_json(text, schema):
    """JsonUtility.FromJson<SaveData>(json)"""
    return _conform(json.loads(text), schema)


def to_json(save, schema):
    """JsonUtility.ToJson(Current)"""
    return json.dumps(_conform(save, schema))


class World:
    """Real time. A device clock is real time plus that device's offset."""

    def __init__(self, now):
        self.now = now

    def advance(self, seconds):
        self.now += seconds


class Server:
    """Fact 3: one save per player, newest saved_at wins."""

    def __init__(self):
        self.saves = {}    # player_id -> raw JSON, stored exactly as uploaded
        self.devices = {}  # player_id -> [Device]

    def register(self, device):
        self.devices.setdefault(device.player_id, []).append(device)

    def upload(self, device, text):
        player_id = device.player_id  # A4
        stored = self.saves.get(player_id)
        # "keeps it only if its saved_at is larger than the stored saved_at.
        #  The server does not read the other fields."
        if stored is None or json.loads(text)["saved_at"] > json.loads(stored)["saved_at"]:
            self.saves[player_id] = text
            # "it sends it to the other devices of the player"
            for other in self.devices[player_id]:
                if other is not device and other.connected:  # A1
                    other.on_cloud_save(text)
        else:
            # "it sends its stored save back to the device that uploaded"
            device.on_cloud_save(stored)


class Device:
    """One install of the app."""

    def __init__(self, name, player_id, world, server, schema=SCHEMA_22_24):
        self.name = name
        self.player_id = player_id
        self.world = world
        self.server = server
        self.schema = schema
        self.local_file = None  # LocalPath
        self.current = None     # SaveSystem.Current, only while the app is open
        self.queue = []         # CloudSync uploads waiting for a connection (A2)
        self.clock_offset = 0   # seconds the device clock is ahead of real time
        self.online = True
        self.app_open = False
        server.register(self)

    @property
    def connected(self):
        return self.online and self.app_open

    def phone_now(self):
        """DateTimeOffset.Now.ToUnixTimeSeconds()"""
        return self.world.now + self.clock_offset

    # SaveSystem (Fact 2)

    def load(self):
        self.current = from_json(self.local_file, self.schema)

    def save(self):
        self.current["saved_at"] = self.phone_now()
        text = to_json(self.current, self.schema)
        self.local_file = text
        self.cloud_upload(text)

    def on_cloud_save(self, server_json):
        server = from_json(server_json, self.schema)
        if server["saved_at"] > self.current["saved_at"]:
            self.current = server
            self.local_file = server_json

    # CloudSync (Fact 3)

    def cloud_upload(self, text):
        self.queue.append(text)
        self.flush()

    def flush(self):
        while self.connected and self.queue:
            self.server.upload(self, self.queue.pop(0))

    # What the player does

    def open_app(self):
        self.app_open = True
        self.load()  # "Called when the game starts."
        self.flush()

    def close_app(self):
        self.app_open = False
        self.current = None

    def set_online(self, online):
        self.online = online
        self.flush()

    def play_level(self, coins=50):
        self.current["level"] += 1
        self.current["coins"] += coins
        self.save()  # "Called after every level and every purchase."

    def buy_no_ads(self):
        self.current["no_ads"] = True
        self.save()
