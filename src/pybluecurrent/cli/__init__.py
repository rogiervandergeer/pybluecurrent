"""The ``pybluecurrent`` command-line interface."""

from typer import Typer

from pybluecurrent.cli.transactions import transactions

app = Typer(no_args_is_help=True, add_completion=False, help="Command-line access to your BlueCurrent account.")


@app.callback()
def _root() -> None:
    """Keep this a command group, so subcommands stay named as more are added."""


app.command()(transactions)

main = app

__all__ = ["app", "main"]
