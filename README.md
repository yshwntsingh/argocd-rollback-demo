# autonomous-rollback-demo

A GitHub + ArgoCD demo of automatic rollback. Two releases are deployed by tag:
`v1.0.0` succeeds, `v2.0.0` fails, and the agent reverts the bad deploy commit in
Git so ArgoCD rolls the cluster back to the previous good image.

## The flow

```
tag v1.0.0 -> workflow writes nginx:1.25 to k8s/deployment.yaml -> commit to main
           -> ArgoCD syncs -> pods healthy                                (SUCCESS)

tag v2.0.0 -> workflow writes nginx:99.99 to k8s/deployment.yaml -> commit to main
           -> ArgoCD syncs -> ImagePullBackOff
           -> agent times out -> opens a REVERT PR         (waits for approval)
           -> a human reviews + merges the PR              <-- human in the loop
           -> ArgoCD syncs -> back to nginx:1.25                          (ROLLED BACK)
```

The rollback is a **Git revert, not `kubectl rollout undo`**. With ArgoCD
`selfHeal: true`, Git is the source of truth, so a kubectl-level rollback would be
overwritten on the next sync. Fixing Git is the durable fix.

## Human-in-the-loop approval

The agent does **not** push the rollback straight to `main`. On failure it:

1. creates a branch `rollback/<sha>` that reverts the bad deploy commit,
2. opens a **pull request**, optionally requesting reviewers and posting the link
   to Slack,
3. stops — the cluster is unchanged.

A person reviews and merges the PR. Only the merge (a commit on `main`) makes
ArgoCD sync the rollback. This is the approval gate.

Relevant settings (env vars):

- `ROLLBACK_MODE=pr` (default) opens a PR. `ROLLBACK_MODE=direct` reverts and
  pushes to `main` with no gate (only if you don't want approval).
- `REVIEWERS=alice,bob` requests review from those GitHub users.
- `WAIT_FOR_MERGE=1` keeps the agent polling until the PR is merged (or closed),
  so it can log/verify the rollback landed; otherwise it exits after opening the PR.
- `SLACK_WEBHOOK=...` posts the PR link for the approver.

`watch_and_rollback()` returns `"healthy"`, `"pr_opened"`, `"rolled_back"`, or
`"error"` so your agent can branch on the outcome.

You can also gate the *forward* deploy with a GitHub Environment that has required
reviewers, if you want approval before v1/v2 reach the cluster too.

## Layout

```
k8s/deployment.yaml      app; the image line is rewritten per release
k8s/service.yaml         ClusterIP service
argocd/application.yaml  ArgoCD Application (edit repoURL before applying)
releases/v1.0.0.env      IMAGE=nginx:1.25   (good)
releases/v2.0.0.env      IMAGE=nginx:99.99  (bad)
.github/workflows/deploy-on-tag.yml   tag -> write image -> commit to main
agent/git_rollback.py    watch pods; on failure revert the deploy commit
```

## One-time setup

1. Create a new empty repo on GitHub named `autonomous-rollback-demo`, then push:

   ```bash
   git remote add origin https://github.com/<YOUR_ORG>/autonomous-rollback-demo.git
   git push -u origin main
   ```

2. Point ArgoCD at it. Edit `argocd/application.yaml` `repoURL`, then:

   ```bash
   kubectl apply -f argocd/application.yaml
   ```

3. Make sure the agent has a checkout of this repo and a token that can push to
   `main`. If your branch protection requires PRs, see "Branch protection" below.

## Run the demo

```bash
# Release 1 - succeeds
git tag v1.0.0 && git push origin v1.0.0
# workflow commits nginx:1.25 -> ArgoCD deploys -> 3 pods Running

# Start the agent watch (or let your existing agent do this after a deploy)
REPO_DIR=. GH_REMOTE=https://github.com/<YOUR_ORG>/autonomous-rollback-demo.git \
GH_TOKEN=<token> python3 agent/git_rollback.py
# returns immediately as healthy

# Release 2 - fails and is rolled back
git tag v2.0.0 && git push origin v2.0.0
# workflow commits nginx:99.99 -> ArgoCD deploys -> ImagePullBackOff

REPO_DIR=. \
GH_REMOTE=https://github.com/<YOUR_ORG>/autonomous-rollback-demo.git \
GH_TOKEN=<token> REVIEWERS=<your-gh-username> \
python3 agent/git_rollback.py
# waits, detects failure, opens a REVERT PR, and stops.
# -> review + merge the PR -> ArgoCD syncs -> back to nginx:1.25
```

Verify:

```bash
kubectl get pods -n monitoring -l app=demo-app -o wide
git log --oneline -n 4        # shows: deploy v2 -> Revert "deploy v2"
```

## Wiring into your existing agent

`agent/git_rollback.py` exposes one function:

```python
from git_rollback import watch_and_rollback
result = watch_and_rollback()
# "healthy" | "pr_opened" (awaiting approval) | "rolled_back" | "error"
```

Call it right after a deploy is observed. It reads config from env vars
(`NAMESPACE`, `SELECTOR`, `DEPLOYMENT`, `TIMEOUT`, `POLL`, `REPO_DIR`, `BRANCH`,
`GH_TOKEN`, `GH_REMOTE`, `DRY_RUN`). Use `DRY_RUN=1` to test detection without
pushing.

## Adding more releases

Create `releases/vX.Y.Z.env` with `IMAGE=<your-image>`, commit it, then
`git tag vX.Y.Z && git push origin vX.Y.Z`.

## Branch protection

If `main` requires PRs, the agent can't push a revert directly. Two options:
- Give the agent a bot account allowed to bypass protection, or
- Change `revert_in_git` to open a revert PR and auto-merge it (GitHub API).
The default here does a direct revert-push, which is simplest for the demo.
