# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for the charm-owned Netplan fragment."""

import stat
import subprocess
from unittest.mock import (
    Mock,
)

import ndr_peering
import pytest
import yaml


def test_only_changed_ports_are_reconfigured(tmp_path, monkeypatch):
    """Install address-only config and avoid reconfiguration on reapply."""
    path = tmp_path / "ndr.yaml"
    other_file = tmp_path / "50-cloud-init.yaml"
    other_file.write_text("operator-owned")
    command = Mock()
    monkeypatch.setattr(ndr_peering, "_run", command)
    ndr_peering.reconcile_addresses(path, {"ndr-1a": "192.0.2.3/24"})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert yaml.safe_load(path.read_text())["network"]["ethernets"] == {
        "ndr-1a": {
            "match": {"name": "ndr-1a"},
            "renderer": "networkd",
            "accept-ra": False,
            "addresses": ["192.0.2.3/24"],
        }
    }
    command.reset_mock()
    ndr_peering.reconcile_addresses(path, {"ndr-1a": "192.0.2.3/24"})
    command.assert_not_called()
    ndr_peering.reconcile_addresses(
        path, {"ndr-1a": "192.0.2.3/24", "ndr-2b": "2001:db8::2/64"}
    )
    command.assert_called_with("networkctl", "reconfigure", "ndr-2b")
    ndr_peering.reconcile_addresses(path, {})
    assert not path.exists()
    assert other_file.read_text() == "operator-owned"


def test_foreign_fragment_is_not_replaced_or_removed(tmp_path):
    """Refuse to write over a pre-existing config outside charm ownership."""
    path = tmp_path / "ndr.yaml"
    path.write_text("managed by operator")
    with pytest.raises(ValueError, match="operator-owned"):
        ndr_peering.reconcile_addresses(path, {"ndr-1a": "192.0.2.3/24"})
    ndr_peering.reconcile_addresses(path, {})
    assert path.read_text() == "managed by operator"


def test_networkd_failure_leaves_previous_fragment_retryable(
    tmp_path, monkeypatch
):
    """Restore the previous fragment if networkd cannot apply the update."""
    path = tmp_path / "ndr.yaml"
    apply = Mock()
    monkeypatch.setattr(ndr_peering, "_run", apply)
    ndr_peering.reconcile_addresses(path, {"ndr-1a": "192.0.2.3/24"})
    previous = path.read_text()
    apply.side_effect = subprocess.CalledProcessError(1, "networkctl")
    with pytest.raises(subprocess.CalledProcessError):
        ndr_peering.reconcile_addresses(
            path, {"ndr-1a": "192.0.2.3/24", "ndr-2b": "2001:db8::2/64"}
        )
    assert path.read_text() == previous
