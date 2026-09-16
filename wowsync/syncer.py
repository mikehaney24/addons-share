"""The sync engine: capture local state, exchange it with the server, and put
it back on disk without disturbing anything machine-specific.

Two repositories back a profile:

  shared.git   work tree is the live game directory. Tracks the addon folders
               and the shared half of WTF. This is what the two machines trade.
  machine.git  work tree is a small staging directory. Tracks Config.wtf,
               config-cache.wtf, and the machine-local globals carved out of
               SavedVariables. Pushed to its own branch so it is versioned and
               restorable, but never merged into the shared branch.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from .config import Config, Profile, SavedVarRule
from .gitrepo import GitRepo
from .paths import ProfilePaths
from .savedvars import join_globals, split_globals
from .util import Fail, log, read_json, write_json

SHARED_BRANCH = "main"


@dataclass
class SyncResult:
    status: str
    detail: str = ""
    commit: str | None = None

    @property
    def ok(self) -> bool:
        return self.status not in ("diverged", "error")


def _literal_prefix(pattern: str) -> str:
    """The wildcard-free leading part of a glob, used as a safety boundary."""
    parts: list[str] = []
    for segment in pattern.split("/"):
        if any(ch in segment for ch in "*?["):
            break
        parts.append(segment)
    return "/".join(parts) or "."


def allowed_prefixes(profile: Profile) -> list[str]:
    prefixes = {_literal_prefix(p) for p in profile.include}
    # Collapse nested prefixes so the check stays cheap and readable.
    result: list[str] = []
    for prefix in sorted(prefixes):
        if not any(prefix == r or prefix.startswith(r + "/") for r in result):
            result.append(prefix)
    return result


def fragment_name(relpath: str) -> str:
    """Stable, flat filename for a SavedVariables machine-local fragment."""
    return relpath.replace("/", "%2F")


class Syncer:
    def __init__(self, config: Config, profile: Profile, paths: ProfilePaths | None = None):
        self.config = config
        self.profile = profile
        self.paths = paths or ProfilePaths(profile.name)
        self.shared = GitRepo(self.paths.shared_git, profile.game_dir)
        self.machine = GitRepo(self.paths.machine_git, self.paths.local_tree)

    # -- lifecycle ------------------------------------------------------

    @property
    def machine_branch(self) -> str:
        return f"machine/{self.config.machine_id}"

    def init(self) -> None:
        if not self.profile.game_dir.is_dir():
            raise Fail(f"game_dir does not exist: {self.profile.game_dir}")
        self.paths.ensure()
        self.shared.init(self.profile.exclude + self.profile.machine_local)
        self.machine.init([])
        if self.profile.git_remote:
            self.shared.set_remote(self.profile.git_remote)
            self.machine.set_remote(self.profile.git_remote)
        if self.machine.head() is None:
            self.machine.git("symbolic-ref", "HEAD", f"refs/heads/{self.machine_branch}")

    def require_init(self) -> None:
        if not self.shared.exists:
            raise Fail(f"profile {self.profile.name!r} is not initialised; run 'wowsync init'")

    # -- state bookkeeping ----------------------------------------------

    def load_state(self) -> dict:
        return read_json(self.paths.state_file, {})

    def save_state(self, **updates) -> dict:
        state = self.load_state()
        state.update(updates)
        write_json(self.paths.state_file, state)
        return state

    # -- machine-local handling -----------------------------------------

    def machine_local_files(self) -> list[Path]:
        found: list[Path] = []
        for pattern in self.profile.machine_local:
            found.extend(sorted(self.profile.game_dir.glob(pattern)))
        return [f for f in found if f.is_file()]

    def saved_variable_targets(self) -> list[tuple[Path, SavedVarRule]]:
        out: list[tuple[Path, SavedVarRule]] = []
        for rule in self.profile.saved_variables:
            if not rule.globals:
                continue
            for path in sorted(self.profile.game_dir.glob(rule.file)):
                if path.is_file():
                    out.append((path, rule))
        return out

    def _capture_machine_local(self) -> None:
        """Copy machine-specific files into the private staging tree."""
        self.paths.ensure()
        staged_now: set[Path] = set()
        for src in self.machine_local_files():
            rel = src.relative_to(self.profile.game_dir)
            dst = self.paths.local_tree / "game" / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            staged_now.add(dst)

        # Drop copies of files that no longer exist in the game directory, so
        # the private branch reflects reality rather than accreting forever.
        game_stage = self.paths.local_tree / "game"
        if game_stage.is_dir():
            for existing in sorted(game_stage.rglob("*")):
                if existing.is_file() and existing not in staged_now:
                    existing.unlink()

    def _strip_saved_variables(self) -> int:
        """Carve machine-local globals out of the copies headed for the repo.

        The file on disk keeps every global so WoW loads normally; the blob
        recorded in the shared repo holds only the globals both machines share.
        """
        count = 0
        for path, rule in self.saved_variable_targets():
            rel = path.relative_to(self.profile.game_dir).as_posix()
            try:
                text = path.read_text("utf-8", errors="surrogateescape")
            except OSError as exc:
                log.warning("cannot read %s: %s", rel, exc)
                continue
            shared_text, local_text = split_globals(text, rule.globals)
            if not local_text.strip():
                continue
            fragment = self.paths.sv_local / fragment_name(rel)
            fragment.parent.mkdir(parents=True, exist_ok=True)
            fragment.write_text(local_text, "utf-8", errors="surrogateescape")
            self.shared.stage_blob(rel, shared_text.encode("utf-8", "surrogateescape"))
            count += 1
        return count

    def reinject_saved_variables(self) -> int:
        """Put this machine's private globals back after a checkout."""
        if not self.paths.sv_local.is_dir():
            return 0
        count = 0
        for rule in self.profile.saved_variables:
            if not rule.globals:
                continue
            for path in sorted(self.profile.game_dir.glob(rule.file)):
                rel = path.relative_to(self.profile.game_dir).as_posix()
                fragment = self.paths.sv_local / fragment_name(rel)
                if not fragment.is_file():
                    continue
                shared_text = path.read_text("utf-8", errors="surrogateescape")
                local_text = fragment.read_text("utf-8", errors="surrogateescape")
                # Guard against double-injection if a checkout was a no-op.
                stripped, _ = split_globals(shared_text, rule.globals)
                merged = join_globals(stripped, local_text)
                if merged != shared_text:
                    path.write_text(merged, "utf-8", errors="surrogateescape")
                    count += 1
        return count

    # -- capture / apply ------------------------------------------------

    def capture(self, subject: str) -> SyncResult:
        """Record the current on-disk state as a commit on both repos."""
        self.require_init()
        self.shared.stage(
            self.profile.include, self.profile.exclude + self.profile.machine_local
        )
        self._strip_saved_variables()
        changed = self.shared.staged_change_count()
        commit = self.shared.commit(
            f"{self.config.machine_id}: {subject}" + (f" ({changed} files)" if changed else ""),
            self.config.machine_id,
        )

        self._capture_machine_local()
        self.machine.stage_all()
        self.machine.commit(f"{self.config.machine_id}: {subject}", self.config.machine_id)

        if commit:
            log.info("snapshot %s  %s (%d files)", commit[:10], subject, changed)
            return SyncResult("captured", f"{changed} files", commit)
        return SyncResult("unchanged")

    def pull(self, adopt: bool = False, subject: str = "local changes") -> SyncResult:
        """Fetch and, when it is a clean fast-forward, apply to the game dir."""
        self.require_init()
        # Whatever is on disk is committed before anything is fetched, so a
        # sync can never be the thing that loses a change.
        self.capture(subject)

        if not self.profile.git_remote:
            return SyncResult("offline", "no git_remote configured")
        if not self.shared.fetch():
            return SyncResult("offline", "server unreachable")
        self.machine.fetch()

        remote = self.shared.ref(f"refs/remotes/origin/{SHARED_BRANCH}")
        local = self.shared.head()

        if remote is None:
            return SyncResult("no-remote-state", "server has no state yet; push to seed it")
        if local is None:
            self._apply(remote)
            return SyncResult("adopted", "took the server's state", remote)
        if local == remote:
            return SyncResult("up-to-date", commit=local)
        if self.shared.is_ancestor(local, remote):
            self._apply(remote)
            return SyncResult("updated", self._describe_range(local, remote), remote)
        if self.shared.is_ancestor(remote, local):
            return SyncResult("ahead", "local has changes the server does not", local)

        if not self.shared.has_common_ancestor(local, remote):
            return self._adopt_unrelated(local, remote, adopt)

        merged = self._try_auto_merge(local, remote)
        if merged is not None:
            return merged
        return self._record_divergence(local, remote)

    def _adopt_unrelated(self, local: str, remote: str, adopt: bool) -> SyncResult:
        """This machine has state of its own that never came from the server.

        That is the normal shape of setting up the second computer: it already
        has addons and settings installed locally, and the histories share no
        ancestor. Taking the server's copy is the right move, but the local
        state is parked on a branch first so nothing is lost.
        """
        first_sync = not self.load_state().get("synced_once")
        if not (adopt or first_sync):
            return SyncResult(
                "unrelated",
                "this machine's history is unrelated to the server's; "
                "re-run with --adopt to replace local state with the server's "
                "(the current state is kept in history either way)",
            )
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        branch = f"preexisting/{self.config.machine_id}/{stamp}"
        self.shared.branch_at(branch, local)
        self.shared.push_branch(f"refs/heads/{branch}", branch)
        self._apply(remote)
        return SyncResult(
            "adopted",
            f"replaced local state with the server's; previous state kept on {branch}",
            remote,
        )

    def _apply(self, target: str) -> None:
        prefixes = allowed_prefixes(self.profile)
        self.shared.read_tree(target, prefixes)
        self.shared.set_head(target)
        injected = self.reinject_saved_variables()
        if injected:
            log.debug("reinjected machine-local globals into %d file(s)", injected)
        self.save_state(last_applied=target, last_applied_at=time.time(), synced_once=True)

    def _describe_range(self, old: str, new: str) -> str:
        count = self.shared.out("rev-list", "--count", f"{old}..{new}", check=False)
        return f"{count} new snapshot(s) from the server"

    def _try_auto_merge(self, local: str, remote: str) -> SyncResult | None:
        """Combine two histories when doing so cannot cost anyone a setting.

        Both machines changing different files -- one installed an addon while
        the other was updated -- merges cleanly and is the common case when an
        addon updater runs on both boxes. A collision on the same file is only
        resolved automatically inside `auto_merge_prefixes`; a collision under
        WTF/ is left for the user to settle.
        """
        base = self.shared.merge_base(local, remote)
        if base is None:
            return None

        tree, conflicts = self.shared.merge_tree(base, local, remote)
        if tree is None:
            return None

        note = ""
        if conflicts:
            unsafe = [
                path
                for path in conflicts
                if not any(
                    path == prefix or path.startswith(prefix.rstrip("/") + "/")
                    for prefix in self.profile.auto_merge_prefixes
                )
            ]
            if unsafe:
                log.debug("not auto-merging; collisions outside addon folders: %s", unsafe[:5])
                return None
            tree, _ = self.shared.merge_tree(base, local, remote, strategy_option="theirs")
            if tree is None:
                return None
            note = f"; kept the server's copy of {len(conflicts)} addon file(s)"

        message = (
            f"{self.config.machine_id}: merge server state"
            f" ({len(conflicts)} collision(s))\n\nMachine: {self.config.machine_id}\n"
        )
        commit = self.shared.commit_tree(tree, [local, remote], message)
        self._apply(commit)
        log.info("merged diverged state into %s%s", commit[:10], note)
        return SyncResult("merged", f"combined local and server changes{note}", commit)

    def _record_divergence(self, local: str, remote: str) -> SyncResult:
        """Both sides moved. Keep everything; decide nothing automatically."""
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        branch = f"conflict/{self.config.machine_id}/{stamp}"
        self.shared.branch_at(branch, local)
        pushed, message = self.shared.push_branch(f"refs/heads/{branch}", branch)
        self.save_state(conflict={"branch": branch, "local": local, "remote": remote, "at": time.time()})
        detail = (
            f"local and server state diverged; local snapshot preserved on {branch}"
            + ("" if pushed else f" (not pushed: {message})")
        )
        # Callers surface this: the CLI prints it, the daemon raises a
        # notification. Logging it here too would just say it twice.
        log.debug("%s", detail)
        return SyncResult("diverged", detail)

    def push(self) -> SyncResult:
        self.require_init()
        if not self.profile.git_remote:
            return SyncResult("offline", "no git_remote configured")
        head = self.shared.head()
        if head is None:
            return SyncResult("nothing-to-push")

        ok, message = self.shared.push(SHARED_BRANCH)
        if not ok:
            # A rejection means the server moved under us; fall back to the
            # ordinary pull path, which either fast-forwards or flags a conflict.
            log.debug("push rejected: %s", message)
            result = self.pull()
            if result.status == "diverged":
                return result
            ok, message = self.shared.push(SHARED_BRANCH)
            if not ok:
                return SyncResult("error", f"push failed: {message}")

        self.machine.push_branch(f"refs/heads/{self.machine_branch}", self.machine_branch)
        self.save_state(
            last_pushed=self.shared.head(), last_pushed_at=time.time(), synced_once=True
        )
        return SyncResult("pushed", commit=self.shared.head())

    def sync(self, subject: str) -> SyncResult:
        """Take the server's state, then publish ours.

        The reported result is whichever half actually did something, so that
        seeding an empty server does not report the failed pull that preceded
        it.
        """
        pulled = self.pull(subject=subject)
        if pulled.status in ("diverged", "unrelated"):
            return pulled

        pushed = self.push()
        if pushed.status in ("error", "diverged"):
            return pushed
        if pushed.status == "pushed" and pulled.status in (
            "up-to-date", "ahead", "no-remote-state", "offline", "unchanged"
        ):
            return pushed
        return pulled

    # -- conflict resolution --------------------------------------------

    def pending_conflict(self) -> dict | None:
        return self.load_state().get("conflict")

    def conflict_files(self) -> list[str]:
        """The files the two sides disagree about, for the user to look at."""
        conflict = self.pending_conflict()
        if not conflict:
            return []
        local, remote = conflict["local"], conflict["remote"]
        base = self.shared.merge_base(local, remote)
        if base is None:
            return self.shared.changed_between(remote, local)
        ours = set(self.shared.changed_between(base, local))
        theirs = set(self.shared.changed_between(base, remote))
        return sorted(ours & theirs)

    def resolve(self, take: str) -> SyncResult:
        """Settle a flagged conflict by choosing one side wholesale.

        Either way the result is a merge commit with both sides as parents, so
        the server still fast-forwards and the side that lost stays in history
        and can be read back out with `wowsync show`.
        """
        conflict = self.pending_conflict()
        if not conflict:
            return SyncResult("nothing-to-resolve", "no conflict is pending")
        if take not in ("local", "server"):
            raise Fail("resolve: take must be 'local' or 'server'")

        local = conflict["local"]
        self.shared.fetch()
        remote = self.shared.ref(f"refs/remotes/origin/{SHARED_BRANCH}") or conflict["remote"]

        chosen = local if take == "local" else remote
        label = "this machine" if take == "local" else "the server"
        message = (
            f"{self.config.machine_id}: resolve conflict in favour of {label}"
            f"\n\nMachine: {self.config.machine_id}\n"
        )
        commit = self.shared.commit_tree(self.shared.tree_of(chosen), [local, remote], message)
        self._apply(commit)

        state = self.load_state()
        state.pop("conflict", None)
        write_json(self.paths.state_file, state)
        pushed = self.push()
        detail = f"kept {label}'s copy" + ("" if pushed.status == "pushed" else f"; {pushed.detail}")
        return SyncResult("resolved", detail, commit)

    # -- history --------------------------------------------------------

    def restore(self, commit: str) -> SyncResult:
        """Roll the shared tree back to an earlier snapshot as a new commit.

        History is never rewound: the rollback is recorded on top of the
        current tip so the server still fast-forwards and the state you rolled
        back from stays recoverable.
        """
        self.require_init()
        resolved = self.shared.ref(commit)
        if resolved is None:
            raise Fail(f"unknown snapshot: {commit}")
        self.capture("safety snapshot before restore")
        self.shared.read_tree(resolved, allowed_prefixes(self.profile))
        new_commit = self.shared.commit(
            f"{self.config.machine_id}: restore to {resolved[:10]}", self.config.machine_id
        )
        self.reinject_saved_variables()
        return SyncResult("restored", f"tree of {resolved[:10]}", new_commit or resolved)
