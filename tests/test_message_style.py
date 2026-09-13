import discord

from mitra_bot.discord_app.message_style import COLORS, embed, notice, pages
from mitra_bot.discord_app.cogs.todo_common import TodoItem, build_task_embed
from mitra_bot.services.fleet_updates import FleetUpdates


def test_notice_keeps_copyable_values_and_links_in_separate_body():
    body = "**Server** Mitra\n```\n203.0.113.1\n```\n[Details](https://example.com)"
    assert notice("Address changed", body, tone="success") == "### ✅ Address changed\n\n" + body


def test_long_health_pages_keep_all_details_and_message_limits():
    sections = [f"**Server {n}**\n" + "a" * 240 for n in range(65)]
    output = list(pages("Server health check", sections))
    assert len(output) > 1
    assert all(len(message) <= 2000 for message in output)
    assert all(sum(section in message for message in output) == 1 for section in sections)


def test_style_keeps_attachment_timestamp_and_custom_tracking_footer():
    from datetime import datetime, timezone
    timestamp = datetime.now(timezone.utc)
    card = embed(title="Server status", color=discord.Color.red(), timestamp=timestamp)
    card.set_image(url="attachment://history.png")
    card.set_footer(text="existing-delivery-marker")
    data = card.to_dict()
    assert data["color"] == COLORS["error"]
    assert data["image"]["url"] == "attachment://history.png"
    assert data["footer"]["text"] == "existing-delivery-marker"
    assert data["timestamp"] == timestamp.isoformat()


def test_task_card_long_text_fits_discord_limits_and_uses_localized_time():
    item = TodoItem(1, "a" * 300, "n" * 5000, "open", [], None, 1, "2026-09-12T12:00:00+00:00")
    data = build_task_embed(item).to_dict()
    assert len(data["title"]) <= 256
    assert len(data["description"]) <= 4096
    assert next(f["value"] for f in data["fields"] if f["name"] == "Created").startswith("<t:")


def test_update_progress_keeps_failure_visible_and_full_list_available():
    plan = {"id": "a" * 32, "version": "0.3.0", "state": "failed", "error": "Mitra did not reconnect",
            "nodes": [{"node": f"server-{n}", "state": "pending"} for n in range(65)]}
    message = FleetUpdates.progress_text(plan)
    assert message.startswith("### ❌ Rolling update stopped")
    assert plan["error"] in message
    assert "/update status" in message
    assert len(message) <= 2000
    full = list(pages(FleetUpdates.progress_title(plan), FleetUpdates.progress_sections(plan, full=True)))
    for node in plan["nodes"]:
        assert any(f"**{node['node']}**" in page for page in full)
