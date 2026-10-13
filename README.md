# Amplifier MCP Tool Module

Modular capability that adds Model Context Protocol (MCP) server integration to Amplifier bundles.

## Overview

This module enables Amplifier to connect to MCP servers and expose their capabilities (Tools, Resources, Prompts) to LLM agents. Connect to any MCP-compatible server and make its tools available to your AI agents.

**What You Get:**
- 🔌 **Multi-server orchestration** - Connect to multiple MCP servers simultaneously
- 🚀 **Dual transport support** - stdio and Streamable HTTP
- 🔄 **Auto-reconnection** - Exponential backoff with circuit breaker for resilience
- 🛡️ **Content protection** - Automatic truncation to prevent context exhaustion
- 📊 **Health monitoring** - Track server status and connection health
- 🔇 **Clean console** - Server logs saved to files, not cluttering output

**Version:** 0.2.2

## Protocol and SDK support

The MCP spec revision this module speaks is determined by the installed `mcp` SDK:

| Installed SDK | Protocol negotiated |
|---|---|
| `mcp>=2.0` | **2026-07-28** — the stateless protocol. Negotiated via `server/discover`; no `initialize` handshake; every request carries `_meta`. |
| `mcp>=1.24,<2` | Handshake-era protocol (2025-11-25 on `mcp` 1.29.0), negotiated via `initialize`. The degradation is logged once per process at WARNING. |

`mcp<1.24` is not supported — the `streamable_http_client` symbol does not exist under
that name in earlier releases, so the module fails at import.

**This is not full 2026-07-28 conformance.** On `mcp>=2.0` the module negotiates and
speaks protocol 2026-07-28 — `server/discover` era detection, `_meta` on every request,
the required HTTP headers, cursor-following pagination — but several obligations are
not implemented, including one the spec makes a client **MUST**:

- `x-mcp-header` mirroring and the `tools/list` exclusion rule it implies (a client MUST)
- `resultType` handling and MRTR / elicitation
- `subscriptions/listen` and all server→client notification handling
- cache hints (`ttlMs` / `cacheScope`)
- OAuth authorization (static config headers only)

Note also that under Amplifier's default logging configuration only WARNING and above
is shown, so the negotiated protocol version — logged at INFO — is not visible to the
user unless the connection degraded to the legacy handshake.

See [`docs/GAP_ANALYSIS.md`](docs/GAP_ANALYSIS.md) for the per-obligation conformance
table, what was actually executed to verify it, and what remains unverified.

---

## Installation

### Recommended: Always-Available MCP Support

Add MCP capabilities to **all** your Amplifier sessions with a single command:

```bash
amplifier bundle add git+https://github.com/microsoft/amplifier-module-tool-mcp@main#subdirectory=behaviors/mcp.yaml --app
```

The `--app` flag registers MCP as an "app bundle" that automatically composes with every session, regardless of which primary bundle you use. Configure your servers in `~/.amplifier/mcp.json` and they'll be available everywhere.

### Quick Demo with Example Servers

Try MCP immediately with pre-configured servers (repomix, context7, deepwiki):

```bash
amplifier bundle add git+https://github.com/microsoft/amplifier-module-tool-mcp@main#subdirectory=examples/bundle.md
amplifier bundle use mcp-example
```

This example bundle includes foundation and comes with repomix, context7, and deepwiki servers pre-configured.

---

## Prerequisites

- **Python 3.11+**
- **[UV](https://github.com/astral-sh/uv)** - Fast Python package manager

### Installing UV

```bash
# macOS/Linux/WSL
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

## Installation

### Recommended: Include the Behavior

Add MCP capability to your bundle by including the behavior:

```yaml
---
bundle:
  name: my-bundle
  version: 1.0.0
  description: My custom bundle with MCP support

includes:
  - bundle: git+https://github.com/microsoft/amplifier-foundation@main
  - bundle: git+https://github.com/microsoft/amplifier-module-tool-mcp@main#subdirectory=behaviors/mcp.yaml
---

# Your bundle instructions...
```

**What this gives you:**
- ✅ MCP tool module configured with production defaults
- ✅ Content size protection enabled (50k char limit)
- ✅ Clean console output (server logs saved to files)
- ✅ Automatic server discovery from `.amplifier/mcp.json`
- ✅ Clean dependency chain (no redundant includes)

**Why this pattern?**
- You control your foundation version
- Explicit about what capabilities you're adding
- Production-ready defaults out of the box
- Easy to customize configuration if needed

### Alternative: Direct Module Integration

You can also add the module directly with custom configuration:

```yaml
---
bundle:
  name: my-bundle
  version: 1.0.0

includes:
  - bundle: git+https://github.com/microsoft/amplifier-foundation@main

tools:
  - module: tool-mcp
    source: git+https://github.com/microsoft/amplifier-module-tool-mcp@main
    config:
      verbose_servers: true  # Custom: show server output
      max_content_size: 100000  # Custom: larger content limit
---

# Your bundle instructions...
```

### For Module Development

If you're developing the tool-mcp module itself:

```bash
cd amplifier-module-tool-mcp
uv sync
```

---

## Quick Start

### 1. Add MCP to Your Bundle

```yaml
# your-bundle.md
includes:
  - bundle: git+https://github.com/microsoft/amplifier-foundation@main
  - bundle: git+https://github.com/microsoft/amplifier-module-tool-mcp@main#subdirectory=behaviors/mcp.yaml
```

### 2. Configure MCP Servers

Create `.amplifier/mcp.json`:

```json
{
  "mcpServers": {
    "postgres": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-postgres"],
      "env": {
        "POSTGRES_CONNECTION_STRING": "${DATABASE_URL}"
      }
    },
    "puppeteer": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-puppeteer"]
    }
  }
}
```

**Popular MCP Servers:**
- `@modelcontextprotocol/server-postgres` - PostgreSQL database operations
- `@modelcontextprotocol/server-puppeteer` - Web automation and scraping
- `@modelcontextprotocol/server-sqlite` - SQLite database operations
- `@modelcontextprotocol/server-brave-search` - Web search via Brave API
- See [MCP Server Registry](https://github.com/modelcontextprotocol/servers) for more

**Note:** Amplifier's built-in filesystem and bash tools are recommended for file operations and shell commands rather than MCP filesystem servers.

### 3. Use Your Bundle

```bash
amplifier bundle use your-bundle.md
amplifier run "What MCP tools are available?"
```

The agent will automatically discover and use tools from your configured MCP servers!

### Complete Example

See `examples/mcp-example.md` for a complete working bundle showing:
- How to include the MCP behavior in your bundle
- Server configuration examples
- Usage instructions
- Customization options

---

## Configuration

### Module Configuration Options

The module can be configured via bundle behavior or direct module inclusion:

```yaml
# In your bundle.md
tools:
  - module: tool-mcp
    source: git+https://github.com/microsoft/amplifier-module-tool-mcp@main
    config:
      # Server output control
      verbose_servers: false  # Default: suppress MCP server debug output
      server_log_dir: ~/.amplifier/logs/mcp-servers/  # Where logs are saved when suppressed
      
      # Content size protection (prevents context exhaustion)
      max_content_size: 50000  # Default: 50k chars (~12k tokens)

      # Optional inline server config (overrides .amplifier/mcp.json)
      servers:
        my-server:
          command: npx
          args: ["-y", "my-mcp-server"]
```

**Configuration options**:

| Option | Default | Description |
|--------|---------|-------------|
| `verbose_servers` | `false` | Show MCP server stderr output in console |
| `server_log_dir` | `~/.amplifier/logs/mcp-servers/` | Directory for server logs when suppressed |
| `max_content_size` | `50000` | Shared character budget for tool text, structured evidence and envelope presentation; integer >= 1024. Oversized tool results get private exact recovery, not silent truncation |
| `artifact_dir` | `~/.amplifier/artifacts/mcp` | Private recovery directory: POSIX owner/mode 0700, or Windows current-process-user SID with protected owner-only DACL. Files persist until the host explicitly deletes them |
| `servers` | (optional) | Inline server configuration (overrides .amplifier/mcp.json) |
| `expose_tools` | `true` | Boolean only. `false` keeps all MCP tool/resource/prompt handles private: no agent-visible tools or visibility advertising. Invalid values fail before any server starts |

**Environment variable override**:
```bash
# Enable verbose output without changing bundle config
AMPLIFIER_MCP_VERBOSE=true amplifier run "test"
```

**When to use verbose mode**:
- Debugging MCP server connection issues
- Diagnosing tool execution failures
- Understanding server initialization

**Default behavior (quiet)**:
- Clean console output
- Server logs saved to `~/.amplifier/logs/mcp-servers/{server-name}.log`
- Check logs if server connection fails

---

### Configuration Priority

1. **Inline config** (servers in bundle config) - highest priority
2. **Project config** (`.amplifier/mcp.json`) - recommended
3. **User config** (`~/.amplifier/mcp.json`) - fallback
4. **Environment variable** (`AMPLIFIER_MCP_CONFIG`) - override

### Opt-in exact tool restrictions

Add `tools` to an individual server configuration (inline `servers` or
`mcpServers` in `mcp.json`):

```json
{
  "mcpServers": {
    "example": {
      "command": "python",
      "args": ["local_server.py"],
      "tools": ["read_document", "search"]
    }
  }
}
```

- **Absent `tools`** preserves legacy unrestricted tool discovery/dispatch.
- **`tools: []`** explicitly denies every tool. A list allows only those exact
  original MCP names, not their `mcp_{server}_` wrapper names. No patterns,
  empty names, surrounding whitespace, non-string entries or `null`.
- Invalid restrictions fail mount before any server starts. Unknown keys in an
  opted-in server, and unsupported tool/permission restriction keys, raise
  visibly rather than silently becoming unrestricted. Supported server keys
  are `tools`, `type`, `command`, `args`, `env`, `url`, `headers`.
- The same policy filters exposure and checks each stdio/HTTP dispatch, including
  direct client calls, previously mounted wrappers and newly advertised tools.
  Annotations such as `readOnlyHint` are retained hints, **not permissions**.

On a server with `tools` present, resources and prompts are neither discovered
nor exposed; lower-level reads/retrievals also fail locally. These are separate
MCP RPCs, not approved tools, and server implementations can have side effects.
Unrestricted servers retain their existing resource/prompt behavior. This is a
connector capability boundary, not an agent-wide sandbox or a guarantee about
other tools, network access, filesystem access or server internals. No OAuth or
sensitivity-label enforcement is added.

Configuration precedence is **inline > environment > project > user**; a higher
priority server entry replaces the entire lower-priority entry, as before.
Malformed existing JSON files/container shapes now fail visibly: silently
falling back could otherwise discard a restriction. Missing files remain normal.

### Private backend port for policy consumers

Set module-level `expose_tools: false` when another module owns agent-facing
policy. MCP alone then mounts **zero** agent-visible capabilities, even if the
consumer fails to import or mount. Load MCP before the consumer.

The public Core inter-module port is:

```python
backend = coordinator.get_capability("mcp.backends.v1")
# {"exposed": bool, "capabilities": dict[str, CoreTool-compatible handle]}
handle = backend["capabilities"]["mcp_example_read_document"]
result = await handle.execute({"document_id": "synthetic"})
```

Keys use the same `mcp_{server}_{name}` namespace as ordinary mounts. Values are
the existing approved/filter-checked wrappers (including resource/prompt wrappers
only on unrestricted servers), with Core Tool properties and `execute`.
Dispatch restrictions, metadata, evidence bounds and recovery stay unchanged.
The port is neither an LLM tool nor a raw-output dump and creates no second client.
Consumers must validate the port and select their own approved handles; the
connector does not implement domain policy.

Default `true` preserves existing tool mounting and visibility. Both modes
retain namespace collision checks. Any existing non-`None` `mcp.backends.v1`
registration is rejected before startup, without overwrite or implicit merging.
Cleanup closes clients and retires retained handles; this session-scoped port
is not reusable for remount. Use a fresh coordinator for a cold remount.
Failed mounts also close/retire clients, including partial startup and
cancellation. Core registration is not transactional: earlier tools/hooks may
remain after a later failure, but retained handles cannot reconnect or issue
RPCs. The backend port is published only after tool and visibility-hook setup.
`visibility` must be a dictionary; `null`, lists and strings fail before startup.
Private mode is an exposure boundary, not a sandbox against other trusted host
modules accessing Core capabilities.

### Tool evidence and schema lifetime

Tool output retains `content` text and `structured_content` together. An MCP
`isError` is a failed `ToolResult` with inspectable output evidence and
`error.mcp_is_error`; JSON-RPC failures retain code/data/context. Nested domain
statuses are not classified by this generic adapter. Tool calls are single-shot:
even the legacy `call_tool_with_retry` entry point no longer retries an operation.
Connection establishment is separate from tool execution.

Catalogue dictionaries retain `output_schema`, `annotations`, `meta` and the
complete SDK-parsed `mcp_tool_definition`; wrappers expose that definition too.
Core's Tool protocol advertises name, description and **input schema**, not MCP
output schemas or annotations. These fields remain available for connector
consumers; this does not claim that providers automatically receive them.

Within the shared presentation budget, `mcp_envelope` retains the SDK-parsed
result, including result/content `_meta`, annotations, nontext content and SDK
extension fields. When duplication alone would exceed the cap, known equal text
and structured fields are removed from this presentation envelope and described
by `mcp_envelope_references`: each entry has a `path` (dictionary keys/list indices)
and `source` (`content` or `structured_content`). Text entries also have `start`
and `length` character offsets. Insert the referenced value at that path to
reconstruct the exact SDK envelope. All other fields remain unchanged; reference
descriptors count against the shared budget. Ordinary 30k text at the default
50k cap therefore needs no recovery file. Small envelopes retain the existing
full-envelope shape without references.

`evidence_complete: true` means exact SDK-envelope evidence is available inline
(possibly by those explicit references), **not** that a service returned all
query results or that its schema proves correctness. No raw oversized envelope
is injected into context.

If the combined text/structured/envelope presentation exceeds the budget:

1. `content_truncated: true` and `evidence_complete: false` identify partial
   presentation. Oversized structured objects become `structured_content: null`
   plus a bounded serialized `structured_preview`, not a misleading partial object.
2. `recovery` supplies an absolute path, UTF-8 byte count, SHA-256, format and
   `complete: true` for the **full SDK-parsed envelope**. Recovery is canonical
   JSON (`sort_keys=True`, compact separators, `ensure_ascii=False`, SDK
   `model_dump(mode="json", by_alias=True, exclude_none=True)`), not original
   transport bytes. Exact text, including the tail, is retained inside it.
3. Recovery files use random identifiers, exclusive creation (never overwrite),
   POSIX mode 0600 and a mode-0700 user-owned directory. On Windows, `pywin32`
   (a Windows-only dependency) creates both leaf directory and file with a
   protected, non-NULL DACL granting full control only to the current process
   user SID. Owner, DACL and file type are checked through handles before any
   evidence is written. `CREATE_NEW` prevents overwrite. The leaf directory
   handle denies delete sharing; reparse-point leaves and unsafe existing DACLs
   are rejected without repairing them. A symlink leaf directory or nonprivate
   existing directory is rejected. Failure to create recovery is a failed result,
   never successful but unrecoverable evidence. Ancestors and same-user/admin
   actions are outside this bounded privacy mechanism; filesystem ACL support
   is required on Windows (no chmod-as-ACL assumption).

The host owns artifact access, disk capacity and deletion. Files survive session
cleanup **until explicitly deleted by the host**; no automatic retention expiry
is implied. Read the indicated file with an authorized local file capability,
verify its byte count/hash, and extract only the needed section. The connector
does not auto-load that file into model context. Resource/prompt wrappers on
unrestricted servers retain their legacy text-only truncation behavior; this
tool-envelope recovery contract does not claim recovery for those separate RPCs.

**Verification limit:** the Windows ACL writer is implemented with executable
fake-Win32 API regressions and a native-Windows-only ACL/roundtrip test.
Mac tests verify the implementation's API sequence, not OS ACL enforcement.
Actual native Windows execution remains unverified here and is required before
Windows platform acceptance. Missing `pywin32`, unsupported filesystem security,
unsafe permissions and incomplete writes fail explicitly.

Mounted input schemas are snapshots. **Cold remount** means cleanup the old
mount/session, then mount with a fresh coordinator/session and current config;
discovery and restrictions run again. Old clients are retired at manager cleanup
and stale wrappers cannot reconnect. There is no automatic hot schema/catalogue
refresh. Arguments are forwarded intact, without coercion or dropping unknown
keys; a server schema mismatch remains a visible failure. Non-object input
schema shapes fail registration rather than being rewritten.

**Intentional compatibility changes:** malformed existing JSON/config now fails
instead of falling back; restriction-key validation and namespace collisions fail
visibly; `max_content_size` has a minimum of 1024; `call_tool_with_retry` is
single-shot to avoid duplicate effects; recovery failure returns `success=False`
rather than degrading unrecoverable evidence into success.

Upstream details can appear in model/provider input, logs and recovery files.
There is no automatic authentication-data redaction or sandbox; configure
trusted servers and host access/retention accordingly.

## Usage

### How MCP Tools Appear to Agents

Once configured, MCP server tools become available like any other Amplifier tool:

```
User: "List files in the project"

Agent: [Uses mcp_filesystem_list_directory tool from MCP server]
Response: [Directory listing from MCP filesystem server]
```

All MCP tools are prefixed with `mcp_{server}_{tool}`. Sanitization and prefix
concatenation can produce identical mounted names; ambiguous tool/resource/prompt
names or collisions with existing host tools fail mount visibly rather than
silently overwriting a capability. No alternative names are invented.

### Example: Using Multiple MCP Servers

```yaml
---
bundle:
  name: data-assistant
  version: 1.0.0

includes:
  - bundle: git+https://github.com/microsoft/amplifier-foundation@main
  - bundle: git+https://github.com/microsoft/amplifier-module-tool-mcp@main#subdirectory=behaviors/mcp.yaml
---

You are a data assistant with access to databases and web automation via MCP.

Use postgres tools for database operations.
Use puppeteer tools for web scraping and automation.
```

With `.amplifier/mcp.json`:
```json
{
  "mcpServers": {
    "postgres": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-postgres"],
      "env": {
        "POSTGRES_CONNECTION_STRING": "${DATABASE_URL}"
      }
    },
    "puppeteer": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-puppeteer"]
    }
  }
}
```

Agent gets tools like:
- `mcp_postgres_query` - Execute SQL queries
- `mcp_postgres_list_tables` - List database tables
- `mcp_puppeteer_navigate` - Navigate to URLs
- `mcp_puppeteer_screenshot` - Capture screenshots

---

## Development

### Local Development Setup

When developing the module locally, use the `file://` protocol to test changes without pushing to GitHub:

```bash
# 1. Clone and set up the module
cd ~/your-workspace
git clone https://github.com/microsoft/amplifier-module-tool-mcp.git
cd amplifier-module-tool-mcp
uv sync

# 2. Run tests
# Use the isolated invocation in "Running Tests" below.

# 3. Create a local development bundle
# See examples/bundle-local-dev.md for a template

# 4. Add your local bundle
amplifier bundle add file://$(pwd)/examples/bundle-local-dev.md

# 5. Use the bundle
amplifier bundle use mcp-local-dev

# 6. Test your changes
amplifier run "list your tools"
```

**Important: Cache Management**

Amplifier caches bundles on first load. When you make changes to your local module, you must clear the cache:

```bash
# Clear MCP module cache
find ~/.amplifier/cache -type d -name "amplifier-module-tool-mcp-*" -exec rm -r {} +

# Then reload your bundle
amplifier run "test command"
```

**Local Bundle Example** (`examples/bundle-local-dev.md`):

```yaml
---
bundle:
  name: mcp-local-dev
  version: 1.0.0
  description: Local development bundle for testing MCP changes

includes:
  - bundle: git+https://github.com/microsoft/amplifier-foundation@main

tools:
  - module: tool-mcp
    source: file:///absolute/path/to/amplifier-module-tool-mcp
    config:
      servers:
        repomix:
          command: npx
          args: ["-y", "repomix", "--mcp"]
---

You are an AI assistant with MCP server capabilities enabled (LOCAL DEV VERSION).
```

### Running Tests

Provision dependencies before entering the clean environment (`uv sync` may be
used for provisioning outside it). The test interpreter must already have Python
3.11+, Amplifier Core, MCP SDK >=1.24, Pydantic >=2, and the dev dependencies
`pytest`, `pytest-asyncio` and `pytest-timeout` installed (`pywin32` on Windows).
Use an absolute interpreter path; do not run `uv` or install dependencies inside
the isolated invocation.

From the repository root, use this POSIX invocation for local and mocked tests,
including real synthetic stdio servers. Replace `TEST_PYTHON` with the absolute
path to that pre-provisioned interpreter:

```bash
TEST_PYTHON=/absolute/path/to/pre-provisioned/venv/bin/python
umask 077
mkdir -p "$PWD/.private-runtime/home" "$PWD/.private-runtime/tmp"
env -i \
  HOME="$PWD/.private-runtime/home" TMPDIR="$PWD/.private-runtime/tmp" \
  PATH=/usr/bin:/bin PYTHONPATH="$PWD" PYTHONNOUSERSITE=1 \
  PYTHONDONTWRITEBYTECODE=1 \
  "$TEST_PYTHON" -m pytest tests -q -p no:cacheprovider \
  --basetemp="$PWD/.private-runtime/pytest" \
  --ignore=tests/test_http_integration.py
```

Boundary fixtures refuse unexpected inherited environment keys or paths outside
the repository. Keep `.private-runtime` private and out of published artifacts;
it contains test-owned subprocess logs, journals and recovery files.
The live HTTP integration tests contact an external service and are excluded
above. Run them separately only when that network access is intended. Tests
requiring unavailable `npx`/`uvx`, or a native Windows host, skip explicitly;
those skips do not establish platform or external-service acceptance.

---

## Documentation

- [`docs/GAP_ANALYSIS.md`](docs/GAP_ANALYSIS.md) — current conformance status against
  the MCP 2026-07-28 specification: what is implemented, what is not, and what is
  deliberately excluded.
- [`CONTENT_SIZE_PROTECTION.md`](CONTENT_SIZE_PROTECTION.md) — how oversized tool
  results are truncated.

Other files under `docs/` are 2025-10-18 development-era snapshots. Each carries a
header saying so; they are retained as history and should not be used for planning.

---

## License

MIT

## Contributing

> [!NOTE]
> This project is not currently accepting external contributions, but we're actively working toward opening this up. We value community input and look forward to collaborating in the future. For now, feel free to fork and experiment!

Most contributions require you to agree to a
Contributor License Agreement (CLA) declaring that you have the right to, and actually do, grant us
the rights to use your contribution. For details, visit [Contributor License Agreements](https://cla.opensource.microsoft.com).

When you submit a pull request, a CLA bot will automatically determine whether you need to provide
a CLA and decorate the PR appropriately (e.g., status check, comment). Simply follow the instructions
provided by the bot. You will only need to do this once across all repos using our CLA.

This project has adopted the [Microsoft Open Source Code of Conduct](https://opensource.microsoft.com/codeofconduct/).
For more information see the [Code of Conduct FAQ](https://opensource.microsoft.com/codeofconduct/faq/) or
contact [opencode@microsoft.com](mailto:opencode@microsoft.com) with any additional questions or comments.

## Trademarks

This project may contain trademarks or logos for projects, products, or services. Authorized use of Microsoft
trademarks or logos is subject to and must follow
[Microsoft's Trademark & Brand Guidelines](https://www.microsoft.com/legal/intellectualproperty/trademarks/usage/general).
Use of Microsoft trademarks or logos in modified versions of this project must not cause confusion or imply Microsoft sponsorship.
Any use of third-party trademarks or logos are subject to those third-party's policies.
