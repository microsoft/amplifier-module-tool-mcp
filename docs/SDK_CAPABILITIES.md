> **HISTORICAL SNAPSHOT — DO NOT PLAN FROM THIS DOCUMENT.**
>
> This is a mid-development snapshot from **2025-10-18**. It describes the module as it
> was during initial development, not as it exists today. Features it lists as missing or
> planned have since been implemented, and it targets MCP spec revision 2025-03-26, which
> the module no longer targets.
>
> For the current state of the module against the MCP **2026-07-28** specification, see
> [`GAP_ANALYSIS.md`](GAP_ANALYSIS.md) (dated 2026-07-29).
>
> Retained as development history only.

---

# MCP Python SDK - Capabilities Audit

**SDK Version**: 1.14.1
**Date**: 2025-10-18
**Purpose**: Determine what the SDK already supports vs what we need to implement

---

## 🎉 SDK Feature Support

### Transport Types

| Transport | SDK Support | Our Status | Notes |
|-----------|-------------|------------|-------|
| stdio | ✅ `mcp.client.stdio` | ✅ Implemented | Working |
| SSE (HTTP) | ✅ `mcp.client.sse` | ❌ Not used | **Ready to add!** |
| Streamable HTTP | ✅ `mcp.client.streamable_http` | ❌ Not used | **Ready to add!** |

**Key Finding**: ✅ **ALL transports available in SDK!**

---

### MCP Primitives & Features

From `ClientSession` methods available:

| Feature | SDK Method | Our Status | Priority |
|---------|-----------|------------|----------|
| **Tools** | ✅ | ✅ | - |
| ├─ list_tools() | ✅ Available | ✅ Using | Done |
| └─ call_tool() | ✅ Available | ✅ Using | Done |
| **Resources** | ✅ | ❌ | 🔴 HIGH |
| ├─ list_resources() | ✅ Available | ❌ Not used | **Easy add!** |
| ├─ read_resource() | ✅ Available | ❌ Not used | **Easy add!** |
| ├─ list_resource_templates() | ✅ Available | ❌ Not used | **Easy add!** |
| ├─ subscribe_resource() | ✅ Available | ❌ Not used | **Easy add!** |
| └─ unsubscribe_resource() | ✅ Available | ❌ Not used | **Easy add!** |
| **Prompts** | ✅ | ❌ | 🟡 MEDIUM |
| ├─ list_prompts() | ✅ Available | ❌ Not used | **Easy add!** |
| └─ get_prompt() | ✅ Available | ❌ Not used | **Easy add!** |
| **Sampling** | ✅ | ❌ | 🟢 LOW |
| └─ complete() | ✅ Available | ❌ Not used | **Easy add!** |
| **Logging** | ✅ | ❌ | 🟡 MEDIUM |
| ├─ set_logging_level() | ✅ Available | ❌ Not used | **Easy add!** |
| └─ send_notification() | ✅ Available | ❌ Not used | **Easy add!** |
| **Progress** | ✅ | ❌ | 🟢 LOW |
| └─ send_progress_notification() | ✅ Available | ❌ Not used | **Easy add!** |
| **Other** | | | |
| ├─ send_ping() | ✅ Available | ❌ Not used | Health checks |
| ├─ send_roots_list_changed() | ✅ Available | ❌ Not used | Workspace mgmt |
| └─ send_request() | ✅ Available | ❌ Not used | Generic RPC |

---

## 💡 HUGE Finding: SDK Has Everything!

**The SDK already implements**:
- ✅ All 3 transport types (stdio, SSE, streamable HTTP)
- ✅ Resources (list, read, subscribe, templates)
- ✅ Prompts (list, get)
- ✅ Sampling (complete)
- ✅ Logging (set_logging_level, notifications)
- ✅ Progress (send_progress_notification)

**We just need to**:
- Wire them up to Amplifier
- Expose them through our wrapper
- Add configuration

**This makes implementation MUCH easier than expected!**

---

## 🚀 Revised Implementation Effort

### Original Estimates (Building from Scratch)

| Feature | Original Estimate | Actual Effort |
|---------|------------------|---------------|
| HTTP/SSE Transport | 2-3 hours | **30 minutes** ✨ |
| Streamable HTTP | 3-4 hours | **30 minutes** ✨ |
| Resources | 4-6 hours | **1-2 hours** ✨ |
| Prompts | 3-4 hours | **1 hour** ✨ |
| Logging | 2 hours | **30 minutes** ✨ |
| Progress | 2 hours | **30 minutes** ✨ |
| Sampling | 6-8 hours | **1-2 hours** ✨ |

**Reason**: SDK does the heavy lifting, we just expose it!

---

## 📋 Updated Roadmap

### Phase 4: High Impact Features (2-3 hours total)

#### 1. HTTP/SSE Transport (~30 min)
```python
from mcp.client.sse import sse_client

class MCPHTTPClient(MCPClient):
    async def connect(self):
        # Similar structure, different transport
        read, write = await self._exit_stack.enter_async_context(
            sse_client(self.url)
        )
        # ... rest is same as stdio
```

#### 2. Resources (~1-2 hours)
```python
# In MCPClient
async def list_resources(self):
    result = await self.session.list_resources()
    return result.resources

async def read_resource(self, uri: str):
    result = await self.session.read_resource(uri)
    return result.contents

# Wrapper for Amplifier (expose as tools)
class MCPResourceTool:
    name = f"mcp_{server}_resource_{resource_name}"
    async def execute(self, input):
        return await client.read_resource(input["uri"])
```

#### 3. Logging (~30 min)
```python
# In MCPClient
async def set_log_level(self, level: str):
    await self.session.set_logging_level(level)

# Handle notifications
# Listen for logging notifications from server
```

**Total**: 2-3 hours for all three!

---

### Phase 5: Prompts & Streamable HTTP (1-2 hours total)

#### 1. Prompts (~1 hour)
```python
# Similar to tools - discover and wrap
async def list_prompts(self):
    result = await self.session.list_prompts()
    return result.prompts

async def get_prompt(self, name: str, args: dict):
    result = await self.session.get_prompt(name, arguments=args)
    return result.messages
```

#### 2. Streamable HTTP (~30 min)
```python
from mcp.client.streamable_http import streamable_http_client

# Same pattern as SSE, just different import
```

---

## 🔍 Detailed SDK Exploration

### Transport Clients Available

```python
# stdio (we use this)
from mcp.client.stdio import stdio_client, StdioServerParameters

# SSE/HTTP (ready to use)
from mcp.client.sse import sse_client

# Streamable HTTP (ready to use - note: no underscore)
from mcp.client.streamable_http import streamablehttp_client

# Alternative HTTP factory methods
from mcp.client.sse import create_mcp_http_client, aconnect_sse
from mcp.client.streamable_http import create_mcp_http_client
```

### ClientSession Full API

**Tools** (we use):
- ✅ `list_tools()` - Get available tools
- ✅ `call_tool(name, arguments)` - Execute tool

**Resources** (SDK has, we don't use):
- ✅ `list_resources()` - Get available resources
- ✅ `read_resource(uri)` - Read resource content
- ✅ `subscribe_resource(uri)` - Subscribe to updates
- ✅ `unsubscribe_resource(uri)` - Unsubscribe
- ✅ `list_resource_templates()` - Get resource templates

**Prompts** (SDK has, we don't use):
- ✅ `list_prompts()` - Get available prompts
- ✅ `get_prompt(name, arguments)` - Get filled prompt

**Sampling** (SDK has, we don't use):
- ✅ `complete(prompt, params)` - Request LLM completion

**Logging** (SDK has, we don't use):
- ✅ `set_logging_level(level)` - Set server log level
- ✅ `send_notification(method, params)` - Generic notifications

**Progress** (SDK has, we don't use):
- ✅ `send_progress_notification(...)` - Send progress updates

**Utilities**:
- ✅ `send_ping()` - Health check
- ✅ `send_roots_list_changed()` - Workspace notification
- ✅ `send_request(method, params)` - Generic JSON-RPC

---

## 📊 What This Means

### Everything is Already in the SDK!

**We thought we needed to**:
- Implement HTTP transport from scratch
- Build resource reading system
- Create prompt management
- Handle logging protocol

**Reality**:
- ✅ SDK has it all
- ✅ Just call the methods
- ✅ Wrap for Amplifier
- ✅ Much faster implementation

### Revised Effort Estimates

| Feature | Old Estimate | New Estimate | Why Faster |
|---------|--------------|--------------|------------|
| HTTP/SSE | 2-3 hours | **30 min** | Just different import |
| Resources | 4-6 hours | **1-2 hours** | SDK does heavy lifting |
| Prompts | 3-4 hours | **1 hour** | SDK does heavy lifting |
| Logging | 2 hours | **30 min** | Already implemented |
| Streamable HTTP | 3-4 hours | **30 min** | Just different import |
| Progress | 2 hours | **30 min** | Already implemented |
| Sampling | 6-8 hours | **1-2 hours** | SDK handles protocol |

**Total for all high-priority features**: ~3-4 hours (not 15-20!)

---

## 🎯 Your Priority List + Ease

Based on your preferences (high impact + logging):

### 1. HTTP/SSE Transport 🔴 ⚡ **EASIEST!**
- **Impact**: HIGH - Unlocks deepwiki + all web servers
- **Effort**: 30 minutes
- **Difficulty**: EASY - Just use `sse_client` instead of `stdio_client`
- **SDK Support**: ✅ Complete

### 2. Resources 🔴 ⚡ **EASY!**
- **Impact**: HIGH - File access, data resources
- **Effort**: 1-2 hours
- **Difficulty**: EASY - SDK has all methods
- **SDK Support**: ✅ Complete (5 methods!)

### 3. Logging 🟡 ⚡ **EASIEST!**
- **Impact**: MEDIUM - Better debugging
- **Effort**: 30 minutes
- **Difficulty**: TRIVIAL - SDK has methods
- **SDK Support**: ✅ Complete

**Total time for all 3**: 2-3 hours!

---

## 📝 Implementation Plan

### Quick Wins (Next 3 Hours)

**Hour 1: HTTP/SSE Transport**
1. Create `MCPHTTPClient` class (copy MCPClient, change transport)
2. Auto-detect transport type from config
3. Test with deepwiki
4. ✅ 5/5 servers working!

**Hour 2: Resources**
1. Add `list_resources()` to MCPClient
2. Add `read_resource()` to MCPClient
3. Create `MCPResourceWrapper` (similar to MCPToolWrapper)
4. Register resources with Amplifier
5. ✅ File access and data resources!

**Hour 3: Logging**
1. Add `set_logging_level()` wrapper
2. Handle logging notifications
3. Forward to Amplifier logging system
4. ✅ Better debugging!

---

## 🎓 Code Examples (SDK Ready!)

### HTTP Transport
```python
# Already available!
from mcp.client.sse import sse_client

async with sse_client("http://localhost:3000/mcp") as (read, write):
    async with ClientSession(read, write) as session:
        await session.initialize()
        # Works exactly like stdio!
```

### Resources
```python
# Already available!
resources = await session.list_resources()
for resource in resources.resources:
    content = await session.read_resource(resource.uri)
    print(f"{resource.name}: {content}")
```

### Prompts
```python
# Already available!
prompts = await session.list_prompts()
for prompt in prompts.prompts:
    filled = await session.get_prompt(prompt.name, arguments={})
    print(f"{prompt.name}: {filled.messages}")
```

### Logging
```python
# Already available!
await session.set_logging_level("debug")
# Server will send logging notifications
```

---

## ✅ Conclusion

### What We're Missing is Just Wiring!

**SDK Has**: ✅ Everything
**We Need**: Wire it to Amplifier (2-3 hours)

### High Priority Features (Your List)

1. ✅ **HTTP/SSE Transport** - SDK ready, 30 min to wire
2. ✅ **Resources** - SDK ready, 1-2 hours to wire
3. ✅ **Logging** - SDK ready, 30 min to wire

**All three: 2-3 hours total work!**

This is WAY easier than building from scratch. The SDK is comprehensive!

---

**Ready to implement these in the next session?** With 2-3 hours we can have HTTP transport, Resources, and Logging all working!
