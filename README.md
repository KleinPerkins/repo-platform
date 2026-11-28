# repo-platform

Repo-platform checks whether an explicitly selected checkout can enter the Dev
development workflow. It checks the live Codex project, the Git directories,
the immutable GitHub repository ID, the Skills discovery receipt and Dev's
current policy. Dev's Personal setup supplies the launcher and the authorized
checkout inventory.

## Preview a checkout

With the Dev launcher and owner session configured, supply the absolute path and
the registered Codex project ID.

```sh
bash ~/dev/bootstrap.sh --profile personal-codex repo-platform preview \
  "$checkout" --project-id "$project_id"
```

Use `--thread-id` to identify a linked worktree's Codex chat. If you know the
expected immutable GitHub ID, pass it with `--repository-id`. Use
`--expected-title` to include a display-title comparison.

The JSON output reports the selected checkout, local HEAD, GitHub default
branch and revision, delivery policy, and any warnings. Preview retries
temporary provider or native connection failures. It leaves the Skills snapshot
and any saved adoption and recovery records unchanged.

## Adopt a checkout

Use `adopt` with the same selection to recheck the live evidence and record a
successful adoption. If required discovery evidence is stale, the configured
Skills owner can refresh it during adoption. This applies to the selected
checkout and any admitted worktree sharing its Git directories. The result
records the observations used for those related worktrees. A failed adoption
preserves the previous accepted record. Each repository's owners govern source
changes, review, merge and release.

If GitHub throttles a public repository read and reports a reset time,
`adopt` saves that time privately. Subsequent previews and adoption attempts
for the same selection report the wait without contacting GitHub or opening
the checkout. At the reported time, run the command again to recheck the
evidence. If GitHub supplies no usable future reset, adoption records a case
for Codex to diagnose through Dev's public GitHub reader.
