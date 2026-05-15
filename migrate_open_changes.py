#!/usr/bin/env python3

import argparse
import json
import logging
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


def parse_project_pairs_file(path: Path) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for line_no, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 2:
            raise ValueError(
                f"invalid format at {path}:{line_no}: expected '<src_project> <dst_project>'"
            )
        pairs.append((parts[0], parts[1]))

    if not pairs:
        raise ValueError(f"no project pairs found in {path}")
    return pairs


def run_command(cmd: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    logging.debug("exec: %s", " ".join(cmd))
    result = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.stdout:
        logging.debug("stdout: %s", result.stdout.strip())
    if result.stderr:
        logging.debug("stderr: %s", result.stderr.strip())
    if check and result.returncode != 0:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(cmd)}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result


def ssh_query_open_changes(
    src_user: str,
    src_ip: str,
    src_port: int,
    src_prj: str,
    limit: int,
    owner: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> list[dict[str, Any]]:
    query = [
        "ssh",
        "-p",
        str(src_port),
        f"{src_user}@{src_ip}",
        "gerrit",
        "query",
        "--format=JSON",
        "--current-patch-set",
        f"project:{src_prj}",
        "status:open",
        f"limit:{limit}",
    ]
    if owner:
        query.append(f"owner:{owner}")
    if since:
        query.append(f"after:{since}")
    if until:
        query.append(f"before:{until}")
    result = run_command(query)

    changes: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        if "type" in obj and obj["type"] == "stats":
            continue
        changes.append(obj)
    return changes


def is_no_new_changes(result: subprocess.CompletedProcess[str]) -> bool:
    output = f"{result.stdout}\n{result.stderr}".lower()
    return "no new changes" in output


def migrate_change(repo_dir: Path, src_url: str, dst_url: str, change: dict[str, Any], dst_prj: str) -> None:
    patch_set = change.get("currentPatchSet", {})
    ref = patch_set.get("ref")
    branch = change.get("branch")
    number = change.get("number")

    if not ref or not branch:
        raise RuntimeError(f"change {number}: missing ref/branch information")

    run_command(["git", "fetch", src_url, ref], cwd=repo_dir)
    run_command(["git", "checkout", "--detach", "FETCH_HEAD"], cwd=repo_dir)

    push_ref = f"HEAD:refs/for/{branch}"
    push_result = run_command(["git", "push", dst_url, push_ref], cwd=repo_dir, check=False)
    if push_result.returncode != 0:
        if is_no_new_changes(push_result):
            logging.info("skip change %s (%s): destination rejected as no new changes", number, ref)
            return
        raise RuntimeError(
            f"change {number}: failed to push\n"
            f"stdout:\n{push_result.stdout}\n"
            f"stderr:\n{push_result.stderr}"
        )

    logging.info("migrated change %s (%s) -> %s:%s", number, ref, dst_prj, branch)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Migrate open Gerrit changes from source project to destination project"
    )
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--src-prj", help="source Gerrit project")
    source_group.add_argument(
        "--project-pairs-file",
        help="path to text file containing '<src_project> <dst_project>' pairs per line",
    )
    parser.add_argument("--src-ip", required=True, help="source Gerrit IP or hostname")
    parser.add_argument("--src-user", required=True, help="source Gerrit SSH user")
    parser.add_argument("--src-port", type=int, default=29418, help="source Gerrit SSH port")

    parser.add_argument("--dst-prj", help="destination Gerrit project")
    parser.add_argument("--dst-ip", required=True, help="destination Gerrit IP or hostname")
    parser.add_argument("--dst-user", required=True, help="destination Gerrit SSH user")
    parser.add_argument("--dst-port", type=int, default=29418, help="destination Gerrit SSH port")

    parser.add_argument("--limit", type=int, default=10, help="max number of open changes to migrate")
    parser.add_argument("--owner", help="optional owner filter, e.g. owner username or email")
    parser.add_argument(
        "--since",
        help="optional since filter for Gerrit query (passed as after:<value>, e.g. 2026-01-01 or 7d)",
    )
    parser.add_argument(
        "--until",
        help="optional until filter for Gerrit query (passed as before:<value>, e.g. 2026-12-31 or 1d)",
    )
    parser.add_argument("--verbose", action="store_true", help="enable debug logging")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.limit <= 0:
        logging.error("--limit must be > 0")
        return 2

    if args.project_pairs_file:
        if args.dst_prj:
            logging.error("--dst-prj cannot be used with --project-pairs-file")
            return 2
        try:
            project_pairs = parse_project_pairs_file(Path(args.project_pairs_file))
        except (OSError, ValueError) as exc:
            logging.error(str(exc))
            return 2
    else:
        if not args.dst_prj:
            logging.error("--dst-prj is required when --src-prj is used")
            return 2
        project_pairs = [(args.src_prj, args.dst_prj)]

    for src_prj, dst_prj in project_pairs:
        src_url = f"ssh://{args.src_user}@{args.src_ip}:{args.src_port}/{src_prj}"
        dst_url = f"ssh://{args.dst_user}@{args.dst_ip}:{args.dst_port}/{dst_prj}"

        logging.info("querying open changes from %s", src_prj)
        changes = ssh_query_open_changes(
            args.src_user,
            args.src_ip,
            args.src_port,
            src_prj,
            args.limit,
            owner=args.owner,
            since=args.since,
            until=args.until,
        )

        if not changes:
            logging.info("no open changes found for %s", src_prj)
            continue

        logging.info("found %d open changes for %s", len(changes), src_prj)

        temp_dir = Path(tempfile.mkdtemp(prefix="gerrit-migrate-"))
        repo_dir = temp_dir / "repo"

        try:
            run_command(["git", "clone", "--no-checkout", src_url, str(repo_dir)])
            for change in changes:
                migrate_change(repo_dir, src_url, dst_url, change, dst_prj)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    logging.info("migration complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
