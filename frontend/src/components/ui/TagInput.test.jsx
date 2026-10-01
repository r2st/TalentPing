import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import TagInput from "./TagInput";

function Controlled({ initial = [], onChange }) {
  const [values, setValues] = useState(initial);
  return (
    <TagInput
      id="t"
      placeholder="Add one"
      values={values}
      onChange={(next) => {
        setValues(next);
        onChange?.(next);
      }}
    />
  );
}

describe("TagInput", () => {
  it("commits a value on Enter and clears the draft", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Controlled onChange={onChange} />);
    const input = screen.getByPlaceholderText("Add one");

    await user.type(input, "python{Enter}");
    expect(onChange).toHaveBeenLastCalledWith(["python"]);
    expect(input).toHaveValue("");
    expect(screen.getByText("python")).toBeInTheDocument();
  });

  it("splits comma-separated input into multiple tags", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Controlled onChange={onChange} />);
    // Typing a comma commits, so type the second value after it.
    await user.type(screen.getByPlaceholderText("Add one"), "go,rust{Enter}");
    expect(onChange).toHaveBeenLastCalledWith(["go", "rust"]);
  });

  it("skips case-insensitive duplicates", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Controlled initial={["Python"]} onChange={onChange} />);
    await user.type(screen.getByPlaceholderText("Add one"), "python{Enter}");
    expect(onChange).toHaveBeenLastCalledWith(["Python"]);
  });

  it("removes a tag via its remove button", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Controlled initial={["python", "go"]} onChange={onChange} />);
    await user.click(screen.getByLabelText("Remove python"));
    expect(onChange).toHaveBeenLastCalledWith(["go"]);
  });

  it("disables the Add button until there is a draft", async () => {
    const user = userEvent.setup();
    render(<Controlled />);
    const add = screen.getByRole("button", { name: "Add" });
    expect(add).toBeDisabled();
    await user.type(screen.getByPlaceholderText("Add one"), "x");
    expect(add).toBeEnabled();
  });
});
