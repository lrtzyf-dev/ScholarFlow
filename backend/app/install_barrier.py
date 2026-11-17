"""Small startup gate shared by updater and runtime ownership, without config IO."""
from pathlib import Path
from .release_files import no_link


def barrier_path(data):
    data = no_link(no_link(data).resolve())
    return no_link(data.parent / ("." + data.name + ".install-pending.json"))


def blocks_startup(data):
    # A malformed/unreadable marker also requires recovery. It cannot be treated
    # as approval to start and migrate a possibly half-replaced installation.
    try:
        barrier_path(data).lstat()
        return True
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return True
