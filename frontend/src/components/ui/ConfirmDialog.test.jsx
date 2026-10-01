import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ConfirmProvider, useConfirm } from "./ConfirmDialog";

function Harness({ onResult, options }) {
  const confirm = useConfirm();
  return (
    <button
      onClick={async () => onResult(await confirm(options))}
    >
      open
    </button>
  );
}

function setup(options, onResult) {
  return render(
    <ConfirmProvider>
      <Harness onResult={onResult} options={options} />
    </ConfirmProvider>,
  );
}

describe("ConfirmDialog / useConfirm", () => {
  it("resolves true when confirmed", async () => {
    const user = userEvent.setup();
    const onResult = vi.fn();
    setup({ title: "Delete?", confirmLabel: "Delete it" }, onResult);

    await user.click(screen.getByText("open"));
    expect(screen.getByRole("dialog")).toHaveTextContent("Delete?");

    await user.click(screen.getByRole("button", { name: "Delete it" }));
    expect(onResult).toHaveBeenCalledWith(true);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("resolves false when cancelled", async () => {
    const user = userEvent.setup();
    const onResult = vi.fn();
    setup({ title: "Delete?" }, onResult);

    await user.click(screen.getByText("open"));
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onResult).toHaveBeenCalledWith(false);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("resolves false on Escape", async () => {
    const user = userEvent.setup();
    const onResult = vi.fn();
    setup({ title: "Delete?" }, onResult);

    await user.click(screen.getByText("open"));
    await user.keyboard("{Escape}");
    expect(onResult).toHaveBeenCalledWith(false);
  });
});
