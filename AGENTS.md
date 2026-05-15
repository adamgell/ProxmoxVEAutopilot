# ProxmoxVEAutopilot Agent Instructions

## MCP Docs First

For work in this repository, prefer the ProxmoxVEAutopilot MCP docs tools before planning or implementing changes when the MCP server is available.

Use the configured MCP server named `proxmoxveautopilot` or a session-specific equivalent such as `proxmoxveautopilot_test`.

At the start of a new repo session, run `./skill.sh status` when shell access is available. This verifies the tunnel, MCP tool list, and docs inventory before relying on the docs tools.

Primary docs tools:

- `autopilot_docs.list`
- `autopilot_docs.search`
- `autopilot_docs.read`

Use these tools to consume current repo docs, plans, and setup notes. Prefer MCP docs over memory or assumptions when the answer depends on repo-specific behavior.

If the MCP server is unavailable, fall back to local files under this checkout and state that MCP docs were not available.

## Local Helper

The repo root `skill.sh` helper manages the live MCP tunnel and local token-injecting proxy without printing the token.

The user-level Codex config points `proxmoxveautopilot` at the local proxy on `http://127.0.0.1:15051/mcp`; the helper supplies the token and backend tunnel. For Codex CLI sessions that need MCP access, start from `./skill.sh shell` or launch directly with `./skill.sh codex "<prompt>"`.

Common commands:

- `./skill.sh status`
- `./skill.sh docs "WinPE CloudOSD"`
- `./skill.sh read <doc_id> [max_chars]`
- `./skill.sh proxy-install`
- `./skill.sh proxy-status`
- `./skill.sh codex "<prompt>"`
- `./skill.sh shell`

Do not expose the MCP bearer token in logs, chat, committed files, or command output.
