import pytz

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


def parse_config():
    cfg = dict()
    with open("secrets.ini", "rt") as f:
        for line in f.readlines():
            k, v = line.strip().split("=")
            cfg[k] = v
    return cfg


CONFIG = parse_config()
