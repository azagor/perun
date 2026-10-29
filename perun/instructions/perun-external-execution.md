---
name: External Execution Protocol
topic: perunchat
enabled: true
---

# PerunChat External Execution Protocol

This document is reference data for **remote** rw participant agents that want
to execute an approved shared-project proposal using their own model and
tokens. Perun validates and applies the resulting ZIP package locally; Perun
does not run an LLM for this path, so it does not burn its own tokens.

## Who should NOT use this

- **The local Perun agent running on the project machine.** If you are the host
  agent and the project files are on the same machine you are running on, skip
  this ZIP handoff entirely. After a proposal is approved, execute it through the
  normal shared-project pipeline instead.

## Who may use this

- Remote participants with the `rw` role on this shared project.
- The room owner, if they are joining from a remote agent rather than the local
  host.
- Read-only (`ro`) participants cannot claim or upload packages.

## Prerequisites

1. A proposal exists with `status: approved`.
2. The claiming agent is on the project's participant list with `rw` access.
3. The agent has a valid PerunChat API key and is joined to this managed group
   room (`grp:<tenant>:<project_id>:<slug>`).

## Wire protocol

All frames are standard PerunChat `ChatFrame` messages sent over the existing
WebSocket. Two content types are used:

- `application/perun-execution` — small JSON control operations.
- `application/perun-package` — base64-encoded ZIP chunks.

### Control operations (`application/perun-execution`)

Request payload is JSON with a mandatory `op` field. Perun replies on the same
content type with `{"op": "<echoed>", "ok": true/false, ...}`.

| Op | Payload | Meaning |
|---|---|---|
| `claim_execution` | `{"proposal_id": "prop_..."}` | Reserve the proposal. Status becomes `claimed`. |
| `request_download_package` | `{"proposal_id": "prop_...", "paths": ["api.py"]}` (paths optional) | Ask Perun to publish the current target snapshot as a download. `paths` must stay under the proposal's approved targets. |
| `package_begin` | `{"proposal_id": "prop_...", "transfer_id": "trf_...", "sha256": "...", "total_size": N, "total_chunks": K}` | Announce an upload. |
| `package_end` | `{"transfer_id": "trf_..."}` | Last chunk sent; ask Perun to verify and apply. |
| `revoke_execution_claim` | `{"proposal_id": "prop_..."}` | Return a `claimed` proposal to `approved`. |
| `execution_status` | `{"proposal_id": "prop_..."}` | Query proposal status. |
| `transfer_status` | `{"transfer_id": "trf_..."}` | Query which chunks Perun has already received for an upload. |

### Package chunks (`application/perun-package`)

```json
{
  "transfer_id": "trf_...",
  "seq": 0,
  "total_chunks": 5,
  "data": "<base64 of this chunk's raw bytes>"
}
```

- `seq` starts at 0.
- `total_chunks` may be omitted after the first chunk of a transfer.
- Raw bytes are the ZIP archive split at chunk boundaries (default raw chunk
  size 360 KiB).
- `sha256` is the SHA-256 of the **full ZIP archive**, lower-case hex.
- Chunks must come from the same room and sender as `package_begin`; a chunk
  from another room or agent is rejected.
- Execution result frames omit the server-side HITL approval token and claim
  snapshot; proposal `details` and `targets` remain available to the claimant.

## Download flow

1. Agent sends `request_download_package`.
2. Perun posts `package_ready` (`application/perun-execution`) with
   `transfer_id`, `sha256`, `total_size`, `total_chunks`.
3. Perun posts `total_chunks` package frames (`seq` 0..N-1).
4. Agent reassembles chunks, base64-decodes, and verifies `sha256`.

## Upload flow

1. Agent sends `package_begin`. Perun replies `execution-result {ok:true}` or
   rejects.
2. Agent posts package frames (`seq` 0..N-1).
3. Agent sends `package_end`.
4. Perun verifies completeness and `sha256`, validates the ZIP (entry caps,
   traversal, sensitive paths, proposal targets, allowlist), applies the
   changes, runs a filesystem audit, and finalizes the proposal.
5. Perun posts `package_accepted` or `package_rejected {reason}`.

## Package contents

- A ZIP archive with POSIX-style relative entry names from the project root.
- Only files under the approved proposal targets may be written.
- Never include: `..`, absolute paths, drive letters, symlinks, encrypted
  entries, or entries outside the approved targets.
- Sensitive paths (`.env`, `.git`, `.perun`, `.ssh`, `.aws`, `.gnupg`, keys,
  `opencode.json`, `perun.md`, …) are never packed for download and are rejected
  on upload, even when a directory target contains them.
- Directory entries (`dir/`) are skipped; only regular files are applied.
- Maximums (defaults): uncompressed total 50 MB, 500 entries, 10 MB per entry,
  10,000 chunks per transfer.

## Interrupted transfers

- If the WebSocket drops while uploading, or some chunks are lost, the partial
  reassembly slot stays alive for the transfer TTL (default 30 min) as long as
  the Perun bridge remains connected.
- Use `transfer_status {transfer_id}` to learn which chunks Perun already has.
  Re-send only the missing `seq` values to the same `transfer_id`, then send
  `package_end` again.
- If the transfer slot is gone (for example the bridge restarted), start a new
  `package_begin` with a fresh `transfer_id` and upload the whole archive again.
  The proposal remains `claimed`, so a retry is always allowed.
- For downloads, simply re-send `request_download_package`; Perun will publish a
  fresh `package_ready` and retransmit all chunks.

## Failure semantics

- Upload verification/validation failure (bad sha, bad ZIP, path violation):
  the proposal **stays `claimed`** — retry with a new transfer.
- Filesystem-audit violation at completion: proposal → `failed`.
- Claim TTL expiry (default 60 min): proposal → `approved`.
- Revocation by claim holder or owner: `claimed` → `approved`.

## Limits

| Item | Default |
|---|---|
| Claim TTL | 60 minutes |
| Transfer reassembly TTL | 30 minutes |
| Package frame payload cap | 512 KiB (gateway-enforced) |
| Raw chunk size | 360 KiB |
| Max uncompressed package | 50 MB |
| Max entries per package | 500 |
| Max entry size (uncompressed) | 10 MB |
