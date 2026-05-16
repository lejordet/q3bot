from asyncio import sleep
from collections import deque
from datetime import datetime, timedelta
import json
import logging
from pathlib import Path
import random

from bspp import bspp
from dateutil.parser import parse
import discord
from discord.ext import commands
import paho.mqtt.client as mqtt
from xrcon.client import XRcon

from q3constants import BOTS, IX_WORLD, MAP_ROTATIONS, STYLE_EMOJI, TZ, parse_since
from q3parselog import Q3LogParse, render_name

logging.basicConfig(
    filename="discord.log",
    level=logging.ERROR,
    format="[%(asctime)s] {%(pathname)s:%(lineno)d} %(levelname)s - %(message)s",
    datefmt="%H:%M:%S",
)
# set up logging to console
console = logging.StreamHandler()
console.setLevel(logging.DEBUG)
# set a format which is simpler for console use
formatter = logging.Formatter("[%(asctime)s] %(name)-12s: %(levelname)-8s %(message)s")
console.setFormatter(formatter)
# add the handler to the root logger
logging.getLogger("").addHandler(console)

logger = logging.getLogger(__name__)

MAP_IGNORE_FILE = ".mapignore"
NEWGAME_COOLDOWN = timedelta(seconds=30)


def load_mapnames_from_pk3(pk3: Path) -> set[str]:
    """
    Reads a .pk3, returns all map names found within

    Args:
        pk3: Path to .pk3 file

    Returns:
        Set of all map names found
    """
    try:
        pk = bspp.process_pk3_file(pk3)
    except Exception as ex:
        logger.error("unable to read %s (%s)", pk3.stem, ex)
        return []

    return [bspp.pp_map(m).map_name for m in pk.map_entities]


def load_custom_maps(path: str, only_include=None):
    """
    Finds a list of custom maps from the extra_maps_folder

    Args:
        path: Directory to parse
        only_include: Optional list of maps to filter down to
    Returns:
        Sorted list of unique maps found
    """
    ignored_patterns = {"pak?.pk3", "*baseq3.pk3"}
    if (Path(path) / ".mapignore").exists():
        in_patterns = (Path(path) / ".mapignore").read_text().splitlines()
        ignored_patterns.update({p for p in in_patterns if len(p.strip()) > 1})
        print(f"Loaded {len(in_patterns)}")

    pakfiles = [a for a in Path(path).iterdir() if a.is_file() and a.suffix == ".pk3"]

    for ignore in ignored_patterns:
        pakfiles = [n for n in pakfiles if not n.match(ignore)]

    # then parse out actual maps
    maps = set()

    for pk in pakfiles:
        pk_maps = load_mapnames_from_pk3(pk)
        maps.update(pk_maps)

    if only_include:
        maps.intersection_update(only_include)

    return sorted(maps)


def load_custom_maprotations(path: str):
    """
    Finds .maprotation files, which are line-by-line taken as map names; returns found data

    Args:
        path: Path to search

    Returns:
        dict of actual file stem -> maps found
    """
    rotations = {}
    for f in Path(path).glob("*.maprotation"):
        rota = load_custom_maps(path, f.read_text().splitlines())
        if len(rota) > 0:
            rotations[f.stem] = rota

    return rotations


def generate_map_rotation_cmds(name, rota):
    result = list()

    for i, mp in enumerate(rota):
        rname = f"{name}{i}"
        nname = f"{name}{(i + 1) % len(rota)}"
        cmd = f'set {rname} "map {mp} ; set nextmap vstr {nname}"'
        result.append(cmd)
    return result, f"vstr {nname}"


SUFFIXES = ["bot", ".com", "wtf", "test", "_yep"]


class Q3Client(commands.Bot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cfg = parse_config()
        self.map_rotations = dict()
        self.map_rotations.update(MAP_ROTATIONS)
        if "extra_maps_dir" in self.cfg:
            self.map_rotations.update(load_custom_maprotations(self.cfg["extra_maps_dir"]))
            self.map_rotations["custom"] = load_custom_maps(self.cfg["extra_maps_dir"])

        self.current_rotation = "default"
        self.newgame_last_used = datetime.now()

        self.mqtt = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, "q3client")
        self.mqtt.on_connect = self.on_mqtt_connect
        self.mqtt.on_message = self.on_mqtt_message
        self.mqtt.on_log = self.on_mqtt_log

        self.mqtt.connect(self.cfg.get("mqtt", "q3mosquitto"))  # default to container name

        self.rcon = XRcon(
            self.cfg.get("rconip", "q3server"),
            27960,
            self.cfg["rconpass"],
            secure_rcon=0,
            timeout=1,
        )
        self.rcon.connect()

        self.game = discord.Game("Quake3E")
        self.game_status_change = False  # if true, we update the presence
        # an attribute we can access from our task
        self.clients = dict()
        self.msgs = deque()

        # background task will be created async
        self.bg_task = None

        self.current_game = dict()

        self.bot_skill = int(self.cfg.get("bot_skill", 4))
        self.bots_active = False
        # if set used as second parameter for handle_autobots:
        self.autobots_change = None
        self.add_commands()

        self.mqtt.loop_start()

    async def setup_hook(self):
        # create the background task and run it in the background
        self.bg_task = self.loop.create_task(self.my_background_task())

    async def on_ready(self):
        logger.info("Logged in as")
        logger.info(self.user.name)
        logger.info(self.user.id)
        logger.info("------")
        await self.change_presence(status=discord.Status.online, activity=self.game)
        await self.set_map_rotation("default", quiet=True)

    async def ensure_status(self, force=False):
        if "mapname" not in self.current_game or force:
            status_, players = self.rcon.getstatus()
            logger.info(status_)
            status = {k.decode("utf-8"): v.decode("utf-8") for k, v in status_.items()}
            self.current_game.update(status)
            logger.info(f"rcon> fetched mapname {status['mapname']}")
            self.game = discord.Game(f"Quake3E on {status['mapname']}")
            self.game_status_change = True
            if len(players) < len(self.clients):  # we probably have too many bots!
                # try to match names
                names = [p.name.decode("utf-8") for p in players]

                logger.info(
                    f"Mismatch in player count, we had {len(self.clients)} "
                    + f"online, but server reports {len(players)}"
                )

                deleteix = list()
                for ix, cl in self.clients.items():
                    if cl.get("n", "<<<<<deleteme>>>>") not in names:
                        logger.info(f" > {cl['n']} as disappeared at some point")
                        deleteix.append(ix)
                for ix in deleteix:
                    del self.clients[ix]
        if self.game_status_change:
            await self.change_presence(status=discord.Status.online, activity=self.game)
            self.game_status_change = False

        if self.autobots_change is not None:  # stylish ternary logic
            clicount = len(self.clients)
            await self.handle_autobots(clicount, self.autobots_change)
            self.autobots_change = None

    async def remove_bots(self):
        logging.info(">>> kick allbots")
        self.rcon.execute("kick allbots")

        # Check that we got rid of them!
        await self.ensure_status(True)
        self.bots_active = False

    async def add_bots(self, count=1):
        """Adds some bots

        Args:
            count: Number of bots to add
        """
        added = list()
        for _ in range(count):
            bot = random.choice(BOTS)
            # suffix = random.choice(SUFFIXES)
            # botname = f"{bot.capitalize()}{suffix}"
            logging.info(f"Adding {bot}")
            self.rcon.execute(f"addbot {bot} {self.bot_skill}")
            added.append(bot)
        await self.ensure_status(True)
        self.bots_active = True
        return added

    async def handle_autobots(self, playercount, joining):
        if self.cfg["autobots"] != "roll out":
            return list()

        if playercount in (0, 1) and not joining and self.bots_active:
            await self.remove_bots()
            return 0
        elif playercount == 1 and joining and not self.bots_active:
            await self.add_bots(1)
            return 2
        elif playercount > 2 and joining and self.bots_active:
            await self.remove_bots()
            return 2

        return list()  # no messages defined yet

    def add_commands(self):
        @self.command(name="status", pass_context=True)
        async def status(ctx):
            """See who's playing and where"""
            logger.info(f"status requested: {ctx}")
            await self.ensure_status(True)
            await ctx.channel.send(
                f"status: {len(self.clients)} players on {self.current_game['mapname']}"
            )
            for _, cli in self.clients.items():
                await ctx.channel.send(
                    f"> {cli.get('n', '<unknown>')}: {cli.get('running_score', '0?')} kills"
                )

        @self.command(name="maps", pass_context=True)
        async def maps(ctx):
            """List available map rotations"""
            await ctx.channel.send(f"*{len(self.map_rotations)} map rotations*")

            for mn, mr in self.map_rotations.items():
                await ctx.channel.send(f"*{mn}*: {', '.join(mr)}")

            await ctx.channel.send(f"Current rotation: {self.current_rotation}")

        @self.command(name="maprotation", pass_context=True)
        async def setrota(ctx, rotations: str, immediate: bool = False, randomize: bool = False):
            """Set a new map rotation

            Args:
                rotations: Comma-separated list of map rotations (from !maps)
                immediate: Immediately change to first map in rotation (y/n)
                randomize: Shuffle the maps in the rotation (y/n)
            """
            await self.set_map_rotation(rotations, immediate, randomize)

        @self.command(name="addbots", pass_context=True)
        async def addbots(ctx, count: int = 1):
            """Add bots

            Args:
                count: Number of bots
            """
            bots = await self.add_bots(count)
            pl_ = "Bots" if len(bots) != 1 else "Bot"
            await ctx.channel.send(f"{pl_} added: {', '.join(bots)}")

        @self.command(name="killbots", pass_context=True)
        async def killbots(ctx):
            """Kill all bots"""
            await self.remove_bots()
            await ctx.channel.send("Removed all bots")

        @self.command(name="stats", pass_context=True)
        async def stats(ctx, limit: str = "all"):
            """Show historical stats

            Args:
                limit: Show stats from 'all', 'week', 'today', yyyy-mm-dd
                       unknown text will be taken as 'all'
            """
            stats = Q3LogParse()
            since = parse_since(limit)
            stats.parse_log()  # TODO: Cache so this can't be used to DOS?
            for text in stats.stats_text(since):
                await ctx.channel.send(text)

        @self.command(name="newgame", pass_context=True)
        async def newgame(ctx, playmap: str = "RANDOM_MAP"):
            """Start a new game

            Args:
                playmap: Go to a specific map
            """
            if "disable_newgame" in self.cfg:
                await ctx.channel.send("newgame is disabled in config")
                return

            if self.newgame_last_used + NEWGAME_COOLDOWN > datetime.now():
                await ctx.channel.send(
                    f"need to wait {NEWGAME_COOLDOWN.total_seconds()} seconds between new games"
                )
                return
            # start timer early
            self.newgame_last_used = datetime.now()

            found_map = False
            await ctx.channel.send("Starting a new game!")
            await self.remove_bots()

            for mr in self.map_rotations:
                if playmap in self.map_rotations[mr]:
                    await ctx.channel.send(f"Heading to {playmap}")
                    await self.set_map_rotation(mr, changemap=False, randomize=True)
                    self.rcon.execute(f"map {playmap}")
                    await self.ensure_status(True)
                    found_map = True

            if not found_map:  # fallback
                await ctx.channel.send(f"Heading to a random map in _{self.current_rotation}_")
                await self.set_map_rotation(self.current_rotation, changemap=True, randomize=True)

            await self.ensure_status(True)
            await ctx.channel.send(f"new game: {self.current_game['mapname']}")

        # self.add_command(status)
        # self.add_command(maps)

    def on_mqtt_log(client, userdata, level, buff):
        console.debug(buff)

    def on_mqtt_connect(self, client, userdata, flags, rc, props):
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
            logger.info(f"Server restarting at {ts:%Y-%m-%d %H:%M}!")
        elif tokens[2] == "InitGame":
            if any(self.clients):  # Only if players are connected
                self.msgs.append(
                    f"New game starting on {payload['mapname']} at {ts:%Y-%m-%d %H:%M}!"
                )
            self.current_game.update(payload)
            self.current_game["fraglimit"] = int(self.current_game.get("fraglimit", 100))
            self.game = discord.Game(f"Quake3E on {payload['mapname']}")
            self.game_status_change = True

            self.clients = dict()
        elif tokens[2] == "Exit":
            if any(self.clients):  # Only if players are connected
                self.msgs.append(
                    f"Game ended due to {payload['reason'].lower()[:-1]} at {ts:%Y-%m-%d %H:%M}"
                )
            self.current_game = dict()
        elif tokens[2] == "Score":
            self.msgs.append(f" > {payload['n']}: {payload['score']} kills")
        elif tokens[2] == "Kill":
            if payload["method"] == "MOD_LIGHTNING":
                self.msgs.append(
                    f"{render_name(payload['n'])} killed "
                    f"{render_name(payload['targetn'])} "
                    f"with {self.cfg.get('lightning_injoke', 'the power of Zeus')}"
                )

            if payload["clientid"] == IX_WORLD:  # falling damage, etc.
                clidx = payload["targetid"]
                name_ = payload["targetn"]
            else:
                clidx = payload["clientid"]
                name_ = payload["n"]

            # grab names while we have the chance
            self.clients.setdefault(payload["targetid"], dict()).setdefault("n", payload["targetn"])

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
                delta = cli["running_score"] - int(self.current_game.get("fraglimit", 100))
                style = random.choice(STYLE_EMOJI)
                if delta == -3 and "threefrags" not in self.current_game:
                    self.msgs.append(f"THREE FRAGS LEFT {style * 3}")
                    self.current_game["threefrags"] = True
                elif delta == -2 and "twofrags" not in self.current_game:
                    self.msgs.append(f"TWO FRAGS LEFT {style * 2}")
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
                clicount = len(self.clients)
                serverstate = f"{clicount} players online" if clicount > 0 else "server empty"

                self.autobots_change = False
                self.msgs.append(f"{render_name(cli.get('n'))} disconnected, {serverstate}")
            elif payload["action"] == "Begin":
                pass  # we trigger on receiving the name instead
            elif payload["action"] == "Connect":
                pass
            elif payload["action"] == "InfoChanged":
                if prev_name is not None and prev_name != cli["n"]:
                    self.msgs.append(f"{prev_name} changed name to {render_name(cli.get('n'))}")
            if prev_name is None and "n" in payload:
                clicount = len(self.clients)
                serverstate = f"{clicount} players online" if clicount > 0 else "server empty"
                self.autobots_change = True
                self.msgs.append(f"{render_name(cli.get('n'))} joined the game, {serverstate}")

        return True

    async def set_map_rotation(self, rotaname, changemap=False, randomize=True, quiet=False):
        await self.wait_until_ready()
        channel = self.get_channel(int(self.cfg["channel"]))
        if not quiet:
            randtext = ", randomized" if randomize else ""
            await channel.send(f"Changing to map rotation {rotaname}{randtext}")

        if changemap and len(self.clients) > 1 and "disabled_mapchange" in self.cfg:
            await channel.send(f"{len(self.clients)} players online, won't change map on them!")
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
        if not quiet:
            await channel.send(f"> {', '.join(rota)}")
        for item in items:
            self.rcon.execute(item)

        if not changemap:
            immediate = f"set nextmap {immediate}"
            self.rcon.execute(immediate)
            if not quiet:
                await channel.send(f"Next map set to {rota[0]}")
            self.rcon.execute(f"say Map rotation changed to {rotaname}, next map is {rota[0]}")
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
                await self.ensure_status()


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
    intents.message_content = True
    client = Q3Client(command_prefix="!", intents=intents)

    client.run(cfg["token"])


if __name__ == "__main__":
    main()
