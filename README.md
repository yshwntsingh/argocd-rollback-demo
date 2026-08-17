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
           -> agent times out -> git revert the bad commit -> push
           -> ArgoCD syncs -> back to nginx:1.25                          (ROLLED BACK)
```

The rollback is a **Git revert, not `kubectl rollout undo`**. With ArgoCD
`selfHeal: true`, Git is the source of truth, so a kubectl-level rollback would be
overwritten on the next sync. Fixing Git is the durable fix.

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

REPO_DIR=. GH_REMOTE=https://github.com/<YOUR_ORG>/autonomous-rollback-demo.git \
GH_TOKEN=<token> python3 agent/git_rollback.py
# waits, detects failure, git-reverts the bad commit, pushes
# ArgoCD syncs -> back to nginx:1.25
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
healthy = watch_and_rollback()   # True = deploy healthy, False = rolled back
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
