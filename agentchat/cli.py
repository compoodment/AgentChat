"""Command-line entry point; the chat screen is imported only when needed."""

import argparse
import json
import math
import sys

from agentchat import store


def _parser():
    parser = argparse.ArgumentParser(prog="chat", description="A tiny local agent group chat")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("ui", help="Open the user's chat screen")
    say = commands.add_parser("say", help="Append a message")
    say.add_argument("--from", dest="sender", choices=(store.USER,) + store.AGENTS, required=True)
    say.add_argument("--kind", choices=("msg", "claim", "release"), default="msg")
    say.add_argument("text", nargs="+", metavar="TEXT")
    wait = commands.add_parser("wait", help="Wait for messages after an agent's last ack")
    wait.add_argument("--as", dest="agent", choices=store.AGENTS, required=True)
    wait.add_argument("--timeout", type=float, default=25)
    ack = commands.add_parser("ack", help="Acknowledge messages after handling them")
    ack.add_argument("--as", dest="agent", choices=store.AGENTS, required=True)
    ack.add_argument("id", type=int)
    status = commands.add_parser("status", help="Publish a bounded presence lease")
    status.add_argument("--as", dest="agent", choices=store.AGENTS, required=True)
    status.add_argument("state", choices=("listening", "working", "idle"))
    status.add_argument("--note", default="")
    auto = commands.add_parser("auto", help="Legacy fixed-rate timed policy")
    auto.add_argument("--seconds", type=float, required=True)
    custom = commands.add_parser("custom", help="Set messages/window-minutes/duration-minutes")
    custom.add_argument("limit", type=int)
    custom.add_argument("window_minutes", type=float)
    custom.add_argument("duration_minutes", type=float)
    mode = commands.add_parser("mode", help="Choose a posting policy")
    mode.add_argument("mode", choices=("normal", "wait", "paused", "resume"))
    commands.add_parser("pause", help="Pause agent posts")
    commands.add_parser("resume", help="Resume the previous posting policy")
    commands.add_parser("guard", help="Show guard mode and any retry delay")
    show = commands.add_parser("show", help="Read a full message without changing acknowledgments")
    show.add_argument("id", type=int)
    log = commands.add_parser("log", help="Read a bounded recent history without acknowledging it")
    log.add_argument("--last", type=int, default=20, metavar="N")
    search = commands.add_parser("search", help="Search full history without changing acknowledgments")
    search.add_argument("text", nargs="+", metavar="TEXT")
    search.add_argument("--last", type=int, default=50, metavar="N")
    search.add_argument("--before", type=int, dest="before_id", metavar="ID")
    for command, help_text in [("take", "Atomically claim a user request before starting work"),
                               ("release-task", "Release a completed or handed-off request")]:
        task = commands.add_parser(command, help=help_text)
        task.add_argument("--as", dest="agent", choices=store.AGENTS, required=True)
        task.add_argument("id", type=int, help="User message id")
    bridge = commands.add_parser("bridge", help="Manage the same-session Codex watcher")
    bridge.add_argument("action", choices=("start", "status", "stop", "retry", "probe", "confirm"))
    bridge.add_argument("wake_id", nargs="?")
    bridge.add_argument("--thread")
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        if args.command in (None, "ui"):
            from agentchat import ui
            return ui.main()
        if args.command == "say":
            message = store.append_message(args.sender, " ".join(args.text), args.kind)
            print(json.dumps(message, ensure_ascii=False))
        elif args.command == "bridge":
            from agentchat import bridge
            if args.action == "start":
                if args.wake_id is not None:
                    raise ValueError("Start requires --thread, not a wake id")
                value = bridge.start(args.thread)
            elif args.action == "confirm":
                if args.thread is not None:
                    raise ValueError("Confirm accepts a wake id, not --thread")
                value = bridge.confirm(args.wake_id)
            else:
                if args.thread is not None or args.wake_id is not None:
                    raise ValueError("Only start accepts --thread; only confirm accepts a wake id")
                value = getattr(bridge, args.action)()
            print(json.dumps(value, ensure_ascii=False))
        elif args.command in ("take", "release-task"):
            action = store.take_task if args.command == "take" else store.release_task
            print(json.dumps(action(args.agent, args.id), ensure_ascii=False))
        elif args.command == "ack":
            store.set_ack(args.agent, args.id)
        elif args.command == "status":
            print(json.dumps(store.set_presence(args.agent, args.state, args.note), ensure_ascii=False))
        elif args.command == "auto":
            print(json.dumps(store.set_autonomy(args.seconds)))
        elif args.command == "custom":
            print(json.dumps(store.set_policy("custom", limit=args.limit,
                             window_s=args.window_minutes*60, duration_s=args.duration_minutes*60)))
        elif args.command in ("mode", "pause", "resume"):
            mode = args.mode if args.command == "mode" else ("paused" if args.command == "pause" else "resume")
            print(json.dumps(store.resume_policy() if mode == "resume" else store.set_policy(mode)))
        elif args.command == "guard":
            print(json.dumps(store.guard_status()))
        elif args.command == "show":
            if args.id < 1:
                raise ValueError("Message id must be positive")
            messages = store.read_messages(args.id - 1, limit=1)
            if not messages or messages[0]["id"] != args.id:
                raise ValueError(f"Message #{args.id} does not exist")
            print(json.dumps(messages[0], ensure_ascii=False))
        elif args.command in ("log", "search"):
            if not 0 <= args.last <= 1000:
                raise ValueError("--last must be between 0 and 1000")
            messages = (store.read_recent(args.last) if args.command == "log" else
                        store.search_messages(" ".join(args.text), args.last, args.before_id))
            for message in messages:
                print(json.dumps(message, ensure_ascii=False))
        elif args.command == "wait":
            after = store.get_ack(args.agent)
            if args.timeout < 0 or not math.isfinite(args.timeout):
                raise ValueError("timeout must be a finite non-negative number")
            messages = store.read_messages(after)
            if not messages:
                store.set_presence(args.agent, "listening", ttl=args.timeout + 60)
                if not store.wait_for_change(after, args.timeout):
                    return 2
                messages = store.read_messages(after)
            if not messages:
                return 2
            store.set_presence(args.agent, "working")
            for message in messages:
                print(json.dumps(message, ensure_ascii=False))
        return 0
    except BrokenPipeError:
        return 1
    except (store.GuardBlocked, store.ClaimConflict, ValueError, OSError, KeyError) as error:
        print(f"chat: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
