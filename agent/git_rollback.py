#!/usr/bin/env python3
"""
git_rollback.py — the GitOps rollback step for your existing agent.

What it does:
  1. Watches the deployment's pods after a deploy.
  2. If they don't reach Ready within TIMEOUT (ImagePullBackOff / ErrImagePull /
     CrashLoopBackOff / stuck not-Ready), it treats the deploy as failed.
  3. Reverts the *bad deploy commit* on main with `git revert` and pushes.
  4. ArgoCD (selfHeal) then syncs the reverted manifest -> previous good image.

Why revert Git instead of `kubectl rollout undo`:
  With ArgoCD selfHeal=true, Git is the source of truth. A kubectl rollback would
  be reverted by ArgoCD on its next sync. The durable fix is a Git commit.

Wire-in options:
  - Standalone:   python3 git_rollback.py
  - From your agent:
        from git_rollback import watch_and_rollback
        healthy = watch_and_rollback()   # returns True if deploy was healthy (no action)
                                         # returns False if it rolled back

Config via environment variables (all have sensible defaults):
  NAMESPACE   default: monitoring
  SELECTOR    default: app=demo-app
  DEPLOYMENT  default: demo-app
  TIMEOUT     seconds to wait for health before rolling back (default: 120)
  POLL        seconds between checks (default: 10)
  REPO_DIR    path to a checkout of the GitOps repo (default: current dir)
  BRANCH      default: main
  GH_TOKEN    optional; if set, used to build an authenticated push URL
  GH_REMOTE   optional explicit https remote, e.g.
              https://github.com/<org>/autonomous-rollback-demo.git
  DRY_RUN     if "1", detect + log but do not push the revert (default: 0)
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime

NAMESPACE = os.getenv("NAMESPACE", "monitoring")
SELECTOR = os.getenv("SELECTOR", "app=demo-app")
DEPLOYMENT = os.getenv("DEPLOYMENT", "demo-app")
TIMEOUT = int(os.getenv("TIMEOUT", "120"))
POLL = int(os.getenv("POLL", "10"))
REPO_DIR = os.getenv("REPO_DIR", os.getcwd())
BRANCH = os.getenv("BRANCH", "main")
GH_TOKEN = os.getenv("GH_TOKEN", "")
GH_REMOTE = os.getenv("GH_REMOTE", "")
DRY_RUN = os.getenv("DRY_RUN", "0") == "1"

BAD_WAITING_REASONS = {"ImagePullBackOff", "ErrImagePull", "CrashLoopBackOff", "InvalidImageName"}


def log(msg, level="INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [{level}] {msg}", flush=True)


def run(cmd, cwd=None, check=True):
    """Run a command, return (rc, stdout, stderr)."""
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and p.returncode != 0:
        log(f"command failed: {' '.join(cmd)}\n{p.stderr.strip()}", "ERROR")
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def get_pods():
    rc, out, _ = run(
        ["kubectl", "get", "pods", "-n", NAMESPACE, "-l", SELECTOR, "-o", "json"],
        check=False,
    )
    if rc != 0 or not out:
        return []
    try:
        return json.loads(out).get("items", [])
    except json.JSONDecodeError:
        return []


def evaluate_health():
    """
    Returns one of: 'healthy', 'failing', 'pending'.
      healthy -> all pods for the current spec are Ready
      failing -> at least one pod is in a known-bad waiting state
      pending -> not ready yet, but no hard failure (keep waiting)
    """
    pods = get_pods()
    if not pods:
        return "pending"

    all_ready = True
    for pod in pods:
        status = pod.get("status", {})
        for cs in status.get("containerStatuses", []):
            waiting = cs.get("state", {}).get("waiting", {})
            reason = waiting.get("reason", "")
            if reason in BAD_WAITING_REASONS:
                return "failing"
            if not cs.get("ready", False):
                all_ready = False
        if not status.get("containerStatuses"):
            all_ready = False

    return "healthy" if all_ready else "pending"


def bad_deploy_sha():
    """The commit ArgoCD last deployed = tip of origin/BRANCH."""
    run(["git", "fetch", "origin", BRANCH], cwd=REPO_DIR, check=False)
    rc, sha, _ = run(["git", "rev-parse", f"origin/{BRANCH}"], cwd=REPO_DIR, check=False)
    return sha if rc == 0 else ""


def commit_touched_manifest(sha):
    """Only revert if that commit actually changed the k8s manifests (safety guard)."""
    rc, out, _ = run(
        ["git", "show", "--name-only", "--pretty=format:", sha],
        cwd=REPO_DIR, check=False,
    )
    return any(line.strip().startswith("k8s/") for line in out.splitlines())


def push_url():
    if GH_REMOTE and GH_TOKEN:
        return GH_REMOTE.replace("https://", f"https://x-access-token:{GH_TOKEN}@")
    if GH_REMOTE:
        return GH_REMOTE
    return "origin"  # rely on the checkout's existing credentials


def revert_in_git(sha):
    log(f"Reverting bad deploy commit {sha[:10]} in Git...", "WARNING")
    run(["git", "checkout", BRANCH], cwd=REPO_DIR, check=False)
    run(["git", "reset", "--hard", f"origin/{BRANCH}"], cwd=REPO_DIR, check=False)
    run(["git", "config", "user.name", "rollback-agent"], cwd=REPO_DIR, check=False)
    run(["git", "config", "user.email", "rollback-agent@users.noreply.github.com"],
        cwd=REPO_DIR, check=False)

    rc, _, err = run(["git", "revert", "--no-edit", sha], cwd=REPO_DIR, check=False)
    if rc != 0:
        log(f"git revert failed: {err}", "ERROR")
        return False

    if DRY_RUN:
        log("DRY_RUN=1 -> revert committed locally but NOT pushed.", "WARNING")
        return True

    target = push_url()
    rc, _, err = run(["git", "push", target, f"HEAD:{BRANCH}"], cwd=REPO_DIR, check=False)
    if rc != 0:
        log(f"git push failed: {err}", "ERROR")
        return False

    log("Revert pushed to main. ArgoCD will sync back to the previous image.", "SUCCESS")
    return True


def watch_and_rollback():
    """
    Core entrypoint. Watches for TIMEOUT seconds.
    Returns True if the deploy was healthy (no rollback needed),
    False if it failed and was rolled back in Git.
    """
    log(f"Watching {DEPLOYMENT} in ns/{NAMESPACE} (selector {SELECTOR}) "
        f"for up to {TIMEOUT}s...")
    deadline = time.time() + TIMEOUT

    while time.time() < deadline:
        state = evaluate_health()
        remaining = int(deadline - time.time())

        if state == "healthy":
            log("Deploy is healthy. No rollback needed.", "SUCCESS")
            return True
        if state == "failing":
            log("Detected failing pods (bad image / crash loop).", "WARNING")
            break
        log(f"Not ready yet... ({remaining}s left)")
        time.sleep(POLL)
    else:
        log("Timed out waiting for pods to become Ready.", "WARNING")

    sha = bad_deploy_sha()
    if not sha:
        log("Could not resolve the deploy commit; aborting rollback.", "ERROR")
        return False
    if not commit_touched_manifest(sha):
        log(f"Tip commit {sha[:10]} did not change k8s/; not reverting.", "WARNING")
        return False

    revert_in_git(sha)
    return False


if __name__ == "__main__":
    healthy = watch_and_rollback()
    sys.exit(0 if healthy else 1)
