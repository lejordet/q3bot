from datetime import datetime, timedelta
from typing import Optional

import pytz
from dateutil.parser import parse

TZ = pytz.timezone("Europe/Oslo")
IX_WORLD = "1022"
STYLE_EMOJI = ["👻", "💀", "☠️", "😵", "🤯", "🤬", "🤘", "🎯", "💣", "🍖"]

MEANS_OF_DEATH = {  # For reference/future use
    0: "MOD_UNKNOWN",
    1: "MOD_SHOTGUN",
    2: "MOD_GAUNTLET",
    3: "MOD_MACHINEGUN",
    4: "MOD_GRENADE",
    5: "MOD_GRENADE_SPLASH",
    6: "MOD_ROCKET",
    7: "MOD_ROCKET_SPLASH",
    8: "MOD_PLASMA",
    9: "MOD_PLASMA_SPLASH",
    10: "MOD_RAILGUN",
    11: "MOD_LIGHTNING",
    12: "MOD_BFG",
    13: "MOD_BFG_SPLASH",
    14: "MOD_WATER",
    15: "MOD_SLIME",
    16: "MOD_LAVA",
    17: "MOD_CRUSH",
    18: "MOD_TELEFRAG",
    19: "MOD_FALLING",
    20: "MOD_SUICIDE",
    21: "MOD_TARGET_LASER",
    22: "MOD_TRIGGER_HURT",
    23: "MOD_GRAPPLE",
}

MOD_TO_WEAPON = {
    0: "unknown",
    1: "shotgun",
    2: "gauntlet",
    3: "machinegun",
    4: "grenade launcher",
    5: "grenade launcher",
    6: "rocket launcher",
    7: "rocket launcher",
    8: "plasma gun",
    9: "plasma gun",
    10: "railgun",
    11: "lightning gun",
    12: "BFG",
    13: "BFG",
    14: "drowning",
    15: "slime",
    16: "lava",
    17: "crushing",
    18: "telefrag",
    19: "falling damage",
    20: "suicide",
    21: "laser",
    22: "world",
    23: "grapple",
}

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

BOTS = [
    "anarki",
    "angel",
    "biker",
    "bitterman",
    "bones",
    "cadavre",
    "crash",
    "daemia",
    "sarge",
    "visor",
]


def is_bot(name):
    # Not perfect, but should work (unless someone takes a bot name)
    return name.lower() in BOTS


def parse_config():
    cfg = dict()
    with open("secrets.ini", "rt") as f:
        for line in f.readlines():
            if line[0] == ";":
                continue
            k, v = line.strip().split("=")
            cfg[k] = v
    return cfg


CONFIG = parse_config()


def parse_since(sincestr: str) -> Optional[datetime]:
    proto_date = None  # if this is a date, it'll be rounded to start-of-day
    if sincestr == "today":
        proto_date = datetime.now()
    elif sincestr == "week":
        proto_date = datetime.now()
        proto_date = proto_date - timedelta(days=proto_date.weekday())

    try:
        # Give the date parser a shot
        proto_date = parse(sincestr)
    except ValueError:
        pass

    if proto_date is not None:
        return proto_date.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        return proto_date
