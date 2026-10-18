# Delivery workflow

CueSync is built through [NexKit](https://github.com/phuongnse/nexkit). NexKit runs one
issue per agent session. People drive it with four comment commands: `/nexkit plan` and
`/nexkit go` on an issue, and `/nexkit fix` and `/nexkit review` on its pull request.

Three roles take part:

- **The owner** decides what only the owner can: questions a plan or a blocked round
  asks of the owner, and whether NexKit merges its own pull requests. NexKit mentions the
  owner when a run needs a person.
- **The local agent session** drives NexKit for the owner. It writes issues, posts
  commands, reviews plans and says go, and watches the work. Its runbook is the
  `cuesync-nexkit` skill in `.claude/skills/`.
- **NexKit's agents** plan, implement, fix and review inside the NexKit pipeline. They
  read [AGENTS.md](../AGENTS.md).

Values that NexKit or the repository settings own, such as protected paths, required
checks, the automatic fix limit and the profiles with their models, are not copied here.
Look them up where they live: `.nexkit/config.json`, `.github/workflows/`, and the rules
on `main`.

## Decisions

- **The local agent posts every command itself, `/nexkit go` included.** It posts
  `/nexkit go` on the latest plan once it holds every agreed change and the local agent
  recommends it. It asks the owner first when the plan has a question only the owner can
  answer, unless the owner has chosen autopilot.
- **NexKit merges a pull request once it passes, while `merge.auto` is on.** NexKit
  squash-merges as soon as every check passes and the AI review approves, then closes the
  issue. The owner turns the setting off to decide each merge again. The switch is in
  `.nexkit/`, so changing it is a hand-made pull request.
- **NexKit mentions the owner when a person is needed.** The owner's login is in the
  `notify` setting. NexKit mentions it when a run ends in a state that needs a person: a
  plan with questions or too large for one session, a blocked or failed run, no automatic
  fix rounds left, or a refused automatic merge.
- **NexKit resolves conflicts with `main` by itself.** With `conflicts.auto_resolve` on,
  each time `main` moves NexKit starts a fix round for every open NexKit pull request that
  now conflicts with it. A conflict that needs a choice ends the round as `blocked`, with
  a question.
- **NexKit pauses at the Claude usage limit and resumes after it resets.** The run's
  comment shows the reset time. No person repeats the command.
- **A parent issue closes when its last part does.** With `close_parent_issues` on,
  NexKit closes a parent issue once every sub-issue is closed and at least one was
  completed.
- **CI runs on `main` after every merge.** A merge by NexKit starts no `push` workflow, so
  NexKit starts the CI workflow on `main` itself, through `merge.after_workflows`. Two
  pull requests that pass on their own can still break `main` together. When CI on `main`
  fails, the local agent runs it once more if the failure looks flaky. Otherwise it opens
  a bug issue with the failure, which goes before other issues.
- **CI and NexKit run the same scripts.** `scripts/setup.sh`, `scripts/lint.sh` and
  `scripts/test.sh` are NexKit's setup and checks and the steps of the CI workflow, so a
  change passes or fails the same way in both.
- **NexKit restores the pip cache that CI on `main` saves.** NexKit never saves a cache,
  because its runs execute code the agent wrote. The `cache` entry in `.nexkit/config.json`
  and the `actions/cache` step in the CI workflow have the same paths and key files. A
  change to what setup downloads updates both in the same hand-made pull request. A missed
  cache never fails a run.
- **Everything goes through NexKit, except changes in paths NexKit may not change.**
  Those are `.github/` and `.nexkit/` (the `protected_paths` setting). Such changes, for
  example a NexKit version bump or a profile change, are hand-made pull requests that the
  owner opens and merges after the CI workflow passes.

## Profiles

Each issue runs on one NexKit profile. The profile sets the models for the plan,
implement, fix and review rounds of that issue. When `/nexkit plan` starts, NexKit's
triage step reads the issue and chooses the profile from their `when` texts.

- **Profiles go by risk: `standard` and `hard`.** The more a mistake would cost, the
  stronger the profile. `standard` is the default.
- **Plan and review are at least as strong as implement**, and review runs on a different
  model from implement. The plan is the contract and the review is the last check.
- **Triage runs on a strong model.** It is one call that reads only the issue, so it costs
  little, and a wrong choice does not fail: the issue just runs on the wrong profile.
- **A `when` text says what a mistake would cost, not where the code is.** It may name
  parts of the system from [AGENTS.md](../AGENTS.md#how-a-request-flows) as examples, but
  never files, classes or milestones, which go stale. When unsure, triage chooses the
  stronger profile.
- **Triage sees only the issue and its discussion.** It does not open links, so an issue
  names the parts of the system it touches in its own text.
- **A comment changes the profile:** `/nexkit plan Use the <profile> profile`. A request
  written in the issue text does not count. A re-plan keeps the profile unless someone
  asks for another one.

## The life of an issue

1. **Write the issue.** One issue is one behaviour, with acceptance criteria that a test
   or command can check (aim for 2–5; more is fine when they all check the same
   behaviour). It has Goal, Scope and Acceptance criteria sections, a `Depends on:` line
   naming the issues that must be closed first, and is in a milestone. It gets every
   repository label that fits its content, and at least one; read the labels with
   `gh label list` and choose by their descriptions.
2. **Plan.** Comment `/nexkit plan`. NexKit's plan agent posts a plan as a comment.
3. **Review the plan** with the checklist in the skill. Any change agreed after the plan
   is posted, however small, is commented in the same issue and `/nexkit plan` is run
   again before `/nexkit go`. A re-plan revises the latest plan and lists what changed in
   its *Since the last plan* section, so a review comment states only the changes: one
   short line each, with the exact values, names and rules, and no reasons or prose. Do
   not open follow-up issues. When the plan reports `too_large`, split the issue into
   parts, each titled by the behaviour it delivers (never the parent's title, whole or as
   a prefix), and add them as sub-issues of the parent, in order and in its milestone.
4. **Implement.** The local agent posts `/nexkit go` on the latest plan. NexKit implements
   that plan and opens a pull request.
5. **Pull request.** Every round runs NexKit's checks and the AI review on the new commit.
6. **Fix.** `/nexkit fix <instructions>` starts another round. NexKit starts the rounds for
   merge conflicts and failed reviews by itself, up to its limits. A round that needs a
   choice stops as `blocked`; answer it with `/nexkit fix <decision>`. After a manual push,
   comment `/nexkit review`.
7. **Merge.** NexKit squash-merges, starts CI on `main` and closes the issue.

## The life of a milestone

The milestones on GitHub are the roadmap; each milestone's description says what it
delivers. Issues in a milestone are delivered in the order their `Depends on:` lines
allow. The owner closes a milestone once its last issue is merged and CI on `main` passes.

## Writing on GitHub

These rules apply to issues, plans, pull request descriptions and reviews.

- Lead with the point. Use short sentences and one idea per bullet.
- Say what changes in behaviour, and why, before naming classes or files.
- Put decisions and risks the owner must judge in their own short section.
- Keep implementation detail out of the main text, or put it in a collapsed `<details>`
  block.
