# mitra_bot/storage/cache_store.py
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional, Set

CACHE_PATH = Path("cache.json")


def read_cache_json() -> Dict[str, Any]:
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def write_cache_json(data: Dict[str, Any]) -> None:
    CACHE_PATH.write_text(json.dumps(data), encoding="utf-8")


def ensure_ups_config(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Mirrors the defaults from bot.py so behavior does not change.
    """
    ups = data.get("ups")
    if not isinstance(ups, dict):
        ups = {}

    ups.setdefault("enabled", True)
    ups.setdefault("poll_seconds", 30)

    # Alert thresholds (seconds remaining)
    ups.setdefault("warn_time_to_empty_seconds", 600)      # 10 minutes
    ups.setdefault("critical_time_to_empty_seconds", 180)  # 3 minutes

    # Optional automatic shutdown control (OFF by default)
    ups.setdefault("auto_shutdown_enabled", False)
    ups.setdefault("auto_shutdown_action", "shutdown")     # shutdown or restart
    ups.setdefault("auto_shutdown_delay_seconds", 0)
    ups.setdefault("auto_shutdown_force", False)

    # Logging
    ups.setdefault("log_enabled", True)
    ups.setdefault("log_file", "ups_stats.jsonl")
    ups.setdefault("graph_default_hours", 6)               # /ups status default window

    # Timezone for timestamps in logs and graphs (default: UTC)
    ups.setdefault("timezone", "UTC")  # e.g. "America/Los_Angeles"

    data["ups"] = ups
    return data


def ensure_todo_config(data: Dict[str, Any]) -> Dict[str, Any]:
    todo_cfg = data.get("todo_config")
    if not isinstance(todo_cfg, dict):
        todo_cfg = {}

    # New simplified schema:
    # todo_config = {
    #   "guilds": {
    #     "<guild_id>": {
    #       "category_id": int|None,
    #       "hub_channel_id": int|None,
    #       "hub_message_id": int|None
    #     }
    #   },
    #   "lists": {
    #     "<list_channel_id>": {
    #       "guild_id": int,
    #       "board_message_id": int|None,
    #       "tasks": [ ... ]
    #     }
    #   }
    # }
    guilds = todo_cfg.get("guilds")
    if not isinstance(guilds, dict):
        guilds = {}
    lists = todo_cfg.get("lists")
    if not isinstance(lists, dict):
        lists = {}

    # Migrate old keys if present.
    old_categories = todo_cfg.get("categories", {})
    old_hubs = todo_cfg.get("hubs", {})
    old_hub_messages = todo_cfg.get("hub_messages", {})
    old_board_messages = todo_cfg.get("board_messages", {})
    old_tasks = todo_cfg.get("tasks", {})

    if isinstance(old_categories, dict):
        for g, cat_id in old_categories.items():
            rec = guilds.get(str(g), {})
            if not isinstance(rec, dict):
                rec = {}
            rec.setdefault("category_id", cat_id)
            rec.setdefault("hub_channel_id", None)
            rec.setdefault("hub_message_id", None)
            guilds[str(g)] = rec

    if isinstance(old_hubs, dict):
        for g, hub_id in old_hubs.items():
            rec = guilds.get(str(g), {})
            if not isinstance(rec, dict):
                rec = {}
            rec.setdefault("category_id", None)
            rec["hub_channel_id"] = hub_id
            rec.setdefault("hub_message_id", None)
            guilds[str(g)] = rec

    if isinstance(old_hub_messages, dict):
        for g, msg_id in old_hub_messages.items():
            rec = guilds.get(str(g), {})
            if not isinstance(rec, dict):
                rec = {}
            rec.setdefault("category_id", None)
            rec.setdefault("hub_channel_id", None)
            rec["hub_message_id"] = msg_id
            guilds[str(g)] = rec

    if isinstance(old_board_messages, dict):
        for ch, msg_id in old_board_messages.items():
            rec = lists.get(str(ch), {})
            if not isinstance(rec, dict):
                rec = {}
            rec.setdefault("guild_id", None)
            rec["board_message_id"] = msg_id
            rec.setdefault("tasks", [])
            lists[str(ch)] = rec

    if isinstance(old_tasks, dict):
        for ch, rows in old_tasks.items():
            rec = lists.get(str(ch), {})
            if not isinstance(rec, dict):
                rec = {}
            rec.setdefault("guild_id", None)
            rec.setdefault("board_message_id", None)
            rec["tasks"] = rows if isinstance(rows, list) else []
            lists[str(ch)] = rec

    # Clean/normalize records.
    for g, rec in list(guilds.items()):
        if not isinstance(rec, dict):
            rec = {}
        rec.setdefault("category_id", None)
        rec.setdefault("hub_channel_id", None)
        rec.setdefault("hub_message_id", None)
        guilds[str(g)] = rec

    for ch, rec in list(lists.items()):
        if not isinstance(rec, dict):
            rec = {}
        rec.setdefault("guild_id", None)
        rec.setdefault("board_message_id", None)
        rec.setdefault("tasks", [])
        if not isinstance(rec.get("tasks"), list):
            rec["tasks"] = []
        lists[str(ch)] = rec

    todo_cfg["guilds"] = guilds
    todo_cfg["lists"] = lists
    data["todo_config"] = todo_cfg

    # Drop legacy top-level todo keys that caused confusion.
    data.pop("todo_channel_id", None)
    data.pop("todo_category_id", None)
    data.pop("todo_board_messages", None)
    return data


def read_cache_with_defaults() -> Dict[str, Any]:
    """
    Load cache.json and apply schema defaults/migrations. Writes back if updated.
    """
    data = read_cache_json()
    before = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    data = ensure_ups_config(data)
    data = ensure_todo_config(data)

    after = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if after != before:
        try:
            write_cache_json(data)
        except Exception:
            logging.exception("Failed to write cache defaults to cache.json")

    return data


# -----------------------------
# Admins / Subscribers
# -----------------------------

def load_admins() -> Set[int]:
    data = read_cache_json()
    admins_list = data.get("admins", [])
    out: Set[int] = set()
    for x in admins_list:
        try:
            out.add(int(x))
        except Exception:
            pass
    return out


def load_subscribers() -> Set[int]:
    try:
        logging.info("Loading subscribers from cache file...")
        data = read_cache_json()
        if "subscribers" not in data:
            logging.warning("No subscribers found in cache file.")
            return set()
        return set(data.get("subscribers", []))
    except Exception:
        logging.exception("Failed to load subscribers from cache.json")
        return set()


async def save_subscribers(subscribers_set: Set[int]) -> None:
    data = read_cache_json()
    data["subscribers"] = list(subscribers_set)
    write_cache_json(data)
    logging.info(
        "Subscribers saved to cache file: %s",
        ", ".join([str(s) for s in subscribers_set]),
    )


# -----------------------------
# Public IP caching
# -----------------------------

async def load_ip() -> Optional[str]:
    try:
        data = read_cache_json()
        return data.get("ip")
    except Exception:
        logging.exception("Failed to load ip from cache.json")
        return None


async def save_ip(ip: str) -> None:
    data = read_cache_json()
    data["ip"] = ip
    write_cache_json(data)
    logging.info("IP address saved to cache file: %s", ip)


# -----------------------------
# UPS convenience helpers
# -----------------------------

def get_ups_config() -> Dict[str, Any]:
    data = read_cache_with_defaults()
    ups = data.get("ups", {})
    return ups if isinstance(ups, dict) else {}


def set_ups_config(patch: Dict[str, Any]) -> Dict[str, Any]:
    """
    Patch UPS config keys and persist. Returns the new UPS config dict.
    """
    data = read_cache_with_defaults()
    ups = data.get("ups", {})
    if not isinstance(ups, dict):
        ups = {}
    ups.update(patch)
    data["ups"] = ups
    write_cache_json(data)
    return ups


# -----------------------------
# Power action helpers
# -----------------------------

def get_power_restart_notice() -> Optional[Dict[str, Any]]:
    data = read_cache_json()
    notice = data.get("power_restart_notice")
    return notice if isinstance(notice, dict) else None


def set_power_restart_notice(notice: Dict[str, Any]) -> None:
    data = read_cache_json()
    data["power_restart_notice"] = notice
    write_cache_json(data)


def clear_power_restart_notice() -> None:
    data = read_cache_json()
    if "power_restart_notice" in data:
        del data["power_restart_notice"]
        write_cache_json(data)


# -----------------------------
# To-Do helpers
# -----------------------------

def get_todos_for_guild(guild_id: int) -> list[Dict[str, Any]]:
    data = read_cache_json()
    todo = data.get("todo")
    if not isinstance(todo, dict):
        return []
    items = todo.get(str(guild_id), [])
    return items if isinstance(items, list) else []


def set_todos_for_guild(guild_id: int, items: list[Dict[str, Any]]) -> None:
    data = read_cache_json()
    todo = data.get("todo")
    if not isinstance(todo, dict):
        todo = {}
    todo[str(guild_id)] = items
    data["todo"] = todo
    write_cache_json(data)


def _todo_cfg(data: Dict[str, Any]) -> Dict[str, Any]:
    cfg = data.get("todo_config")
    if not isinstance(cfg, dict):
        cfg = {}
    cfg.setdefault("guilds", {})
    cfg.setdefault("lists", {})
    return cfg


def _guild_rec(cfg: Dict[str, Any], guild_id: int) -> Dict[str, Any]:
    guilds = cfg.get("guilds", {})
    if not isinstance(guilds, dict):
        guilds = {}
    rec = guilds.get(str(guild_id), {})
    if not isinstance(rec, dict):
        rec = {}
    rec.setdefault("category_id", None)
    rec.setdefault("hub_channel_id", None)
    rec.setdefault("hub_message_id", None)
    guilds[str(guild_id)] = rec
    cfg["guilds"] = guilds
    return rec


def _find_list_rec(cfg: Dict[str, Any], list_channel_id: int) -> Optional[Dict[str, Any]]:
    lists = cfg.get("lists", {})
    if not isinstance(lists, dict):
        return None
    rec = lists.get(str(list_channel_id))
    return rec if isinstance(rec, dict) else None


def _ensure_list_rec(cfg: Dict[str, Any], list_channel_id: int) -> Dict[str, Any]:
    lists = cfg.get("lists", {})
    if not isinstance(lists, dict):
        lists = {}
    rec = lists.get(str(list_channel_id), {})
    if not isinstance(rec, dict):
        rec = {}
    rec.setdefault("guild_id", None)
    rec.setdefault("board_message_id", None)
    rec.setdefault("tasks", [])
    if not isinstance(rec.get("tasks"), list):
        rec["tasks"] = []
    lists[str(list_channel_id)] = rec
    cfg["lists"] = lists
    return rec


def get_todo_category_id_for_guild(guild_id: int) -> Optional[int]:
    data = read_cache_json()
    cfg = _todo_cfg(data)
    rec = _guild_rec(cfg, guild_id)
    raw = rec.get("category_id")
    try:
        return int(raw) if raw is not None else None
    except Exception:
        return None


def set_todo_category_id_for_guild(guild_id: int, category_id: int) -> None:
    data = read_cache_json()
    cfg = _todo_cfg(data)
    rec = _guild_rec(cfg, guild_id)
    rec["category_id"] = int(category_id)
    data["todo_config"] = cfg
    write_cache_json(data)


def get_todo_list_board_message_id(list_channel_id: int) -> Optional[int]:
    data = read_cache_json()
    cfg = _todo_cfg(data)
    rec = _find_list_rec(cfg, list_channel_id)
    raw = rec.get("board_message_id") if rec else None
    try:
        return int(raw) if raw is not None else None
    except Exception:
        return None


def clear_todo_list_board_message_id(list_channel_id: int) -> None:
    data = read_cache_json()
    cfg = _todo_cfg(data)
    rec = _find_list_rec(cfg, list_channel_id)
    if rec is not None:
        rec["board_message_id"] = None
    data["todo_config"] = cfg
    write_cache_json(data)


def set_todo_list_board_message_id(list_channel_id: int, message_id: int, *, guild_id: Optional[int] = None) -> None:
    data = read_cache_json()
    cfg = _todo_cfg(data)
    rec = _ensure_list_rec(cfg, list_channel_id)
    rec["board_message_id"] = int(message_id)
    if guild_id is not None:
        rec["guild_id"] = int(guild_id)
    data["todo_config"] = cfg
    write_cache_json(data)


def get_todo_tasks_for_list_channel(list_channel_id: int) -> list[Dict[str, Any]]:
    data = read_cache_json()
    cfg = _todo_cfg(data)
    rec = _find_list_rec(cfg, list_channel_id)
    rows = rec.get("tasks", []) if rec else []
    return rows if isinstance(rows, list) else []


def set_todo_tasks_for_list_channel(list_channel_id: int, items: list[Dict[str, Any]], *, guild_id: Optional[int] = None) -> None:
    data = read_cache_json()
    cfg = _todo_cfg(data)
    rec = _ensure_list_rec(cfg, list_channel_id)
    rec["tasks"] = items
    if guild_id is not None:
        rec["guild_id"] = int(guild_id)
    data["todo_config"] = cfg
    write_cache_json(data)


def clear_todo_tasks_for_list_channel(list_channel_id: int) -> None:
    data = read_cache_json()
    cfg = _todo_cfg(data)
    rec = _find_list_rec(cfg, list_channel_id)
    if rec is not None:
        rec["tasks"] = []
    data["todo_config"] = cfg
    write_cache_json(data)


def get_todo_hub_channel_id_for_guild(guild_id: int) -> Optional[int]:
    data = read_cache_json()
    cfg = _todo_cfg(data)
    rec = _guild_rec(cfg, guild_id)
    raw = rec.get("hub_channel_id")
    try:
        return int(raw) if raw is not None else None
    except Exception:
        return None


def set_todo_hub_channel_id_for_guild(guild_id: int, channel_id: int) -> None:
    data = read_cache_json()
    cfg = _todo_cfg(data)
    rec = _guild_rec(cfg, guild_id)
    rec["hub_channel_id"] = int(channel_id)
    data["todo_config"] = cfg
    write_cache_json(data)


def get_todo_hub_message_id_for_guild(guild_id: int) -> Optional[int]:
    data = read_cache_json()
    cfg = _todo_cfg(data)
    rec = _guild_rec(cfg, guild_id)
    raw = rec.get("hub_message_id")
    try:
        return int(raw) if raw is not None else None
    except Exception:
        return None


def set_todo_hub_message_id_for_guild(guild_id: int, message_id: int) -> None:
    data = read_cache_json()
    cfg = _todo_cfg(data)
    rec = _guild_rec(cfg, guild_id)
    rec["hub_message_id"] = int(message_id)
    data["todo_config"] = cfg
    write_cache_json(data)


def clear_todo_hub_message_id_for_guild(guild_id: int) -> None:
    data = read_cache_json()
    cfg = _todo_cfg(data)
    rec = _guild_rec(cfg, guild_id)
    rec["hub_message_id"] = None
    data["todo_config"] = cfg
    write_cache_json(data)


def remove_todo_list_channel(list_channel_id: int) -> None:
    data = read_cache_json()
    cfg = _todo_cfg(data)

    lists = cfg.get("lists", {})
    if isinstance(lists, dict):
        lists.pop(str(list_channel_id), None)
        cfg["lists"] = lists

    # If this was a hub channel for any guild, clear that reference.
    guilds = cfg.get("guilds", {})
    if isinstance(guilds, dict):
        for g, rec in guilds.items():
            if not isinstance(rec, dict):
                continue
            if str(rec.get("hub_channel_id")) == str(list_channel_id):
                rec["hub_channel_id"] = None
                rec["hub_message_id"] = None

    data["todo_config"] = cfg
    write_cache_json(data)


def get_todo_list_channel_ids_for_guild(guild_id: int) -> list[int]:
    data = read_cache_json()
    cfg = _todo_cfg(data)
    lists = cfg.get("lists", {})
    if not isinstance(lists, dict):
        return []
    ids: List[int] = []
    for raw_id, rec in lists.items():
        if not isinstance(rec, dict):
            continue
        rec_guild_id = rec.get("guild_id")
        # Legacy/fallback: include entries with unknown guild_id so old cache can still resolve tasks.
        if rec_guild_id is not None and str(rec_guild_id) != str(guild_id):
            continue
        try:
            ids.append(int(raw_id))
        except Exception:
            pass
    return sorted(set(ids))
