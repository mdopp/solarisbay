# solilos-abs-mcp

A small read-only **Audiobookshelf → MCP** shim. It runs as a container in
the `solbay` pod and exposes the household's Audiobookshelf library
to Hermes as MCP tools, so a skill can answer "do we already own this book?"
right after a note is ingested (#89) — **without** a wizard-managed ABS REST
token living in the skill.

Image: `ghcr.io/mdopp/solilos-abs-mcp`.

## Why a shim (and why read-only)

Audiobookshelf has no native MCP server, and the community one
(`michaeldvinci/audiobookshelf-mcp`) is stdio-only, unpublished as an image,
and exposes write/destructive tools (create/delete collections, backups).
This shim is the smaller, safer fit: it speaks the same streamable-HTTP MCP
transport Hermes already uses for the gatekeeper room-MCP (#104), issues only
GETs against the ABS REST API, and holds the ABS credential in its own env —
so a prompt-injected agent can neither reach the credential nor mutate the
library.

## Tools

| Tool | Purpose |
|---|---|
| `abs_search(query, limit=5)` | Search the book libraries; returns `{title, author, library, item_id}` hits. |
| `abs_availability(title, author="")` | "Is this already in ABS?" — returns `{available, matches}`. |

## Configuration (env)

| Var | Default | Meaning |
|---|---|---|
| `ABS_BASE_URL` | `http://127.0.0.1:13378` | Audiobookshelf base URL (host loopback; both pods are hostNetwork). |
| `ABS_API_KEY` | _(blank)_ | Audiobookshelf API key (Settings → Users → API Keys, or a user token). Blank ⇒ every tool returns `abs_unavailable`. |
| `MCP_HOST` | `127.0.0.1` | Bind host. Loopback so only Hermes reaches it; a `0.0.0.0` bind under hostNetwork would expose it on the LAN (cf. #116). |
| `MCP_PORT` | `10770` | Streamable-HTTP MCP port (distinct from the gatekeeper's 10700/10750/10760). |
| `ABS_MCP_TOKEN` | _(blank)_ | Optional bearer protecting the MCP endpoint. Blank is fine for the loopback default. |

## Verifying the ABS response shape

The ABS public API docs are stale on the library-search response, so
`abs_client.py` parses defensively. Pin it against the live instance:

```bash
# inside the box, with a real ABS API key:
curl -s -H "Authorization: Bearer $ABS_API_KEY" \
  http://127.0.0.1:13378/api/libraries | jq '.libraries[] | {id,name,mediaType}'
curl -s -H "Authorization: Bearer $ABS_API_KEY" \
  "http://127.0.0.1:13378/api/libraries/<book-lib-id>/search?q=dune&limit=3" \
  | jq '.book[].libraryItem.media.metadata | {title, authorName}'
```

If the fields differ, adjust `_metadata` / `_author_of` in `abs_client.py`.
