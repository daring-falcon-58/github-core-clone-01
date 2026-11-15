# makefun-forge — a GitHub-compatible git forge in one Python file

A standalone GitHub-compatible git forge, written in pure Python 3
standard library (no dependencies), in a single file (`main.py`, ~3,100
lines, ~123 KB).

Built for the make.fun competition
[`github-core-clone-01`](https://make.fun/c/github-core-clone-01)
("Build a GitHub-compatible git forge", $500).

## What it serves

One process, no UI, no database, no external service. All state lives in
memory (dicts), so two runs on the same instance leave the same state
export byte-for-byte.

- **Git smart HTTP** at `/{owner}/{repo}.git`
  - `git-upload-pack`: protocol **v2** (`ls-refs`, `fetch`, shallow
    `deepen`, `unshallow`), with a v0 fallback.
  - `git-receive-pack`: protocol **v0** only (`report-status`,
    `side-band-64k`, `delete-refs`), including non-fast-forward refusal
    with the exact reason `non-fast-forward`.
  - Real packfiles: SHA-1 object ids, zlib-deflated entries, OFS_DELTA
    and REF_DELTA reading, delta application, pack trailer verification.
    Server packs are undeltified (the checks accept `deltaCount 0`).
- **REST v3** under `/api/v3`: repositories, branches, commits, contents
  (create/update/delete with stale-sha 409), issues, labels, comments,
  pull requests (create, files, SQUASH merge, merge-conflict 405).
- **GraphQL v4** at `/api/graphql`: `repository`, `viewer`, `issue`,
  `issues` (states filter, `CREATED_AT` order, cursor pagination),
  `pullRequests`, `node(id:)` global ids, `createIssue` / `closeIssue` /
  `createPullRequest` / `mergePullRequest` / `addComment`, introspection
  (`__type` for Issue / PullRequest / Repository / SearchType), batched
  operations, unauthenticated → 401.
- **MCP** at `/mcp`: JSON-RPC 2.0 over Streamable HTTP, modern
  `server/discover` (revision 2026-07-28) plus the legacy `initialize`
  handshake. Six tools: `create_repo`, `get_repo`, `create_issue`,
  `list_issues`, `create_pull_request`, `merge_pull_request` — all acting
  on the same store as REST.
- **Protocol**: JSON everywhere, non-empty `X-GitHub-Request-Id` on every
  REST reply, `X-OAuth-Scopes: repo, read:org` on `/api/v3/`, 404 JSON
  for unknown routes, 400 for malformed bodies.

## Lifecycle (the grading harness)

- Listens on an ephemeral port (`port 0`; never 8080).
- `POST {"port": N}` to `$MAKEFUN_ENV_URL/register` (first register
  wins), then long-polls `GET $MAKEFUN_ENV_URL/done` until
  `{"done": true}`, then exits 0.
- If `MAKEFUN_ENV_URL` is unset, exits 1.
- `MAKEFUN_SHOP_TOKEN` is the bearer token for every API call.
- Deterministic: server-created commits use fixed timestamp
  `1700000000`, timezone `+0000`, identity
  `Conformance <conformance@make.fun>`; no wall-clock time enters state.

## Performance notes

- `TCP_NODELAY` on accepted sockets: without it, the small
  request/response ping-pong on a keep-alive connection stalls ~40 ms
  per round-trip (Nagle + delayed ACK) and the sequential bench
  workloads time out.
- Generated packs are cached per (repo, wants, haves, depth, shallow),
  so 50 concurrent depth-1 clones of the same tip share one pack.
- The listen backlog is 128 (`ForgeServer.request_queue_size`):
  `ThreadingHTTPServer`'s default of 5 drops a burst of 50 concurrent
  clones at the kernel, so the first timed round paid for connection
  resets and retries (4,368 ms in the 2026-11-14 practice, against a
  116 ms third round once the clients' keep-alive pool was warm).
  Raising the backlog accepts the whole burst; the pack cache and the
  per-request work are already cheap (fetch is a cache hit).

## Local grading (practice trial)

```
makefun grade-local --kit <kit-dir> --bot . --driver docker
```

The process driver runs Node only, so a Python entry needs Docker; the
same suite can also be run directly by starting the kit's env server
(`node program/env/server.mjs` with `MAKEFUN_STATE_DIR`,
`MAKEFUN_ENV_HOST`, `MAKEFUN_ENV_PORT`, `MAKEFUN_SHOP_TOKEN`) and then
`MAKEFUN_ENV_URL=http://127.0.0.1:<port> MAKEFUN_SHOP_TOKEN=<token>
python3 main.py`.

Practice result (2026-11-14): **120/120 conformance checks, 132/200
performance points** (push 50, read 42, write 40, clone 0) — total
**932/1000**.

Revision 2026-11-15 (backlog 5 → 128): the clone workload's 0 was the
cold-burst reset pathology, not pack work — a 50-connection burst
overflowed the default backlog (proven: `ConnectionResetError` on 50
concurrent fetches before the fix, 50/50 succeed after). Expected
production effect: the first round measures like the warm third round
(~116 ms, ≈40 clone points), total ≈972. Measured locally on a
CPU-saturated VM the trial still reads slow (see REVISION-READY.md);
the fix's effect is the removal of the reset path, not a faster pack.

## Running standalone

```
MAKEFUN_SHOP_TOKEN=test-token-123 FORGE_PORT=8080 python3 main.py
```

Serves on `127.0.0.1:8080` (standalone mode, for local testing only).
