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

1. **Inline config** - `servers:` block in your bundle's tool config (used by example)
2. **Environment variable** - `$AMPLIFIER_MCP_CONFIG` pointing to a JSON file
3. **Project config** - `.amplifier/mcp.json` in current directory (HTTP only)
4. **User config** - `~/.amplifier/mcp.json` in home directory

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

If you prefer to keep trusted stdio server configuration separate from your
bundle, copy the reference file to your user configuration:

```bash
# Copy the reference mcp.json to your user configuration
mkdir -p ~/.amplifier
cp examples/mcp.json ~/.amplifier/mcp.json
# Edit ~/.amplifier/mcp.json to customize

# Then use a minimal bundle without inline config
amplifier bundle use git+https://github.com/microsoft/amplifier-module-tool-mcp@main#subdirectory=behaviors/mcp.yaml
```

Project-local `.amplifier/mcp.json` files may configure HTTP servers, but stdio
commands are ignored to prevent a cloned repository from executing programs.

## Popular MCP Servers

See [MCP Server Registry](https://github.com/modelcontextprotocol/servers) for more servers.

**Note:** Amplifier's built-in filesystem and bash tools are recommended for file operations and shell commands rather than MCP filesystem servers.
