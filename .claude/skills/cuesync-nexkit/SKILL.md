---
name: cuesync-nexkit
description: Runbook for the local agent session that drives NexKit for the CueSync owner (posting commands, reading plans, triaging pull requests, splitting issues). Use when the owner asks to plan, implement, check, fix or split work through NexKit, or asks about the state of a NexKit issue or pull request. Not for agents running inside the NexKit pipeline.
---

# Driving NexKit for CueSync

The rules, the roles and the life of an issue are in
[docs/delivery.md](../../../docs/delivery.md). Read it first. This file holds only the
practical parts: where values live, the `gh` commands, the review checklists and what to
do when something fails.

You post every command yourself. Post `/nexkit go` only on the latest plan, after the
check below that it holds every agreed change, and only when you recommend it. Ask the
owner first when the plan has a question only the owner can answer, unless the owner has
chosen autopilot. Never approve or merge a pull request yourself. While `merge.auto` is
on, NexKit merges and closes the issue.

## Where the values live

Look these up each time. Do not copy them into notes or docs, because they change with a
NexKit release or a settings change.

- **NexKit repository and version:** the `nexkit_repository` and `nexkit_ref` inputs in
  the workflow file.

  ```bash
  grep -E 'nexkit_(repository|ref):' .github/workflows/nexkit.yml
  ```

- **NexKit's own docs** (how it works, configuration, troubleshooting): list them at that
  ref, then read the one you need.

  ```bash
  gh api "repos/<nexkit_repository>/contents/docs?ref=<nexkit_ref>" --jq '.[].path'
  gh api "repos/<nexkit_repository>/contents/<path>?ref=<nexkit_ref>" \
    -H 'Accept: application/vnd.github.raw'
  ```

- **Protected paths, the automatic fix limit, automatic merge and conflicts:**
  `.nexkit/config.json`. A missing key takes NexKit's default, from its configuration doc
  at the pinned ref.

  ```bash
  jq '{protected_paths, fix, merge, conflicts, usage_limit, close_parent_issues, notify}' .nexkit/config.json
  ```

- **Profiles, their `when` texts and models:** `.nexkit/config.json`. A profile's stage
  settings override `stages`, which override the top-level `model` and `effort`. The rules
  behind the profiles are in [delivery.md](../../../docs/delivery.md#profiles).

  ```bash
  jq '{default_profile, triage: .stages.triage, profiles}' .nexkit/config.json
  ```

- **The cache NexKit restores before setup:** the `cache` key of `.nexkit/config.json`.
  The `actions/cache` step in `.github/workflows/ci.yml` saves it on `main` and must match.

- **Required checks on `main`:** the active rules for the branch.

  ```bash
  gh api repos/{owner}/{repo}/rules/branches/main \
    --jq '.[] | select(.type=="required_status_checks") | .parameters.required_status_checks[].context'
  ```

- **The pull request for an issue:** the link in NexKit's run comment on the issue.
- **A run's state:** the `<!-- nexkit:run <base64> -->` marker in NexKit's run comment
  holds JSON with `command`, `status`, `outcome` and `attention`.

## Commands

Post a command:

```bash
gh issue comment <N> --body "/nexkit plan"
gh pr comment <N> --body "/nexkit fix <instructions>"
```

Change the profile of an issue. A request written in the issue text does not count; it
must be a comment:

```bash
gh issue comment <N> --body "/nexkit plan Use the hard profile"
```

Read the latest plan. It is the newest NexKit comment on the issue, and shows the profile,
its models and why triage chose it:

```bash
gh issue view <N> --comments
```

See all open NexKit work and what needs a person with NexKit's local `status` command at
the pinned ref. Start every session with it:

```bash
REPO=$(grep -oE 'nexkit_repository: \S+' .github/workflows/nexkit.yml | cut -d' ' -f2)
REF=$(grep -oE 'nexkit_ref: \S+' .github/workflows/nexkit.yml | cut -d' ' -f2)
KIT="${TMPDIR:-/tmp}/nexkit-$REF"
[ -d "$KIT" ] || git clone -q --depth 1 --branch "$REF" "https://github.com/$REPO" "$KIT"
python3 "$KIT/bin/nexkit" status
```

Find what is ready: an issue is ready when every issue named in its `Depends on:` line is
closed. Work in the earliest open milestone first.

```bash
gh issue list --milestone "<milestone title>" --state open --json number,title,body
```

Split an issue that reports `too_large`. Create each part with
`gh issue create --title "<part's behaviour>" --milestone "<parent's milestone>" --label "<labels>" --body "<body>"`,
then add the parts to the parent in order. Each part gets its own labels by the writing
checklist, not the parent's, and a `Depends on:` line: the parent's dependencies for the
first part and the previous part for each later one, unless the split says otherwise.
Title each part by the behaviour it delivers. Never reuse the parent's title, whole or as
a prefix: write `OpenSubtitles downloads respect the quota`, not
`OpenSubtitles source: quota`. The sub-issue link already shows the parent.

```bash
CHILD_ID=$(gh api repos/{owner}/{repo}/issues/<child> --jq .id)
gh api -X POST repos/{owner}/{repo}/issues/<parent>/sub_issues -F sub_issue_id="$CHILD_ID"
```

`sub_issue_id` is the issue's database `id`, not its number.

## Checklist: writing an issue

- One behaviour, aiming for 2–5 acceptance criteria, each checkable by a test or command.
  More than five: check whether the issue holds more than one behaviour, and split it if
  so.
- Sections: Goal, Scope, Acceptance criteria, optional Notes, and a `Depends on:` line
  when other issues must land first.
- The text names the parts of the system it touches, in the terms of
  [AGENTS.md](../../../AGENTS.md#how-a-request-flows) (subtitle sources, stream lookup,
  debrid, audio windows, sync engine, cache, background jobs, addon configuration), even
  when it links to docs. Triage reads only the issue and its discussion, not the links.
- It gets every repository label that fits, and at least one. Read them with
  `gh label list` and choose by their descriptions. A bug issue for a CI failure on `main`
  gets `bug`.

## Checklist: reviewing a plan

- It delivers the issue's Goal and Scope, and each acceptance criterion is checkable by a
  test or command. Do not re-plan only to cut the number of criteria.
- The profile in the plan comment fits the issue, judged against the `when` texts. If not,
  re-plan with `/nexkit plan Use the <profile> profile`. If a `when` text caused the wrong
  choice, tell the owner and propose a fix to that text as a hand-made pull request.
- It follows [AGENTS.md](../../../AGENTS.md): tests never hit the network
  (`httpx.MockTransport` or recorded fixtures), audio test media is generated with ffmpeg
  `lavfi`, secrets are never logged or put in errors, slow work never blocks the Stremio
  response, new dependencies get a lower bound in `pyproject.toml`.
- It neither changes nor needs changes in `.github/` or `.nexkit/`. If it needs one (for
  example a new system package in setup), that is a hand-made pull request first.
- It does not build what a later issue or milestone owns, and does not assume work of an
  open issue is done.
- Names it introduces for shared pieces (`Cue`, `Subtitle`, `SubtitleSource`,
  `StreamRef`, `DebridProvider`, `AudioWindow`, `WindowOffset`, ...) match the names the
  issues use, so later issues fit.
- Decisions and risks the owner must judge are in their own short sections.
- Every change agreed after the plan was posted, from the review or from the owner, is
  commented in the same issue and `/nexkit plan` is run again. No follow-up issues.
- Changes in a comment are terse: one short line each, with the exact values, names and
  rules, and no reasons or prose. Nothing the issue or the docs already say.
- After a re-plan, its *Since the last plan* section lists every change that was asked
  for, and nothing else changed without a reason.
- Use `/nexkit plan from scratch` only when the plan has gone wrong, and comment every
  choice to keep before posting it.

## Checklist: reviewing a pull request

- The behaviour change matches the issue and the approved plan.
- Docs that the change makes false are updated in the same pull request.
- Tests cover the change, including failure paths (timeouts, bad keys, no match).
- The required checks on `main` are green.
- No edits in `.github/` or `.nexkit/`, unless the pull request is hand-made.
- If the pull request changes what `scripts/setup.sh` downloads in a way the pip cache
  does not cover, tell the owner and propose the hand-made pull request for the cache.

## When something fails

| Failure | What the local agent does |
| --- | --- |
| Publish fails after `/nexkit go` | Read the run log. If `main` was force-pushed, the same `/nexkit go` fixes it. Otherwise diagnose first. Ask the owner before posting `/nexkit go` again. |
| A round ends as `blocked` | Answer the question with `/nexkit fix <decision>`. Ask the owner first when the choice is theirs. |
| Merge conflict with `main` | Nothing: NexKit starts a conflict round by itself. Answer a `blocked` question with `/nexkit fix <decision>`. After `conflicts.max_rounds` such rounds, comment `/nexkit fix` yourself. |
| Conflict in `.github/` or `.nexkit/`, or a merge that brings workflow changes | Merge `origin/main` into the pull request branch by hand, in a separate worktree. Run `scripts/setup.sh`, `scripts/lint.sh` and `scripts/test.sh`. Ask the owner before pushing, then comment `/nexkit review`. |
| The plan comment says triage failed | The plan ran on the previous plan's profile or on `default_profile`. If that profile is wrong for the issue, re-plan with `/nexkit plan Use the <profile> profile`. |
| A run is paused at the Claude usage limit (⏸️) | Nothing: NexKit runs the same command again after the reset. If it stops a second time with an unknown reset time, repeat the command. |
| The *Restore caches* step reports a missed cache | Nothing for one run: setup installs from scratch, and the next successful CI run on `main` saves the cache. If every run misses, compare the `cache` entry with the CI cache step and check that CI on `main` succeeds. |
| The review job errored | The round's comment says why. Comment `/nexkit review` to retry, unless it names an authentication error: then tell the owner, because a retry stops the same way. |
| The review requested changes | NexKit runs automatic fix rounds up to the configured limit. After that, ask the owner and post `/nexkit fix <instructions>`. |
| NexKit's automatic merge was refused (`merge_refused`) | A conflict starts a conflict round by itself. Otherwise the round's comment says why, usually a rule on `main`. NexKit has mentioned the owner; tell them what you found. |
| CI on `main` fails after a merge | If the failure looks flaky, run it once more with `gh run rerun <id> --failed`. Otherwise tell the owner and open a bug issue with the `bug` label, the failure and the run link, in the current milestone, and plan it before other issues. |

For the mechanics behind each failure, read NexKit's troubleshooting doc at the pinned
ref, found as described under "Where the values live".
