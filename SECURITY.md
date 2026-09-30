# Security Policy

## Reporting a vulnerability

Report it through GitHub's private reporting route: this repository's
**Security** tab → **Report a vulnerability**. That opens a private advisory
visible only to you and the maintainer; nothing you write there appears on the
issue tracker.

Please do not file a public issue for a vulnerability. A public issue is
world-readable from the moment it is posted, including by anyone who has not
yet found the bug.

Useful to include: what an attacker can do, the input or configuration that
reaches it, and how to reproduce it. You do not need a working exploit, and you
do not need to have tried a fix.

The advisory thread is the record, so the maintainer can coordinate a fix and a
disclosure timeline there.

## Maintainer and access

**This project has a single maintainer, with no named successor and no
emergency-access arrangement.** There is nobody else who can act on an
advisory, release a fix, or take over the account if the maintainer is
unavailable. That is a real and deliberate single point of failure, and it is
stated here rather than left for a reader to infer.

Concretely, it means:

- If the maintainer is unavailable, an open advisory waits. There is no
  escalation path.
- Reported vulnerabilities are triaged by one person, on a best-effort
  basis. There is no published response-time commitment, because a commitment
  only one person can keep is not one.
- Nothing about this repository's operation depends on emergency access,
  because no such arrangement exists to depend on. The renderers hold one
  GitHub credential and produce static files; losing the maintainer stops the
  renders rather than exposing anything further. Note that the credential is
  an account credential and is only as narrow as whoever minted it — scope it
  down rather than assuming it is read-only.

Advisories are still worth reporting. A report that arrives while the
maintainer is reachable is worth a great deal more than one that does not.

## Credentials

### Which credential, and with what scope

The renderers authenticate to the GitHub API with **one GitHub personal access
token (classic)**, belonging to the account whose profile the cards render.
The renderers read it from `--token` / `GH_TOKEN`, or from a file named by
`--token-file` / `GH_TOKEN_FILE`.

Documented scope: **`public_repo`**. `read:user` is *not* required — nothing
here reads the account's email address.

`public_repo` is the documented, conventional choice rather than the strict
minimum: nothing the renderers request is private data, so the queries do not
need a scope that reaches private repositories. A narrower token works where
your setup permits it. A token with *broader* scopes will also work and the
renderers will not complain, which is precisely why the scope should be
checked rather than assumed.

Scope discipline, so the token's blast radius is not overstated:

- **No rendered card carries private repository content.** Every render site
  applies a public/external predicate that rejects a private repository
  outright, so a private repo cannot appear on a published SVG.
- **The API is not asked for "public only".** Only the owned-repository
  listing passes `privacy: PUBLIC` to GitHub. The pull-request and issue
  queries carry no such argument — there is no server-side filter for it — so
  private entries are returned to the renderers and filtered out locally, at
  render time.
- **The on-disk caches are not free of private data.** Because that filtering
  happens at render time, the cached pull-request and issue mappings are
  written before it, and they retain a private repository's name, URL, and
  private flag. Titles and bodies are never requested, so no private *content*
  is stored — but private repository *identity* is. Treat the cache files as
  sensitive: they should be readable only by the account the renderers run as.
- None of that constrains the token. It is an account credential, and anyone
  holding it acts as its owner for as long as it is valid, regardless of what
  the renderers do with it. Treat a leaked token as a leaked account
  credential, not as a leaked read-only key.

### Who owns it

The **maintainer** owns this credential and is the only party who can rotate
or revoke it. There is no second holder.

### Routine rotation

The maintainer rotates the token by:

1. Minting a replacement with the same scope.
2. Writing it to the token file, readable only by the account the renderers
   run as.
3. Restarting the renderer's timer or service so the new value is picked up.
4. Running a render and confirming it succeeds — a successful render is what
   proves the new credential works, rather than assuming the file was read.
5. Revoking the previous token.

Revocation is step 5, not step 1, so the renderers are never left with a
credential that does not work.

### On suspected exposure

The maintainer responds to a suspected or confirmed exposure in this order:

1. **Revoke** the exposed token immediately. This is the containment step and
   it comes first on purpose: it stops further use, and it is safe to do
   before the diagnosis is complete. Nothing about the investigation depends
   on the token remaining valid.
2. **Rotate**: mint a replacement as above, so rendering resumes with a
   credential that was never exposed.
3. **Check the account's security log** — sign-ins, authorizations, and token
   activity — to establish whether the token was used by anyone other than
   the maintainer, and to see what was authorized while it was live. Anything
   unfamiliar there is a separate incident from the renderers themselves.
4. **Re-render**, and check the output. After a credential is replaced, the
   cards must reflect current data; a stale render is the symptom that the
   new credential is not actually in use. A `--resync` discards the cached
   history, which is worth doing after an exposure so no residue of the old
   fetch survives in the cache files.
5. **Review and revoke authorizations** the token granted that are no longer
   needed, including any OAuth grants made while it was exposed.

### Reporting a credential problem

Do not put a token, or any part of one, in an advisory, an issue, a commit, or
a pull request — including in a "here is what leaked" reproduction. Describe
the credential's identity (kind, scope, when last rotated) and not its value.