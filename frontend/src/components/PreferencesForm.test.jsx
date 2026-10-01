import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import PreferencesForm, { preferencePayload } from "./PreferencesForm";

const BASE_PREFS = {
  resume_id: null,
  target_roles: [],
  target_industries: [],
  locations: [],
  remote_only: false,
  salary_min: null,
  min_fit_score: 60,
  daily_application_limit: 5,
  auto_send: false,
  form_autofill_enabled: false,
  follow_up_count: 2,
  follow_up_interval_days: 4,
  follow_up_stop_on_reply: true,
  // extra server field that must not leak into the save payload
  applications_created: 12,
};

describe("PreferencesForm", () => {
  it("renders the core fields", () => {
    render(<PreferencesForm prefs={BASE_PREFS} onChange={() => {}} />);
    expect(screen.getByLabelText("Minimum fit score")).toBeInTheDocument();
    expect(screen.getByLabelText("Applications per day")).toBeInTheDocument();
    expect(screen.getByLabelText(/Send automatically/)).toBeInTheDocument();
  });

  it("emits a patch when a toggle changes", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<PreferencesForm prefs={BASE_PREFS} onChange={onChange} />);

    await user.click(screen.getByLabelText(/Send automatically/));
    expect(onChange).toHaveBeenCalledWith({ auto_send: true });
  });

  it("emits a patch when an industry chip is toggled", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<PreferencesForm prefs={BASE_PREFS} onChange={onChange} />);

    await user.click(screen.getByRole("button", { name: "fintech" }));
    expect(onChange).toHaveBeenCalledWith({ target_industries: ["fintech"] });
  });

  it("marks suggested fields with a 'from resume' badge", () => {
    render(
      <PreferencesForm
        prefs={{ ...BASE_PREFS, target_roles: ["Staff Engineer"] }}
        onChange={() => {}}
        suggested={["target_roles"]}
        notes={{ target_roles: "Inferred from your last two roles" }}
      />,
    );
    expect(screen.getAllByText("from resume").length).toBeGreaterThan(0);
  });

  it("preferencePayload keeps only the persisted fields", () => {
    const payload = preferencePayload(BASE_PREFS);
    expect(payload).not.toHaveProperty("applications_created");
    expect(payload).toHaveProperty("min_fit_score", 60);
    expect(Object.keys(payload)).toContain("follow_up_stop_on_reply");
  });
});
