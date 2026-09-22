"""Independent regressions: receipts verify the request, not just any row change."""
from test_receipt_outcome import row, store
from backend import ops, tools


def test_different_persisted_title_cannot_confirm_requested_edit():
    with store(row(), row(text="a different saved title", due="2026-09-23")):
        result = tools._do_update_item(id="row1", text="the requested title")
    assert result.state != ops.COMPLETED


def test_already_satisfied_request_is_verified_without_claiming_new_change():
    before = row()
    with store(before, dict(before)):
        result = tools._do_update_item(id="row1", text=before["text"])
    assert result.state == ops.COMPLETED
    assert "already" in result.text.lower()
    assert not result.detail.get("changed")


def test_omitted_state_is_not_silently_changed_by_a_title_edit():
    with store(row(state="waiting", waiting_on="Tony"),
               row(text="the requested title", state="active", waiting_on=None)):
        result = tools._do_update_item(id="row1", text="the requested title")
    assert result.state != ops.COMPLETED
