# Security

## Supported versions

Security fixes are applied to the latest release on the default branch.

## Defaults and trust boundary

- The bot token is read from `CLICKCLACK_BOT_TOKEN`; it is never stored in
  `config.yaml`, cursor files, error messages, or logs by this plugin.
- Channel messages require an exact bot-handle mention by default.
- Bot-authored messages are rejected before Hermes is invoked.
- User-owned bots accept only their owner unless `allowed_user_ids` is set.
- Service bots require `allowed_user_ids` unless `allow_all_users: true` is
  explicitly configured.
- Only `allowed_channel_ids` are accepted.
- Each Hermes instance must have a unique ClickClack bot token.

The plugin does not reduce Hermes' own authority. If Hermes has terminal,
browser, Git, cloud, or MCP credentials, a permitted ClickClack user can ask it
to use those capabilities. Give each participant a dedicated Hermes instance,
dedicated credentials, and an isolated writable project directory.

## Reporting a vulnerability

Open a private GitHub security advisory for the repository. Do not include a
live bot token, API key, session cookie, private ClickClack URL, or private
message contents in the report.

