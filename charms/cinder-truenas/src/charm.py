#!/usr/bin/env python3

#
# Copyright 2021 Canonical Ltd.
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

"""Cinder Ceph Operator Charm.

This charm provide Cinder <-> Ceph integration as part
of an OpenStack deployment
"""
import functools
import logging
from typing import (
    Mapping,
)

import ops
import ops.charm
import ops_sunbeam.charm as charm
import ops_sunbeam.guard as sunbeam_guard
import ops_sunbeam.relation_handlers as relation_handlers
import ops_sunbeam.relation_handlers as sunbeam_rhandlers
import ops_sunbeam.tracing as sunbeam_tracing

import charms.cinder_k8s.v0.storage_backend as sunbeam_storage_backend  # noqa
import charms.operator_libs_linux.v2.snap as snap

logger = logging.getLogger(__name__)

SNAP = "gboutry-cinder-volume-truenas"


@sunbeam_tracing.trace_type
class StorageBackendProvidesHandler(sunbeam_rhandlers.RelationHandler):
    """Relation handler for storage-backend interface type."""

    def setup_event_handler(self):
        """Configure event handlers for an storage-backend relation."""
        logger.debug("Setting up Identity Service event handler")
        sb_svc = sunbeam_tracing.trace_type(
            sunbeam_storage_backend.StorageBackendProvides
        )(
            self.charm,
            self.relation_name,
        )
        self.framework.observe(sb_svc.on.api_ready, self._on_ready)
        return sb_svc

    def _on_ready(self, event) -> None:
        """Handles AMQP change events."""
        # Ready is only emitted when the interface considers
        # that the relation is complete (indicated by a password)
        self.callback_f(event)

    @property
    def ready(self) -> bool:
        """Check whether storage-backend interface is ready for use."""
        return self.interface.remote_ready()


@sunbeam_tracing.trace_sunbeam_charm
class CinderTruenasOperatorCharm(charm.OSBaseOperatorCharm):
    """Cinder/TrueNas Operator charm."""

    service_name = "cinder-volume"

    mandatory_relations = {
        "database",
        "amqp",
        "storage-backend",
    }

    def __init__(self, framework):
        super().__init__(framework)
        self._state.set_default(api_ready=False)
        self.framework.observe(
            self.on.install,
            self._on_install,
        )

    def _on_install(self, _: ops.InstallEvent):
        """Run install on this unit."""
        self.ensure_snap_present()

    @functools.cache
    def get_snap_cache(self) -> snap.SnapCache:
        """Return snap cache."""
        return snap.SnapCache()

    def ensure_snap_present(self):
        """Install snap if it is not already present."""
        config = self.model.config.get
        try:
            cache = self.get_snap_cache()
            hypervisor = cache[SNAP]

            if not hypervisor.present:
                hypervisor.ensure(
                    snap.SnapState.Latest, channel=config("snap-channel")  # type: ignore
                )
        except snap.SnapError as e:
            logger.error(
                "An exception occurred when installing %s. Reason: %s",
                SNAP,
                e.message,
            )

    def get_relation_handlers(
        self, handlers: list[relation_handlers.RelationHandler] | None = None
    ) -> list[relation_handlers.RelationHandler]:
        """Relation handlers for the service."""
        handlers = super().get_relation_handlers()
        self.sb_svc = StorageBackendProvidesHandler(
            self,
            "storage-backend",
            self.api_ready,
            "storage-backend" in self.mandatory_relations,
        )
        handlers.append(self.sb_svc)
        return handlers

    def api_ready(self, event) -> None:
        """Event handler for bootstrap of service when api services are ready."""
        self._state.api_ready = True
        self.configure_charm(event)

    @property
    def databases(self) -> Mapping[str, str]:
        """Provide database name for cinder services."""
        return {"database": "cinder"}

    def configure_unit(self, event) -> None:
        """Run configuration on this unit."""
        self.check_leader_ready()
        self.check_relation_handlers_ready(event)
        config = self.model.config.get
        self.ensure_snap_present()
        try:
            contexts = self.contexts()
            snap_data = {
                "rabbitmq.url": contexts.amqp.transport_url,
                "database.url": contexts.database.connection,
                "cinder.project-id": contexts.identity_credentials.project_id,
                "cinder.user-id": contexts.identity_credentials.username,
                # do this via multiple relation to configure multiple backends
                "truenas.truenas-1": {
                    "ixsystems-login": config("login"),
                    "ixsystems-password": config("password"),
                    "ixsystems-server-hostname": config("server-hostname"),
                    "ixsystems-transport-type": config("transport-type"),
                    "ixsystems-volume-backend-name": config("volume-backend-name"),
                    "ixsystems-iqn-prefix": config("iqn-prefix"),
                    "ixsystems-datastore-pool": config("datastore-pool"),
                    "ixsystems-dataset-path": config("dataset-path"),
                    "ixsystems-portal-id": config("portal-id"),
                    "ixsystems-initiator-id": config("initiator-id"),
                },
            }
        except AttributeError as e:
            raise sunbeam_guard.WaitingExceptionError(
                "Data missing: {}".format(e.name)
            )

        self.set_snap_data(snap_data)
        # self.ensure_services_running()
        self._state.unit_bootstrapped = True

    def set_snap_data(self, snap_data: dict):
        """Set snap data on local snap."""
        cache = self.get_snap_cache()
        cinder_volume = cache[SNAP]
        new_settings = {}
        old_settings = cinder_volume.get(None, typed=True)
        for key, new_value in snap_data.items():
            group, subkey = key.split(".")
            if (
                old_value := old_settings.get(group, {}).get(subkey)
            ) is not None:
                if old_value != new_value:
                    new_settings[key] = new_value
            # Setting a value to None will unset the value from the snap,
            # which will fail if the value was never set.
            elif new_value is not None:
                new_settings[key] = new_value
        if new_settings:
            logger.debug(f"Applying new snap settings {new_settings}")
            cinder_volume.set(new_settings, typed=True)
        else:
            logger.debug("Snap settings do not need updating")


if __name__ == "__main__":  # pragma: nocover
    ops.main(CinderTruenasOperatorCharm)
