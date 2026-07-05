"""Enable ``python -m music_cli`` as an alias for the ``music`` console script."""

from music_cli.cli import main

raise SystemExit(main())
