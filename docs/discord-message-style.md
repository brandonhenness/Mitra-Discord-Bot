# Discord message style

Use `mitra_bot.discord_app.message_style` for new Discord messages. This keeps
command replies, notifications and edited messages consistent without changing
delivery, authorization, mentions or interaction controls.

- Start with a short sentence-case heading that names the outcome or subject.
- Put details in a separate paragraph or labeled fields. Name the affected server
  prominently when an operation targets a machine.
- Use neutral blurple for information, green for success, yellow for caution and
  red for failures. Pair color with readable text; do not rely on color alone.
- Use `notice()` for short replies and `embed()` for cards with fields or graphs.
- Use `pages()` for long plain-text reports. Keep related paragraphs together
  and stay within Discord's 2,000-character message limit.
- Put copyable IP addresses in fenced code blocks with opening and closing
  fences on their own lines. Discord decides whether a client shows a copy button.
- Use Discord timestamps for events so viewers see their local time. Label old
  observations and cached data explicitly.
- Explain errors and the next useful step. An unconfirmed operation must never
  be described as successful or invite an unsafe automatic retry.
- Keep intentional subscriber mentions, ephemeral visibility, button IDs,
  attachment URLs and delivery tracking metadata intact. Do not expose internal
  tracking identifiers in visible alert footers.

## Coverage

The presentation pass covers IP status/change alerts; peer alerts, status,
incidents, dashboards and doctor reports; subscriptions and channel settings;
UPS status, settings and power events; update prompts, progress, settings and
results; local and peer power confirmations/results; ToDo boards, task cards,
modals and replies; About; permission checks and command failure responses.

Existing posted messages retain their old appearance until edited or refreshed.
Changes take effect for new messages after installing and restarting the updated
bot. All peers should use the same release for consistent presentation.

Validation covers message pagination, long task text, palette selection,
attachment and tracking preservation, update failures and complete fleet reports.
Live Discord rendering should also be checked on desktop and mobile before release.
