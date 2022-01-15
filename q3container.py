import json
import logging

import docker
import paho.mqtt.client as mqtt
import redis

from q3constants import CONFIG

logging.basicConfig(
    filename="mqtt.log",
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


MQTTSERVER = CONFIG["mqtt"]
DCK = docker.from_env()
SHUTDOWN = False


def handle_log():
    """Attach to container, and follow all incoming log lines"""
    q3 = DCK.containers.get("q3server")
    logger.info(f"Following container {q3}")
    for line in q3.logs(tail=0, follow=True, stream=True, timestamps=True):
        yield line.decode("utf-8").strip()


def handle_log_all():
    """Replay entire log, then stop following"""
    q3 = DCK.containers.get("quake3e_ded")
    logger.info(f"Replaying container {q3}")
    for line in q3.logs(stream=True, follow=False, timestamps=True):
        yield line.decode("utf-8").strip()


def parse_combined_line(line):
    line = " ".join(line)
    skip = 1 if line[0] == "\\" else 0
    fixed = line[skip:].split("\\")
    keys = fixed[::2]
    values = fixed[1::2]

    return dict(zip(keys, values))


def parse_scores(line):
    obj = dict()
    # line looks like "score: NUM ping: NUM client: ID NAME".split(" ")
    i = 0
    while i < len(line):
        if line[i] == "score:":
            obj["score"] = int(line[i + 1])
        elif line[i] == "ping:":
            obj["ping"] = int(line[i + 1])
        elif line[i] == "client:":
            obj["clientid"] = line[i + 1]
            obj["n"] = line[i + 2]
        i += 1

    return obj


def parse_line(line):
    tokens = line.split(" ")
    buildobj = {"timestamp": tokens[0], "line": line}
    if tokens[1][-1] != ":":
        buildobj["action"] = "info"
        return None  # shortcut

    action = tokens[1][:-1]

    kval = {"timestamp": tokens[0], "line": line}
    buildobj["action"] = action
    buildobj["content"] = json.dumps(kval)  # dummy value, to ensure this is set
    if len(tokens) < 3:
        return buildobj

    idx = tokens[2]  # might not be an index, we'll figure it out
    if action in (
        "Item",
        "Hunk_Clear",
        "IP",
    ):
        return None
    elif action == "ClientConnect":
        buildobj["action"] = "Client"
        kval["action"] = "Connect"
        kval["clientid"] = idx
        buildobj["clientid"] = idx
        # self.clients[tokens[2]] = dict()  # unknown
        # no msg
    elif action == "ClientBegin":
        buildobj["action"] = "Client"
        kval["action"] = "Begin"
        kval["clientid"] = idx
        buildobj["clientid"] = idx
    elif action == "ClientDisconnect":
        buildobj["action"] = "Client"
        kval["action"] = "Disconnect"
        kval["clientid"] = idx
        buildobj["clientid"] = idx
    elif action == "ClientUserinfoChanged":
        buildobj["action"] = "Client"
        kval.update(parse_combined_line(tokens[3:]))
        kval["action"] = "InfoChanged"
        kval["clientid"] = idx
        buildobj["clientid"] = idx
    elif action == "score":
        buildobj["action"] = "Score"
        kval.update(parse_scores(tokens[1:]))
    elif action == "InitGame":
        kval.update(parse_combined_line(tokens[2:]))
    elif action == "Kill":
        kval["clientid"] = idx
        kval["targetid"] = tokens[3]
        kval["methodid"] = tokens[4][:-1]  # strip trailing ":"
        kval["n"] = tokens[5]
        kval["targetn"] = tokens[7]
        kval["method"] = tokens[9]
    elif action == "Exit":
        kval["reason"] = " ".join(tokens[2:])
    elif action == "Server":
        kval["mapname"] = idx
    else:
        logger.debug(f"Not sending {action}")
        return None

    buildobj["content"] = json.dumps(kval).encode("utf-8")
    return buildobj


def redis_line(buildobj):
    if isinstance(buildobj["content"], bytes):
        buildobj["content"] = buildobj["content"].decode("utf-8")
    return json.dumps(buildobj)


def on_connect(client, userdata, flags, rc):
    logger.info("Connected with result code " + str(rc))
    client.subscribe("q3bot/#")


def on_message(client, userdata, msg):
    global SHUTDOWN
    tokens = msg.topic.split("/")
    if tokens[0] != "q3bot":
        logger.info("q3bot", msg.topic + " " + str(msg.payload))
        return

    if tokens[1] == "shutdown":
        logger.info("Got shutdown signal")
        SHUTDOWN = True
    else:
        logger.info("q3bot", msg.topic + " " + str(msg.payload))


def on_log(client, userdata, level, buff):
    console.debug(buff)


def main():
    src = mqtt.Client("q3bot")
    src.on_connect = on_connect
    src.on_message = on_message
    src.enable_logger(logger)
    src.connect(MQTTSERVER)

    src.loop_start()
    src.publish("q3server/status", "hello", retain=True)
    src.will_set("q3server/status", "offline", retain=True)

    rhost = CONFIG.get("redishost", "localhost")
    rport = int(CONFIG.get("redisport", "6379"))
    rdb = int(CONFIG.get("redisdb", "0"))
    r = redis.Redis(host=rhost, port=rport, db=rdb)

    for line in handle_log():
        obj = parse_line(line)
        if obj is None:
            continue
        logger.info(f"Publishing {obj}")
        path = f"q3server/log/{obj['action']}"
        if "clientid" in obj:
            path += f"/{obj['clientid']}"
        if "content" in obj:
            res = src.publish(path, obj["content"], qos=2)
            res.wait_for_publish()

            robj = redis_line(obj)
            r.rpush("q3log", robj)
        else:
            logger.error(f"No content in {obj}")
        if SHUTDOWN:
            break

    src.loop_stop()


if __name__ == "__main__":
    main()
