# MCP Module Examples

This directory contains example bundles showing how to use the MCP tool module.

## Recommended: Always-Available MCP (via Behavior)

For most users, add the MCP behavior with `--app` to make it available across all sessions:

```bash
# Add MCP behavior as app bundle (always active)
amplifier bundle add git+https://github.com/microsoft/amplifier-module-tool-mcp@main#subdirectory=behaviors/mcp.yaml --app

# Configure servers in ~/.amplifier/mcp.json
```

## Example Bundle with Pre-Configured Servers

This example includes foundation and comes with repomix, context7, and deepwiki servers pre-configured:

```bash
amplifier bundle add git+https://github.com/microsoft/amplifier-module-tool-mcp@main#subdirectory=examples/bundle.md
amplifier bundle use mcp-example
```

Or copy it to customize:

```bash
curl -o my-mcp-bundle.md https://raw.githubusercontent.com/microsoft/amplifier-module-tool-mcp/main/examples/bundle.md
amplifier bundle use my-mcp-bundle.md
```

## What's Included

### bundle.md
Complete working example with inline MCP server configuration showing:
- **repomix** - Code packaging and analysis
- **context7** - Documentation search  
- **deepwiki** - GitHub repository documentation (Streamable HTTP transport example)

### mcp.json
Reference configuration file showing the same servers in JSON format (not used by the example bundle, provided for reference)

## Configuration Options

The example bundle uses inline configuration (servers defined directly in the bundle). You can also use external configuration files.

### Configuration Discovery Priority

The MCP module looks for server configuration in this order (highest priority first):

1. **Trusted host inline config** - reviewed `servers:` block (used by example)
2. **Environment variable** - `$AMPLIFIER_MCP_CONFIG`, an absolute host-owned file outside the project
3. **User config** - host-owned `~/.amplifier/mcp.json` in home directory

Project definitions are ignored for both stdio and HTTP. See the
[configuration policy and migration guide](../README.md#configuration-priority)
for canonical-path checks, symlink/hardlink restrictions, and inline host trust.

### Customizing the Example

Copy the bundle and modify the `servers:` section:

```yaml
tools:
  - module: tool-mcp
    source: git+https://github.com/microsoft/amplifier-module-tool-mcp@main
    config:
      servers:
        my-server:
          command: npx
          args: ["-y", "my-mcp-server"]
        # Add more servers here
```

### Using External Configuration Files

If you prefer to keep trusted stdio and HTTP server configuration separate from
your bundle, review the reference file and merge approved entries into your user
configuration. Do not overwrite an existing file:

```bash
# Review examples/mcp.json, then create/edit your user configuration
mkdir -p ~/.amplifier
# Merge approved entries into ~/.amplifier/mcp.json manually

# Then use a minimal bundle without inline config
amplifier bundle use git+https://github.com/microsoft/amplifier-module-tool-mcp@main#subdirectory=behaviors/mcp.yaml
```

Do not symlink user configuration into a project or select a project file using
the environment variable. Make an independent, reviewed host-owned copy instead.
HTTP endpoints and their credential headers need the same review as commands.

## Popular MCP Servers

See [MCP Server Registry](https://github.com/modelcontextprotocol/servers) for more servers.

**Note:** Amplifier's built-in filesystem and bash tools are recommended for file operations and shell commands rather than MCP filesystem servers.
