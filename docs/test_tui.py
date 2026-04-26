#!/usr/bin/env python3
"""Simple Textual TUI example for cdda2img."""

from textual.app import App, ComposeResult
from textual.color import Color
from textual.widgets import Checkbox, Footer, Header, Label, ListItem, ListView, Static

FICTIONAL_FILES = [
    "01 - Kauge Kaja.flac",
    "02 - Koiduuni.flac",
    "03 - Epic Blockbuster 2.flac",
    "04 - Rulers of Our Lands.flac",
    "05 - Action Strike.flac",
    "06 - Travelers Notebook.flac",
    "07 - After the End.flac",
    "08 - Dreams of Tomorrow.flac",
    "09 - Horizon Rising.flac",
    "10 - Silent Passage.flac",
    "11 - The Long Road.flac",
    "12 - Final Ember.flac",
]

# Block characters: centre is solid, halo is light shade
CENTRE_CHAR = "█"
HALO_CHAR = "░"

# How much dimmer the halo is relative to the centre (0.0 - 1.0)
HALO_DIM = 0.35


class LightCell(Static):
    """A single cell of the recording light — either centre or halo."""

    DEFAULT_CSS = """
    LightCell {
        width: 2;
        height: 1;
    }
    """

    def __init__(self, char: str, dim: float = 1.0) -> None:
        super().__init__()
        self._char = char
        self._dim = dim  # brightness multiplier relative to the main step

    def on_mount(self) -> None:
        # Step and direction are driven externally by RecordingLight
        self._step = 0

    def set_step(self, step: int) -> None:
        dimmed = int(step * self._dim)
        self.styles.color = Color(dimmed, 0, 0)
        self.styles.background = Color(0, 0, 0)
        self._step = step
        self.refresh()

    def render(self) -> str:
        return self._char


class RecordingLight(Static):
    """A 3x3 grid pulsing red recording indicator light."""

    DEFAULT_CSS = """
    RecordingLight {
        width: 6;
        height: 3;
        layout: grid;
        grid-size: 3 3;
        grid-gutter: 0;
        margin: 0 2;
    }
    """

    def compose(self) -> ComposeResult:
        for row in range(3):
            for col in range(3):
                is_centre = row == 1 and col == 1
                yield LightCell(
                    char=CENTRE_CHAR if is_centre else HALO_CHAR,
                    dim=1.0 if is_centre else HALO_DIM,
                )

    def on_mount(self) -> None:
        self._step = 0
        self._direction = 1
        self.set_interval(1 / 155, self._tick)

    def _tick(self) -> None:
        self._step += self._direction
        if self._step >= 255:
            self._direction = -1
        elif self._step <= 0:
            self._direction = 1
        for cell in self.query(LightCell):
            cell.set_step(self._step)


class HelloTUI(App):
    """A simple Hello, World! TUI example."""

    CSS = """
    Screen {
        layout: vertical;
    }

    #title {
        content-align: center middle;
        height: 3;
        color: $success;
        text-style: bold;
        border: solid $primary;
        margin: 1 2;
    }

    #options {
        height: auto;
        margin: 0 2;
        border: solid $primary;
    }

    #file-list {
        height: 1fr;
        margin: 1 2;
        border: solid $primary;
        border-title-color: $primary;
    }

    #status {
        height: 3;
        content-align: left middle;
        background: $primary-darken-2;
        color: $text;
        padding: 0 2;
    }
    """

    TITLE = "cdda2img"
    SUB_TITLE = "CD Audio to Image"

    def compose(self) -> ComposeResult:
        yield Header()

        yield Label("Hello, World!", id="title")

        with ListView(id="options"):
            yield ListItem(Checkbox("Normalise audio", value=True))
            yield ListItem(Checkbox("Trim silence"))

        yield ListView(
            *[ListItem(Label(f)) for f in FICTIONAL_FILES],
            id="file-list",
        )

        yield RecordingLight()

        yield Label("No file selected.", id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#file-list").border_title = "Files"
        self.query_one("#options").border_title = "Options"

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        """Update status bar when a file is highlighted."""
        if event.list_view.id != "file-list":
            return
        if event.item is not None:
            label = event.item.query_one(Label)
            self.query_one("#status", Label).update(f"Selected: {label.render()}")


if __name__ == "__main__":
    HelloTUI().run()
