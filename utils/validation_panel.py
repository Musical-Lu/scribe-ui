"""
The validation panel docked under the video (SUNET/scribe-ui#138).

"Validate" used to answer with a dialog listing every caption with a
problem. Its rows jumped to their caption on a click, but a keyboard could
not reach them, and a problem the editor itself shows (a red character
count) was told by colour alone. The panel replaces that list: it shows one
issue at a time, in words, and steps through them with Previous and Next.

- **Issues, not captions.** A caption with two problems comes up twice; the
  sequence is SRTEditor.validation_items(), in caption order.
- **Stepping keeps the keyboard where it was.** Previous and Next are never
  rebuilt, only relabelled, so focus stays on the one just pressed. At either
  end the button that would go further is disabled, and focus moves to the
  other one first -- a disabled button cannot keep focus, and the browser
  would otherwise drop it on the page.
- **Each step brings the caption into view and marks it**
  (TranscriptEditor.review(): `.transcript-cell-reviewing`), without seeking
  the recording or taking the caret out of wherever the reader left it.
- **"Caption 17" is a button.** It is how the keyboard gets from the issue
  to the text: it puts the caret in that caption -- for a line that is too
  long, where the line passes the limit, which is where a break usually
  belongs.
- **Closing keeps the markings.** The captions stay marked invalid until the
  next Validate; closing only puts the panel away, clears which caption is
  under review, and hands focus back to the Validate button.
- **Words first, colour second.** Every issue says what is wrong and whether
  it is an error (the file is broken) or a warning (a guideline is missed);
  the icon and its colour only repeat that.

Uncertain words and My edits are not here: they are review information, not
a subtitle rule the result breaks.
"""

from typing import Callable, Optional

from nicegui import ui


def position_text(position: int, total: int) -> str:
    """
    "2 of 5" -- where the reader is in the issues.
    """

    return f"{position + 1} of {total}"


def heading_text(total: int) -> str:
    """
    The panel's heading. How many there are is "2 of 5" beside it.
    """

    return "Validation issues" if total else "No validation issues"


class ValidationPanel:
    """
    Built once, hidden, under the video; `show()` fills and opens it each
    time Validate runs.

    Parameters:
        transcript: the TranscriptEditor, for review() and focus().
        captions: returns the editor's current caption list, to tell whether
            the caption an issue names still exists.
        return_focus: returns the element focus goes back to on close (the
            Validate button), or None.
    """

    def __init__(
        self,
        transcript,
        captions: Callable[[], list],
        return_focus: Callable[[], Optional[ui.element]] = lambda: None,
    ) -> None:
        self.transcript = transcript
        self.captions = captions
        self.return_focus = return_focus
        self.items: list = []
        self.position = 0
        self.checked = 0

        # Design B of the six drawn for it: a stripe down the left and a
        # tinted header, both in the colour of the issue shown (amber for a
        # warning, red for an error, green for none), with Previous, the
        # position and Next as a compact group in that header -- and the
        # issue itself below, its kind in small capitals over the rule it
        # breaks. The colour only repeats what the words say.
        with ui.element("section").classes("validation-panel w-full").props(
            'aria-labelledby="validation-panel-heading"'
        ) as self.panel:
            with ui.element("div").classes("validation-panel-head"):
                self.heading = (
                    ui.label("")
                    .classes("validation-panel-heading")
                    .props('id=validation-panel-heading tabindex=-1')
                )

                with ui.element("div").classes("validation-panel-nav") as self.foot:
                    self.previous = ui.button(
                        icon="chevron_left", on_click=self.go_previous, color=None
                    ).props('flat aria-label="Previous issue"').classes(
                        "validation-panel-step"
                    )
                    self.position_label = ui.label("").classes(
                        "validation-panel-position"
                    )
                    self.next = ui.button(
                        icon="chevron_right", on_click=self.go_next, color=None
                    ).props('flat aria-label="Next issue"').classes(
                        "validation-panel-step"
                    )

                ui.button(icon="close", on_click=self.close).props(
                    'flat aria-label="Close validation panel"'
                ).classes("editor-btn editor-icon validation-panel-close").tooltip(
                    "Close"
                )

            # What is shown -- kind, rule, caption -- is one live region, so
            # a screen reader hears the new issue as Next moves to it, while
            # focus stays on Next. The position is said inside it too, since
            # the visible "2 of 5" sits up in the header beside the arrows.
            with ui.element("div").classes("validation-panel-body").props(
                "role=status aria-live=polite aria-atomic=true"
            ):
                self.spoken_position = ui.label("").classes("sr-only")

                with ui.element("div").classes("validation-panel-issue") as self.issue:
                    with ui.element("div").classes("validation-panel-kind"):
                        self.icon = ui.icon("warning").props("aria-hidden=true")
                        self.kind = ui.label("")
                    self.title = ui.label("").classes("validation-panel-title")
                    with ui.element("div").classes("validation-panel-detail"):
                        self.caption_button = (
                            ui.button("", on_click=self.go_to_caption, color=None)
                            .props("flat dense no-caps")
                            .classes("validation-panel-caption")
                        )
                        self.separator = ui.label("·").props("aria-hidden=true")
                        self.detail = ui.label("")

                self.gone = ui.label(
                    "This caption has changed since Validate ran. "
                    "Run Validate again for current results."
                ).classes("validation-panel-gone")

                self.all_clear = ui.label("").classes("validation-panel-clear")

        self.panel.set_visibility(False)

    # -- Opening and closing ---------------------------------------------

    def show(self, items: list, checked: int) -> None:
        """
        Open the panel on the first of `items` (from validation_items()), or
        say there are none. Called by every Validate, so running it again
        reopens and refreshes the panel. Focus moves into the panel -- to
        Next when there is somewhere to go, otherwise to the heading -- so a
        keyboard user is where the results are.
        """

        self.items = items
        self.position = 0
        self.checked = checked
        self.panel.set_visibility(True)
        self.draw()

        if len(self.items) > 1:
            self.focus(self.next)
        else:
            self.focus(self.heading)

    def close(self) -> None:
        """
        Put the panel away. The captions keep their validation markings;
        only the "under review" marking goes. Focus returns to Validate.
        """

        self.panel.set_visibility(False)
        self.transcript.review(None)

        target = self.return_focus()
        if target is not None:
            self.focus(target)

    # -- Stepping -----------------------------------------------------------

    def go_next(self) -> None:
        if self.position < len(self.items) - 1:
            self.position += 1
            self.draw()
        # At the end Next is disabled; keep focus on something that can
        # still take it.
        if self.position == len(self.items) - 1:
            self.focus(self.previous)

    def go_previous(self) -> None:
        if self.position > 0:
            self.position -= 1
            self.draw()
        if self.position == 0:
            self.focus(self.next)

    def go_to_caption(self) -> None:
        """
        From the issue to the text: the caret goes into the caption, where
        the problem is.
        """

        item = self.current()
        if item is None or not self.exists(item):
            return

        caption = item["caption"]
        offset = min(item.get("offset", 0), len(caption.text))
        self.transcript.focus(caption.index, offset)

    # -- Drawing ------------------------------------------------------------

    def current(self) -> Optional[dict]:
        if 0 <= self.position < len(self.items):
            return self.items[self.position]
        return None

    def exists(self, item: dict) -> bool:
        """
        Whether the caption an issue names is still in the list -- a merge,
        a delete or an undo since Validate replaces caption objects.
        """

        return any(caption is item["caption"] for caption in self.captions())

    def draw(self) -> None:
        total = len(self.items)
        item = self.current()

        self.heading.set_text(heading_text(total))
        self.issue.set_visibility(item is not None)
        self.foot.set_visibility(total > 0)
        self.all_clear.set_visibility(total == 0)
        self.gone.set_visibility(False)

        if item is None:
            self.panel.classes(replace="validation-panel w-full is-clear")
            noun = "caption" if self.checked == 1 else "captions"
            self.all_clear.set_text(
                f"All {self.checked} {noun} follow the subtitle guidelines."
            )
            self.spoken_position.set_text("")
            self.transcript.review(None)
            return

        caption = item["caption"]
        severity = "is-error" if item["error"] else "is-warning"

        # The stripe and the header's tint follow the issue shown.
        self.panel.classes(replace=f"validation-panel w-full {severity}")
        self.position_label.set_text(position_text(self.position, total))
        self.spoken_position.set_text(
            f"Issue {position_text(self.position, total)}."
        )
        self.kind.set_text("Error" if item["error"] else "Warning")
        self.title.set_text(item["title"])
        self.icon.name = "error" if item["error"] else "warning"
        self.caption_button.set_text(f"Caption {caption.index}")
        self.caption_button.props(
            f'aria-label="Go to caption {caption.index}"'
        )
        self.detail.set_text(item["detail"])

        self.previous.set_enabled(self.position > 0)
        self.next.set_enabled(self.position < total - 1)

        if self.exists(item):
            self.caption_button.set_enabled(True)
            self.transcript.review(caption.index)
        else:
            self.caption_button.set_enabled(False)
            self.gone.set_visibility(True)
            self.transcript.review(None)

    @staticmethod
    def focus(element: ui.element) -> None:
        ui.run_javascript(
            f"const t = getElement({element.id});"
            "const el = t && (t.$el || t);"
            "if (el && el.focus) el.focus();"
        )
