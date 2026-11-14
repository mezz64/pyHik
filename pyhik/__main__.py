"""``python -m pyhik <command>``: command line tools that come with pyhik.

Commands are imported only when they are asked for, so nothing here costs anything
for programs that use pyhik as a library.
"""

import importlib
import sys
from typing import Optional, Sequence

COMMANDS = {
    "checkisapi": "try the ISAPI image settings against a real camera or NVR",
}


def usage() -> str:
    width = max(len(name) for name in COMMANDS)
    lines = ["usage: python -m pyhik <command> [options]", "", "commands:"]
    lines += ["  %-*s  %s" % (width, name, text) for name, text in COMMANDS.items()]
    lines += ["", "Run python -m pyhik <command> --help for the options of a command."]
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        print(usage())
        return 0 if args else 2
    if args[0] not in COMMANDS:
        print("pyhik: unknown command %r\n\n%s" % (args[0], usage()), file=sys.stderr)
        return 2
    return importlib.import_module("pyhik." + args[0]).main(args[1:])


if __name__ == "__main__":
    sys.exit(main())
