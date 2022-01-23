import json
import logging
import operator
from io import StringIO

import redis
from dateutil.parser import parse

from q3constants import CONFIG, IX_WORLD, MOD_TO_WEAPON, TZ, is_bot

logger = logging.getLogger(__name__)


def render_name(name):
    if name is None:
        return "<unknown>❓"
    elif is_bot(name):
        return f"{name} 🤖"
    else:
        return name


def find_winners(scores):
    winscore = max(scores.values())
    return [k for k, v in scores.items() if v == winscore]


def render_winners(winners):
    if len(winners) == 1:
        return winners[0]
    else:
        return f"{', '.join(map(render_name, winners))} (shared victory)"


class Q3LogParse(object):
    def __init__(self):
        rhost = CONFIG.get("redishost", "q3redis")
        rport = int(CONFIG.get("redisport", "6379"))
        rdb = int(CONFIG.get("redisdb", "0"))
        self.r = redis.Redis(host=rhost, port=rport, db=rdb)
        self.scores = dict()
        self.games = dict()
        self.last_start = None
        self.last_map = None
        self.last_safe_idx = None

    def handle_message(self, idx, message):
        payload = message["content"]
        try:
            payload = json.loads(payload)
        except json.decoder.JSONDecodeError:
            logger.error(f"{payload} isn't JSON")
            return True

        tokens = (None, None, message["action"])

        ts = parse(payload["timestamp"]).astimezone(TZ)
        curts = self.last_start
        # This is the action!
        if tokens[2] == "ShutdownGame":  # happens after the scores have been published
            if curts in self.games and len(self.scores) > 1:
                self.games[curts]["scores"] = self.scores
                winners = find_winners(self.scores)
                self.games[curts]["winners"] = winners
                self.games[curts]["duration"] = (
                    self.games[curts]["ended"] - self.games[curts]["started"]
                )
                logger.info(
                    f"Game {self.last_map}@{ts} had {len(self.scores)} players,"
                    f" and {render_winners(winners)} won"
                )
            else:
                if curts in self.games:
                    del self.games[curts]

            self.scores = dict()  # commit and reset
            self.last_map = None
            self.last_start = None
        elif tokens[2] == "InitGame":
            # Start of game
            self.last_start = ts
            self.last_map = payload["mapname"]
            self.last_safe_idx = idx
            self.games[ts] = payload
            self.games[ts]["started"] = ts

            self.games[ts]["fraglimit"] = int(self.games[ts].get("fraglimit", 100))

            # Add structures we'll fill later
            self.games[ts]["winners"] = list()
            self.games[ts]["kills"] = dict()
            self.games[ts]["weapons"] = dict()
        elif tokens[2] == "Exit":
            # At end of gameplay, but before scores are published
            if curts in self.games:
                self.games[curts]["reason"] = payload["reason"].lower()[:-1]
                self.games[curts]["ended"] = ts
        elif tokens[2] == "Score":
            # Scores are published one-by-one before ShutdownGame
            self.scores[payload["n"]] = payload["score"]
        elif tokens[2] == "Kill":
            # On each kill
            if payload["clientid"] == IX_WORLD:  # falling damage, etc.
                name_ = payload["targetn"]
            else:
                name_ = payload["n"]

            mod = MOD_TO_WEAPON.get(int(payload["methodid"]), "unknown")
            tgt_ = payload["targetn"]
            if curts in self.games:
                self.games[curts]["kills"].setdefault(name_, dict()).setdefault(tgt_, 0)
                self.games[curts]["kills"][name_][tgt_] += 1
                if name_ != tgt_:  # only count actual kills
                    self.games[curts]["weapons"].setdefault(name_, dict()).setdefault(
                        mod, 0
                    )

                    self.games[curts]["weapons"][name_][mod] += 1

        return True

    def player_meta(self, since=None):
        """
        Get:
            - player kills dictionary; player -> target -> kills
            - player games stats dictionary; player -> stat -> dict()

        Optionally since some datetime.
        """
        plkill = dict()
        plgames = dict()
        plweapons = dict()

        for gts, data in self.games.items():
            if since is not None and gts < since:
                continue
            if "scores" not in data:
                continue

            for pl, score in data["scores"].items():
                plgames.setdefault(pl, dict()).setdefault("games", list())
                plgames[pl].setdefault("mapscore", dict())

                plgames[pl]["games"].append(gts)
                plgames[pl]["mapscore"].setdefault(data["mapname"], 0)
                plgames[pl]["mapscore"][data["mapname"]] += score

            for pl in data["winners"]:
                plgames[pl].setdefault("wins", list())
                plgames[pl]["wins"].append(gts)

            for pl, dtgt in data["kills"].items():
                plkill.setdefault(pl, dict())

                for tgt, kills in dtgt.items():
                    plkill[pl].setdefault(tgt, 0)
                    plkill[pl][tgt] += kills

            for pl, dmod in data["weapons"].items():
                plweapons.setdefault(pl, dict())

                for tgt, mod in dmod.items():
                    plweapons[pl].setdefault(tgt, 0)
                    plweapons[pl][tgt] += mod

        return plkill, plgames, plweapons

    def player_wins(self, plgames):
        """Invert games to get a wins/games stat"""

        plwin_ = list()
        # For each player, calculate wins, games, and win percentage
        for pl, data in plgames.items():
            games = len(data["games"])
            if "mapscore" in data:
                mapsc_ = list(
                    sorted(
                        data["mapscore"].items(),
                        key=operator.itemgetter(1),
                        reverse=True,
                    )
                )[0]
            else:
                mapsc_ = (None, None)  # fallback

            wins = 0
            frac = 0.0

            if "wins" in data:
                wins = len(data["wins"])
                frac = wins / games

            plwin_.append((pl, frac, wins, games, mapsc_[0]))

        return {
            pl: (frac, wins, games, bestmap)
            for pl, frac, wins, games, bestmap in sorted(
                plwin_, key=operator.itemgetter(1), reverse=True
            )
        }

    def stats_text(self, since=None):
        if since is not None and since.tzinfo is None:
            since = TZ.localize(since)
        player_kills, player_games, player_weapons = self.player_meta(since)
        player_wins = self.player_wins(player_games)
        first_game = min(self.games.keys())
        since_ = max(first_game, since) if since is not None else first_game
        sincegames = list(filter(lambda x: x >= since_, self.games.keys()))
        output = StringIO()
        output.write(
            f"**{len(sincegames)}** games recorded since "
            f"{since_:%Y-%m-%d %H:%M}, "
            f"_{len(player_kills)}_ players\n"
        )

        for winner, wins_ in player_wins.items():
            frac, wins, games, bestmap = wins_
            winner_ = render_name(winner)
            weapons_ = sorted(
                player_weapons[winner].items(),
                key=operator.itemgetter(1),
                reverse=True,
            )

            map_part = f"Best map: _{bestmap}_\n" if bestmap is not None else ""

            weap_part = (
                f"Favourite weapon: {weapons_[0][0]} (_{weapons_[0][1]}_ kills)\n"
                if len(weapons_) > 0
                else ""
            )

            output.write(
                f"\n**{winner_}**: {wins} wins in {games} games"
                f" ({100*frac:.0f}% win ratio)\n"
                f"{map_part}"
                f"{weap_part}"
            )
            targets_ = dict(
                sorted(
                    player_kills[winner].items(),
                    key=operator.itemgetter(1),
                    reverse=True,
                )
            )
            self.stringify_kills(output, targets_)

        output.seek(0)
        return output.read()

    def stringify_kills(self, output, targets_):
        i = 1
        for target, kills in targets_.items():
            target_ = render_name(target)
            output.write(f" {i}) {target_}: _{kills}_ kills\n")
            i += 1

    def parse_log(self):
        # already_parsed = self.r.get("q3log_lastparse")
        parse_from = 0
        # if already_parsed is not None:
        #     parse_from = int(already_parsed)

        # log_len = self.r.llen("q3log")
        parse_to = -1
        # if log_len is not None:
        #     parse_to = int(log_len)

        lines = self.r.lrange("q3log", parse_from, parse_to)

        for ix, ln in enumerate(lines):
            self.handle_message(ix, json.loads(ln.decode("utf-8")))

        # Clean up orphaned games
        to_delete = list()
        for ts, game in self.games.items():
            if "scores" not in game:
                to_delete.append(ts)

        for ts in to_delete:
            del self.games[ts]


def main():
    parsed = Q3LogParse()
    parsed.parse_log()
    print(parsed.stats_text(parse("2021-01-01")))


if __name__ == "__main__":
    main()
