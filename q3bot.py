import json
import logging
import random
from asyncio import sleep
from collections import deque
from pathlib import Path

import discord
import paho.mqtt.client as mqtt
import pytz
from dateutil.parser import parse
from discord.ext import commands
from xrcon.client import XRcon

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


def load_custom_maps(path):
    return [
        a.stem.replace("map-", "")
        for a in Path(path).iterdir()
        if a.is_file() and a.suffix == ".pk3"
    ]


def generate_map_rotation_cmds(name, rota):
    result = list()

    for i, mp in enumerate(rota):
        rname = f"{name}{i}"
        nname = f"{name}{(i + 1) % len(rota)}"
        cmd = f'set {rname} "map {mp} ; set nextmap vstr {nname}"'
        result.append(cmd)
    return result, f"vstr {nname}"


# Some basic map rotations
MAP_ROTATIONS = {
    "default": [
        "q3dm1",
        "q3dm2",
        "q3dm3",
        "q3dm4",
        "q3dm5",
        "pro-q3dm6",
        "q3dm7",
        "pro-q3dm13",
        "q3dm15",
        "q3dm16",
        "q3dm17",
        "q3tourney2",
        "q3tourney3",
        "q3tourney5",
    ],
    "1v1": [
        "q3dm0",
        "q3dm1",
        "q3dm2",
        "q3dm3",
        "q3dm4",
        "q3dm5",
        "q3dm7",
        "q3tourney2",
        "q3tourney3",
        "q3tourney5",
    ],
    "large": ["q3dm6", "q3dm7", "q3dm8", "q3dm9", "q3dm18"],
}


class Q3Client(commands.Bot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cfg = parse_config()
        self.map_rotations = dict()
        self.map_rotations.update(MAP_ROTATIONS)
        if "extra_maps_dir" in self.cfg:
            self.map_rotations["custom"] = load_custom_maps(self.cfg["extra_maps_dir"])

        self.current_rotation = "default"

        self.mqtt = mqtt.Client("q3client")
        self.mqtt.on_connect = self.on_mqtt_connect
        self.mqtt.on_message = self.on_mqtt_message
        self.mqtt.on_log = self.on_mqtt_log

        self.mqtt.connect(self.cfg["mqtt"])

        self.rcon = XRcon(
            self.cfg["rconip"], 27960, self.cfg["rconpass"], secure_rcon=0, timeout=5
        )
        self.rcon.connect()

        self.game = discord.Game("Quake3E")
        # an attribute we can access from our task
        self.clients = dict()
        self.msgs = deque()
        # create the background task and run it in the background
        self.bg_task = self.loop.create_task(self.my_background_task())

        self.current_game = dict()

        self.add_commands()

        self.mqtt.loop_start()

    async def on_ready(self):
        logger.info("Logged in as")
        logger.info(self.user.name)
        logger.info(self.user.id)
        logger.info("------")
        await self.change_presence(status=discord.Status.online, activity=self.game)

    def ensure_status(self):
        if "mapname" not in self.current_game:
            status_ = self.rcon.getstatus()[0]
            logger.info(status_)
            status = {k.decode("utf-8"): v.decode("utf-8") for k, v in status_.items()}
            self.current_game.update(status)
            logger.info(f"rcon> fetched mapname {status['mapname']}")

    def add_commands(self):
        @self.command(name="status", pass_context=True)
        async def status(ctx):
            """See who's playing and where"""
            logger.info(f"status requested: {ctx}")
            self.ensure_status()
            await ctx.channel.send(
                f"status: {len(self.clients)} players on "
                f"{self.current_game['mapname']}"
            )
            for _, cli in self.clients.items():
                await ctx.channel.send(
                    f"> {cli.get('n', '<unknown>')}: "
                    f"{cli.get('running_score', '??')} kills"
                )

        @self.command(name="maps", pass_context=True)
        async def maps(ctx):
            """List available map rotations"""
            await ctx.channel.send(f"*{len(self.map_rotations)} map rotations*")

            for mn, mr in self.map_rotations.items():
                await ctx.channel.send(f"*{mn}*: {', '.join(mr)}")

            await ctx.channel.send(f"Current rotation: {self.current_rotation}")

        @self.command(name="maprotation", pass_context=True)
        async def setrota(
            ctx, rotations: str, immediate: bool = False, randomize: bool = False
        ):
            """Set a new map rotation

            Args:
                rotations: Comma-separated list of map rotations (from !maps)
                immediate: Immediately change to first map in rotation (y/n)
                randomize: Shuffle the maps in the rotation (y/n)
            """
            await self.set_map_rotation(rotations, immediate, randomize)

        # self.add_command(status)
        # self.add_command(maps)

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
                f"Game ended due to {payload['reason'].lower()[:-1]} "
                f"at {ts:%Y-%m-%d %H:%M}"
            )
            self.current_game = dict()
        elif tokens[2] == "Score":
            self.msgs.append(f" > {payload['n']}: {payload['score']} kills")
        elif tokens[2] == "Kill":
            if payload["method"] == "MOD_LIGHTNING":
                self.msgs.append(
                    f"{payload['n']} killed {payload['targetn']} "
                    f"with {self.cfg.get('lightning_injoke', 'the power of Zeus')}"
                )

            if payload["clientid"] == IX_WORLD:  # falling damage, etc.
                clidx = payload["targetid"]
                name_ = payload["targetn"]
            else:
                clidx = payload["clientid"]
                name_ = payload["n"]

            # grab names while we have the chance
            self.clients.setdefault(payload["targetid"], dict()).setdefault(
                "n", payload["targetn"]
            )

            cli = self.clients.setdefault(clidx, dict())
            cli.setdefault("running_score", 0)
            cli.setdefault("n", name_)  # set this in case we didn't know

            if clidx == payload["targetid"]:  # Suicide
                cli["running_score"] -= 1
            else:
                cli["running_score"] += 1
                if cli["running_score"] > 0 and (cli["running_score"] % 5) == 0:
                    if "n" in cli:
                        self.msgs.append(f"{cli['n']} has {cli['running_score']} kills")
                delta = cli["running_score"] - int(
                    self.current_game.get("fraglimit", 100)
                )
                style = random.choice(STYLE_EMOJI)
                if delta == -3 and "threefrags" not in self.current_game:
                    self.msgs.append(f"THREE FRAGS LEFT {style*3}")
                    self.current_game["threefrags"] = True
                elif delta == -2 and "twofrags" not in self.current_game:
                    self.msgs.append(f"TWO FRAGS LEFT {style*2}")
                    self.current_game["twofrags"] = True
                elif delta == -1 and "onefrag" not in self.current_game:
                    self.msgs.append(f"ONE FRAG LEFT {style}")
                    self.current_game["onefrag"] = True
        elif tokens[2] == "Client":
            clidx = payload["clientid"]
            if not any(self.clients):
                # New game!
                map = self.current_game.get("mapname", "<unknown map>")
                self.msgs.append(
                    f"Q3E server {self.cfg['servername']}: "
                    f"New game starting on {map} "
                    f"at {ts:%Y-%m-%d %H:%M}!"
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

    async def set_map_rotation(self, rotaname, changemap=False, randomize=True):
        await self.wait_until_ready()
        channel = self.get_channel(int(self.cfg["channel"]))
        await channel.send(
            f"Changing to map rotation {rotaname}{', randomized' if randomize else ''}"
        )

        if changemap and len(self.clients) > 1:
            await channel.send(
                f"{len(self.clients)} players online, won't change map on them!"
            )
            changemap = False

        # build the map list
        rota = list()

        for rn in rotaname.split(","):
            rn = rn.strip()
            if rn in self.map_rotations:
                rota += self.map_rotations[rn]
            else:
                await channel.send(f"Can't find map rotation {rn}")
        if randomize:
            random.shuffle(rota)

        # find a quake-safe map name
        if "," in rotaname:
            rotaname_ = "combo"
        else:
            rotaname_ = rotaname
        if randomize:
            rotaname_ = f"{rotaname_}rnd"

        items, immediate = generate_map_rotation_cmds(rotaname_, rota)
        await channel.send(f"> {', '.join(rota)}")
        self.rcon.execute("; ".join(items))

        if not changemap:
            immediate = f"set nextmap {immediate}"
            self.rcon.execute(immediate)
            await channel.send(f"Next map set to {rota[0]}")
            self.rcon.execute(
                f"say Map rotation changed to {rotaname}, next map is {rota[0]}"
            )
        else:
            await channel.send(f"Immediately changing to {rota[0]}")
            self.rcon.execute(immediate)
        self.current_rotation = rotaname

    async def my_background_task(self):
        await self.wait_until_ready()
        channel = self.get_channel(int(self.cfg["channel"]))

        while not self.is_closed():
            msg = None
            try:
                msg = self.msgs.popleft()
            except IndexError:
                await sleep(0.01)  # tiny sleep to avoid spamming CPU

            if msg is not None:
                await channel.send(msg)
            else:
                self.ensure_status()


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
    client = Q3Client(command_prefix="!", intents=intents)

    client.run(cfg["token"])


if __name__ == "__main__":
    main()
