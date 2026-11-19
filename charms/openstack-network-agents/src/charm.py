#!/usr/bin/env python3

# Copyright 2025 Canonical Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Openstack Network Agents subordinate charm.

This charm deploys the `openstack-network-agents` snap and configures
OVS bridge mapping + optional chassis-as-gw on the network role node.
"""

from __future__ import (
    annotations,
)

import ipaddress
import json
import logging
import re
import subprocess
from urllib.parse import (
    quote,
)

import charms.operator_libs_linux.v2.snap as snap
import ops
import ops_sunbeam.charm as sunbeam_charm
import ops_sunbeam.guard as sunbeam_guard
import ops_sunbeam.relation_handlers as sunbeam_rhandlers
import ops_sunbeam.tracing as sunbeam_tracing
import yaml
from cryptography import (
    x509,
)

logger = logging.getLogger(__name__)

MICROOVN_SNAP = "microovn"
MICROOVN_DAEMON_YAML = "/var/snap/microovn/common/state/daemon.yaml"
OVN_CHASSIS_PLUG = "ovn-chassis"
OVN_CHASSIS_SLOT = "microovn:ovn-chassis"
DATA_BINDING = "data"
NDR_CONFIG = "ndr.config"
NDR_FIELDS = {"domain", "family", "service-name", "host", "router-id"}
NDR_LABEL = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
NDR_HOST = re.compile(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?")


def parse_dynamic_routing_instances(value: str) -> list[dict[str, str]]:
    """Validate the local agent plan before applying it."""
    instances = json.loads(value)
    if not isinstance(instances, list):
        raise ValueError("instances must be a JSON array")
    for entry in instances:
        _validate_dynamic_routing_instance(entry)
    if len({(entry["domain"], entry["family"]) for entry in instances}) != len(
        instances
    ):
        raise ValueError("duplicate instance domain and address family")
    for field in ("service-name", "host", "router-id"):
        if len({entry[field] for entry in instances}) != len(instances):
            raise ValueError(f"duplicate instance {field}")
    return sorted(instances, key=lambda entry: entry["service-name"])


def _validate_dynamic_routing_instance(entry: object) -> None:
    """Check one instance before storing its identity or starting its agent."""
    if not isinstance(entry, dict) or entry.keys() != NDR_FIELDS:
        raise ValueError("each instance must contain the NDR fields")
    if not all(
        isinstance(item, str) and 0 < len(item) <= 255
        for item in entry.values()
    ):
        raise ValueError("instance fields must be strings of 1-255 characters")
    if entry["family"] not in {"ipv4", "ipv6"}:
        raise ValueError("instance family must be ipv4 or ipv6")
    for field in ("domain", "service-name"):
        if not NDR_LABEL.fullmatch(entry[field]):
            raise ValueError(f"invalid instance {field}")
    if not NDR_HOST.fullmatch(entry["host"]):
        raise ValueError("invalid instance host")
    router_id = ipaddress.IPv4Address(entry["router-id"])
    if router_id.is_unspecified or router_id.is_multicast:
        raise ValueError("router ID must be a nonzero unicast IPv4 address")


class AgentError(Exception):
    """Custom exception for Agent errors."""


@sunbeam_tracing.trace_type
class JujuInfoRequiresHandler(sunbeam_rhandlers.RelationHandler):
    """Relation handler for juju-info interface."""

    def setup_event_handler(self):
        """Set up event handler."""
        rel = self.charm.on[self.relation_name]
        self.framework.observe(rel.relation_joined, self._on_event)
        self.framework.observe(rel.relation_changed, self._on_event)
        self.framework.observe(rel.relation_broken, self._on_event)
        return None

    def _on_event(self, event: ops.RelationEvent) -> None:
        """Handle juju-info relation events."""
        self.callback_f(event)

    @property
    def ready(self) -> bool:
        """True if at least one juju-info relation is present."""
        return bool(self.charm.model.relations.get(self.relation_name))


@sunbeam_tracing.trace_sunbeam_charm
class OpenstackNetworkAgentsOperatorCharm(
    sunbeam_charm.OSBaseOperatorCharmSnap
):
    """Snap-based subordinate for OVN provider networking and dynamic routing."""

    service_name = "openstack-network-agents"

    def __init__(self, framework: ops.framework.Framework) -> None:
        super().__init__(framework)
        self.framework.observe(
            self.on.update_status,
            self._on_update_status,
        )
        self.framework.observe(
            self.on.set_network_agents_local_settings_action,
            self._set_network_agents_local_settings_action,
        )
        self.framework.observe(
            self.on.list_nics_action,
            self._list_nics_action,
        )
        self.framework.observe(
            self.on.set_dynamic_routing_instances_action,
            self._set_dynamic_routing_instances_action,
        )
        self.framework.observe(
            self.on.amqp_relation_broken, self._on_amqp_lost
        )
        self.framework.observe(
            self.on.amqp_relation_changed, self._on_amqp_changed
        )

    @property
    def data_address(self) -> str | None:
        """Get address from data binding."""
        use_binding = self.model.config.get("use-data-binding")
        if not use_binding:
            return None
        binding = self.model.get_binding(DATA_BINDING)
        if binding is None:
            return None
        address = binding.network.bind_address
        if address is None:
            return None
        return str(address)

    @property
    def snap_name(self) -> str:
        """Snap to install (configurable for dev/testing)."""
        return str(self.model.config.get("snap-name"))

    @property
    def snap_channel(self) -> str:
        """Channel to track in the Snap Store."""
        return str(self.model.config.get("snap-channel"))

    def _on_install(self, event: ops.InstallEvent):
        """Handle the install event."""
        try:
            super()._on_install(event)
        except sunbeam_guard.WaitingExceptionError as e:
            logger.warning("Deferring install event: %s", e.msg)
            event.defer()

    def _on_update_status(self, event: ops.UpdateStatusEvent):
        """Re-run configure_charm on update-status.

        The base class does not observe update-status.  As a subordinate
        whose principal (MicroOVN) may not be ready at initial deploy time,
        we need periodic retries to finish configuration once the snap
        installation succeeds via the deferred install event.
        """
        self.configure_charm(event)

    def _get_microovn_node_name(self) -> str | None:
        """Read the local MicroOVN node name from daemon.yaml."""
        try:
            with open(MICROOVN_DAEMON_YAML) as f:
                data = yaml.safe_load(f)
            return data.get("name")
        except (FileNotFoundError, PermissionError, yaml.YAMLError) as e:
            logger.warning("Failed to read %s: %s", MICROOVN_DAEMON_YAML, e)
            return None

    def _get_microovn_node_services(self) -> set[str]:
        """Get the MicroOVN cluster services for the local node.

        Reads the node name from daemon.yaml, then parses the output of
        ``microovn status`` to find the services assigned to this node
        (e.g. ``{"central", "chassis", "switch"}``).
        """
        node_name = self._get_microovn_node_name()
        if not node_name:
            return set()

        try:
            result = subprocess.run(
                ["microovn", "status"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                logger.warning(
                    "microovn status failed: %s", result.stderr.strip()
                )
                return set()
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            logger.warning("Failed to run microovn status: %s", e)
            return set()

        lines = result.stdout.splitlines()
        for i, line in enumerate(lines):
            if node_name in line:
                if i + 1 < len(lines):
                    svc_line = lines[i + 1].strip()
                    if svc_line.startswith("Services:"):
                        svc_str = svc_line.removeprefix("Services:")
                        return {s.strip() for s in svc_str.split(",")}
        return set()

    def _check_microovn_ready(self) -> bool:
        """Check if microovn snap is ready.

        The microovn snap must be present and the node must have the
        ``switch`` service listed in ``microovn status``.
        """
        microovn = self.snap_module.SnapCache()[MICROOVN_SNAP]
        if not microovn.present:
            logger.warning("%s snap is not present", MICROOVN_SNAP)
            return False

        node_services = self._get_microovn_node_services()
        if not node_services:
            logger.warning(
                "Could not determine %s services for this node",
                MICROOVN_SNAP,
            )
            return False

        if "switch" not in node_services:
            logger.warning(
                "%s node is missing the switch service", MICROOVN_SNAP
            )
            return False
        return True

    def ensure_snap_present(self):
        """Install snap after verifying microovn readiness."""
        if not self._check_microovn_ready():
            raise sunbeam_guard.WaitingExceptionError(
                "Microovn snap is not ready"
            )
        return super().ensure_snap_present()

    def ensure_services_running(self, enable: bool = True) -> None:
        """The snap configure hook starts the configured dynamic routing agents."""
        pass

    def stop_services(self, relation: set[str] | None = None) -> None:
        """Stop agent instances when the charm loses its principal or AMQP."""
        if (
            relation is None or relation.intersection({"juju-info", "amqp"})
        ) and (self.get_snap().present):
            self._configure_dynamic_routing(amqp_available=False)

    def get_relation_handlers(
        self, handlers: list[sunbeam_rhandlers.RelationHandler] | None = None
    ) -> list[sunbeam_rhandlers.RelationHandler]:
        """Return a list of relation handlers used by this charm."""
        handlers = handlers or []
        if self.can_add_handler("juju-info", handlers):
            juju_info = JujuInfoRequiresHandler(
                self,
                "juju-info",
                self.configure_charm,
                "juju-info" in self.mandatory_relations,
            )
            handlers.append(juju_info)
        return super().get_relation_handlers(handlers)

    def _connect_ovn_chassis(self) -> None:
        """Connect the snap ovn-chassis plug to microovn:ovn-chassis."""
        openstack_network_agents = self.get_snap()

        try:
            openstack_network_agents.connect(
                OVN_CHASSIS_PLUG, slot=OVN_CHASSIS_SLOT
            )
            logger.info(
                "Connected microovn:ovn-chassis slot to openstack-network-agents:ovn-chassis plug"
            )
        except snap.SnapError as e:
            logger.error(
                f"Failed to connect to microovn:ovn-chassis: {e.message}"
            )
            raise

    def _set_network_agents_local_settings_action(
        self, event: ops.ActionEvent
    ) -> None:
        """Action to set per-unit local settings for provider networking."""
        local_settings = [
            "network.external-interface",
            "network.bridge-name",
            "network.physnet-name",
            "network.bridge-mapping",
            "network.enable-chassis-as-gw",
        ]

        new_snap_settings = {}
        for setting in local_settings:
            action_param = setting.split(".")[1]
            # enable-chassis-as-gw is a boolean, so we want to allow false values to be set
            if event.params.get(action_param) is not None:
                new_snap_settings[setting] = event.params.get(action_param)
        if new_snap_settings:
            self.set_snap_data(new_snap_settings)

    def _dynamic_routing_transport(self) -> dict[str, str]:
        """Get transport settings for the same RabbitMQ vhost as Neutron."""
        interface = self.amqp.interface
        if not (hostname := interface.hostname) or not (
            password := interface.password
        ):
            return {}
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        port = interface.ssl_port or self.amqp.DEFAULT_PORT
        transport = {
            "transport-url": (
                f"rabbit://{quote(self.amqp.username, safe='')}:"
                f"{quote(password, safe='')}@{hostname}:{port}/"
                f"{quote(self.amqp.vhost, safe='')}"
            ),
        }
        if interface.ssl_port:
            ca_cert = interface.ssl_ca
            if not ca_cert:
                return {}
            try:
                x509.load_pem_x509_certificates(ca_cert.encode())
            except ValueError:
                return {}
            transport["ca-cert"] = ca_cert
        return transport

    def _configure_dynamic_routing(
        self,
        instances: list[dict[str, str]] | None = None,
        *,
        amqp_available: bool = True,
    ) -> bool:
        """Store placement in the snap and supply transport while AMQP is ready."""
        snap_service = self.get_snap()
        raw = snap_service.get(None, typed=True).get("ndr", {}).get("config")
        current = json.loads(raw) if isinstance(raw, str) else raw
        # Import older placements once if AMQP loss cleared the snap config.
        legacy = getattr(self._state, "ndr_instances", None)
        if instances is None:
            if current is not None:
                instances = current.get(
                    "paused-instances", current["instances"]
                )
            elif legacy is not None:
                instances = json.loads(legacy)
            else:
                return False

        transport = (
            self._dynamic_routing_transport()
            if instances and amqp_available
            else {}
        )
        desired = {"instances": instances, **transport}
        if instances and not transport:
            # Older snaps stop when the runnable list is empty.
            desired = {"instances": [], "paused-instances": instances}
        if current != desired:
            snap_service.set(
                {NDR_CONFIG: json.dumps(desired, sort_keys=True)}, typed=True
            )
        if legacy is not None:
            self._state.ndr_instances = None
        return not instances or bool(transport)

    def _set_dynamic_routing_instances_action(
        self, event: ops.ActionEvent
    ) -> None:
        """Replace the desired dynamic routing plans for this unit."""
        try:
            instances = parse_dynamic_routing_instances(
                event.params["instances"]
            )
        except (TypeError, ValueError):
            event.fail("Invalid dynamic routing instance list")
            return
        try:
            configured = self._configure_dynamic_routing(instances)
        except (self.snap_module.SnapError, TimeoutError):
            event.fail(
                "Failed to apply the dynamic routing configuration to the snap"
            )
            return
        event.set_results(
            {"configured": configured, "instances": len(instances)}
        )

    def _on_amqp_lost(self, event: ops.RelationBrokenEvent) -> None:
        """Remove the transport immediately when the AMQP relation is removed."""
        self.stop_services({"amqp"})

    def _on_amqp_changed(self, event: ops.RelationChangedEvent) -> None:
        """Stop agents if credentials are removed without breaking the relation."""
        if not self.amqp.ready:
            self.stop_services({"amqp"})

    def configure_snap(self, event: ops.EventBase) -> None:
        """Push configuration into the snap."""
        self._connect_ovn_chassis()
        snap_data = {
            "settings.debug": bool(self.model.config.get("debug") or False),
            "network.external-bridge-address": str(
                self.model.config.get("external-bridge-address")
            ),
        }
        data_addr = self.data_address
        if data_addr:
            snap_data["network.ip-address"] = data_addr
        self.set_snap_data(snap_data)
        self._configure_dynamic_routing()

    def _agent_cli_cmd(self, cmd: str):
        """Helper to run cli commands on the snap."""
        process = subprocess.run(
            [
                "snap",
                "run",
                self.snap_name,
                "--verbose",
            ]
            + cmd.split(),
            capture_output=True,
        )

        stderr = process.stderr.decode("utf-8")
        logger.debug("logs: %s", stderr)
        stdout = process.stdout.decode("utf-8")
        logger.debug("stdout: %s", stdout)
        if process.returncode != 0:
            raise AgentError(stderr)

        return stdout

    def _list_nics_action(self, event: ops.ActionEvent):
        """Run list_nics action."""
        try:
            stdout = self._agent_cli_cmd("list-nics --format json")
        except AgentError as e:
            event.fail(str(e))
            return

        # cli returns a json dict with keys "nics" and "candidate"
        event.set_results({"result": stdout})


if __name__ == "__main__":  # pragma: nocover
    ops.main(OpenstackNetworkAgentsOperatorCharm)
