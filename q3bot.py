import json
import logging
import random
from asyncio import sleep
from collections import deque

import discord
import paho.mqtt.client as mqtt
import pytz
from dateutil.parser import parse

logging.basicConfig(
    filename="discord.log",
    level=logging.INFO,
    format="[%(asctime)s] {%(pathname)s:%(lineno)d} %(levelname)s - %(message)s",
    datefmt="%H:%M:%S",
)
# set up logging to console
console = logging.StreamHandler()
console.setLevel(logging.DEBUG)
# set a format which is simpler for console use
formatter = logging.Formatter("%(name)-12s: %(levelname)-8s %(message)s")
console.setFormatter(formatter)
# add the handler to the root logger
logging.getLogger("").addHandler(console)

logger = logging.getLogger(__name__)

TZ = pytz.timezone("Europe/Oslo")
IX_WORLD = "1022"
STYLE_EMOJI = ["👻", "💀", "☠️", "😵", "🤯", "🤬", "🤘", "🎯", "💣", "🍖"]


class Q3Client(discord.Client):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cfg = parse_config()
        self.mqtt = mqtt.Client("q3client")
        self.mqtt.on_connect = self.on_mqtt_connect
        self.mqtt.on_message = self.on_mqtt_message
        self.mqtt.on_log = self.on_mqtt_log

        self.mqtt.connect(self.cfg["mqtt"])
        # an attribute we can access from our task
        self.clients = dict()
        self.msgs = deque()
        # create the background task and run it in the background
        self.bg_task = self.loop.create_task(self.my_background_task())

        self.current_game = dict()

        self.mqtt.loop_start()

    async def on_ready(self):
        logger.info("Logged in as")
        logger.info(self.user.name)
        logger.info(self.user.id)
        logger.info("------")

    def on_mqtt_log(client, userdata, level, buff):
        console.debug(buff)

    def on_mqtt_connect(self, client, userdata, flags, rc):
        logger.info("Connected with result code " + str(rc))
        try:
            client.subscribe("q3server/#", qos=2)
        except Exception as details:
            logger.exception("Subscription failed", exc_info=details)

    def on_mqtt_message(self, client, userdata, msg):
        tokens = msg.topic.split("/")
        if tokens[0] != "q3server":
            logger.info("q3client", msg.topic + " " + str(msg.payload))
            return True

        if tokens[1] != "log":
            return True

        payload = msg.payload.decode("utf-8")
        try:
            payload = json.loads(payload)
        except json.decoder.JSONDecodeError:
            logger.error(f"{payload} isn't JSON")
            return True

        logstr = f"{msg.topic} {payload}"
        logger.info(logstr)

        ts = parse(payload["timestamp"]).astimezone(TZ)
        # This is the action!
        if tokens[2] == "ShutdownGame":
            self.clients = dict()
            self.current_game = dict()
            self.msgs.append(f"Server restarting at {ts:%Y-%m-%d %H:%M}!")
        elif tokens[2] == "InitGame":
            if any(self.clients):  # Only if players are connected
                self.msgs.append(
                    f"New game starting on {payload['mapname']} at {ts:%Y-%m-%d %H:%M}!"
                )
            self.current_game.update(payload)
            self.current_game["fraglimit"] = int(
                self.current_game.get("fraglimit", 100)
            )

            self.clients = dict()
        elif tokens[2] == "Exit":
            self.msgs.append(
                f"Game ended due to {payload['reason'].lower()}"
                "at {ts:%Y-%m-%d %H:%M}"
            )
            self.current_game = dict()
        elif tokens[2] == "Score":
            self.msgs.append(f" > {payload['n']}: {payload['score']} kills")
        elif tokens[2] == "Kill":
            if payload["method"] == "MOD_LIGHTNING":
                self.msgs.append(
                    f"{payload['n']} killed {payload['targetn']}"
                    "with ⚡ SHÆÆÆÆÆÆÆÆÆÆÆÆÆÆFT! ⚡"
                )

            if payload["clientid"] == IX_WORLD:  # falling damage, etc.
                clidx = payload["targetid"]
            else:
                clidx = payload["clientid"]

            cli = self.clients.setdefault(clidx, dict())
            cli.setdefault("running_score", 0)

            if clidx == payload["targetid"]:  # Suicide
                cli["running_score"] -= 1
            else:
                cli["running_score"] += 1
                if cli["running_score"] > 0 and (cli["running_score"] % 5) == 0:
                    if "n" in cli:
                        self.msgs.append(f"{cli['n']} has {cli['running_score']} kills")
                delta = cli["running_score"] - self.current_game.get("fraglimit", 100)
                style = random.choice(STYLE_EMOJI)
                if delta == -3:
                    self.msgs.append(f"THREE FRAGS LEFT {style*3}")
                elif delta == -2:
                    self.msgs.append(f"TWO FRAGS LEFT {style*2}")
                elif delta == -1:
                    self.msgs.append(f"ONE FRAG LEFT {style}")
        elif tokens[2] == "Client":
            clidx = payload["clientid"]
            if not any(self.clients):
                # New game!
                map = self.current_game.get("mapname", "<unknown map>")
                self.msgs.append(
                    f"Q3E server {self.cfg['servername']}:"
                    f"New game starting on {map}"
                    f" at {ts:%Y-%m-%d %H:%M}!"
                )
            cli = self.clients.setdefault(clidx, dict())
            prev_name = cli.get("n")
            cli.update(payload)
            if payload["action"] == "Disconnect":
                del self.clients[clidx]
                serverstate = (
                    f"{len(self.clients)} players online"
                    if any(self.clients)
                    else "server empty"
                )
                self.msgs.append(
                    f"{cli.get('n', '<unknown>')} disconnected, {serverstate}"
                )
            elif payload["action"] == "Begin":
                pass  # we trigger on receiving the name instead
            elif payload["action"] == "Connect":
                pass
            elif payload["action"] == "InfoChanged":
                if prev_name is not None and prev_name != cli["n"]:
                    self.msgs.append(
                        f"{prev_name} changed name to {cli.get('n', '<unknown>')}"
                    )
            if prev_name is None and "n" in payload:
                serverstate = (
                    f"{len(self.clients)} players online"
                    if any(self.clients)
                    else "server empty"
                )
                self.msgs.append(
                    f"{cli.get('n', '<unknown>')} joined the game, {serverstate}"
                )

        return True

    async def my_background_task(self):
        await self.wait_until_ready()
        print(self.cfg)
        channel = self.get_channel(int(self.cfg["channel"]))
        print(channel)
        while not self.is_closed():
            msg = None
            try:
                msg = self.msgs.popleft()
            except IndexError:
                await sleep(0.01)  # tiny sleep to avoid spamming CPU

            if msg is not None:
                await channel.send(msg)


def parse_config():
    cfg = dict()
    with open("secrets.ini", "rt") as f:
        for line in f.readlines():
            k, v = line.strip().split("=")
            cfg[k] = v
    return cfg


def main():
    cfg = parse_config()
    intents = discord.Intents.default()
    intents.typing = False
    intents.presences = False
    client = Q3Client(intents=intents)

    client.run(cfg["token"])


if __name__ == "__main__":
    main()
