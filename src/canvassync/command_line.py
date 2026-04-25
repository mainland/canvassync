import argparse
import logging
import re
import sys
from abc import ABC, abstractmethod
from argparse import ArgumentParser, Namespace
from pathlib import Path

import pandas as pd
import yaml
from rich import print

from .sync import CanvasSync


class Command(ABC):
    description: str
    """Command line description."""

    def __init__(self, description: str):
        self.description = description

    @abstractmethod
    def add_arguments(self, parser: ArgumentParser) -> None: ...

    @abstractmethod
    def handle(self, parser: ArgumentParser, args: Namespace) -> int: ...

    def run(self) -> int:
        parser = ArgumentParser(
            description=self.description,
            formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        )
        parser.add_argument(
            "-d",
            "--debug",
            action="store_const",
            const=logging.DEBUG,
            dest="loglevel",
            default=logging.WARNING,
            help="print debugging information",
        )
        parser.add_argument(
            "-v",
            "--verbose",
            action="store_const",
            const=logging.INFO,
            dest="loglevel",
            help="be verbose",
        )
        parser.add_argument(
            "-q",
            "--quiet",
            action="store_true",
            default=False,
            help="do not ask for confirmation",
        )
        parser.add_argument(
            "-n",
            "--dry-run",
            action="store_true",
            default=False,
            help="dry run",
        )

        self.add_arguments(parser)

        args = parser.parse_args()

        logging.basicConfig(
            format="%(asctime)s:%(name)s:%(levelname)s:%(message)s",
            level=args.loglevel,
        )

        logging.getLogger("tzlocal").disabled = True
        logging.getLogger("pypandoc").disabled = True
        logging.getLogger("canvasapi.requester").disabled = True
        logging.getLogger("urllib3.connectionpool").disabled = True

        return self.handle(parser, args)


class SyncCommand(Command):
    sync_obj: CanvasSync
    """Sync."""

    def __init__(self) -> None:
        super().__init__(description="Synchronize with Canvas")

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("--config", type=Path, required=True)
        parser.add_argument("--root", type=Path)
        parser.add_argument(
            "--api-key",
            "--canvas-api-key",
            help="Canvas API key; overrides api_key from the config file",
        )

        #
        # Subparsers for individual commands
        #
        subparsers = parser.add_subparsers()
        subparsers.required = True
        subparsers.dest = "command"

        #
        # Manipulate courses
        #
        courses_parser = subparsers.add_parser(
            "courses", help="Manipulate courses"
        )
        courses_parser.set_defaults(func=self.courses)

        #
        # Dump
        #
        dump_parser = subparsers.add_parser("dump", help="Dump Canvas modules")
        dump_parser.set_defaults(func=self.dump)

        #
        # Synchronize
        #
        sync_parser = subparsers.add_parser(
            "sync", help="Synchronize with Canvas"
        )
        sync_parser.add_argument("--limit", action="append", default=None)
        sync_parser.set_defaults(func=self.sync)

        #
        # Render markdown
        #
        render_parser = subparsers.add_parser("render", help="Render markdown")
        render_parser.add_argument("path", type=Path)
        render_parser.add_argument("--output", "-o", type=Path)
        render_parser.set_defaults(func=self.render)

        #
        # Dump roster
        #
        roster_parser = subparsers.add_parser("roster", help="Dump roster")
        roster_parser.add_argument(
            "--section", help="Limit roster to students from matching section"
        )
        roster_parser.add_argument(
            "--inactive",
            action="store_true",
            default=False,
            help="Include inactive students",
        )
        roster_parser.add_argument(
            "--email",
            action="store_true",
            default=False,
            help="Produce student emails instead of CSV",
        )
        roster_parser.add_argument("--output", "-o", type=Path)
        roster_parser.set_defaults(func=self.roster)

    def handle(self, parser: ArgumentParser, args: Namespace) -> int:
        if not hasattr(args, "func"):
            parser.error("Command not specified")

        try:
            try:
                self.sync_obj = CanvasSync(
                    args.config,
                    root=args.root,
                    api_key=args.api_key,
                )
            except yaml.YAMLError as exc:
                print(exc, file=sys.stderr)
                sys.exit(1)

            args.func(args)
            return 0
        except Exception:
            logging.exception("Error")
            return -1

    def dump(self, args: Namespace) -> None:
        for module in self.sync_obj.course.get_modules():
            print(module.name)

            for item in module.get_module_items():
                indent = " " * 2 * (item.indent + 1)
                print(f"{indent:}{item.title:} ({item.type:})")

    def sync(self, args: Namespace) -> None:
        self.sync_obj.sync(limits=args.limit)

    def render(self, args: Namespace) -> None:
        rendered = self.sync_obj.render_markdown(args.path)

        if args.output:
            with args.output.open("w", encoding="utf8") as f:
                f.write(rendered)
        else:
            print(rendered)

    def courses(self, args: Namespace) -> None:
        courses = self.sync_obj.canvas.get_courses()

        for course in courses:
            if hasattr(course, "sis_course_id"):
                print(
                    f"Course Name: {course.name},"
                    f"SIS ID: '{course.sis_course_id}'"
                )
            else:
                print(f"Course Name: {course.name}")

    def roster(self, args: Namespace) -> None:
        enrollments = self.sync_obj.course.get_enrollments()

        students = [
            enrollment
            for enrollment in enrollments
            if enrollment.type == "StudentEnrollment"
        ]

        users = self.sync_obj.course.get_users()
        user_emails = {user.id: user.email for user in users}

        items: list[tuple[str, str, str, str, str, str, bool]] = []

        sections: dict[str, str] = {}

        for student in students:
            # Get student name
            name = student.user["sortable_name"]
            m = re.match("^(.*), (.*)$", name)
            assert m is not None
            last = m.group(1)
            first = m.group(2)

            # Get student login ID
            login_id = student.user["login_id"]

            # Get student section
            section_id = student.sis_section_id

            if student.course_section_id in sections:
                section_name = sections[student.course_section_id]
            else:
                section = self.sync_obj.canvas.get_section(
                    student.course_section_id
                )
                section_name = section.name
                sections[student.course_section_id] = section_name

            # Get enrollment state
            active = student.enrollment_state == "active"

            if (args.inactive or active) and (
                args.section is None or re.search(args.section, section_name)
            ):
                items.append(
                    (
                        first,
                        last,
                        login_id,
                        user_emails.get(student.user_id, ""),
                        section_id,
                        section_name,
                        active,
                    )
                )

        df = pd.DataFrame(
            items,
            columns=[
                "First",
                "Last",
                "Username",
                "Email",
                "Section ID",
                "Section Name",
                "Enrollment State",
            ],
        )

        if args.email:
            emails = "\n".join(df["Email"])

            if args.output:
                with args.output.open("w", encoding="utf8") as f:
                    f.write(emails)
            else:
                print(emails)
        else:
            if args.output:
                df.to_csv(args.output)
            else:
                print(df)


def sync() -> int:
    command = SyncCommand()
    return command.run()
