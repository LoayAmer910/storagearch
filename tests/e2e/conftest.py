"""Whole journeys. The window journeys use the GUI fixtures; the cloud journeys run against a local stand-in."""
from support.cloud_fixtures import cloud_app, worker  # noqa: F401
from support.gui_fixtures import cloud_home, gui_window, message_boxes, sandbox  # noqa: F401
