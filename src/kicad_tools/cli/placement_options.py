"""Options shared by the placement optimizers."""

import argparse

DEFAULT_DECOUPLING_RADIUS_MM = 6.0


def add_decoupling_radius_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--decoupling-radius",
        type=float,
        default=DEFAULT_DECOUPLING_RADIUS_MM,
        metavar="MM",
        help="Maximum distance from a bypass capacitor's supply pad to its designated "
        "IC pin (default: %(default)s mm).",
    )
