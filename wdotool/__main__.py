import sys

from wdotool import cli, daemon

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "__daemon":
        sys.exit(daemon.daemon_main())
    sys.exit(cli.main())
