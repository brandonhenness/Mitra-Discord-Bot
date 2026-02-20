from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from typing import Dict, List, Optional, Tuple

import discord
from discord.ext import commands

from mitra_bot.storage.cache_store import (
    clear_todo_hub_message_id_for_guild,
    clear_todo_list_board_message_id,
    clear_todo_tasks_for_list_channel,
    get_todo_category_id_for_guild,
    get_todo_hub_channel_id_for_guild,
    get_todo_hub_message_id_for_guild,
    get_todo_list_board_message_id,
    get_todo_list_channel_ids_for_guild,
    get_todo_tasks_for_list_channel,
    remove_todo_list_channel,
    set_todo_category_id_for_guild,
    set_todo_hub_channel_id_for_guild,
    set_todo_hub_message_id_for_guild,
    set_todo_list_board_message_id,
    set_todo_tasks_for_list_channel,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clamp(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "..."


def _status_emoji(status: str) -> str:
    if status == "in_progress":
        return "🟡"
    if status == "done":
        return "✅"
    return "⬜"


def _status_label(status: str) -> str:
    if status == "in_progress":
        return "In Progress"
    if status == "done":
        return "Done"
    return "Open"


@dataclass
class TodoItem:
    id: int
    title: str
    notes: str
    status: str
    assignee_ids: List[int]
    thread_id: Optional[int]
    created_by: int
    created_at: str


def _to_item(raw: dict) -> TodoItem:
    status = raw.get("status")
    if not isinstance(status, str):
        status = "done" if bool(raw.get("done", False)) else "open"
    if status not in {"open", "in_progress", "done"}:
        status = "open"

    assignee_ids: List[int] = []
    raw_assignees = raw.get("assignee_ids")
    if isinstance(raw_assignees, list):
        for x in raw_assignees:
            try:
                assignee_ids.append(int(x))
            except Exception:
                pass
    elif raw.get("assignee_id") is not None:
        try:
            assignee_ids = [int(raw.get("assignee_id"))]
        except Exception:
            assignee_ids = []

    return TodoItem(
        id=int(raw.get("id", 0)),
        title=str(raw.get("title", "Untitled")),
        notes=str(raw.get("notes", "")),
        status=status,
        assignee_ids=assignee_ids,
        thread_id=int(raw["thread_id"]) if raw.get("thread_id") is not None else None,
        created_by=int(raw.get("created_by", 0)),
        created_at=str(raw.get("created_at", "")),
    )


def _to_raw(item: TodoItem) -> dict:
    return {
        "id": item.id,
        "title": item.title,
        "notes": item.notes,
        "status": item.status,
        "done": item.status == "done",
        "assignee_ids": item.assignee_ids,
        "assignee_id": item.assignee_ids[0] if item.assignee_ids else None,
        "thread_id": item.thread_id,
        "created_by": item.created_by,
        "created_at": item.created_at,
    }


def _assignee_mentions(item: TodoItem) -> str:
    if not item.assignee_ids:
        return "_Unassigned_"
    return ", ".join(f"<@{uid}>" for uid in item.assignee_ids)


def _build_task_embed(item: TodoItem) -> discord.Embed:
    embed = discord.Embed(
        title=f"Task #{item.id}: {item.title}",
        color=discord.Color.blurple(),
        description=item.notes or "_No notes_",
    )
    embed.add_field(
        name="Status",
        value=f"{_status_emoji(item.status)} {_status_label(item.status)}",
        inline=True,
    )
    embed.add_field(name="Assignees", value=_assignee_mentions(item), inline=True)
    embed.add_field(name="Created By", value=f"<@{item.created_by}>", inline=True)
    embed.add_field(name="Created At", value=item.created_at, inline=False)
    return embed


class ThreadEditTaskModal(discord.ui.Modal):
    def __init__(self, cog: "TodoCog", list_channel_id: int, task_id: int, current_title: str, current_notes: str) -> None:
        super().__init__(title="Edit Task")
        self.cog = cog
        self.list_channel_id = list_channel_id
        self.task_id = task_id
        self.title_input = discord.ui.InputText(label="Title", value=current_title, max_length=120)
        self.notes_input = discord.ui.InputText(
            label="Notes (optional)",
            style=discord.InputTextStyle.long,
            required=False,
            value=current_notes,
            max_length=600,
        )
        self.add_item(self.title_input)
        self.add_item(self.notes_input)

    async def callback(self, interaction: discord.Interaction) -> None:
        items = self.cog._load_items(self.list_channel_id)
        item = next((x for x in items if x.id == self.task_id), None)
        if item is None:
            await interaction.response.send_message("Task not found.", ephemeral=True)
            return

        title = (self.title_input.value or "").strip()
        if not title:
            await interaction.response.send_message("Title cannot be empty.", ephemeral=True)
            return

        item.title = title
        item.notes = (self.notes_input.value or "").strip()
        self.cog._save_items(self.list_channel_id, items)
        await self.cog.refresh_board(self.list_channel_id)
        await interaction.response.edit_message(
            embed=_build_task_embed(item),
            view=TaskThreadView(self.cog),
        )


class TaskThreadView(discord.ui.View):
    def __init__(self, cog: "TodoCog") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    async def _resolve(self, interaction: discord.Interaction) -> Tuple[Optional[int], List[TodoItem], Optional[TodoItem]]:
        if interaction.guild is None or not isinstance(interaction.channel, discord.Thread):
            return None, [], None
        return self.cog.find_task_by_thread(interaction.guild, interaction.channel.id)

    async def _set_status(self, interaction: discord.Interaction, status: str) -> None:
        list_channel_id, items, item = await self._resolve(interaction)
        if list_channel_id is None or item is None:
            await interaction.response.send_message("This thread is not linked to a task.", ephemeral=True)
            return
        item.status = status
        self.cog._save_items(list_channel_id, items)
        await self.cog.refresh_board(list_channel_id)
        await interaction.response.edit_message(embed=_build_task_embed(item), view=TaskThreadView(self.cog))

    @discord.ui.button(label="Edit", style=discord.ButtonStyle.primary, custom_id="todo:thread:edit", row=0)
    async def edit_button(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        list_channel_id, items, item = await self._resolve(interaction)
        if list_channel_id is None or item is None:
            await interaction.response.send_message("This thread is not linked to a task.", ephemeral=True)
            return
        await interaction.response.send_modal(
            ThreadEditTaskModal(
                self.cog,
                list_channel_id=list_channel_id,
                task_id=item.id,
                current_title=item.title,
                current_notes=item.notes,
            )
        )

    @discord.ui.button(label="Open", style=discord.ButtonStyle.secondary, custom_id="todo:thread:open", row=1)
    async def set_open(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        await self._set_status(interaction, "open")

    @discord.ui.button(label="In Progress", style=discord.ButtonStyle.primary, custom_id="todo:thread:in_progress", row=1)
    async def set_in_progress(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        await self._set_status(interaction, "in_progress")

    @discord.ui.button(label="Done", style=discord.ButtonStyle.success, custom_id="todo:thread:done", row=1)
    async def set_done(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        await self._set_status(interaction, "done")

    @discord.ui.button(label="Assign Me", style=discord.ButtonStyle.success, custom_id="todo:thread:assign_me", row=2)
    async def assign_me(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        list_channel_id, items, item = await self._resolve(interaction)
        if list_channel_id is None or item is None:
            await interaction.response.send_message("This thread is not linked to a task.", ephemeral=True)
            return
        if interaction.user is None:
            await interaction.response.send_message("Invalid user.", ephemeral=True)
            return
        uid = interaction.user.id
        if uid not in item.assignee_ids:
            item.assignee_ids.append(uid)
            self.cog._save_items(list_channel_id, items)

        if isinstance(interaction.channel, discord.Thread) and isinstance(interaction.user, discord.Member):
            try:
                await interaction.channel.add_user(interaction.user)
            except Exception:
                logging.debug("Could not add assigning user to thread.")

        await self.cog.refresh_board(list_channel_id)
        await interaction.response.edit_message(embed=_build_task_embed(item), view=TaskThreadView(self.cog))

    @discord.ui.button(label="Unassign Me", style=discord.ButtonStyle.danger, custom_id="todo:thread:unassign_me", row=2)
    async def unassign_me(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        list_channel_id, items, item = await self._resolve(interaction)
        if list_channel_id is None or item is None:
            await interaction.response.send_message("This thread is not linked to a task.", ephemeral=True)
            return
        if interaction.user is None:
            await interaction.response.send_message("Invalid user.", ephemeral=True)
            return
        uid = interaction.user.id
        if uid in item.assignee_ids:
            item.assignee_ids = [x for x in item.assignee_ids if x != uid]
            self.cog._save_items(list_channel_id, items)
            if isinstance(interaction.channel, discord.Thread) and isinstance(interaction.user, discord.Member):
                try:
                    await interaction.channel.remove_user(interaction.user)
                except Exception:
                    logging.debug("Could not remove user from thread.")
            await self.cog.refresh_board(list_channel_id)

        await interaction.response.edit_message(embed=_build_task_embed(item), view=TaskThreadView(self.cog))


class AddTaskModal(discord.ui.Modal):
    def __init__(self, cog: "TodoCog", guild_id: int, list_channel_id: int) -> None:
        super().__init__(title="Add Task")
        self.cog = cog
        self.guild_id = guild_id
        self.list_channel_id = list_channel_id
        self.title_input = discord.ui.InputText(label="Title", placeholder="Task title", max_length=120)
        self.notes_input = discord.ui.InputText(
            label="Notes (optional)",
            style=discord.InputTextStyle.long,
            required=False,
            max_length=600,
            placeholder="Details, checklist, links, context",
        )
        self.add_item(self.title_input)
        self.add_item(self.notes_input)

    async def callback(self, interaction: discord.Interaction) -> None:
        title = (self.title_input.value or "").strip()
        notes = (self.notes_input.value or "").strip()
        if not title:
            await interaction.response.send_message("Title cannot be empty.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        items = self.cog._load_items(self.list_channel_id)
        next_id = max((i.id for i in items), default=0) + 1
        item = TodoItem(
            id=next_id,
            title=title,
            notes=notes,
            status="open",
            assignee_ids=[interaction.user.id] if interaction.user else [],
            thread_id=None,
            created_by=interaction.user.id if interaction.user else 0,
            created_at=_now_iso(),
        )
        items.append(item)
        self.cog._save_items(self.list_channel_id, items)

        board_message = await self.cog.get_or_create_board_message_for_list(self.guild_id, self.list_channel_id)
        if board_message is None:
            await interaction.response.send_message("Could not find or create list board message.", ephemeral=True)
            return

        thread_mention = "Not created"
        try:
            list_channel = interaction.guild.get_channel(self.list_channel_id) if interaction.guild else None
            if not isinstance(list_channel, discord.TextChannel):
                await interaction.followup.send("List channel not found.", ephemeral=True)
                return

            # Create a standalone public thread in the list channel.
            # Using board_message.create_thread only allows one thread per board message.
            thread = await list_channel.create_thread(
                name=_clamp(f"todo-{item.id}-{item.title}", 100),
                auto_archive_duration=1440,
            )
            item.thread_id = thread.id
            self.cog._save_items(self.list_channel_id, items)
            await thread.send(embed=_build_task_embed(item), view=TaskThreadView(self.cog))
            if isinstance(interaction.user, discord.Member):
                try:
                    await thread.add_user(interaction.user)
                except Exception:
                    logging.debug("Could not add creator to task thread.")
            thread_mention = thread.mention
        except Exception:
            logging.exception("Failed to auto-create task thread.")

        await self.cog.refresh_board(self.list_channel_id)
        if interaction.guild is not None:
            await self.cog.refresh_hub(interaction.guild.id)
        await interaction.followup.send(
            f"Task created: `#{item.id}` in <#{self.list_channel_id}>. Thread: {thread_mention}",
            ephemeral=True,
        )


class BoardView(discord.ui.View):
    def __init__(self, cog: "TodoCog", guild_id: int, list_channel_id: int) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = guild_id
        self.list_channel_id = list_channel_id

    @discord.ui.button(label="Add Task", style=discord.ButtonStyle.success, custom_id="todo:list:add_task")
    async def add_task(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        guild_id = self.guild_id
        list_channel_id = self.list_channel_id
        if guild_id == 0 and interaction.guild is not None:
            guild_id = interaction.guild.id
        if list_channel_id == 0 and isinstance(interaction.channel, discord.TextChannel):
            list_channel_id = interaction.channel.id
        if guild_id == 0 or list_channel_id == 0:
            await interaction.response.send_message("Could not resolve list context.", ephemeral=True)
            return
        await interaction.response.send_modal(AddTaskModal(self.cog, guild_id, list_channel_id))


class ListCreateModal(discord.ui.Modal):
    def __init__(self, cog: "TodoCog", guild_id: int) -> None:
        super().__init__(title="Create To-Do List")
        self.cog = cog
        self.guild_id = guild_id
        self.name_input = discord.ui.InputText(
            label="List Name",
            placeholder="Example: Infrastructure",
            max_length=80,
        )
        self.add_item(self.name_input)

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("This must be used in a server.", ephemeral=True)
            return
        safe = self.cog._sanitize_list_name(self.name_input.value or "")
        if not safe:
            await interaction.response.send_message("Invalid list name.", ephemeral=True)
            return
        channel = await self.cog.create_list_channel(interaction.guild, safe)
        if channel is None:
            await interaction.response.send_message("Failed to create list channel.", ephemeral=True)
            return
        await self.cog.refresh_board(channel.id)
        await self.cog.refresh_hub(interaction.guild.id)
        await interaction.response.send_message(f"Created list: {channel.mention}", ephemeral=True)


class HubView(discord.ui.View):
    def __init__(self, cog: "TodoCog", guild_id: int) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = guild_id

    @discord.ui.button(label="Create List", style=discord.ButtonStyle.primary, custom_id="todo:hub:create_list")
    async def create_list(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        guild_id = self.guild_id
        if guild_id == 0 and interaction.guild is not None:
            guild_id = interaction.guild.id
        if guild_id == 0:
            await interaction.response.send_message("Could not resolve guild context.", ephemeral=True)
            return
        await interaction.response.send_modal(ListCreateModal(self.cog, guild_id))


class TodoCog(commands.Cog):
    def __init__(self, bot: discord.Bot) -> None:
        self.bot = bot
        self._board_locks: Dict[int, asyncio.Lock] = {}
        self._hub_locks: Dict[int, asyncio.Lock] = {}
        self._ready_initialized = False
        self._assignee_sync_task: Optional[asyncio.Task] = None

    todo = discord.SlashCommandGroup(name="todo", description="Thread-first task management")

    def _load_items(self, list_channel_id: int) -> List[TodoItem]:
        return [_to_item(x) for x in get_todo_tasks_for_list_channel(list_channel_id)]

    def _save_items(self, list_channel_id: int, items: List[TodoItem]) -> None:
        guild_id: Optional[int] = None
        channel = self.bot.get_channel(list_channel_id)
        if isinstance(channel, discord.TextChannel):
            guild_id = channel.guild.id
        set_todo_tasks_for_list_channel(
            list_channel_id, [_to_raw(i) for i in items], guild_id=guild_id
        )

    async def _find_existing_board_message(
        self, channel: discord.TextChannel
    ) -> Optional[discord.Message]:
        try:
            async for msg in channel.history(limit=50):
                if self.bot.user is None or msg.author.id != self.bot.user.id:
                    continue
                if not msg.embeds:
                    continue
                if (msg.embeds[0].title or "").strip() == "To-Do List":
                    return msg
        except Exception:
            logging.debug("Could not scan for existing board message in channel_id=%s", channel.id)
        return None

    async def _find_existing_hub_message(
        self, channel: discord.TextChannel
    ) -> Optional[discord.Message]:
        try:
            async for msg in channel.history(limit=50):
                if self.bot.user is None or msg.author.id != self.bot.user.id:
                    continue
                if not msg.embeds:
                    continue
                if (msg.embeds[0].title or "").strip() == "To-Do Lists":
                    return msg
        except Exception:
            logging.debug("Could not scan for existing hub message in channel_id=%s", channel.id)
        return None

    async def _get_or_create_todo_category(self, guild: discord.Guild) -> Optional[discord.CategoryChannel]:
        category_id = get_todo_category_id_for_guild(guild.id)
        category: Optional[discord.CategoryChannel] = None
        if category_id:
            ch = guild.get_channel(category_id)
            if isinstance(ch, discord.CategoryChannel):
                category = ch

        if category is None:
            # Reuse existing category by name if cache was reset.
            category = discord.utils.get(guild.categories, name="To-Do")

        if category is None:
            try:
                category = await guild.create_category("To-Do")
                set_todo_category_id_for_guild(guild.id, category.id)
                logging.info("Created todo category id=%s in guild_id=%s", category.id, guild.id)
            except Exception:
                logging.exception("Failed to create todo category in guild_id=%s", guild.id)
                return None
        else:
            set_todo_category_id_for_guild(guild.id, category.id)
        return category

    async def _get_or_create_hub_channel(self, guild: discord.Guild) -> Optional[discord.TextChannel]:
        category = await self._get_or_create_todo_category(guild)
        if category is None:
            return None

        channel_id = get_todo_hub_channel_id_for_guild(guild.id)
        if channel_id:
            ch = guild.get_channel(channel_id)
            if isinstance(ch, discord.TextChannel):
                return ch

        # Reuse existing hub by name if cache was reset.
        for ch in category.text_channels:
            if ch.name == "todo-lists":
                set_todo_hub_channel_id_for_guild(guild.id, ch.id)
                return ch

        try:
            channel = await guild.create_text_channel("todo-lists", category=category)
            set_todo_hub_channel_id_for_guild(guild.id, channel.id)
            logging.info("Created todo hub channel id=%s in guild_id=%s", channel.id, guild.id)
            await self._ensure_hub_at_top(guild, channel)
            return channel
        except Exception:
            logging.exception("Failed to create todo hub channel in guild_id=%s", guild.id)
            return None

    async def _ensure_hub_at_top(
        self, guild: discord.Guild, hub_channel: discord.TextChannel
    ) -> None:
        category = hub_channel.category
        if category is None:
            return
        try:
            # Keep list hub pinned as the first text channel in the category.
            await hub_channel.edit(position=0, category=category)
        except Exception:
            logging.debug("Failed to move todo hub channel to top in guild_id=%s", guild.id)

    def _sanitize_list_name(self, raw_name: str) -> str:
        safe = raw_name.strip().lower().replace(" ", "-")
        safe = "".join(ch for ch in safe if ch.isalnum() or ch == "-")
        return safe[:90]

    async def create_list_channel(self, guild: discord.Guild, safe_name: str) -> Optional[discord.TextChannel]:
        category = await self._get_or_create_todo_category(guild)
        if category is None:
            return None
        try:
            return await guild.create_text_channel(f"todo-{safe_name}", category=category)
        except Exception:
            logging.exception("Failed to create todo list channel in guild_id=%s", guild.id)
            return None

    def _list_channels_in_category(self, guild: discord.Guild) -> List[discord.TextChannel]:
        category_id = get_todo_category_id_for_guild(guild.id)
        hub_id = get_todo_hub_channel_id_for_guild(guild.id)
        if not category_id:
            return []
        cat = guild.get_channel(category_id)
        if not isinstance(cat, discord.CategoryChannel):
            return []
        return [ch for ch in cat.text_channels if ch.id != hub_id]

    def _build_hub_embed(self, guild: discord.Guild) -> discord.Embed:
        lists = self._list_channels_in_category(guild)
        embed = discord.Embed(
            title="To-Do Lists",
            description="Create and manage multiple to-do lists.",
            color=discord.Color.teal(),
        )
        if not lists:
            embed.add_field(name="Lists", value="No lists yet. Click **Create List**.", inline=False)
            return embed
        lines = [f"- {ch.mention}" for ch in lists]
        embed.add_field(name="Available Lists", value="\n".join(lines), inline=False)
        return embed

    async def _get_or_create_hub_message(self, guild: discord.Guild) -> Optional[discord.Message]:
        lock = self._hub_locks.setdefault(guild.id, asyncio.Lock())
        async with lock:
            hub_channel = await self._get_or_create_hub_channel(guild)
            if hub_channel is None:
                return None
            hub_message_id = get_todo_hub_message_id_for_guild(guild.id)
            if hub_message_id:
                try:
                    return await hub_channel.fetch_message(hub_message_id)
                except Exception:
                    logging.warning("Stored todo hub message missing for guild_id=%s", guild.id)
                    clear_todo_hub_message_id_for_guild(guild.id)

            existing = await self._find_existing_hub_message(hub_channel)
            if existing is not None:
                set_todo_hub_message_id_for_guild(guild.id, existing.id)
                return existing

            try:
                msg = await hub_channel.send(embed=self._build_hub_embed(guild), view=HubView(self, guild.id))
            except Exception:
                logging.exception("Failed to create todo hub message in guild_id=%s", guild.id)
                return None
            set_todo_hub_message_id_for_guild(guild.id, msg.id)
            return msg

    async def refresh_hub(self, guild_id: int) -> None:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        msg = await self._get_or_create_hub_message(guild)
        if msg is None:
            return
        await msg.edit(embed=self._build_hub_embed(guild), view=HubView(self, guild.id))

    def _build_board_embed(self, list_channel_id: int) -> discord.Embed:
        items = self._load_items(list_channel_id)
        open_count = sum(1 for i in items if i.status == "open")
        in_progress_count = sum(1 for i in items if i.status == "in_progress")
        done_count = sum(1 for i in items if i.status == "done")
        embed = discord.Embed(
            title="To-Do List",
            description="Tasks are managed in their own threads. Use **Add Task** below.",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Open", value=f"`{open_count}`", inline=True)
        embed.add_field(name="In Progress", value=f"`{in_progress_count}`", inline=True)
        embed.add_field(name="Done", value=f"`{done_count}`", inline=True)

        if not items:
            embed.add_field(name="Tasks", value="No tasks yet. Click **Add Task**.", inline=False)
            return embed

        task_lines: List[str] = []
        assignee_lines: List[str] = []
        for item in items[:25]:
            thread = f"<#{item.thread_id}>" if item.thread_id else "_thread pending_"
            task_lines.append(f"{_status_emoji(item.status)} `#{item.id}` {thread}")
            assignee_lines.append(_assignee_mentions(item))
        embed.add_field(name="Task", value="\n".join(task_lines), inline=True)
        embed.add_field(name="Assignees", value="\n".join(assignee_lines), inline=True)
        if len(items) > 25:
            embed.set_footer(text=f"Showing first 25 of {len(items)} tasks.")
        return embed

    async def get_or_create_board_message_for_list(
        self, guild_id: int, list_channel_id: int
    ) -> Optional[discord.Message]:
        lock = self._board_locks.setdefault(list_channel_id, asyncio.Lock())
        async with lock:
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                return None
            channel = guild.get_channel(list_channel_id)
            if not isinstance(channel, discord.TextChannel):
                return None

            message_id = get_todo_list_board_message_id(list_channel_id)
            if message_id:
                try:
                    return await channel.fetch_message(message_id)
                except Exception:
                    logging.warning("Stored todo board message missing for list_channel_id=%s", list_channel_id)

            existing = await self._find_existing_board_message(channel)
            if existing is not None:
                set_todo_list_board_message_id(list_channel_id, existing.id, guild_id=guild_id)
                return existing

            view = BoardView(self, guild_id, list_channel_id)
            try:
                msg = await channel.send(embed=self._build_board_embed(list_channel_id), view=view)
            except Exception:
                logging.exception("Failed to create todo board message in list_channel_id=%s", list_channel_id)
                return None
            set_todo_list_board_message_id(list_channel_id, msg.id, guild_id=guild_id)
            logging.info("Created todo board message id=%s in channel_id=%s", msg.id, list_channel_id)
            return msg

    async def refresh_board(self, list_channel_id: int) -> None:
        channel = self.bot.get_channel(list_channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        await self._reconcile_assignees_with_thread_members(channel.guild, list_channel_id)
        msg = await self.get_or_create_board_message_for_list(channel.guild.id, list_channel_id)
        if msg is None:
            return
        await msg.edit(
            embed=self._build_board_embed(list_channel_id),
            view=BoardView(self, channel.guild.id, list_channel_id),
        )

    async def _reconcile_assignees_with_thread_members(
        self, guild: discord.Guild, list_channel_id: int
    ) -> bool:
        items = self._load_items(list_channel_id)
        changed = False
        for item in items:
            if not item.assignee_ids or item.thread_id is None:
                continue

            thread = guild.get_channel(item.thread_id)
            if thread is None:
                try:
                    fetched = await self.bot.fetch_channel(item.thread_id)
                except Exception:
                    fetched = None
                thread = fetched if isinstance(fetched, discord.Thread) else None

            if not isinstance(thread, discord.Thread):
                continue

            try:
                members = await thread.fetch_members()
            except Exception:
                # If we can't read members, don't destructively change assignees.
                continue

            member_ids = {int(m.id) for m in members}
            new_assignees = [uid for uid in item.assignee_ids if uid in member_ids]
            if new_assignees != item.assignee_ids:
                item.assignee_ids = new_assignees
                changed = True

        if changed:
            self._save_items(list_channel_id, items)
        return changed

    async def ensure_lists_for_guild(self, guild: discord.Guild) -> None:
        hub_channel = await self._get_or_create_hub_channel(guild)
        if hub_channel is None:
            return
        await self._ensure_hub_at_top(guild, hub_channel)
        await self.refresh_hub(guild.id)

        category_list_ids = {ch.id for ch in self._list_channels_in_category(guild)}
        cached_ids = set(get_todo_list_channel_ids_for_guild(guild.id))

        # Remove stale cached list entries that are no longer in the active To-Do category.
        for stale_id in sorted(cached_ids - category_list_ids):
            remove_todo_list_channel(stale_id)

        for list_channel_id in sorted(category_list_ids):
            await self.refresh_board(list_channel_id)

    async def ensure_board_for_all_guilds(self) -> None:
        for guild in self.bot.guilds:
            try:
                await self.ensure_lists_for_guild(guild)
            except Exception:
                logging.exception("Failed to ensure todo lists for guild_id=%s", guild.id)

    def find_task_by_thread(
        self, guild: discord.Guild, thread_id: int
    ) -> Tuple[Optional[int], List[TodoItem], Optional[TodoItem]]:
        category_ids = [ch.id for ch in self._list_channels_in_category(guild)]
        channel_ids = sorted(set(get_todo_list_channel_ids_for_guild(guild.id) + category_ids))
        for list_channel_id in channel_ids:
            items = self._load_items(list_channel_id)
            for item in items:
                if item.thread_id == thread_id:
                    return list_channel_id, items, item
        return None, [], None

    async def _create_task(
        self,
        guild: discord.Guild,
        list_channel_id: int,
        title: str,
        notes: str,
        creator_id: int,
        creator_member: Optional[discord.Member],
    ) -> Tuple[TodoItem, str]:
        items = self._load_items(list_channel_id)
        next_id = max((i.id for i in items), default=0) + 1
        item = TodoItem(
            id=next_id,
            title=title,
            notes=notes,
            status="open",
            assignee_ids=[creator_id],
            thread_id=None,
            created_by=creator_id,
            created_at=_now_iso(),
        )
        items.append(item)
        self._save_items(list_channel_id, items)

        thread_mention = "Not created"
        try:
            list_channel = guild.get_channel(list_channel_id)
            if not isinstance(list_channel, discord.TextChannel):
                return item, "List channel not found"

            thread = await list_channel.create_thread(
                name=_clamp(f"todo-{item.id}-{item.title}", 100),
                auto_archive_duration=1440,
            )
            item.thread_id = thread.id
            self._save_items(list_channel_id, items)
            await thread.send(embed=_build_task_embed(item), view=TaskThreadView(self))
            if isinstance(creator_member, discord.Member):
                try:
                    await thread.add_user(creator_member)
                except Exception:
                    logging.debug("Could not add creator to task thread.")
            thread_mention = thread.mention
        except Exception:
            logging.exception("Failed to auto-create task thread.")

        await self.refresh_board(list_channel_id)
        await self.refresh_hub(guild.id)
        return item, thread_mention

    def _resolve_list_channel_id_from_context(
        self, guild: discord.Guild, channel: Optional[discord.abc.GuildChannel], explicit_list: Optional[discord.TextChannel]
    ) -> Optional[int]:
        if explicit_list is not None:
            return explicit_list.id
        if isinstance(channel, discord.TextChannel):
            list_ids = set(get_todo_list_channel_ids_for_guild(guild.id))
            category_ids = {ch.id for ch in self._list_channels_in_category(guild)}
            if channel.id in (list_ids | category_ids):
                return channel.id
        return None

    async def _unassign_user_for_thread(
        self, guild: discord.Guild, thread_id: int, user_id: int
    ) -> None:
        list_channel_id, items, item = self.find_task_by_thread(guild, thread_id)
        if list_channel_id is None or item is None:
            return
        if user_id not in item.assignee_ids:
            return
        item.assignee_ids = [uid for uid in item.assignee_ids if uid != user_id]
        self._save_items(list_channel_id, items)
        thread = guild.get_thread(thread_id)
        if isinstance(thread, discord.Thread):
            await self._refresh_task_thread_panel(thread, item)
        await self.refresh_board(list_channel_id)
        await self.refresh_hub(guild.id)

    async def _refresh_task_thread_panel(self, thread: discord.Thread, item: TodoItem) -> None:
        """
        Refresh the bot's task panel message inside a task thread.
        Keeps slash command changes immediately visible in-thread.
        """
        try:
            async for msg in thread.history(limit=50):
                if self.bot.user is None or msg.author.id != self.bot.user.id:
                    continue
                if not msg.embeds:
                    continue
                title = msg.embeds[0].title or ""
                if title.startswith(f"Task #{item.id}:"):
                    await msg.edit(embed=_build_task_embed(item), view=TaskThreadView(self))
                    return
        except Exception:
            logging.exception("Failed to refresh task panel in thread_id=%s", thread.id)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._ready_initialized:
            return
        self._ready_initialized = True
        # Register persistent views so old button messages remain interactive after restart.
        self.bot.add_view(TaskThreadView(self))
        self.bot.add_view(BoardView(self, 0, 0))
        self.bot.add_view(HubView(self, 0))
        await self.ensure_board_for_all_guilds()
        if self._assignee_sync_task is None or self._assignee_sync_task.done():
            self._assignee_sync_task = asyncio.create_task(self._assignee_sync_loop())

    async def _assignee_sync_loop(self) -> None:
        while not self.bot.is_closed():
            try:
                for guild in self.bot.guilds:
                    list_ids = get_todo_list_channel_ids_for_guild(guild.id)
                    for list_channel_id in list_ids:
                        changed = await self._reconcile_assignees_with_thread_members(guild, list_channel_id)
                        if changed:
                            await self.refresh_board(list_channel_id)
                            await self.refresh_hub(guild.id)
            except Exception:
                logging.exception("Todo assignee sync loop failed.")
            await asyncio.sleep(45)

    def cog_unload(self) -> None:
        if self._assignee_sync_task is not None:
            self._assignee_sync_task.cancel()

    @commands.Cog.listener()
    async def on_raw_thread_member_remove(
        self, payload: discord.RawThreadMembersUpdateEvent
    ) -> None:
        guild_id = getattr(payload, "guild_id", None)
        thread_id = getattr(payload, "thread_id", None)
        if guild_id is None or thread_id is None:
            return

        guild = self.bot.get_guild(int(guild_id))
        if guild is None:
            return

        data = getattr(payload, "data", {}) or {}
        removed_raw = []
        if isinstance(data, dict):
            removed_raw = data.get("removed_member_ids", [])
        # Compatibility fallback in case the event object exposes a direct field in future versions.
        if not removed_raw:
            removed_raw = getattr(payload, "removed_member_ids", []) or []
        removed_ids: List[int] = []
        for x in removed_raw:
            try:
                removed_ids.append(int(x))
            except Exception:
                pass
        if not removed_ids:
            return

        for uid in removed_ids:
            await self._unassign_user_for_thread(guild, int(thread_id), uid)

    @commands.Cog.listener()
    async def on_thread_member_remove(
        self, member: discord.ThreadMember
    ) -> None:
        thread = getattr(member, "thread", None)
        if thread is None:
            return
        guild = getattr(thread, "guild", None)
        if guild is None:
            return
        user_id = getattr(member, "id", None)
        if user_id is None:
            return
        await self._unassign_user_for_thread(guild, int(thread.id), int(user_id))

    @commands.Cog.listener()
    async def on_raw_thread_delete(self, payload: discord.RawThreadDeleteEvent) -> None:
        guild_id = getattr(payload, "guild_id", None)
        thread_id = getattr(payload, "thread_id", None)
        if guild_id is None or thread_id is None:
            return
        guild = self.bot.get_guild(int(guild_id))
        if guild is None:
            return
        list_channel_id, items, item = self.find_task_by_thread(guild, int(thread_id))
        if list_channel_id is None or item is None:
            return
        items = [x for x in items if x.id != item.id]
        self._save_items(list_channel_id, items)
        await self.refresh_board(list_channel_id)
        await self.refresh_hub(guild.id)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        if not isinstance(channel, discord.TextChannel):
            return
        hub_channel_id = get_todo_hub_channel_id_for_guild(channel.guild.id)
        remove_todo_list_channel(channel.id)
        clear_todo_tasks_for_list_channel(channel.id)
        clear_todo_list_board_message_id(channel.id)
        if hub_channel_id == channel.id:
            clear_todo_hub_message_id_for_guild(channel.guild.id)
        await self.refresh_hub(channel.guild.id)

    @todo.command(name="add_task", description="Add a task to a to-do list")
    async def add_task_slash(
        self,
        ctx: discord.ApplicationContext,
        title: str = discord.Option(str, description="Task title", required=True),
        notes: str = discord.Option(str, description="Optional notes", required=False, default=""),
        list_channel: Optional[discord.TextChannel] = discord.Option(
            discord.TextChannel,
            description="List channel (optional if run inside a list channel)",
            required=False,
            default=None,
        ),
    ) -> None:
        if ctx.guild is None or ctx.user is None:
            await ctx.respond("This command can only be used in a server.", ephemeral=True)
            return
        clean_title = title.strip()
        if not clean_title:
            await ctx.respond("Title cannot be empty.", ephemeral=True)
            return

        list_channel_id = self._resolve_list_channel_id_from_context(ctx.guild, ctx.channel, list_channel)
        if list_channel_id is None:
            await ctx.respond(
                "Run this command in a list channel or pass `list_channel` explicitly.",
                ephemeral=True,
            )
            return

        await ctx.defer(ephemeral=True)
        item, thread_mention = await self._create_task(
            guild=ctx.guild,
            list_channel_id=list_channel_id,
            title=clean_title,
            notes=(notes or "").strip(),
            creator_id=ctx.user.id,
            creator_member=ctx.user if isinstance(ctx.user, discord.Member) else None,
        )
        await ctx.followup.send(
            f"Task created: `#{item.id}` in <#{list_channel_id}>. Thread: {thread_mention}",
            ephemeral=True,
        )

    @todo.command(name="edit", description="Edit the current task (run inside a task thread)")
    async def edit_in_thread(
        self,
        ctx: discord.ApplicationContext,
        title: str = discord.Option(str, description="New task title", required=True),
        notes: str = discord.Option(str, description="New notes (optional)", required=False, default=""),
    ) -> None:
        if ctx.guild is None or not isinstance(ctx.channel, discord.Thread):
            await ctx.respond("Use this command inside a task thread.", ephemeral=True)
            return
        list_channel_id, items, item = self.find_task_by_thread(ctx.guild, ctx.channel.id)
        if list_channel_id is None or item is None:
            await ctx.respond("This thread is not linked to a task.", ephemeral=True)
            return
        clean_title = title.strip()
        if not clean_title:
            await ctx.respond("Title cannot be empty.", ephemeral=True)
            return
        item.title = clean_title
        item.notes = (notes or "").strip()
        self._save_items(list_channel_id, items)
        await self._refresh_task_thread_panel(ctx.channel, item)
        await self.refresh_board(list_channel_id)
        await self.refresh_hub(ctx.guild.id)
        await ctx.respond(f"Updated task `#{item.id}`.", ephemeral=True)

    @todo.command(name="status", description="Set status for current task thread")
    async def status_in_thread(
        self,
        ctx: discord.ApplicationContext,
        status: str = discord.Option(
            str,
            description="Task status",
            required=True,
            choices=["open", "in_progress", "done"],
        ),
    ) -> None:
        if ctx.guild is None or not isinstance(ctx.channel, discord.Thread):
            await ctx.respond("Use this command inside a task thread.", ephemeral=True)
            return
        list_channel_id, items, item = self.find_task_by_thread(ctx.guild, ctx.channel.id)
        if list_channel_id is None or item is None:
            await ctx.respond("This thread is not linked to a task.", ephemeral=True)
            return
        item.status = status
        self._save_items(list_channel_id, items)
        await self._refresh_task_thread_panel(ctx.channel, item)
        await self.refresh_board(list_channel_id)
        await self.refresh_hub(ctx.guild.id)
        await ctx.respond(
            f"Set task `#{item.id}` to **{_status_label(status)}**.",
            ephemeral=True,
        )

    @todo.command(name="assign_me", description="Assign yourself to current task thread")
    async def assign_me_in_thread(self, ctx: discord.ApplicationContext) -> None:
        if ctx.guild is None or not isinstance(ctx.channel, discord.Thread):
            await ctx.respond("Use this command inside a task thread.", ephemeral=True)
            return
        if ctx.user is None:
            await ctx.respond("Invalid user.", ephemeral=True)
            return
        list_channel_id, items, item = self.find_task_by_thread(ctx.guild, ctx.channel.id)
        if list_channel_id is None or item is None:
            await ctx.respond("This thread is not linked to a task.", ephemeral=True)
            return
        uid = ctx.user.id
        if uid not in item.assignee_ids:
            item.assignee_ids.append(uid)
            self._save_items(list_channel_id, items)
        if isinstance(ctx.user, discord.Member):
            try:
                await ctx.channel.add_user(ctx.user)
            except Exception:
                logging.debug("Could not add assigning user to thread.")
        await self._refresh_task_thread_panel(ctx.channel, item)
        await self.refresh_board(list_channel_id)
        await self.refresh_hub(ctx.guild.id)
        await ctx.respond(f"Assigned you to task `#{item.id}`.", ephemeral=True)

    @todo.command(name="unassign_me", description="Unassign yourself from current task thread")
    async def unassign_me_in_thread(self, ctx: discord.ApplicationContext) -> None:
        if ctx.guild is None or not isinstance(ctx.channel, discord.Thread):
            await ctx.respond("Use this command inside a task thread.", ephemeral=True)
            return
        if ctx.user is None:
            await ctx.respond("Invalid user.", ephemeral=True)
            return
        list_channel_id, items, item = self.find_task_by_thread(ctx.guild, ctx.channel.id)
        if list_channel_id is None or item is None:
            await ctx.respond("This thread is not linked to a task.", ephemeral=True)
            return
        uid = ctx.user.id
        if uid in item.assignee_ids:
            item.assignee_ids = [x for x in item.assignee_ids if x != uid]
            self._save_items(list_channel_id, items)
            if isinstance(ctx.user, discord.Member):
                try:
                    await ctx.channel.remove_user(ctx.user)
                except Exception:
                    logging.debug("Could not remove user from thread.")
            await self._refresh_task_thread_panel(ctx.channel, item)
            await self.refresh_board(list_channel_id)
            await self.refresh_hub(ctx.guild.id)
        await ctx.respond(f"Unassigned you from task `#{item.id}`.", ephemeral=True)

    @todo.command(name="list_create", description="Create a new to-do list channel")
    async def list_create(
        self,
        ctx: discord.ApplicationContext,
        name: str = discord.Option(str, description="List name", required=True),
    ) -> None:
        if ctx.guild is None:
            await ctx.respond("This command can only be used in a server.", ephemeral=True)
            return
        category = await self._get_or_create_todo_category(ctx.guild)
        if category is None:
            await ctx.respond("Could not create or find To-Do category.", ephemeral=True)
            return

        safe = name.strip().lower().replace(" ", "-")
        safe = self._sanitize_list_name(safe) or "todo-list"
        channel = await self.create_list_channel(ctx.guild, safe)
        if channel is None:
            await ctx.respond("Failed to create list channel.", ephemeral=True)
            return

        hub_channel = await self._get_or_create_hub_channel(ctx.guild)
        if hub_channel is not None:
            await self._ensure_hub_at_top(ctx.guild, hub_channel)
        await self.get_or_create_board_message_for_list(ctx.guild.id, channel.id)
        await self.refresh_board(channel.id)
        await self.refresh_hub(ctx.guild.id)
        await ctx.respond(f"Created to-do list channel: {channel.mention}", ephemeral=True)

    @todo.command(name="assign", description="Assign a member to current task thread")
    async def assign_in_thread(
        self,
        ctx: discord.ApplicationContext,
        user: discord.Member = discord.Option(discord.Member, description="Member to assign", required=True),
    ) -> None:
        if ctx.guild is None or not isinstance(ctx.channel, discord.Thread):
            await ctx.respond("Use this command inside a task thread.", ephemeral=True)
            return
        list_channel_id, items, item = self.find_task_by_thread(ctx.guild, ctx.channel.id)
        if list_channel_id is None or item is None:
            await ctx.respond("This thread is not linked to a task.", ephemeral=True)
            return

        if user.id not in item.assignee_ids:
            item.assignee_ids.append(user.id)
            self._save_items(list_channel_id, items)
        try:
            await ctx.channel.add_user(user)
        except Exception:
            logging.debug("Could not add assignee to thread.")
        await self._refresh_task_thread_panel(ctx.channel, item)
        await self.refresh_board(list_channel_id)
        await self.refresh_hub(ctx.guild.id)
        await ctx.respond(f"Assigned task `#{item.id}` to {user.mention}.", ephemeral=True)

    @todo.command(name="unassign", description="Unassign a member from current task thread")
    async def unassign_in_thread(
        self,
        ctx: discord.ApplicationContext,
        user: discord.Member = discord.Option(discord.Member, description="Member to unassign", required=True),
    ) -> None:
        if ctx.guild is None or not isinstance(ctx.channel, discord.Thread):
            await ctx.respond("Use this command inside a task thread.", ephemeral=True)
            return
        list_channel_id, items, item = self.find_task_by_thread(ctx.guild, ctx.channel.id)
        if list_channel_id is None or item is None:
            await ctx.respond("This thread is not linked to a task.", ephemeral=True)
            return

        if user.id in item.assignee_ids:
            item.assignee_ids = [uid for uid in item.assignee_ids if uid != user.id]
            self._save_items(list_channel_id, items)
            try:
                await ctx.channel.remove_user(user)
            except Exception:
                logging.debug("Could not remove assignee from thread.")
            await self._refresh_task_thread_panel(ctx.channel, item)
            await self.refresh_board(list_channel_id)
            await self.refresh_hub(ctx.guild.id)

        await ctx.respond(f"Unassigned {user.mention} from task `#{item.id}`.", ephemeral=True)


def setup(bot: discord.Bot) -> None:
    bot.add_cog(TodoCog(bot))
