"""Datasheet command handlers."""

__all__ = ["add_package_parsers", "run_datasheet_command"]


def add_package_parsers(subparsers) -> None:
    """Register the package commands on both datasheet CLI entry points."""
    for name, help_text in (
        ("extract-package", "Extract package information from PDF"),
        ("suggest-footprint", "Suggest matching footprints for packages in PDF"),
    ):
        parser = subparsers.add_parser(name, help=help_text)
        parser.add_argument("pdf", help="Path to PDF file")
        parser.add_argument("-o", "--output", help="Output file path (default: stdout)")
        parser.add_argument("--pages", help="Page range to search (e.g., '1-10' or '1,2,5')")
        parser.add_argument("--format", choices=["text", "json"], default="text")


def run_datasheet_command(args) -> int:
    """Handle datasheet subcommands."""
    if not args.datasheet_command:
        print("Usage: kicad-tools datasheet <command> [options]")
        print(
            "Commands: search, download, list, cache, convert, extract-images, extract-tables, "
            "info, extract-package, suggest-footprint"
        )
        return 1

    from ..datasheet_cmd import main as datasheet_main

    if args.datasheet_command == "search":
        sub_argv = ["search", args.part]
        if args.format != "text":
            sub_argv.extend(["--format", args.format])
        if args.limit != 10:
            sub_argv.extend(["--limit", str(args.limit)])
        return datasheet_main(sub_argv) or 0

    elif args.datasheet_command == "download":
        sub_argv = ["download", args.part]
        if args.output:
            sub_argv.extend(["-o", args.output])
        if args.force:
            sub_argv.append("--force")
        if getattr(args, "format", "text") != "text":
            sub_argv.extend(["--format", args.format])
        return datasheet_main(sub_argv) or 0

    elif args.datasheet_command == "list":
        sub_argv = ["list"]
        if args.format != "text":
            sub_argv.extend(["--format", args.format])
        return datasheet_main(sub_argv) or 0

    elif args.datasheet_command == "cache":
        sub_argv = ["cache", args.cache_action]
        if getattr(args, "older_than", None):
            sub_argv.extend(["--older-than", str(args.older_than)])
        if getattr(args, "format", "text") != "text":
            sub_argv.extend(["--format", args.format])
        return datasheet_main(sub_argv) or 0

    elif args.datasheet_command == "convert":
        sub_argv = ["convert", args.pdf]
        if args.output:
            sub_argv.extend(["-o", args.output])
        if args.pages:
            sub_argv.extend(["--pages", args.pages])
        if getattr(args, "format", "text") != "text":
            sub_argv.extend(["--format", args.format])
        return datasheet_main(sub_argv) or 0

    elif args.datasheet_command == "extract-images":
        sub_argv = ["extract-images", args.pdf, "-o", args.output]
        if args.pages:
            sub_argv.extend(["--pages", args.pages])
        if args.min_size != 100:
            sub_argv.extend(["--min-size", str(args.min_size)])
        if args.format != "text":
            sub_argv.extend(["--format", args.format])
        return datasheet_main(sub_argv) or 0

    elif args.datasheet_command == "extract-tables":
        sub_argv = ["extract-tables", args.pdf]
        if args.output:
            sub_argv.extend(["-o", args.output])
        if args.pages:
            sub_argv.extend(["--pages", args.pages])
        if args.format != "markdown":
            sub_argv.extend(["--format", args.format])
        return datasheet_main(sub_argv) or 0

    elif args.datasheet_command in ("extract-package", "suggest-footprint"):
        sub_argv = [args.datasheet_command, args.pdf, "--format", args.format]
        if args.pages is not None:
            sub_argv.extend(["--pages", args.pages])
        if args.output is not None:
            sub_argv.extend(["--output", args.output])
        return datasheet_main(sub_argv) or 0

    elif args.datasheet_command == "info":
        sub_argv = ["info", args.pdf]
        if args.format != "text":
            sub_argv.extend(["--format", args.format])
        return datasheet_main(sub_argv) or 0

    return 1
