#!/usr/bin/env python3

# Copyright 2024 Canonical Ltd.
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


"""Sunbeam Clusterd Charm.

This charm manages a clusterd deployment. Clusterd is a service storing
every metadata about a sunbeam deployment.
"""

import logging
from pathlib import (
    Path,
)

import clusterd
import ops.framework
import ops_sunbeam.charm as sunbeam_charm
import requests
import tenacity
from charms.operator_libs_linux.v2 import (
    snap,
)
from ops.main import (
    main,
)
from ops_sunbeam.relation_handlers import (
    RelationHandler,
)
from relation_handlers import (
    ClusterdNewNodeEvent,
    ClusterdNodeAddedEvent,
    ClusterdPeerHandler,
    ClusterdRemoveNodeEvent,
)

logger = logging.getLogger(__name__)


class SunbeamClusterdCharm(sunbeam_charm.OSBaseOperatorCharm):
    """Charm the service."""

    _state = ops.StoredState()
    service_name = "sunbeam-clusterd"
    clusterd_port = 7000

    def __init__(self, framework: ops.Framework) -> None:
        """Run constructor."""
        super().__init__(framework)
        self._state.set_default(channel="config", departed=False)
        self.framework.observe(self.on.install, self._on_install)
        self.framework.observe(self.on.stop, self._on_stop)
        self.framework.observe(
            self.on.get_credentials_action, self._on_get_credentials_action
        )
        self._clusterd = clusterd.ClusterdClient(
            Path("/var/snap/openstack/common/state/control.socket")
        )

    def get_relation_handlers(
        self, handlers: list[RelationHandler] | None = None
    ) -> list[RelationHandler]:
        """Setup charm relation handlers."""
        handlers = handlers or []
        if self.can_add_handler("peers", handlers):
            self.peers = ClusterdPeerHandler(
                self,
                "peers",
                self.configure_charm,
                "peers" in self.mandatory_relations,
            )
            handlers.append(self.peers)
        return super().get_relation_handlers(handlers)

    def _on_install(self, event: ops.InstallEvent) -> None:
        """Handle install event."""
        try:
            self.ensure_snap_present()
        except TimeoutError:
            logger.debug("Snap installation failed, retrying.")
            event.defer()
            return
        self.clusterd_ready()

    def _on_stop(self, event: ops.StopEvent) -> None:
        """Handle stop event."""
        try:
            self._clusterd.shutdown()
        except clusterd.ClusterdUnavailableError:
            logger.debug("Clusterd not available, skipping shutdown.")
        snap.SnapCache()["openstack"].stop()

    def _on_get_credentials_action(self, event: ops.ActionEvent) -> None:
        """Handle get-credentials action."""
        if not self.peers.interface.state.joined:
            event.fail("Clusterd not joined yet")

        event.set_results(
            {
                "url": "https://"
                + self._binding_address()
                + ":"
                + str(self.clusterd_port)
            }
        )

    def _binding_address(self) -> str:
        """Return the binding address."""
        relation = self.model.get_relation("peers")

        if relation is None:
            raise ValueError("Missing relation peers")

        binding = self.model.get_binding(relation)

        if binding is None:
            raise ValueError("Missing binding peers")

        if binding.network.bind_address is None:
            raise ValueError("Missing binding address")

        return str(binding.network.bind_address)

    def ensure_snap_present(self):
        """Install/refresh snap if needed."""
        config = self.model.config.get
        snap_channel = config("snap-channel")

        try:
            cache = snap.SnapCache()
            openstack = cache["openstack"]
            if not openstack.present or snap_channel != openstack.channel:
                openstack.ensure(snap.SnapState.Latest, channel=snap_channel)
                self._state.channel = openstack.channel
                self.set_workload_version()
        except snap.SnapError as e:
            logger.error(
                "An exception occurred when installing snap. Reason: %s",
                e.message,
            )

    def set_workload_version(self):
        """Set workload version."""
        cache = snap.SnapCache()
        openstack = cache["openstack"]
        if not openstack.present:
            return
        version = openstack.channel + f"(rev {openstack.revision})"
        self.unit.set_workload_version(version)

    def configure_app_leader(self, event: ops.EventBase):
        """Configure leader unit."""
        if not self.clusterd_ready():
            logger.debug("Clusterd not ready yet.")
            event.defer()
            return
        if not self.is_leader_ready():
            self.bootstrap_cluster()
            self.peers.interface.state.joined = True
        super().configure_app_leader(event)
        if isinstance(event, ClusterdNewNodeEvent):
            self.add_node_to_cluster(event)

    def configure_app_non_leader(self, event: ops.EventBase):
        """Configure non-leader unit."""
        super().configure_app_non_leader(event)
        if isinstance(event, ClusterdNodeAddedEvent):
            self.join_node_to_cluster(event)

    def configure_unit(self, event: ops.EventBase):
        """Configure unit."""
        super().configure_unit(event)
        self.ensure_snap_present()
        if isinstance(event, ClusterdRemoveNodeEvent):
            self.remove_node_from_cluster(event)
        config = self.model.config.get
        snap_data = {
            "daemon.debug": config("debug", False),
        }
        self.set_snap_data(snap_data)

    def set_snap_data(self, snap_data: dict):
        """Set snap data on local snap."""
        cache = snap.SnapCache()
        openstack = cache["openstack"]
        new_settings = {}
        for k in sorted(snap_data.keys()):
            try:
                if snap_data[k] != openstack.get(k):
                    new_settings[k] = snap_data[k]
            except snap.SnapError:
                # Trying to retrieve an unset parameter results in a snapError
                # so assume the snap.SnapError means there is missing config
                # that needs setting.
                new_settings[k] = snap_data[k]
        if new_settings:
            logger.debug(f"Applying new snap settings {new_settings}")
            openstack.set(new_settings, typed=True)
        else:
            logger.debug("Snap settings do not need updating")

    @tenacity.retry(
        stop=tenacity.stop_after_attempt(10),
        retry=(
            tenacity.retry_if_exception_type(clusterd.ClusterdUnavailableError)
            | tenacity.retry_if_not_result(lambda result: result)
        ),
        after=tenacity.after_log(logger, logging.WARNING),
        wait=tenacity.wait_exponential(multiplier=1, min=1, max=30),
    )
    def clusterd_ready(self) -> bool:
        """Check whether clusterd is ready."""
        if not self._clusterd.ready():
            return False
        return True

    def bootstrap_cluster(self):
        """Bootstrap the cluster."""
        logger.info("Bootstrapping the cluster")
        self._clusterd.bootstrap(
            self.unit.name.replace("/", "-"),
            self._binding_address() + ":" + str(self.clusterd_port),
        )

    def add_node_to_cluster(self, event: ClusterdNewNodeEvent) -> None:
        """Generate token for node joining."""
        if event.unit is None:
            logger.debug("No unit to add")
            return
        unit_key = f"{event.unit.name}.join_token"
        if self.peers.get_app_data(unit_key):
            logger.debug(f"Already generated token for {event.unit.name}")
            return

        try:
            token = self._clusterd.generate_token(
                event.unit.name.replace("/", "-")
            )
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code >= 500:
                logger.error(f"Clusterd error: {str(e)}")
                logger.debug("Failed to generate token, retrying.")
                event.defer()
                return
            raise e
        self.peers.set_app_data({unit_key: token})

    def remove_node_from_cluster(self, event: ClusterdRemoveNodeEvent) -> None:
        """Remove node from cluster."""
        if event.departing_unit is None:
            logger.debug("No unit to remove")
            return

        self_departing = event.departing_unit.name == self.unit.name
        departing_key = f"{event.departing_unit.name}.join_token"
        unit_name = event.departing_unit.name.replace("/", "-")

        logger.debug(f"Departing unit: {event.departing_unit.name}")
        try:
            logger.debug(f"Removing member {unit_name}")
            self._clusterd.remove_node(unit_name, allow_not_found=True)
        except clusterd.ClusterdUnavailableError as e:
            if "Remote end closed connection without response" in str(e):
                logger.debug(
                    "Forwarded request failed, most likely because member was leader"
                    " and this member was removed."
                )
                return
            if self_departing:
                logger.debug(
                    "Happened during self removal, ignoring. Error: %s", e
                )
                return
        except requests.exceptions.HTTPError as e:
            is_503 = e.response is not None and e.response.status_code == 503
            if self_departing and is_503:
                logger.debug(
                    "Clusterd is not initialized, most likely because"
                    " leader has already removed this unit from clusterd."
                    " Error: %s",
                    e.response.text,
                )
                return
            raise e
        finally:
            departing_key = f"{event.departing_unit.name}.join_token"
            if self.unit.is_leader():
                self.peers.interface._app_data_bag.pop(
                    departing_key,
                    None,
                )

    def join_node_to_cluster(self, event: ClusterdNodeAddedEvent) -> None:
        """Join node to cluster."""
        token = self.peers.get_app_data(f"{self.unit.name}.join_token")
        if token is None:
            logger.warning("No token found for unit %s", self.unit.name)
            return
        member = self.unit.name.replace("/", "-")
        if not self.peers.interface.state.joined:
            self._clusterd.join(
                member,
                self._binding_address() + ":" + str(self.clusterd_port),
                token,
            )
            self.peers.interface.state.joined = True
            self.peers.set_unit_data({"joined": "true"})

        self.status.set(ops.WaitingStatus("Waiting for clusterd role"))
        is_role_set = self._wait_until_role_set(member)
        if not is_role_set:
            logger.debug("Member %s is still pending", member)
            event.defer()
            return
        self.status.set(ops.ActiveStatus())

    @tenacity.retry(
        wait=tenacity.wait_fixed(5),
        stop=tenacity.stop_after_delay(300),
        retry=tenacity.retry_if_not_result(lambda result: result),
    )
    def _wait_until_role_set(self, name: str) -> bool:
        member = self._clusterd.get_member(name)
        role = member.get("role")
        logger.debug(f"Member {name} role: {role}")
        if role == "PENDING":
            return False
        return True


if __name__ == "__main__":  # pragma: nocover
    main(SunbeamClusterdCharm)
