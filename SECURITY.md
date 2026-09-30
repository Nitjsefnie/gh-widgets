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
  because no such arrangement exists to depend on. The renderers hold a
  read-only GitHub credential and produce static files; losing the maintainer
  stops the renders, it does not put anyone's data at risk.

Advisories are still worth reporting. A report that arrives while the
maintainer is reachable is worth a great deal more than one that does not.

## Credentials

### Which credential, and with what scope

The renderers authenticate to the GitHub API with **one GitHub personal access
token (classic)**, belonging to the account whose profile the cards render.
The renderers read it from `--token` / `GH_TOKEN`, or from a file named by
`--token-file` / `GH_TOKEN_FILE`.

Required scope: **`public_repo`**. `read:user` is *not* required — nothing
here reads the account's email address. A token with broader scopes will work,
and the renderers will not complain, but it is not needed and it widens what a
leak of the token would mean.

Scope discipline, so the token's blast radius is not overstated:

- Every repository query is filtered to public data (`privacy: PUBLIC`), and
  private repositories and private pull requests are skipped before
  serialization. The rendered SVGs therefore cannot contain private
  repository content.
- That constrains *what the renderers publish*. It does not constrain the
  token: it is an account credential, and anyone holding it acts as its owner
  for as long as it is valid, regardless of what the renderers do with it.
  Treat a leaked token as a leaked account credential.

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
   new credential is not actually in use.
5. **Review and revoke authorizations** the token granted that are no longer
   needed, including any OAuth grants made while it was exposed.

### Reporting a credential problem

Do not put a token, or any part of one, in an advisory, an issue, a commit, or
a pull request — including in a "here is what leaked" reproduction. Describe
the credential's identity (kind, scope, when last rotated) and not its value.