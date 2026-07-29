# Gap Analysis — `amplifier-module-tool-mcp` vs MCP spec 2026-07-28

**Date:** 2026-07-29
**Module version:** 0.2.2
**Target spec revision:** 2026-07-28
**SDK dependency:** `mcp>=1.24` (no upper bound) — see `pyproject.toml`

This document supersedes the previous `GAP_ANALYSIS.md` (dated 2025-10-18, written
against spec revision 2025-03-26). That version listed Streamable HTTP, Resources,
and Prompts as missing; all three had long since been implemented. It was wrong in
both directions and should not be used for planning.

---

## 1. Thirty-second summary

**Which protocol the module speaks depends on the installed SDK:**

| Installed SDK | Protocol negotiated | Handshake |
|---|---|---|
| `mcp>=2.0` | 2026-07-28 (stateless) | `server/discover`; no `initialize`, no `notifications/initialized`; full `_meta` on every request |
| `mcp>=1.24,<2` | handshake-era (2025-11-25 observed on 1.29.0) | legacy `initialize()`; degradation logged once per process at INFO |

**Implemented and conforming:** protocol negotiation with automatic era fallback,
cursor-following pagination, real `clientInfo` identity, `structured_content`
passthrough, typed JSON-RPC error surfacing, SDK 1.x/2.x field compatibility,
stdio and Streamable HTTP transports, Tools / Resources / Prompts.

**Known gaps (not implemented):** `subscriptions/listen` and all server→client
notification handling, MRTR / elicitation, cache hints (`ttlMs` / `cacheScope`),
`x-mcp-header` mirroring, OAuth authorization.

**Deliberately excluded:** Roots, Sampling, HTTP+SSE transport — all deprecated in
2026-07-28. These are correctly absent and should not be built.

---

## 2. How to read the evidence claims in this document

Claims in this document are one of two kinds, and they are labelled:

- **[executed]** — observed by running the code, including raw capture of the
  stdio JSON-RPC wire traffic. Section 7 lists exactly what was executed.
- **[inspection]** — read from the source. Accurate as a description of what the
  code does; not proof that it behaves that way against a live server.

Nothing in this document is labelled verified unless it was actually run. Where a
behavior is only known by reading the code, it says so.

---

## 3. What changed in 2026-07-28

This is a breaking protocol revision, not an incremental one. The protocol became
stateless: every request self-describes rather than relying on a negotiated session.

### Removed

- `initialize` + `notifications/initialized` handshake
- `Mcp-Session-Id` header — no protocol-level sessions
- `logging/setLevel` — log level is now per-request via `_meta`
- `ping`
- `resources/subscribe` / `resources/unsubscribe` — replaced by `subscriptions/listen`
- HTTP GET stream endpoint (servers answer `405`)
- `Last-Event-ID` / SSE event IDs / stream resumability — a broken stream loses the
  in-flight request and the client MUST re-issue with a new request ID
- `notifications/roots/list_changed`
- Server-initiated JSON-RPC requests (roots / sampling / elicitation) — replaced by MRTR
- Core `tasks/*` — moved to the `io.modelcontextprotocol/tasks` extension

### Added

- **`_meta` on every request.** `io.modelcontextprotocol/protocolVersion` (MUST),
  `io.modelcontextprotocol/clientCapabilities` (MUST), and
  `io.modelcontextprotocol/clientInfo` (SHOULD). A request missing a required field
  is malformed and the server MUST reject it with `-32602` / HTTP 400.
- **`server/discover`** — servers MUST implement it; clients MAY call it for up-front
  version selection and SHOULD use it as the stdio backward-compatibility probe.
- **`resultType` on every result** — `"complete"` or `"input_required"`. Clients MUST
  treat an absent field (older servers) as `"complete"`.
- **MRTR (Multi Round-Trip Requests)** — servers return `InputRequiredResult` with
  `inputRequests`; the client answers via `inputResponses` on a retry of the original
  request, echoing `requestState`. This is now the only path for elicitation.
- **`subscriptions/listen`** — one long-lived POST-response stream, opt-in per
  notification type, tagged with `io.modelcontextprotocol/subscriptionId`.
- **Required HTTP headers** on every POST: `MCP-Protocol-Version` (MUST match the body
  `_meta`, else `HeaderMismatch` `-32020` / HTTP 400), `Mcp-Method`, `Mcp-Name`, and
  `Accept` listing both `application/json` and `text/event-stream`.
- **`x-mcp-header` mirroring — MUST for clients.** Clients must mirror annotated tool
  params into `Mcp-Param-{Name}` headers, and MUST exclude from `tools/list` any tool
  whose `x-mcp-header` annotations violate the constraints.
- **`CacheableResult`** — `ttlMs` and `cacheScope` on `tools/list`, `prompts/list`,
  `resources/list`, `resources/read`, `resources/templates/list`, and `DiscoverResult`.
- **New error codes** — `-32020` HeaderMismatch, `-32021` MissingRequiredClientCapability,
  `-32022` UnsupportedProtocolVersion. Resource-not-found moved `-32002` → `-32602`
  (clients SHOULD still accept `-32002` from older servers).

### Deprecated but still functional

Roots, Sampling, Logging, HTTP+SSE transport, `includeContext: thisServer|allServers`,
and OAuth Dynamic Client Registration. Earliest removal 2027-07-28. The module
implements none of Roots, Sampling, or HTTP+SSE, so there is nothing to migrate.

---

## 4. Conformance table

Legend: **OK** = implemented and conforming · **GAP** = required or expected, absent ·
**N/A** = deliberately excluded

| # | Spec obligation | Status | Where / note |
|---|---|:--:|---|
| 1 | Works on current SDK (`mcp` 2.x) | OK | `sdk_compat.sdk_field` bridges 1.x/2.x field renames [executed] |
| 2 | Works on `mcp` 1.x | OK | falls back to legacy handshake, degradation logged [executed] |
| 3 | Accurate dependency range | OK | `mcp>=1.24`, no upper bound; 1.23 and earlier fail at import |
| 4 | `_meta.protocolVersion` on every request (MUST) | OK | supplied by SDK under `negotiate_auto` [executed, wire capture] |
| 5 | `_meta.clientCapabilities` on every request (MUST) | OK | present as `{}`, which is legal [executed, wire capture] |
| 6 | `_meta.clientInfo` (SHOULD) | OK | `sdk_compat.build_client_info()` [executed, wire capture] |
| 7 | `server/discover` used for era detection | OK | via SDK `negotiate_auto` [executed, wire capture] |
| 8 | No `initialize` / `notifications/initialized` on modern path | OK | absent from wire capture on `mcp==2.0.0` [executed] |
| 9 | `nextCursor` pagination, cursors opaque | OK | `pagination.collect_paginated` [executed, multi-page server] |
| 10 | Empty-string cursor is NOT end-of-results | OK | terminates on `next_cursor is None` only [executed] |
| 11 | Stop calling `logging/setLevel` on modern sessions | OK | `client.py:503`, `streamable_http_client.py:435` [inspection] |
| 12 | Recognise `-32020` / `-32021` / `-32022` | OK | `sdk_compat.MCP_ERROR_DESCRIPTIONS` [inspection] |
| 13 | Resource-not-found `-32602`, still accept `-32002` | OK | both codes described; neither is special-cased in control flow [inspection] |
| 14 | `structuredContent` surfaced to callers | OK | `wrapper.py:100` [inspection] |
| 15 | `resultType` handling (MUST; absent ⇒ `complete`) | GAP | no module code reads it; see §5.2 |
| 16 | MRTR retry loop (`inputRequests` / `inputResponses` / `requestState`) | GAP | see §5.2 |
| 17 | Elicitation | GAP | see §5.2 |
| 18 | `subscriptions/listen` + `subscriptionId` tagging | GAP | see §5.1 |
| 19 | `listChanged` observed | GAP | consequence of §5.1 |
| 20 | `x-mcp-header` mirroring + invalid-tool exclusion (MUST) | GAP | see §5.4 |
| 21 | `ttlMs` / `cacheScope` honoured | GAP | see §5.3 |
| 22 | HTTP: `MCP-Protocol-Version`, `Mcp-Method`, `Mcp-Name` headers | OK | `MCP-Protocol-Version: 2026-07-28` on all 5 requests, matching body `_meta`; `Mcp-Method` exact on each (server/discover, tools/list, resources/list, prompts/list, tools/call); `Mcp-Name: add` present on tools/call only, correctly absent on list calls [executed, 6-request HTTP capture] — see §6 |
| 23 | HTTP: `Accept` lists both content types | OK | `accept: application/json, text/event-stream` on every request [executed, 6-request HTTP capture] — see §6 |
| 24 | Broken stream ⇒ re-issue with new request ID | partial | reconnection is full teardown + fresh negotiation, not per-request re-issue [inspection] |
| 25 | OAuth / authorization | GAP | static config headers only; see §5.5 |
| 26 | Roots | N/A | deprecated in 2026-07-28; deliberately not implemented |
| 27 | Sampling | N/A | deprecated in 2026-07-28; deliberately not implemented |
| 28 | HTTP+SSE transport | N/A | deprecated; Streamable HTTP only |
| 29 | `Mcp-Session-Id` | N/A | never used |
| 30 | `Last-Event-ID` resumability | N/A | removed from the protocol |

---

## 5. Known gaps, in detail

These are real, unimplemented obligations. They are listed here rather than buried
so that anyone planning work off this document sees them.

### 5.1 `subscriptions/listen` — no server→client notification handling at all

The module constructs `ClientSession` with no notification callbacks. There is no
`subscriptions/listen` stream, no `subscriptionId` tracking, and consequently no
`listChanged` observation: if a server adds or removes a tool after connect, the
module will not notice until the connection is torn down and re-established.

Grep evidence: no occurrence of `subscri`, `listChanged`, or any notification handler
in `amplifier_module_tool_mcp/`. [inspection]

### 5.2 MRTR / elicitation — not wired

`resultType` is never read by module code. On `mcp>=2.0` the SDK parses the field and
exposes `run_input_required_driver` and an `elicitation_callback` hook, but nothing in
this module supplies either. A server that returns `resultType: "input_required"` will
therefore not be answered; what the caller sees in that case has not been tested.

This is not a small wiring job. It needs a design pass first: MRTR requires the client
to answer a structured input request, and there is no established pattern for how an
Amplifier agent produces that answer — whether it is prompted to, whether the tool
call blocks, and how `requestState` is threaded through the module's tool-wrapper
boundary. That design question is unanswered.

Note also that if MRTR is wired, `_meta.clientCapabilities` must stop being `{}` and
begin declaring `elicitation`. [inspection]

### 5.3 Caching hints ignored

`ttlMs` and `cacheScope` arrive on `CacheableResult` payloads and are discarded.
Discovery runs once per connection and the result is held for the connection's
lifetime with no TTL. This is a performance gap, not a correctness one — the module is
not wrong, only unnecessarily chatty on reconnect.

Grep evidence: no occurrence of `ttlMs`, `ttl_ms`, or `cacheScope`. [inspection]

### 5.4 `x-mcp-header` mirroring — a client MUST, not implemented

The spec states that while `x-mcp-header` is optional for servers, clients MUST
support it. Two obligations are unmet:

1. Mirroring annotated tool parameters into `Mcp-Param-{Name}` HTTP headers.
2. Excluding from `tools/list` any tool whose `x-mcp-header` annotations violate the
   spec's constraints.

The second is the more consequential: the module currently exposes every tool a server
advertises, including ones the spec says a conforming client must filter out. This
applies to the Streamable HTTP transport only; it has no meaning over stdio.

Grep evidence: no occurrence of `x-mcp-header` or `Mcp-Param`. [inspection]

### 5.5 OAuth / authorization

Authorization is limited to static headers from configuration, with `${ENV}`
substitution. There is no OAuth flow, no token refresh, and none of the client-side
MUSTs that the authorization spec carries (`iss` validation, issuer-keyed credential
storage, `application_type` in Dynamic Client Registration). This is a separate
workstream, not a defect in the protocol layer.

---

## 6. What is not known (still-unverified items)

Items 22–23 (HTTP headers: `MCP-Protocol-Version`, `Mcp-Method`, `Mcp-Name`, `Accept`)
were verified via real request capture against a Streamable HTTP server on `mcp==2.0.0`
(see § 7). Three HTTP-layer requirements remain unverified due to lab constraints:

1. **`Mcp-Name` on `resources/read` and `prompts/get`** — `tools/call` was captured with
   correct header; the mechanism is generic and method-name-driven, so it should fire
   identically for resource and prompt read operations. [inspection]
   
2. **Non-ASCII header value encoding** — the `=?base64?...?=` sentinel encoding for
   non-ASCII-valued tool names and arguments. Never exercised in capture; test used
   ASCII-only identifiers. The RFC 2047 encoder exists in the SDK source; mechanism is
   unverified in practice.
   
3. **`x-mcp-header` mirroring** — `Mcp-Param-*` headers for annotated tool parameters.
   This is a client MUST (spec § 4.3.4) and remains unimplemented. See § 5.4.

---

## 7. What was actually executed

The following were run, not inferred:

1. **Raw stdio JSON-RPC wire capture on `mcp==2.0.0`.** Confirmed that
   `server/discover` is sent, that no `initialize` and no `notifications/initialized`
   appear anywhere in the exchange, and that every request carries:

   ```json
   {
     "io.modelcontextprotocol/protocolVersion": "2026-07-28",
     "io.modelcontextprotocol/clientInfo": {
       "name": "amplifier-module-tool-mcp",
       "version": "0.2.2"
     },
     "io.modelcontextprotocol/clientCapabilities": {}
   }
   ```

2. **Streamable HTTP request capture on `mcp==2.0.0`.** A live HTTP MCP server
   (`MCPServer.run_streamable_http_async`, `stateless_http=True`) was instrumented
   with a logging reverse proxy to record every inbound request's full header set.
   The module's `MCPStreamableHTTPClient` drove the full flow (connect → tools/list →
   resources/list → prompts/list → tools/call). Six requests captured:

   | Header | Result |
   |---|---|
   | `MCP-Protocol-Version: 2026-07-28` on all POST requests | **SATISFIED** — matched body `_meta.protocolVersion` in every case |
   | `Mcp-Method` (spec method name) on all requests | **SATISFIED** — exact method: `server/discover`, `tools/list`, `resources/list`, `prompts/list`, `tools/call` |
   | `Mcp-Name` on tool/resource/prompt calls | **SATISFIED** — `mcp-name: add` on `tools/call` only; correctly absent on list calls |
   | `Accept: application/json, text/event-stream` | **SATISFIED** — on every request |
   | No `Mcp-Session-Id` header | **SATISFIED** — absent from all client requests |
   | No GET requests, no `Last-Event-ID` | **SATISFIED** — all requests were POST; no GET, no `Last-Event-ID` |

   These headers are emitted by the `mcp` SDK, not module code (grep of
   `amplifier_module_tool_mcp/` finds zero header-name references). The module
   receives this functionality for free via the SDK's `negotiate()`.

3. **Legacy fallback on `mcp==1.29.0`.** The degradation is logged and the module
   falls back to `initialize()`, negotiating 2025-11-25.

4. **Pagination against a real multi-page stdio server.** The cursor chain
   `None -> '' -> 'c2' -> None` was followed correctly and all 5 tools were
   discovered — including the empty-string cursor, which the spec says MUST NOT be
   treated as end-of-results.

5. **Test suite.** 80 tests, passing on both `mcp==1.29.0` and `mcp==2.0.0`.

Everything else in this document is drawn from reading the source and is labelled
`[inspection]`.

---

## 8. Historical documents

The following documents in this repository are mid-development snapshots from
2025-10-18 and no longer describe the code. Each carries a header saying so. They are
retained as development history; do not plan from them.

- `docs/EXECUTIVE_SUMMARY.md`
- `docs/COMPLETION_ROADMAP.md`
- `docs/SDK_CAPABILITIES.md`
- `docs/development/IMPLEMENTATION_STATUS.md`
- `docs/development/END_TO_END_INTEGRATION.md`
- `docs/development/PHASE2_SUMMARY.md`
- `docs/development/PHASE3_SUMMARY.md`
- `docs/development/PHASE4_SUMMARY.md`
