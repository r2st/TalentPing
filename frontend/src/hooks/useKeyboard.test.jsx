import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { isTyping, useKeyboard, useListKeyboard } from "./useKeyboard";

/* -------------------------------------------------------------------------- */
/* useKeyboard                                                                */
/* -------------------------------------------------------------------------- */

function Bound({ handlers, enabled = true }) {
  useKeyboard(handlers, { enabled });
  return (
    <div>
      <input aria-label="search" />
      <textarea aria-label="notes" />
      <div contentEditable aria-label="rich" role="textbox" tabIndex={0} />
    </div>
  );
}

describe("useKeyboard", () => {
  it("fires the handler bound to a key", async () => {
    const user = userEvent.setup();
    const onR = vi.fn();
    render(<Bound handlers={{ r: onR }} />);

    await user.keyboard("r");

    expect(onR).toHaveBeenCalledTimes(1);
  });

  it("ignores keys with no handler", async () => {
    const user = userEvent.setup();
    const onR = vi.fn();
    render(<Bound handlers={{ r: onR }} />);

    await user.keyboard("q");

    expect(onR).not.toHaveBeenCalled();
  });

  it("leaves browser and OS chords alone", async () => {
    const user = userEvent.setup();
    const onR = vi.fn();
    render(<Bound handlers={{ r: onR }} />);

    // Cmd-R is reload; a page shortcut must never swallow it.
    await user.keyboard("{Meta>}r{/Meta}");
    await user.keyboard("{Control>}r{/Control}");

    expect(onR).not.toHaveBeenCalled();
  });

  it("does not fire while the user is typing in an input", async () => {
    const user = userEvent.setup();
    const onR = vi.fn();
    const onJ = vi.fn();
    render(<Bound handlers={{ r: onR, j: onJ }} />);

    // Without the guard, typing "reply to Jordan" fires r and j per character.
    await user.click(screen.getByLabelText("search"));
    await user.type(screen.getByLabelText("search"), "reply to Jordan");

    expect(onR).not.toHaveBeenCalled();
    expect(onJ).not.toHaveBeenCalled();
  });

  it("does not fire while the user is typing in a textarea", async () => {
    const user = userEvent.setup();
    const onR = vi.fn();
    render(<Bound handlers={{ r: onR }} />);

    await user.type(screen.getByLabelText("notes"), "rewrite this");

    expect(onR).not.toHaveBeenCalled();
  });

  it("still lets Escape through from inside a field", async () => {
    const user = userEvent.setup();
    const onEscape = vi.fn();
    render(<Bound handlers={{ Escape: onEscape }} />);

    // Escape is exactly what a user reaches for while in a field.
    await user.click(screen.getByLabelText("search"));
    await user.keyboard("{Escape}");

    expect(onEscape).toHaveBeenCalledTimes(1);
  });

  it("binds nothing while disabled", async () => {
    const user = userEvent.setup();
    const onR = vi.fn();
    render(<Bound handlers={{ r: onR }} enabled={false} />);

    await user.keyboard("r");

    expect(onR).not.toHaveBeenCalled();
  });

  it("unbinds on unmount", async () => {
    const user = userEvent.setup();
    const onR = vi.fn();
    const { unmount } = render(<Bound handlers={{ r: onR }} />);

    unmount();
    await user.keyboard("r");

    expect(onR).not.toHaveBeenCalled();
  });

  it("calls the latest handler without re-binding", async () => {
    const user = userEvent.setup();
    const first = vi.fn();
    const second = vi.fn();
    const { rerender } = render(<Bound handlers={{ r: first }} />);

    rerender(<Bound handlers={{ r: second }} />);
    await user.keyboard("r");

    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledTimes(1);
  });
});

describe("isTyping", () => {
  it.each([
    ["INPUT", { tagName: "INPUT" }, true],
    ["TEXTAREA", { tagName: "TEXTAREA" }, true],
    ["SELECT", { tagName: "SELECT" }, true],
    ["DIV", { tagName: "DIV" }, false],
    ["contenteditable", { tagName: "DIV", isContentEditable: true }, true],
  ])("reads %s correctly", (_label, target, expected) => {
    expect(isTyping({ target })).toBe(expected);
  });

  it("is false for an event with no target", () => {
    expect(isTyping({ target: null })).toBe(false);
  });
});

/* -------------------------------------------------------------------------- */
/* useListKeyboard                                                            */
/* -------------------------------------------------------------------------- */

const ITEMS = [{ id: 1 }, { id: 2 }, { id: 3 }];

function List({ items = ITEMS, initial = null, ...rest }) {
  const [selectedId, setSelectedId] = useState(initial);
  useListKeyboard({ items, selectedId, onSelect: setSelectedId, ...rest });
  return <p data-testid="cursor">{selectedId === null ? "none" : selectedId}</p>;
}

describe("useListKeyboard", () => {
  it("starts at the top on j when nothing is selected", async () => {
    const user = userEvent.setup();
    render(<List />);

    await user.keyboard("j");

    expect(screen.getByTestId("cursor")).toHaveTextContent("1");
  });

  it("starts at the bottom on k when nothing is selected", async () => {
    const user = userEvent.setup();
    render(<List />);

    await user.keyboard("k");

    expect(screen.getByTestId("cursor")).toHaveTextContent("3");
  });

  it("steps forward and back", async () => {
    const user = userEvent.setup();
    render(<List initial={1} />);

    await user.keyboard("jj");
    expect(screen.getByTestId("cursor")).toHaveTextContent("3");

    await user.keyboard("k");
    expect(screen.getByTestId("cursor")).toHaveTextContent("2");
  });

  it("stops at the ends rather than wrapping", async () => {
    const user = userEvent.setup();
    render(<List initial={3} />);

    await user.keyboard("jjj");
    expect(screen.getByTestId("cursor")).toHaveTextContent("3");

    await user.keyboard("kkkkk");
    expect(screen.getByTestId("cursor")).toHaveTextContent("1");
  });

  it("does nothing on an empty list", async () => {
    const user = userEvent.setup();
    render(<List items={[]} />);

    await user.keyboard("jk");

    expect(screen.getByTestId("cursor")).toHaveTextContent("none");
  });

  it("opens the selection with Enter", async () => {
    const user = userEvent.setup();
    const onOpen = vi.fn();
    render(<List initial={2} onOpen={onOpen} />);

    await user.keyboard("{Enter}");

    expect(onOpen).toHaveBeenCalledWith(2);
  });

  it("Enter on an empty cursor moves to the top instead of opening nothing", async () => {
    const user = userEvent.setup();
    const onOpen = vi.fn();
    render(<List onOpen={onOpen} />);

    await user.keyboard("{Enter}");

    expect(onOpen).not.toHaveBeenCalled();
    expect(screen.getByTestId("cursor")).toHaveTextContent("1");
  });

  it("closes on Escape", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    render(<List initial={1} onClose={onClose} />);

    await user.keyboard("{Escape}");

    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("replies to the selection with r", async () => {
    const user = userEvent.setup();
    const onReply = vi.fn();
    render(<List initial={2} onReply={onReply} />);

    await user.keyboard("r");

    expect(onReply).toHaveBeenCalledWith(2);
  });

  it("does not reply when nothing is selected", async () => {
    const user = userEvent.setup();
    const onReply = vi.fn();
    render(<List onReply={onReply} />);

    await user.keyboard("r");

    expect(onReply).not.toHaveBeenCalled();
  });

  it("suspends the whole map when disabled", async () => {
    const user = userEvent.setup();
    const onOpen = vi.fn();
    render(<List initial={1} onOpen={onOpen} enabled={false} />);

    await user.keyboard("j{Enter}");

    expect(onOpen).not.toHaveBeenCalled();
    expect(screen.getByTestId("cursor")).toHaveTextContent("1");
  });

  it("honours a custom id accessor", async () => {
    const user = userEvent.setup();
    render(
      <List
        items={[{ slug: "a" }, { slug: "b" }]}
        idOf={(item) => item.slug}
        initial="a"
      />,
    );

    await user.keyboard("j");

    expect(screen.getByTestId("cursor")).toHaveTextContent("b");
  });
});
