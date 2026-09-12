from pathlib import Path


BUNDLE = Path(__file__).parents[1] / "plugin" / "dashboard" / "dist" / "index.js"


def test_dashboard_loads_settings_even_when_assignment_log_fails() -> None:
    source = BUNDLE.read_text(encoding="utf-8")
    assert 'api("/settings").then(setSettings)' in source
    assert 'api("/assignments").then((result) => setAssignments(result.assignments || []))' in source
    assert "Promise.all([api(\"/settings\"), api(\"/assignments\")])" not in source


def test_grant_folder_form_is_collapsed_by_default() -> None:
    source = BUNDLE.read_text(encoding="utf-8")
    assert 'h("details", { className: "rounded border p-3" }' in source
    assert 'h("summary", { className: "cursor-pointer font-medium" }, "Grant a folder")' in source
