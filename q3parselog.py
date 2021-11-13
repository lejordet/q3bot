import json
import logging
import operator
from io import StringIO

import redis
from dateutil.parser import parse

from q3constants import IX_WORLD, TZ
from q3container import handle_log_all, parse_line

logger = logging.getLogger(__name__)


def fill(reload=False):
    r = redis.Redis()
    lines = 0
    total = 0
    if reload:
        r.delete("q3log")
    for log in handle_log_all():
        p = parse_line(log)
        total += 1
        if p is not None:
            if isinstance(p["content"], bytes):
                p["content"] = p["content"].decode("utf-8")  # we need the string again
            r.rpush("q3log", json.dumps(p))
            lines += 1
    print(f"Pushed {lines}/{total} lines to the log")


def find_winners(scores):
    winscore = max(scores.values())
    return [k for k, v in scores.items() if v == winscore]


def render_winners(winners):
    if len(winners) == 1:
        return winners[0]
    else:
        return f"{', '.join(winners)} (shared victory)"


class Q3LogParse(object):
    def __init__(self):
        self.scores = dict()
        self.games = dict()
        self.player_wins = dict()
        self.player_kills = dict()
        self.last_start = None
        self.last_map = None

    def handle_message(self, message):
        payload = message["content"]
        try:
            payload = json.loads(payload)
        except json.decoder.JSONDecodeError:
            logger.error(f"{payload} isn't JSON")
            return True

        tokens = (None, None, message["action"])

        ts = parse(payload["timestamp"]).astimezone(TZ)

        # This is the action!
        if tokens[2] == "ShutdownGame":  # happens after the scores have been published
            curts = self.last_start
            if curts in self.games and len(self.scores) > 1:
                self.games[curts]["scores"] = self.scores
                winners = find_winners(self.scores)
                self.games[curts]["winners"] = winners
                logger.info(self.games[curts])
                self.games[curts]["duration"] = (
                    self.games[curts]["ended"] - self.games[curts]["started"]
                )
                logger.info(
                    f"Game {self.last_map}@{ts} had {len(self.scores)} players,"
                    f" and {render_winners(winners)} won"
                )
                for pl in winners:
                    curwin = self.player_wins.setdefault(pl, 0)
                    self.player_wins[pl] = curwin + 1
            else:
                if curts in self.games:
                    del self.games[curts]
                    logger.info(f"Discarded game {ts}, no players")

            self.scores = dict()  # commit and reset
            self.last_map = None
            self.last_start = None
        elif tokens[2] == "InitGame":
            self.last_start = ts
            self.last_map = payload["mapname"]

            self.games[ts] = payload
            self.games[ts]["started"] = ts

            self.games[ts]["fraglimit"] = int(self.games[ts].get("fraglimit", 100))
        elif tokens[2] == "Exit":
            curts = self.last_start
            if curts in self.games:
                self.games[curts]["reason"] = payload["reason"].lower()[:-1]
                self.games[curts]["ended"] = ts
        elif tokens[2] == "Score":
            self.scores[payload["n"]] = payload["score"]
        elif tokens[2] == "Kill":
            if payload["clientid"] == IX_WORLD:  # falling damage, etc.
                name_ = payload["targetn"]
            else:
                name_ = payload["n"]
            tgt_ = payload["targetn"]

            # make sure nested dict is filled
            self.player_kills.setdefault(name_, dict()).setdefault(tgt_, 0)
            self.player_kills[name_][tgt_] += 1

        return True

    def __str__(self):
        output = StringIO()
        output.write(f"**{len(self.games)}** games recorded, _{len(self.player_kills)}_ players\n")

        winners_ = dict(
            sorted(self.player_wins.items(), key=operator.itemgetter(1), reverse=True)
        )
        non_winners_ = set(self.player_kills.keys()).difference(self.player_wins.keys())

        for winner, wins in winners_.items():
            output.write(f"**{winner}**: {wins} wins\n")
            targets_ = dict(sorted(self.player_kills[winner].items(), key=operator.itemgetter(1), reverse=True))
            i = 1
            for target, kills in targets_.items():
                output.write(f" {i}) {target}: _{kills}_ kills\n")
                i += 1

        for n in non_winners_:
            output.write(f"**{n}**\n")
            targets_ = dict(sorted(self.player_kills[n].items(), key=operator.itemgetter(1), reverse=True))
            i = 1
            for target, kills in targets_.items():
                output.write(f" {i}) {target}: _{kills}_ kills\n")
                i += 1

        output.seek(0)
        return output.read()


def reparse_log():
    r = redis.Redis()
    lines = r.lrange("q3log", 0, -1)

    parsed = Q3LogParse()
    for ln in lines:
        parsed.handle_message(json.loads(ln.decode("utf-8")))

    return parsed


def main():
    parsed = reparse_log()
    print(parsed)


if __name__ == "__main__":
    main()
