# The secret store

A credential does not belong in `AGENTS.md`. It does not belong in memory
either. It belongs in a store that is **encrypted at rest** and **pulled on
demand**.

## What it looks like

```bash
perun secret set deploy-token --value "<token>" --hint "GitHub deploy token"
perun secret list                 # names + hints, never values
perun secret get deploy-token     # the value, on demand
perun secret delete deploy-token
```

Via MCP:

```
perun_secret(action="list")
perun_secret(action="get", name="deploy-token")
perun_secret(action="set", name="deploy-token", value="…", hint="…")
```

## How it is protected

* The value is encrypted with **Fernet** (AES-CBC + HMAC). The key is derived
  from a master password with **PBKDF2-HMAC-SHA256** (480,000 iterations) and
  a random per-secret salt.
* The master password is protected with **Windows DPAPI**, so it is bound to
  the current Windows user and is never written in plaintext.
* `PERUN_PASSWORD` takes precedence over the stored password, so a
  hand-configured environment keeps working.
* On disk the row looks like `enc:<base64>` — the plaintext appears nowhere.

`perun secret status` reports whether the store is currently readable and
writable.

## Where the *name* lives

The project file keeps the **name**, never the value:

```markdown
- label: Deploy token
  kind: secret
  path: deploy-token
```

An agent that needs it calls `perun_secret(name="deploy-token")` at the
moment it is used. One prompt carries the credential instead of every prompt
carrying it.

`kind: secret` anchors are also understood by `perun context verify`: an
anchor of that kind whose `path` names an unknown secret is reported as
unresolved.

## Relationship to the memory guard

`memory.remember()` **refuses** anything that looks like a credential — the
sensitive-data guard applies to facts *and* to doc chunking, so a secret
cannot slip into the embedding store by accident. This store is the
sanctioned home for it.

## Rules

* Never put a secret in `AGENTS.md`, in a prompt, in a source file, or in
  memory.
* Never commit an encrypted blob to a repository — the ciphertext is still a
  secret.
* Prefer a **path** to a credential (a key file, a token in the OS
  credential manager) over the credential itself, where one exists.
