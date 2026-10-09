"""
Termux Pokemon battle bot — FULLY BOT CONTROLLED (Telethon)

TERMUX
  python bot.py      -> sirf "Bot token do" poochega (ek baar; sessions/token.txt me save)

BOT ME (pehli baar)
  /start             -> jo pehla banda /start kare wahi OWNER
  /count 3           -> kitni ID add karni hai (optional, chain login)
  /add               -> login: bot number maangega -> code -> (2FA password)
                        code aise bhejo: 4 4 8 4 7   (digits ke beech space/dash)
  /name 2 Ryuzen     -> ACC2 ka IN-GAME naam map karo (limit detect ke liye)
  /ids  /logout 2

CHALANE KE COMMANDS (sab bot me)
  /run /stop  | /a (accept/training) | /s (self) | /next
  /sc @group  /chats  /rm @group  /status  /mode random|strongest|preferred  /ping /help

LOGIC
  * ACC1 = MAIN (farmer, PD nahi kamata) -> KABHI limited mark nahi hoga
  * ACC2,3,4... = earners -> inki daily limit lagti hai
  * "Daily battle limit reached" dikha -> current opponent limited -> auto ACC3 -> ACC4 -> ACC5...
  * saari opponent ID limited -> auto ACCEPT (training) mode (bot DM par notify)
  * 1 hi ID ho -> khud ACCEPT (training) mode
  * self mode me tag karne layak koi msg na mile -> opponent khud random msg
    ("hi", "hello", "yo"...) bhejta hai, ACC1 usi par reply karke /challenge deta hai
  * /sc se chat add -> purana chat hatta nahi, koi group leave nahi hota

pip install telethon
"""

import asyncio
import glob
import json
import os
import random
import re
import sqlite3
import sys
import time
from datetime import datetime

from telethon import TelegramClient, events, Button
from telethon.errors import (PhoneCodeInvalidError, PhoneCodeExpiredError,
                             SessionPasswordNeededError, PasswordHashInvalidError,
                             PhoneNumberInvalidError, FloodWaitError)
from telethon.utils import get_display_name, get_peer_id

# ========================= CONFIG =========================
API_ID = 2227210
API_HASH = "5372738ac54c8940b091817ffb4b4e58"

SESSIONS_DIR = "sessions"
ALIAS_FILE = "names.json"
MAX_IDS = 9

DEFAULT_CHATS = ["@dbbdjdksksksk"]
AUTO_START = False

# ---- MAIN ID rules ----
MAIN_NEVER_LIMITED = True        # ACC1 kabhi limited nahi (sirf farming, PD nahi kamata)
ROTATE_ON_ANY_LIMIT = True       # limit msg dikha -> turant agli ID par shift
ALL_LIMITED_ACTION = "accept"    # sab opponent limited -> "accept" | "stop"

# ---- accept (training) ----
ACCEPT_ANYONE = True
ALLOWED_CHALLENGERS = []
READY_DELAY_MIN = 2.0
READY_DELAY_MAX = 3.0

# ---- self mode ----
CHALLENGE_TEXT = "/challenge"
RANDOM_MSGS = ["hi", "hello", "hey", "yo", "hmm", "ok", "sup", "oye", "hi hi", "👋"]
RESTART_DELAY = 8
MAX_ROUNDS = 0
MAX_FORMAT_RETRIES = 3
REFRESH_TARGET_EACH_ROUND = False

# ---- battle ----
MOVE_STRATEGY = "random"
PREFERRED_MOVES = ["Psychic", "Aura Sphere", "Psycho Cut", "Ancient Power"]
MOVE_DELAY = 1.2
SWITCH_DELAY = 0.8
READY_BUTTON = "Ready"
NOTIFY_BATTLE = False            # True = har battle event bot DM me

NON_MOVE_BUTTONS = {"poke balls", "run", "pokemon", "view team",
                    "settings ⚙️", "edit team", "ready"}

BATTLE_MARKER = "has challenged"
SWITCH_MARKER = "choose your next pokemon"
END_MARKERS = ("has defeated", "prize:", "battle ended", "battle cancelled",
               "this battle is over")
LIMIT_MARKERS = ("daily battle limit reached", "battle limit reached",
                 "no prize will be given")
REJECT_REPLIES = ("this battle is over", "not your turn", "not your battle",
                  "you are not", "battle has ended")
FORMAT_ERRORS = ("format: reply to a user", "reply to a user")

TURN_RE = re.compile(r"current turn:\s*(.+)", re.IGNORECASE)
POWER_RE = re.compile(r"power:\s*(\d+)", re.IGNORECASE)
CHALLENGER_RE = re.compile(r"^(.*?)\s+has challenged\s+(.*?)[.!\n]", re.IGNORECASE)
DEFEAT_RE = re.compile(r"([^\n]+?)\s+has defeated\s+([^\n.!]+)", re.IGNORECASE)

HISTORY_LIMIT = 100
# ==========================================================

ACC = []
BOT = {"client": None, "owner": None, "username": ""}
LOGIN = {"step": None, "idx": None, "client": None, "phone": None,
         "hash": None, "want": 0}

done_actions = set()
dead_msgs = set()

S = {
    "running": False, "role": "accept", "strategy": MOVE_STRATEGY,
    "main": 0, "opp": None,
    "chats": {}, "targets": {}, "rounds": {}, "format_retries": {},
    "last_restart": {}, "restarting": set(), "fixing": set(),
    "accepted": 0, "rotating": False,
}


def log(tag, text):
    print(f"[{datetime.now():%H:%M:%S}] [{tag}] {text}", flush=True)


async def _dm(text):
    c, o = BOT["client"], BOT["owner"]
    if not c or not o:
        return
    try:
        await c.send_message(o, text)
    except Exception:
        pass


def notify(text, force=True):
    log("BOT", re.sub(r"\*+", "", text).replace("\n", " | "))
    if force or NOTIFY_BATTLE:
        try:
            asyncio.create_task(_dm(text))
        except RuntimeError:
            pass


def main_acc():
    return ACC[S["main"]] if ACC and S["main"] < len(ACC) else None


def opp_acc():
    return ACC[S["opp"]] if (S["opp"] is not None and S["opp"] < len(ACC)) else None


def total_rounds():
    return sum(S["rounds"].values())


# ---------------------- FILES / TOKEN / ALIAS ----------------------
def fpath(name):
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    return os.path.join(SESSIONS_DIR, name)


def session_path(name):
    name = re.sub(r"[^\w\-.]+", "_", str(name)).strip(" .") or "session"
    return fpath(name)


def assert_unlocked(path):
    f = path + ".session"
    if not os.path.isfile(f):
        return
    try:
        con = sqlite3.connect(f, timeout=1)
        con.execute("BEGIN IMMEDIATE")
        con.rollback()
        con.close()
    except sqlite3.OperationalError:
        raise SystemExit(f"[SYS] '{f}' locked — purana script band karo.")


def make_client(session_name):
    p = session_path(session_name)
    assert_unlocked(p)
    return TelegramClient(p, API_ID, API_HASH, connection_retries=5,
                          retry_delay=2, request_retries=5, timeout=15)


def ask_token() -> str:
    tok = (os.environ.get("BOT_TOKEN") or "").strip()
    if tok:
        print("BOT_TOKEN env se mil gaya")
        return tok
    tf = fpath("token.txt")
    if os.path.isfile(tf):
        tok = open(tf).read().strip()
        if tok:
            print(f"token.txt se liya ({tok[:12]}...)")
            return tok
    while True:
        tok = input("Bot token do (BotFather se): ").strip()
        if re.fullmatch(r"\d{6,}:[\w-]{20,}", tok):
            open(tf, "w").write(tok)
            return tok
        print("token galat lag raha hai, dobara do.")


def load_owner():
    env = (os.environ.get("OWNER_ID") or "").strip()
    if env.isdigit():
        return int(env)
    f = fpath("owner.txt")
    if os.path.isfile(f):
        raw = open(f).read().strip()
        if raw.isdigit():
            return int(raw)
    return None


def save_owner(uid):
    open(fpath("owner.txt"), "w").write(str(uid))


def load_aliases():
    try:
        return {int(k): v for k, v in json.load(open(fpath(ALIAS_FILE))).items()}
    except Exception:
        return {}


def save_alias(slot, name):
    data = load_aliases()
    cur = data.get(slot, [])
    if name not in cur:
        cur.append(name)
    data[slot] = cur
    json.dump({str(k): v for k, v in data.items()}, open(fpath(ALIAS_FILE), "w"))


def free_slot():
    used = {int(re.search(r"acc(\d+)", a["session"]).group(1)) for a in ACC}
    for i in range(1, MAX_IDS + 1):
        if i not in used:
            return i
    return None


# ---------------------- PARSING ----------------------
def norm(s):
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def all_buttons(msg):
    if not msg or not msg.reply_markup:
        return []
    out = []
    try:
        for row in msg.reply_markup.rows:
            for b in row.buttons:
                out.append(b.text or "")
    except Exception:
        pass
    return out


def is_blank(x):
    return not x.strip() or not re.search(r"[0-9A-Za-z]", x)


def move_buttons(msg):
    return [b for b in all_buttons(msg)
            if b.strip().lower() not in NON_MOVE_BUTTONS and not is_blank(b)]


def switch_buttons(msg):
    out = [b for b in all_buttons(msg)
           if not is_blank(b) and b.strip().lower() not in NON_MOVE_BUTTONS
           and re.fullmatch(r"\d+", b.strip())]
    return out or [b for b in all_buttons(msg)
                   if not is_blank(b) and b.strip().lower() not in NON_MOVE_BUTTONS]


def move_powers(text):
    powers, lines = {}, (text or "").splitlines()
    for i, line in enumerate(lines):
        m = POWER_RE.search(line)
        if m and i > 0:
            nm = re.sub(r"\[.*?\]", "", lines[i - 1]).strip()
            if nm:
                powers[nm.lower()] = int(m.group(1))
    return powers


def pick_move(msg):
    moves = move_buttons(msg)
    if not moves:
        return None
    if S["strategy"] == "random":
        return random.choice(moves)
    if S["strategy"] == "strongest":
        p = move_powers(msg.message)
        return max(moves, key=lambda m: p.get(m.strip().lower(), -1)) if p else moves[0]
    for want in PREFERRED_MOVES:
        for m in moves:
            if want.lower() in m.lower():
                return m
    return random.choice(moves)


def my_turn(text, name):
    m = TURN_RE.search(text or "")
    return bool(m) and norm(m.group(1)) == norm(name)


def my_battle(text, name):
    return norm(name) in norm(text)


def battle_over(text):
    return any(k in norm(text) for k in END_MARKERS)


def limit_reached(text):
    return any(k in norm(text) for k in LIMIT_MARKERS)


def is_format_error(text):
    return any(k in norm(text) for k in FORMAT_ERRORS)


def challenger_name(text):
    m = CHALLENGER_RE.search((text or "").strip())
    return m.group(1).strip() if m else ""


def challenger_allowed(text):
    if ACCEPT_ANYONE and not ALLOWED_CHALLENGERS:
        return True
    who = norm(challenger_name(text))
    return any(norm(a).lstrip("@") in who for a in ALLOWED_CHALLENGERS)


def acc_names(a):
    out = {a.get("display") or "", a.get("username") or ""}
    out |= set(a.get("aliases") or [])
    return {norm(x) for x in out if x and len(norm(x)) >= 3}


def match_acc(name):
    """In-game naam -> ACC index (exact pehle, phir partial)."""
    n = norm(name)
    if not n:
        return None
    for i, a in enumerate(ACC):
        if n in acc_names(a):
            return i
    for i, a in enumerate(ACC):
        for nm in acc_names(a):
            if len(nm) >= 4 and (nm in n or n in nm):
                return i
    return None


def players():
    m = main_acc()
    if not m:
        return set()
    if S["role"] == "accept":
        return {m["name"]}
    o = opp_acc()
    return {m["name"]} | ({o["name"]} if o else set())


# ---------------------- CHATS ----------------------
async def resolve_for(client, cid):
    info = S["chats"].get(cid) or {}
    for ref in (info.get("ref"), cid):
        if ref is None:
            continue
        try:
            return await client.get_entity(ref)
        except Exception:
            continue
    return cid


async def add_chat(ref):
    m = main_acc()
    if not m:
        return None, "pehle /add se ID login karo"
    try:
        ent = await m["client"].get_entity(ref)
    except Exception as e:
        return None, f"resolve failed: {e}"
    try:
        cid = get_peer_id(ent)
    except Exception:
        cid = getattr(ent, "id", None)
    if cid is None:
        return None, "chat id nahi mila"
    uname = getattr(ent, "username", None)
    S["chats"][cid] = {"ref": f"@{uname}" if uname else cid,
                       "title": get_display_name(ent) or str(cid)}
    S["rounds"].setdefault(cid, 0)
    S["format_retries"].setdefault(cid, 0)
    return cid, S["chats"][cid]["title"]


def remove_chat(ref):
    key = str(ref).strip().lower()
    for cid, info in list(S["chats"].items()):
        if key in (str(cid).lower(), str(info["ref"]).lower(), info["title"].lower()):
            S["chats"].pop(cid, None)
            S["targets"].pop(cid, None)
            return info["title"]
    return None


def ctitle(cid):
    return (S["chats"].get(cid) or {}).get("title", str(cid))


# ---------------------- TARGET ----------------------
async def find_opp_message(cid):
    o, m = opp_acc(), main_acc()
    if not o or not m:
        return None
    chat = await resolve_for(m["client"], cid)
    try:
        async for msg in m["client"].iter_messages(chat, limit=HISTORY_LIMIT,
                                                  from_user=o["id"]):
            return msg.id
    except Exception:
        try:
            async for msg in m["client"].iter_messages(chat, limit=HISTORY_LIMIT):
                if msg.sender_id == o["id"]:
                    return msg.id
        except Exception:
            pass
    return None


async def send_random_msg(cid):
    o = opp_acc()
    if not o:
        return None
    try:
        chat = await resolve_for(o["client"], cid)
        txt = random.choice(RANDOM_MSGS)
        sent = await o["client"].send_message(chat, txt)
        log(o["name"], f"[{ctitle(cid)}] random msg '{txt}' (id={sent.id})")
        return sent.id
    except Exception as e:
        log(o["name"], f"[{ctitle(cid)}] random msg failed: {e}")
        return None


async def ensure_target(cid, force=False):
    if S["role"] != "self" or not opp_acc() or cid not in S["chats"]:
        return False
    if S["targets"].get(cid) and not force:
        return True
    S["targets"].pop(cid, None)
    if not force:
        mid = await find_opp_message(cid)
        if mid:
            S["targets"][cid] = mid
            return True
    mid = await send_random_msg(cid)
    if mid:
        await asyncio.sleep(1.2)
        S["targets"][cid] = mid
        return True
    notify(f"⚠️ {ctitle(cid)}: target nahi mila")
    return False


# ---------------------- ACTIONS ----------------------
async def dump_message(tag, event):
    msg = event.message
    try:
        sender = get_display_name(await event.get_sender()) or str(msg.sender_id)
    except Exception:
        sender = str(msg.sender_id)
    print("-" * 70, flush=True)
    log(tag, f"[{ctitle(event.chat_id)}] from={sender} | msg_id={msg.id}")
    print(msg.message or "<media/empty>", flush=True)
    b = all_buttons(msg)
    if b:
        log(tag, f"buttons={b}")
    print("-" * 70, flush=True)


async def refetch(client, cid, msg):
    try:
        f = await client.get_messages(await resolve_for(client, cid), ids=msg.id)
        return f or msg
    except Exception:
        return msg


async def click_once(acc, cid, msg, button_text, key_state, expect_turn=None):
    mkey = (cid, msg.id)
    if mkey in dead_msgs or not S["running"]:
        return
    key = (acc["name"], cid, msg.id, key_state)
    if key in done_actions:
        return
    done_actions.add(key)

    fresh = await refetch(acc["client"], cid, msg)
    if battle_over(fresh.message or ""):
        dead_msgs.add(mkey)
        if S["role"] == "self" and main_acc() and acc["name"] == main_acc()["name"]:
            asyncio.create_task(restart_battle(cid, "ended before click"))
        return
    if expect_turn and not my_turn(fresh.message or "", expect_turn):
        return
    if button_text not in all_buttons(fresh):
        return

    try:
        res = await fresh.click(text=button_text)
        ans = getattr(res, "message", None) or ""
        log(acc["name"], f"[{ctitle(cid)}] clicked '{button_text}'"
                         + (f" | bot: {ans}" if ans else ""))
        if limit_reached(ans):
            await on_limit(ans, cid)
        if any(r in norm(ans) for r in REJECT_REPLIES):
            dead_msgs.add(mkey)
            if S["role"] == "self" and main_acc() and acc["name"] == main_acc()["name"]:
                asyncio.create_task(restart_battle(cid, "rejected click"))
    except Exception as e:
        done_actions.discard(key)
        log(acc["name"], f"[{ctitle(cid)}] click failed: {e}")


# ---------------------- LIMIT + ROTATE ----------------------
def next_free_opponent():
    for i in range(len(ACC)):
        if i != S["main"] and not ACC[i]["limited"]:
            return i
    return None


async def rotate_opponent(reason=""):
    if S["rotating"]:
        return
    S["rotating"] = True
    try:
        nxt = next_free_opponent()
        if nxt is None:
            if ALL_LIMITED_ACTION == "accept":
                S["role"] = "accept"
                S["opp"] = None
                S["targets"].clear()
                notify("🚫 saari earner ID ki limit khatam\n"
                       "➡ **ACCEPT (training) mode on** — random challenges accept honge")
            else:
                S["running"] = False
                notify(f"🚫 saari ID limited — stopped | rounds {total_rounds()}")
            return
        S["opp"] = nxt
        S["targets"].clear()
        done_actions.clear()
        dead_msgs.clear()
        left = sum(1 for i, a in enumerate(ACC) if i != S["main"] and not a["limited"])
        notify(f"➡ **opponent switch**: {ACC[nxt]['display']} ({ACC[nxt]['name']}) "
               f"{reason}\nbaki free IDs: {left}")
        await asyncio.sleep(3)
        for cid in list(S["chats"]):
            if not S["running"]:
                break
            await ensure_target(cid, force=True)
            await send_challenge(cid, "after switch")
    finally:
        S["rotating"] = False


async def on_limit(text, cid):
    """
    ACC1 (main) PD nahi kamata -> uski limit nahi hoti.
    Jo bhi limit msg aaye, wo current opponent (earner) ka maana jayega.
    """
    m = DEFEAT_RE.search(text or "")
    winner = (m.group(1).strip() if m else "")
    loser = (m.group(2).strip() if m else "")

    hit = match_acc(winner)
    if hit is None:
        hit = match_acc(loser)
    # main ID ki limit possible nahi -> opponent par daal do
    if hit is None or (MAIN_NEVER_LIMITED and hit == S["main"]):
        hit = S["opp"]

    who = winner or loser or "unknown"

    if hit is not None and hit < len(ACC) and hit != S["main"]:
        if ACC[hit]["limited"] and S["role"] == "self" and hit == S["opp"]:
            pass
        ACC[hit]["limited"] = True
        notify(f"🚫 **limit**: {ACC[hit]['display']} ({ACC[hit]['name']}) "
               f"| game name: {who} — {ctitle(cid)}")
    else:
        notify(f"🚫 limit msg: {who} — {ctitle(cid)}")

    if S["role"] != "self":
        return

    # safety: current opponent ko bhi limited maan lo taaki wahi dobara na chune
    if ROTATE_ON_ANY_LIMIT and S["opp"] is not None and S["opp"] < len(ACC):
        ACC[S["opp"]]["limited"] = True

    await rotate_opponent("(limit)")


# ---------------------- SELF LOOP ----------------------
async def send_challenge(cid, reason=""):
    m = main_acc()
    if not S["running"] or S["role"] != "self" or not m or not opp_acc():
        return
    if cid not in S["chats"]:
        return
    if not S["targets"].get(cid):
        await ensure_target(cid)
    if not S["targets"].get(cid):
        return
    S["rounds"][cid] = S["rounds"].get(cid, 0) + 1
    try:
        chat = await resolve_for(m["client"], cid)
        sent = await m["client"].send_message(chat, CHALLENGE_TEXT,
                                             reply_to=S["targets"][cid])
        log(m["name"], f"[{ctitle(cid)}] round {S['rounds'][cid]}: challenge -> "
                       f"{opp_acc()['name']} (reply {S['targets'][cid]}, "
                       f"msg {sent.id}) {reason}")
    except Exception as e:
        notify(f"⚠️ {ctitle(cid)}: challenge failed — {e}")
        S["targets"].pop(cid, None)


async def fix_target_and_retry(cid):
    if cid in S["fixing"] or not S["running"] or S["role"] != "self":
        return
    if S["format_retries"].get(cid, 0) >= MAX_FORMAT_RETRIES:
        notify(f"⚠️ {ctitle(cid)}: format error repeat — is chat me ruk gaya")
        return
    S["fixing"].add(cid)
    S["format_retries"][cid] = S["format_retries"].get(cid, 0) + 1
    try:
        await ensure_target(cid, force=True)
        await asyncio.sleep(2)
        await send_challenge(cid, "target fixed")
    finally:
        S["fixing"].discard(cid)


async def restart_battle(cid, reason="battle over"):
    if S["role"] != "self":
        log("SYS", f"[{ctitle(cid)}] {reason} -> next challenge ka wait")
        return
    if not S["running"] or cid in S["restarting"] or S["rotating"]:
        return
    if time.time() - S["last_restart"].get(cid, 0.0) < RESTART_DELAY:
        return
    if MAX_ROUNDS and S["rounds"].get(cid, 0) >= MAX_ROUNDS:
        return
    S["restarting"].add(cid)
    try:
        log("SYS", f"[{ctitle(cid)}] {reason} -> next round in {RESTART_DELAY}s")
        await asyncio.sleep(RESTART_DELAY)
        if not S["running"] or cid not in S["chats"] or S["rotating"]:
            return
        if REFRESH_TARGET_EACH_ROUND:
            await ensure_target(cid, force=True)
        await send_challenge(cid, "auto-restart")
        S["last_restart"][cid] = time.time()
    finally:
        S["restarting"].discard(cid)


async def start_loop():
    if not ACC:
        notify("pehle /add se koi ID login karo")
        return
    S["running"] = True
    S["accepted"] = 0
    done_actions.clear()
    dead_msgs.clear()
    for cid in S["chats"]:
        S["rounds"][cid] = 0
        S["format_retries"][cid] = 0
    if MAIN_NEVER_LIMITED and main_acc():
        main_acc()["limited"] = False
    if S["role"] == "self":
        if len(ACC) < 2:
            S["role"], S["opp"] = "accept", None
        elif S["opp"] is None or ACC[S["opp"]]["limited"]:
            S["opp"] = next_free_opponent()
            if S["opp"] is None:
                S["role"] = "accept"
    o = opp_acc()
    notify(f"▶️ **started** [{S['role']}]\nmain (farmer): {main_acc()['display']}"
           + (f"\nopponent (earner): {o['display']}" if o else "")
           + f"\nchats: {', '.join(c['title'] for c in S['chats'].values()) or '-'}")
    if S["role"] == "self":
        for cid in list(S["chats"]):
            await ensure_target(cid, force=True)
            await send_challenge(cid, "start")


async def stop_loop():
    S["running"] = False
    S["restarting"].clear()
    notify(f"⏹ **stopped** | rounds {total_rounds()} | accepted {S['accepted']}")


# ---------------------- BATTLE HANDLER ----------------------
async def handle(acc, cid, msg):
    text = msg.message or ""
    mkey = (cid, msg.id)
    m = main_acc()
    is_main = bool(m) and acc["name"] == m["name"]

    if limit_reached(text):
        dead_msgs.add(mkey)
        if is_main or not m:
            await on_limit(text, cid)
        return

    if is_main and S["running"] and S["role"] == "self" and is_format_error(text):
        asyncio.create_task(fix_target_and_retry(cid))
        return

    if battle_over(text):
        dead_msgs.add(mkey)
        if is_main:
            if S["role"] == "self":
                asyncio.create_task(restart_battle(cid))
            else:
                log(acc["name"], f"[{ctitle(cid)}] battle khatam — next ka wait")
        return

    if not S["running"] or not msg.reply_markup or mkey in dead_msgs:
        return
    if acc["name"] not in players():
        return

    name = acc["display"]

    if BATTLE_MARKER in norm(text):
        S["format_retries"][cid] = 0
        if not my_battle(text, name):
            return
        if S["role"] == "accept" and is_main and not challenger_allowed(text):
            return
        for b in all_buttons(msg):
            if READY_BUTTON.lower() in b.lower():
                await asyncio.sleep(random.uniform(READY_DELAY_MIN, READY_DELAY_MAX))
                await click_once(acc, cid, msg, b, "ready")
                if S["role"] == "accept" and is_main:
                    S["accepted"] += 1
                    notify(f"✅ accepted: {challenger_name(text) or 'unknown'} "
                           f"({ctitle(cid)}) | total {S['accepted']}",
                           force=NOTIFY_BATTLE)
                return
        return

    if not my_battle(text, name) or not my_turn(text, name):
        return

    h = hash(text) & 0xffffff

    if SWITCH_MARKER in norm(text):
        slots = switch_buttons(msg)
        if not slots:
            return
        pick = slots[0] if len(slots) == 1 else random.choice(slots)
        await asyncio.sleep(SWITCH_DELAY)
        await click_once(acc, cid, msg, pick, f"switch:{h}", expect_turn=name)
        return

    mv = pick_move(msg)
    if not mv:
        return
    await asyncio.sleep(MOVE_DELAY)
    await click_once(acc, cid, msg, mv, f"move:{h}", expect_turn=name)


# ---------------------- ACCOUNT ----------------------
def attach(acc):
    client = acc["client"]

    def in_chats(event):
        return event.chat_id in S["chats"]

    @client.on(events.NewMessage(incoming=True, func=in_chats))
    async def _new(event):
        await dump_message(acc["name"], event)
        await handle(acc, event.chat_id, event.message)

    @client.on(events.MessageEdited(incoming=True, func=in_chats))
    async def _edit(event):
        await dump_message(acc["name"] + "/EDIT", event)
        await handle(acc, event.chat_id, event.message)


async def register_account(slot, client):
    me = await client.get_me()
    acc = {"name": f"ACC{slot}", "session": f"acc{slot}", "client": client,
           "id": me.id, "display": get_display_name(me),
           "username": me.username or "", "limited": False,
           "aliases": load_aliases().get(slot, [])}
    ACC.append(acc)
    ACC.sort(key=lambda a: int(re.search(r"acc(\d+)", a["session"]).group(1)))
    S["main"] = 0
    if MAIN_NEVER_LIMITED and ACC:
        ACC[0]["limited"] = False
    if len(ACC) == 1:
        S["role"], S["opp"] = "accept", None
    elif S["opp"] is None:
        S["role"] = "self"
        S["opp"] = next_free_opponent()
    attach(acc)
    log(acc["name"], f"logged in: {acc['display']} (@{acc['username']}) id={me.id}")
    return acc


async def load_existing_sessions():
    for f in sorted(glob.glob(os.path.join(SESSIONS_DIR, "acc*.session"))):
        m = re.search(r"acc(\d+)\.session$", f)
        if not m:
            continue
        slot = int(m.group(1))
        client = make_client(f"acc{slot}")
        try:
            await client.connect()
            if await client.is_user_authorized():
                await register_account(slot, client)
            else:
                await client.disconnect()
        except Exception as e:
            log("SYS", f"acc{slot} load failed: {e}")
            try:
                await client.disconnect()
            except Exception:
                pass


# ---------------------- BOT UI ----------------------
def panel_text():
    if not ACC:
        return ("**Pokemon Battle Panel**\nkoi ID login nahi hai.\n"
                "`/add` bhejo — bot number maangega.")
    o = opp_acc()
    free = sum(1 for i, a in enumerate(ACC) if i != S["main"] and not a["limited"])
    chats = "\n".join(f"• {c['title']}" for c in S["chats"].values()) or "• -"
    return (f"**Pokemon Battle Panel**\n"
            f"state: {'🟢 running' if S['running'] else '🔴 stopped'}\n"
            f"mode: `{S['role']}`  |  moves: `{S['strategy']}`\n"
            f"IDs: {len(ACC)}  |  free earners: {free}\n"
            f"main (farmer): {main_acc()['display']} ♾ no-limit\n"
            f"opponent (earner): {o['display'] if o else '-'}\n"
            f"rounds: {total_rounds()}  |  accepted: {S['accepted']}\n"
            f"chats:\n{chats}")


def panel_buttons():
    return [
        [Button.inline("⏹ Stop" if S["running"] else "▶️ Start",
                       b"stop" if S["running"] else b"run"),
         Button.inline("🔄 Next ID", b"next")],
        [Button.inline("🛡 Accept mode", b"accept"),
         Button.inline("⚔️ Self mode", b"self")],
        [Button.inline("➕ Add ID", b"add"), Button.inline("👥 IDs", b"ids")],
        [Button.inline("💬 Chats", b"chats"), Button.inline("🎯 Moves", b"movemode")],
        [Button.inline("♻️ Reset limits", b"reset"),
         Button.inline("🔁 Refresh", b"refresh")],
    ]


def ids_text():
    if not ACC:
        return "koi ID nahi — /add bhejo"
    out = []
    for i, a in enumerate(ACC):
        if i == S["main"]:
            tag = " **(MAIN / farmer, ♾ no-limit)**"
        elif i == S["opp"]:
            tag = " ← current earner"
        else:
            tag = ""
        al = f" | game: {', '.join(a['aliases'])}" if a.get("aliases") else ""
        state = "♾" if i == S["main"] and MAIN_NEVER_LIMITED else (
            "🚫limit" if a["limited"] else "✅")
        out.append(f"{a['name']}: {a['display']} (@{a['username'] or '-'}) "
                   f"{state}{tag}{al}")
    return "\n".join(out)


HELP = ("**Login**\n"
        "/add — nayi ID login (bot number maangega, phir code)\n"
        "/count 3 — kitni ID add karni hai (chain login)\n"
        "/name 2 Ryuzen — ACC2 ka IN-GAME naam map (limit detect ke liye)\n"
        "/ids — list | /logout 2 — ACC2 hatao | /cancel\n\n"
        "**Chalana**\n"
        "/run — start | /stop — stop\n"
        "/a — accept (training) mode\n"
        "/s — self mode (ACC1 vs earner ID)\n"
        "/next — agli earner ID par shift | /reset — limits clear\n"
        "/sc @group — chat add | /chats | /rm @group\n"
        "/status — panel | /mode random|strongest|preferred | /ping\n\n"
        "ℹ️ ACC1 = main farmer, uski limit kabhi count nahi hoti.\n"
        "⚠️ Login code aise bhejo: `4 4 8 4 7` (digits ke beech space/dash).")


async def ensure_chats(arg=None):
    if arg:
        cid, info = await add_chat(arg)
        if not cid:
            return False, info
    if not S["chats"]:
        for r in DEFAULT_CHATS:
            await add_chat(r)
    return bool(S["chats"]), "ok" if S["chats"] else "koi chat nahi — /sc @group bhejo"


def reset_login(keep_want=True):
    want = LOGIN["want"] if keep_want else 0
    LOGIN.update({"step": None, "idx": None, "client": None,
                  "phone": None, "hash": None, "want": want})


async def begin_add(respond):
    if LOGIN["step"]:
        await respond("login already chal raha hai — /cancel karo ya aage badho")
        return
    slot = free_slot()
    if slot is None:
        await respond(f"max {MAX_IDS} ID ho gayi")
        return
    LOGIN.update({"step": "phone", "idx": slot, "client": make_client(f"acc{slot}")})
    role = "MAIN farmer" if slot == 1 else "earner"
    await respond(f"📱 **ACC{slot}** ({role}) ke liye phone number bhejo\n"
                  f"format: `+917404565817`\n(cancel: /cancel)")


def register_bot(bot):
    def is_owner(e):
        return BOT["owner"] is not None and e.sender_id == BOT["owner"]

    @bot.on(events.NewMessage(pattern=r"(?i)^/start\b"))
    async def _start(e):
        if BOT["owner"] is None:
            BOT["owner"] = e.sender_id
            save_owner(e.sender_id)
            await e.respond(f"👑 owner set: `{e.sender_id}`\n\n" + HELP)
            await e.respond(panel_text(), buttons=panel_buttons())
            return
        if not is_owner(e):
            await e.respond("ye bot private hai.")
            return
        await e.respond(panel_text(), buttons=panel_buttons())

    @bot.on(events.NewMessage(pattern=r"(?i)^/(help|commands)\b", func=is_owner))
    async def _help(e):
        await e.respond(HELP)

    @bot.on(events.NewMessage(pattern=r"(?i)^/(status|panel)\b", func=is_owner))
    async def _status(e):
        await e.respond(panel_text(), buttons=panel_buttons())

    @bot.on(events.NewMessage(pattern=r"(?i)^/ping\b", func=is_owner))
    async def _ping(e):
        await e.respond(f"alive | running={S['running']} | mode={S['role']} | "
                        f"ids={len(ACC)} | chats={len(S['chats'])}")

    # ---------- login ----------
    @bot.on(events.NewMessage(pattern=r"(?i)^/count(\s+\d+)?$", func=is_owner))
    async def _count(e):
        arg = (e.pattern_match.group(1) or "").strip()
        if not arg:
            await e.respond("usage: /count 3")
            return
        LOGIN["want"] = max(1, min(MAX_IDS, int(arg)))
        await e.respond(f"ok — total {LOGIN['want']} ID chahiye. Login shuru...")
        await begin_add(e.respond)

    @bot.on(events.NewMessage(pattern=r"(?i)^/add\b", func=is_owner))
    async def _add(e):
        await begin_add(e.respond)

    @bot.on(events.NewMessage(pattern=r"(?i)^/cancel\b", func=is_owner))
    async def _cancel(e):
        c = LOGIN["client"]
        if c:
            try:
                await c.disconnect()
            except Exception:
                pass
        reset_login(keep_want=False)
        await e.respond("❌ login cancel")

    @bot.on(events.NewMessage(pattern=r"(?i)^/logout(\s+\d+)?$", func=is_owner))
    async def _logout(e):
        arg = (e.pattern_match.group(1) or "").strip()
        if not arg:
            await e.respond("usage: /logout 2")
            return
        slot = int(arg)
        for i, a in enumerate(ACC):
            if a["name"] == f"ACC{slot}":
                try:
                    await a["client"].log_out()
                except Exception:
                    pass
                ACC.pop(i)
                S["main"] = 0
                S["opp"] = next_free_opponent() if len(ACC) > 1 else None
                if len(ACC) < 2:
                    S["role"] = "accept"
                await e.respond(f"🗑 ACC{slot} hata diya | baki: {len(ACC)}")
                return
        await e.respond("wo ID nahi mili")

    @bot.on(events.NewMessage(pattern=r"(?i)^/name(\s+\d+)?(\s+.+)?$", func=is_owner))
    async def _name(e):
        slot = (e.pattern_match.group(1) or "").strip()
        nm = (e.pattern_match.group(2) or "").strip()
        if not slot or not nm:
            await e.respond("usage: `/name 2 Ryuzen`  (ACC2 ka in-game naam)")
            return
        slot = int(slot)
        save_alias(slot, nm)
        for a in ACC:
            if a["name"] == f"ACC{slot}":
                a.setdefault("aliases", [])
                if nm not in a["aliases"]:
                    a["aliases"].append(nm)
                await e.respond(f"✅ ACC{slot} ({a['display']}) game name: **{nm}**")
                return
        await e.respond(f"save ho gaya — ACC{slot} login hone par apply hoga")

    @bot.on(events.NewMessage(func=lambda e: e.is_private and LOGIN["step"]
                              and BOT["owner"] == e.sender_id
                              and not (e.message.message or "").startswith("/")))
    async def _login_steps(e):
        text = (e.message.message or "").strip()
        client = LOGIN["client"]
        slot = LOGIN["idx"]

        if LOGIN["step"] == "phone":
            phone = re.sub(r"[^\d+]", "", text)
            if not re.fullmatch(r"\+?\d{8,15}", phone):
                await e.respond("number galat — aise bhejo: `+917404565817`")
                return
            if not phone.startswith("+"):
                phone = "+" + phone
            try:
                await client.connect()
                sent = await client.send_code_request(phone)
            except PhoneNumberInvalidError:
                await e.respond("❌ number invalid")
                return
            except FloodWaitError as ex:
                await e.respond(f"⏳ flood wait {ex.seconds}s — baad me try karo")
                reset_login()
                return
            except Exception as ex:
                await e.respond(f"❌ code bhejne me error: {ex}")
                return
            LOGIN.update({"phone": phone, "hash": sent.phone_code_hash, "step": "code"})
            await e.respond("✅ code bhej diya.\nAb code **digits ke beech space/dash "
                            "daal ke** bhejo: `4 4 8 4 7` ya `4-4-8-4-7`\n"
                            "(plain code chat me bhejne par Telegram use expire kar deta hai)")
            return

        if LOGIN["step"] == "code":
            code = re.sub(r"\D", "", text)
            if not (4 <= len(code) <= 6):
                await e.respond("code 5 digit ka hota hai — `4 4 8 4 7` jaise bhejo")
                return
            try:
                await client.sign_in(phone=LOGIN["phone"], code=code,
                                     phone_code_hash=LOGIN["hash"])
            except SessionPasswordNeededError:
                LOGIN["step"] = "password"
                await e.respond("🔐 2FA password bhejo")
                return
            except PhoneCodeInvalidError:
                await e.respond("❌ code galat — dobara bhejo")
                return
            except PhoneCodeExpiredError:
                await e.respond("❌ code expire — /cancel karke /add se phir try karo")
                return
            except Exception as ex:
                await e.respond(f"❌ sign-in error: {ex}")
                return
            acc = await register_account(slot, client)
            reset_login()
            await e.respond(f"✅ **{acc['name']} login done** — {acc['display']}\n"
                            f"total IDs: {len(ACC)} | mode: `{S['role']}`\n"
                            f"tip: `/name {slot} <in-game naam>` set kar do")
            if LOGIN["want"] and len(ACC) < LOGIN["want"]:
                await begin_add(e.respond)
            else:
                LOGIN["want"] = 0
                await e.respond(panel_text(), buttons=panel_buttons())
            return

        if LOGIN["step"] == "password":
            try:
                await client.sign_in(password=text)
            except PasswordHashInvalidError:
                await e.respond("❌ password galat — dobara bhejo")
                return
            except Exception as ex:
                await e.respond(f"❌ 2FA error: {ex}")
                return
            acc = await register_account(slot, client)
            reset_login()
            await e.respond(f"✅ **{acc['name']} login done** — {acc['display']}\n"
                            f"total IDs: {len(ACC)}")
            if LOGIN["want"] and len(ACC) < LOGIN["want"]:
                await begin_add(e.respond)
            else:
                LOGIN["want"] = 0
                await e.respond(panel_text(), buttons=panel_buttons())
            return

    # ---------- control ----------
    @bot.on(events.NewMessage(pattern=r"(?i)^/ids\b", func=is_owner))
    async def _ids(e):
        await e.respond(ids_text())

    @bot.on(events.NewMessage(pattern=r"(?i)^/reset\b", func=is_owner))
    async def _reset(e):
        for a in ACC:
            a["limited"] = False
        if len(ACC) >= 2:
            S["role"] = "self"
            S["opp"] = next_free_opponent()
        await e.respond("♻️ saari limits clear | opponent: "
                        f"{opp_acc()['display'] if opp_acc() else '-'}")

    @bot.on(events.NewMessage(pattern=r"(?i)^/run\b", func=is_owner))
    async def _run(e):
        if not ACC:
            await e.respond("pehle /add se ID login karo")
            return
        ok, msg = await ensure_chats()
        if not ok:
            await e.respond(msg)
            return
        if S["running"]:
            await e.respond("already running")
            return
        await start_loop()

    @bot.on(events.NewMessage(pattern=r"(?i)^/stop\b", func=is_owner))
    async def _stop(e):
        await stop_loop()

    @bot.on(events.NewMessage(pattern=r"(?i)^/a(\s+.+)?$", func=is_owner))
    async def _accept(e):
        if not ACC:
            await e.respond("pehle /add se ID login karo")
            return
        S["role"] = "accept"
        S["opp"] = None
        S["targets"].clear()
        ok, msg = await ensure_chats((e.pattern_match.group(1) or "").strip() or None)
        if not ok:
            await e.respond(msg)
            return
        await e.respond("🛡 accept (training) mode set")
        if not S["running"]:
            await start_loop()

    @bot.on(events.NewMessage(pattern=r"(?i)^/s(\s+.+)?$", func=is_owner))
    async def _self(e):
        if len(ACC) < 2:
            await e.respond("self mode ke liye 2+ ID chahiye — /add karo")
            return
        S["role"] = "self"
        S["targets"].clear()
        if S["opp"] is None or ACC[S["opp"]]["limited"]:
            S["opp"] = next_free_opponent()
        ok, msg = await ensure_chats((e.pattern_match.group(1) or "").strip() or None)
        if not ok:
            await e.respond(msg)
            return
        o = opp_acc()
        await e.respond(f"⚔️ self mode | earner: {o['display'] if o else '-'}")
        if S["running"]:
            for cid in list(S["chats"]):
                await ensure_target(cid, force=True)
                await send_challenge(cid, "mode switch")
        else:
            await start_loop()

    @bot.on(events.NewMessage(pattern=r"(?i)^/next\b", func=is_owner))
    async def _next(e):
        if len(ACC) < 2:
            await e.respond("sirf 1 ID hai")
            return
        if S["opp"] is not None:
            ACC[S["opp"]]["limited"] = True
        S["role"] = "self"
        await rotate_opponent("(manual)")

    @bot.on(events.NewMessage(pattern=r"(?i)^/sc(\s+.+)?$", func=is_owner))
    async def _sc(e):
        arg = (e.pattern_match.group(1) or "").strip()
        if not arg:
            await e.respond("usage: /sc @group  ya  /sc -1001234567890")
            return
        cid, info = await add_chat(arg)
        await e.respond(f"✅ chat added: {info}\ntotal: {len(S['chats'])}"
                        if cid else f"❌ {info}")

    @bot.on(events.NewMessage(pattern=r"(?i)^/chats\b", func=is_owner))
    async def _chats(e):
        await e.respond("**active chats** (koi group leave nahi hota)\n" +
                        ("\n".join(f"• {c['title']} ({c['ref']})"
                                   for c in S["chats"].values()) or "• -"))

    @bot.on(events.NewMessage(pattern=r"(?i)^/rm(\s+.+)?$", func=is_owner))
    async def _rm(e):
        arg = (e.pattern_match.group(1) or "").strip()
        if arg.lower() == "all":
            S["chats"].clear()
            S["targets"].clear()
            await e.respond("list clear (koi group leave nahi hua)")
            return
        if not arg:
            await e.respond("usage: /rm @group | /rm all")
            return
        t = remove_chat(arg)
        await e.respond(f"🗑 list se hataya: {t}" if t else "nahi mila")

    @bot.on(events.NewMessage(pattern=r"(?i)^/mode(\s+\w+)?$", func=is_owner))
    async def _mode(e):
        arg = (e.pattern_match.group(1) or "").strip().lower()
        if arg in ("random", "strongest", "preferred"):
            S["strategy"] = arg
            await e.respond(f"🎯 moves: {arg}")
        else:
            await e.respond("usage: /mode random | strongest | preferred")

    @bot.on(events.CallbackQuery(func=lambda e: BOT["owner"] == e.sender_id))
    async def _cb(e):
        data = e.data.decode()
        if data == "add":
            await e.answer()
            await begin_add(e.respond)
            return
        if data == "run":
            if not ACC:
                await e.answer("pehle ID add karo", alert=True)
                return
            ok, msg = await ensure_chats()
            if not ok:
                await e.answer(msg, alert=True)
                return
            if not S["running"]:
                await start_loop()
            await e.answer("started")
        elif data == "stop":
            await stop_loop()
            await e.answer("stopped")
        elif data == "accept":
            S["role"] = "accept"
            S["opp"] = None
            S["targets"].clear()
            await ensure_chats()
            if ACC and not S["running"]:
                await start_loop()
            await e.answer("accept mode")
        elif data == "self":
            if len(ACC) < 2:
                await e.answer("2+ ID chahiye", alert=True)
                return
            S["role"] = "self"
            if S["opp"] is None or ACC[S["opp"]]["limited"]:
                S["opp"] = next_free_opponent()
            await ensure_chats()
            if S["running"]:
                for cid in list(S["chats"]):
                    await ensure_target(cid, force=True)
                    await send_challenge(cid, "mode switch")
            else:
                await start_loop()
            await e.answer("self mode")
        elif data == "next":
            if len(ACC) < 2:
                await e.answer("sirf 1 ID", alert=True)
                return
            if S["opp"] is not None:
                ACC[S["opp"]]["limited"] = True
            S["role"] = "self"
            await rotate_opponent("(button)")
            await e.answer("switched")
        elif data == "reset":
            for a in ACC:
                a["limited"] = False
            if len(ACC) >= 2:
                S["role"] = "self"
                S["opp"] = next_free_opponent()
            await e.answer("limits clear")
        elif data == "ids":
            await e.answer()
            await e.respond(ids_text())
        elif data == "chats":
            await e.answer()
            await e.respond("\n".join(f"• {c['title']} ({c['ref']})"
                                      for c in S["chats"].values()) or "• -")
        elif data == "movemode":
            order = ["random", "strongest", "preferred"]
            S["strategy"] = order[(order.index(S["strategy"]) + 1) % 3]
            await e.answer(f"moves: {S['strategy']}")
        else:
            await e.answer()
        try:
            await e.edit(panel_text(), buttons=panel_buttons())
        except Exception:
            pass


# ---------------------- MAIN ----------------------
async def main():
    token = ask_token()

    bot = make_client("control_bot")
    await bot.start(bot_token=token)
    me_bot = await bot.get_me()
    BOT.update({"client": bot, "username": me_bot.username, "owner": load_owner()})
    register_bot(bot)
    log("BOT", f"bot online: @{me_bot.username}")

    await load_existing_sessions()
    if ACC:
        log("SYS", f"{len(ACC)} session loaded: "
                   f"{', '.join(a['display'] for a in ACC)}")
        for r in DEFAULT_CHATS:
            await add_chat(r)

    if BOT["owner"]:
        try:
            await bot.send_message(
                BOT["owner"],
                f"🤖 **online** @{me_bot.username}\n"
                f"IDs: {len(ACC)} | mode: `{S['role']}`\n\n" + HELP)
        except Exception:
            pass

    print(f"\n✅ ready — Telegram me @{me_bot.username} kholo, /start bhejo.")
    print("   Phir /add — number aur code wahin bot me daalna hai.\n")

    if AUTO_START and ACC and S["chats"]:
        await start_loop()

    await bot.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nstopped")
    except Exception as e:
        print(f"fatal: {e}")
        sys.exit(1)