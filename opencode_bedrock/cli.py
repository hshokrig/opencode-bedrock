from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import __version__
from .errors import BedrockError
from .projects import Project, list_projects
from .projects import add as add_project
from .projects import get as get_project
from .sandbox import find_bwrap, find_opencode
from .service import (
    Record,
    alive,
    client,
    find_record,
    list_records,
    scrub,
)
from .service import (
    start as start_service,
)
from .service import (
    stop as stop_service,
)
from .tasks import add as add_task
from .tasks import list_tasks
from .workspace import canonical_workspace


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="opencode-bedrock")
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)

    project = commands.add_parser("project", help="manage registered repositories")
    project_commands = project.add_subparsers(dest="project_command", required=True)
    project_add = project_commands.add_parser("add")
    project_add.add_argument("--name", required=True)
    project_add.add_argument("--path", required=True)
    project_add.add_argument("--allow-non-git", action="store_true")
    project_commands.add_parser("list")

    start = commands.add_parser("start", help="start a workspace service")
    _selector(start)
    start.add_argument("--allow-non-git", action="store_true")
    start.add_argument(
        "--region", default=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    )
    start.add_argument("--inference-profile", default=os.environ.get("BEDROCK_INFERENCE_PROFILE"))
    start.add_argument("--endpoint", default=os.environ.get("BEDROCK_RUNTIME_ENDPOINT"))
    start.add_argument(
        "--headless-policy", choices=["approval", "workspace-write"], default="approval"
    )
    start.add_argument("--foreground", action="store_true")
    start.add_argument("--port", type=int)
    start.add_argument("--opencode-bin")
    start.add_argument(
        "--agent-model",
        action="append",
        default=[],
        metavar="AGENT=PROFILE",
        help="use another Bedrock inference profile for one agent",
    )

    for name in ["stop", "restart", "attach", "logs", "task", "tasks"]:
        command = commands.add_parser(name)
        _selector(command)
        if name == "logs":
            command.add_argument("--follow", action="store_true")
        if name == "task":
            command.add_argument("prompt")
            command.add_argument("--agent", choices=["build", "plan"], default="build")

    status = commands.add_parser("status")
    _selector(status)
    status.add_argument("--json", action="store_true")

    approval = commands.add_parser("approval", help="inspect or answer pending approvals")
    approval_commands = approval.add_subparsers(dest="approval_command", required=True)
    approval_list = approval_commands.add_parser("list")
    _selector(approval_list)
    for name, reply in [("approve", "once"), ("always", "always"), ("reject", "reject")]:
        item = approval_commands.add_parser(name)
        _selector(item)
        item.add_argument("request_id")
        if reply == "reject":
            item.add_argument("--message")

    doctor = commands.add_parser("doctor")
    doctor.add_argument("--opencode-bin")
    doctor.add_argument("--check-sandbox", action=argparse.BooleanOptionalAction, default=True)
    return root


def _selector(command: argparse.ArgumentParser) -> None:
    group = command.add_mutually_exclusive_group()
    group.add_argument("--project")
    group.add_argument("--workspace")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return dispatch(args)
    except BedrockError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


def dispatch(args: argparse.Namespace) -> int:
    if args.command == "project":
        return project_command(args)
    if args.command == "start":
        project, workspace = selection(args, required=True, allow_non_git=args.allow_non_git)
        assert workspace is not None
        if not args.region:
            raise BedrockError("set --region or AWS_REGION")
        if not args.inference_profile:
            raise BedrockError("set --inference-profile or BEDROCK_INFERENCE_PROFILE")
        record = start_service(
            workspace=workspace,
            project=project.name if project else None,
            region=args.region,
            inference_profile=args.inference_profile,
            endpoint=args.endpoint,
            headless_policy=args.headless_policy,
            foreground=args.foreground,
            port=args.port,
            opencode_value=args.opencode_bin,
            agent_models=parse_agent_models(args.agent_model),
        )
        if not args.foreground:
            print_record(record)
        return 0
    if args.command == "status":
        return status_command(args)
    if args.command in {"stop", "restart", "attach", "logs", "task", "tasks"}:
        project, workspace = selection(args, required=True, allow_non_git=True)
        record = find_record(
            project.name if project else None,
            workspace,
            require_running=args.command not in {"stop", "restart"},
        )
        if args.command == "stop":
            stop_service(record)
            print(f"stopped: {record.workspace}")
            return 0
        if args.command == "restart":
            stop_service(record)
            next_record = start_service(
                workspace=Path(record.workspace),
                project=record.project,
                region=record.region,
                inference_profile=record.inference_profile,
                endpoint=record.endpoint,
                headless_policy=record.headless_policy,
                foreground=False,
                port=record.port,
                opencode_value=record.opencode,
                agent_models=record.agent_models,
            )
            print_record(next_record)
            return 0
        if args.command == "attach":
            return attach(record)
        if args.command == "logs":
            return logs(record, args.follow)
        if args.command == "task":
            return submit_task(record, args.prompt, args.agent)
        return tasks_command(record)
    if args.command == "approval":
        return approval_command(args)
    if args.command == "doctor":
        return doctor_command(args)
    raise BedrockError(f"unsupported command: {args.command}")


def project_command(args: argparse.Namespace) -> int:
    if args.project_command == "add":
        project = add_project(args.name, args.path, args.allow_non_git)
        print(f"registered {project.name}: {project.path}")
        return 0
    projects = list_projects()
    if not projects:
        print("no registered projects")
        return 0
    for project in projects:
        kind = "git" if project.git else "directory"
        print(f"{project.name}\t{project.path}\t{kind}")
    return 0


def status_command(args: argparse.Namespace) -> int:
    project, workspace = selection(args, required=False, allow_non_git=True)
    records = (
        [find_record(project.name if project else None, workspace, require_running=False)]
        if project or workspace
        else list_records()
    )
    payload = [scrub(record) for record in records]
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    if not payload:
        print("no services")
        return 0
    for item in payload:
        state = "running" if item["running"] else "stale/stopped"
        label = item["project"] or "-"
        print(f"{state}\t{label}\t{item['workspace']}\tpid={item['pid']}\tport={item['port']}")
    return 0


def attach(record: Record) -> int:
    print(f"active workspace: {record.workspace}")
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        in {
            "COLORTERM",
            "HOME",
            "LANG",
            "LC_ALL",
            "LOGNAME",
            "NO_COLOR",
            "PATH",
            "SHELL",
            "TERM",
            "TERM_PROGRAM",
            "TERM_PROGRAM_VERSION",
            "TMUX",
            "USER",
            "XDG_CACHE_HOME",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "XDG_STATE_HOME",
        }
    }
    env.update(
        {
            "OPENCODE_SERVER_PASSWORD": record.password,
            "OPENCODE_SERVER_USERNAME": "opencode",
            "OPENCODE_DISABLE_AUTOUPDATE": "1",
            "OPENCODE_DISABLE_MODELS_FETCH": "1",
            "OPENCODE_DISABLE_LSP_DOWNLOAD": "1",
            "OPENCODE_DISABLE_DEFAULT_PLUGINS": "1",
            "OPENCODE_DISABLE_EXTERNAL_SKILLS": "1",
            "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
            "OPENCODE_DISABLE_SHARE": "1",
            "OPENCODE_PURE": "1",
        }
    )
    result = subprocess.run(
        [
            record.opencode,
            "attach",
            f"http://127.0.0.1:{record.port}",
            "--dir",
            record.workspace,
        ],
        check=False,
        env=env,
    )
    return result.returncode


def logs(record: Record, follow: bool) -> int:
    print(f"active workspace: {record.workspace}")
    path = Path(record.log)
    if not path.exists():
        raise BedrockError(f"log does not exist: {path}")
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            print(line, end="")
        if not follow:
            return 0
        while alive(record):
            line = handle.readline()
            if line:
                print(line, end="", flush=True)
                continue
            time.sleep(0.2)
    return 0


def submit_task(record: Record, prompt: str, agent: str) -> int:
    if not prompt.strip():
        raise BedrockError("task prompt must not be empty")
    session = client(record).create_session(prompt.strip()[:80])
    session_id = session["id"]
    task = add_task(record, session_id, prompt, agent)
    client(record).prompt_async(session_id, prompt, agent)
    print(f"submitted {task['id']} to {record.project or record.workspace}")
    print(f"session: {session_id}")
    return 0


def tasks_command(record: Record) -> int:
    for task in list_tasks(record):
        print(f"{task['id']}\t{task['status']}\t{task['agent']}\t{task['session_id']}")
    return 0


def approval_command(args: argparse.Namespace) -> int:
    project, workspace = selection(args, required=True, allow_non_git=True)
    record = find_record(project.name if project else None, workspace)
    api = client(record)
    if args.approval_command == "list":
        pending = api.permissions()
        if not pending:
            print("no pending approvals")
            return 0
        for item in pending:
            print(
                f"{item['id']}\tsession={item['sessionID']}\t{item['permission']}\t"
                f"{json.dumps(item.get('patterns', []))}"
            )
        return 0
    reply = {"approve": "once", "always": "always", "reject": "reject"}[args.approval_command]
    request = next(
        (item for item in api.permissions() if item.get("id") == args.request_id),
        {},
    )
    api.reply(args.request_id, reply, getattr(args, "message", None))
    with Path(record.log).open("a", encoding="utf-8") as handle:
        handle.write(
            f"[opencode-bedrock] approval request={args.request_id} "
            f"session={request.get('sessionID', 'unknown')} "
            f"permission={request.get('permission', 'unknown')} "
            f"action={reply} time={time.time():.3f}\n"
        )
    print(f"{reply}: {args.request_id}")
    return 0


def doctor_command(args: argparse.Namespace) -> int:
    failures = 0
    try:
        opencode = find_opencode(args.opencode_bin)
        result = subprocess.run(
            [str(opencode), "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        print(f"opencode: {opencode} ({result.stdout.strip() or 'version check failed'})")
        failures += int(result.returncode != 0)
    except BedrockError as error:
        print(f"FAIL opencode: {error}")
        failures += 1
    if args.check_sandbox:
        try:
            bwrap = find_bwrap()
            result = subprocess.run(
                [str(bwrap), "--ro-bind", "/", "/", "--", "/usr/bin/true"],
                check=False,
                timeout=10,
            )
            print(f"bubblewrap: {bwrap} ({'ok' if result.returncode == 0 else 'unusable'})")
            failures += int(result.returncode != 0)
        except BedrockError as error:
            print(f"FAIL sandbox: {error}")
            failures += 1
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    profile = os.environ.get("BEDROCK_INFERENCE_PROFILE")
    print(f"AWS region: {'configured' if region else 'not set'}")
    print(f"Bedrock profile: {'configured' if profile else 'not set'}")
    print("AWS calls: not run (use opencode-bedrock-verify-aws explicitly inside AWS)")
    return 1 if failures else 0


def selection(
    args: argparse.Namespace,
    required: bool,
    allow_non_git: bool,
) -> tuple[Project | None, Path | None]:
    if getattr(args, "project", None):
        project = get_project(args.project)
        return project, project.path
    if getattr(args, "workspace", None):
        return None, canonical_workspace(args.workspace, allow_non_git)
    if required:
        raise BedrockError("select exactly one workspace with --project or --workspace")
    return None, None


def parse_agent_models(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise BedrockError("--agent-model must use AGENT=PROFILE")
        name, profile = value.split("=", 1)
        if not name or not profile:
            raise BedrockError("--agent-model must use AGENT=PROFILE")
        if name in result:
            raise BedrockError(f"agent model is configured more than once: {name}")
        result[name] = profile
    return result


def print_record(record: Record) -> None:
    print(f"started:   {record.project or record.key}")
    print(f"workspace: {record.workspace}")
    print(f"endpoint:  http://127.0.0.1:{record.port}")
    print(f"pid:       {record.pid}")
    print(f"log:       {record.log}")


if __name__ == "__main__":
    raise SystemExit(main())
