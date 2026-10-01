"""A fake Playwright page, so the ATS adapters can be tested without a browser.

The adapters only ever talk to a page through
:class:`app.services.browser_runner.Form`, which speaks a deliberately small
vocabulary: read the controls, fill one, choose an option, upload a file, click
the first selector that exists, read the text. :class:`FakePage` implements
exactly that vocabulary and nothing else — which is the point. A test can build
a three-step Workday wizard, run the real adapter over it, and assert on what
was typed where.

Anything the fake doesn't recognise raises, the same way Playwright does for a
selector that isn't on the page. That is what exercises the adapters' "one
stubborn field must not abandon the other fourteen" behaviour.
"""
from __future__ import annotations

from dataclasses import dataclass, field


def control(
    *,
    name: str = "",
    label: str = "",
    field_type: str = "text",
    tag: str = "input",
    options: tuple[str, ...] = (),
    required: bool = False,
    automation_id: str = "",
    field_id: str = "",
    placeholder: str = "",
) -> dict:
    """One control, in the shape ``FIELD_JS`` returns from a real page."""
    return {
        "name": name,
        "field_id": field_id,
        "field_type": field_type,
        "label": label,
        "placeholder": placeholder,
        "tag": tag,
        "options": list(options),
        "required": required,
        "automation_id": automation_id,
    }


def selector_for(payload: dict) -> str:
    """The selector ``FormField.selector`` will derive for this control."""
    if payload.get("automation_id"):
        return f'[data-automation-id="{payload["automation_id"]}"]'
    if payload.get("field_id"):
        return f'[id="{payload["field_id"]}"]'
    return f'[name="{payload["name"]}"]'


@dataclass
class Step:
    """One page (or one wizard step) the fake browser will show."""

    controls: list[dict] = field(default_factory=list)
    # Selectors that exist on this step. Clicking one that isn't here raises,
    # exactly as Playwright does.
    clickable: tuple[str, ...] = ()
    # Clicking any of these moves to the next step.
    advances_on: tuple[str, ...] = ()
    visible: tuple[str, ...] = ()
    # Checkbox selectors that start ticked. Clicking one toggles it, exactly as
    # a real checkbox does — which is what lets a test catch an adapter that
    # "unticks" a box by clicking it blind.
    ticked: tuple[str, ...] = ()
    text: str = ""


class FakeContext:
    def __init__(self) -> None:
        self.saved_state = {"cookies": [{"name": "li_at", "value": "fake"}]}

    def storage_state(self) -> dict:
        return self.saved_state


class FakePage:
    """A scripted page. Advance through ``steps`` by clicking their buttons."""

    def __init__(self, steps: list[Step], *, url: str = "https://example.test/apply"):
        self.steps = steps
        self.index = 0
        self.url = url
        self.context = FakeContext()
        self.frames: list = []

        # What the run did, for assertions.
        self.visited: list[str] = []
        self.filled: dict[str, str] = {}
        self.selected: dict[str, str] = {}
        self.uploaded: dict[str, str] = {}
        self.clicked: list[str] = []
        # Live checkbox state, seeded per step the first time it is read.
        self.checkboxes: dict[str, bool] = {}
        self.screenshots: list[str] = []
        self.goto_failures = 0

    # ---- current step ----

    @property
    def step(self) -> Step:
        return self.steps[min(self.index, len(self.steps) - 1)]

    def _selectors(self) -> set[str]:
        """Every selector that addresses something on this step.

        Controls read through ``eval_on_selector_all``, plus the raw selectors a
        step declares — a login form's ``#username`` is typed into directly
        rather than discovered, and it still has to exist.
        """
        return (
            {selector_for(payload) for payload in self.step.controls}
            | set(self.step.clickable)
            | set(self.step.visible)
        )

    # ---- the Playwright surface Form uses ----

    def goto(self, url: str, **_kwargs) -> None:
        if self.goto_failures > 0:
            self.goto_failures -= 1
            raise RuntimeError("net::ERR_TIMED_OUT")
        self.visited.append(url)
        self.url = url

    def eval_on_selector_all(self, _selector: str, _js: str) -> list[dict]:
        return [dict(payload, index=i) for i, payload in enumerate(self.step.controls)]

    def fill(self, selector: str, value: str, timeout: int | None = None) -> None:
        if selector not in self._selectors():
            raise RuntimeError(f"no element matching {selector}")
        self.filled[selector] = value

    def select_option(
        self,
        selector: str,
        label: str | None = None,
        value: str | None = None,
        timeout: int | None = None,
    ) -> None:
        if selector not in self._selectors():
            raise RuntimeError(f"no element matching {selector}")
        chosen = label if label is not None else value
        payload = next(
            p for p in self.step.controls if selector_for(p) == selector
        )
        if payload.get("options") and chosen not in payload["options"]:
            raise RuntimeError(f"{chosen!r} is not an option")
        self.selected[selector] = chosen or ""

    def set_input_files(
        self, selector: str, path: str, timeout: int | None = None
    ) -> None:
        if selector not in self._selectors():
            raise RuntimeError(f"no element matching {selector}")
        self.uploaded[selector] = path

    def is_checked(self, selector: str) -> bool:
        if selector in self.checkboxes:
            return self.checkboxes[selector]
        if selector in self.step.ticked:
            return True
        if selector in self._selectors():
            return False
        raise RuntimeError(f"no element matching {selector}")

    def click(self, selector: str, timeout: int | None = None) -> None:
        step = self.step
        if selector in step.ticked or selector in self.checkboxes:
            self.checkboxes[selector] = not self.is_checked(selector)
            self.clicked.append(selector)
            return
        if selector in step.advances_on:
            self.clicked.append(selector)
            self.index = min(self.index + 1, len(self.steps))
            return
        if selector in step.clickable or selector in step.visible:
            self.clicked.append(selector)
            return
        raise RuntimeError(f"no element matching {selector}")

    def is_visible(self, selector: str) -> bool:
        return selector in self.step.visible

    def inner_text(self, _selector: str = "body") -> str:
        return self.step.text

    def wait_for_timeout(self, _ms: int) -> None:
        return None

    def screenshot(self, path: str, full_page: bool = False) -> None:
        self.screenshots.append(path)
