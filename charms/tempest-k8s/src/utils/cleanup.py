# Copyright 2024 Canonical Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Utils for cleaning up tempest-related resources."""
import argparse
import os
from collections.abc import (
    Callable,
)

import yaml
from keystoneauth1.exceptions.catalog import (
    EndpointNotFound,
)
from keystoneauth1.exceptions.http import (
    Unauthorized,
)
from openstack.connection import (
    Connection,
)
from openstack.exceptions import (
    ForbiddenException,
)

RESOURCE_PREFIX = "tempest-"


class CleanUpError(Exception):
    """Exception raised when clean-up process terminated unsuccessfully."""


def _connect_to_os(env: dict) -> Connection:
    """Establish connection to the OpenStack cloud."""
    return Connection(
        auth_url=env["OS_AUTH_URL"],
        project_name=env["OS_PROJECT_NAME"],
        username=env["OS_USERNAME"],
        password=env["OS_PASSWORD"],
        user_domain_id=env["OS_USER_DOMAIN_ID"],
        project_domain_id=env["OS_USER_DOMAIN_ID"],
        cacert=env["OS_CACERT"],
    )


def _get_exclusion_resources(account_file: str) -> dict[str, set[str]]:
    """Get users and projects to be excluded from clean-up.

    The resources are looked up in an account_file which is generated by
    `tempest account-generator`.

    Note that this lookup will only work in workload container as it needs
    access to the generated test accounts file.
    """
    try:
        with open(account_file, "r") as file:
            parsed_file = yaml.safe_load(file)

        return {
            "projects": {account["project_name"] for account in parsed_file},
            "users": {account["username"] for account in parsed_file},
        }
    except FileNotFoundError as e:
        raise CleanUpError("Test account file doesn't exist.") from e
    except KeyError as e:
        raise CleanUpError("Malformated test account file.") from e


def _get_test_projects_in_domain(
    conn: Connection, domain_id: str, exclude_projects: set[str] = set()
) -> list[str]:
    """Get test projects in domain to do cleanup for.

    Projects in exclude_projects will not be included in the returned list.
    """
    return [
        project.id
        for project in conn.identity.projects(domain_id=domain_id)
        if project.name.startswith(RESOURCE_PREFIX)
        and project.name not in exclude_projects
    ]


def _cleanup_compute_resources(conn: Connection, project_id: str) -> None:
    """Delete compute resources with names starting with prefix in the specified project.

    The compute resources to be removed are instances and keypairs.
    """
    # Delete instances
    for server in conn.compute.servers(project_id=project_id):
        if server.name.startswith(RESOURCE_PREFIX):
            conn.compute.delete_server(server.id)

    # Delete keypairs
    for keypair in conn.compute.keypairs():
        if keypair.name.startswith(RESOURCE_PREFIX):
            conn.compute.delete_keypair(keypair)


def _cleanup_block_resources(conn: Connection, project_id: str) -> None:
    """Delete block storage resources with names starting with prefix in the specified project.

    The block storage resources to be removed are snapshots and instances.
    """
    # Delete snapshots
    for snapshot in conn.block_store.snapshots(
        details=True, project_id=project_id
    ):
        if snapshot.name.startswith(RESOURCE_PREFIX):
            conn.block_store.delete_snapshot(snapshot.id)

    # Delete volumes
    for volume in conn.block_store.volumes(
        details=True, project_id=project_id
    ):
        if volume.name.startswith(RESOURCE_PREFIX):
            conn.block_store.delete_volume(volume.id)


def _cleanup_images(conn: Connection, project_id: str) -> None:
    """Delete images with names starting with prefix and owned by the specified project."""
    for image in conn.image.images():
        # TODO: to be extra careful, we should also check the prefix of the image
        # However, some tempest tests are not creating images with the prefix, so
        # we should wait until https://review.opendev.org/c/openstack/tempest/+/908358
        # is released.
        if image.owner == project_id:
            conn.image.delete_image(image.id)


def _cleanup_networks_resources(conn: Connection, project_id: str) -> None:
    """Delete network resources with names starting with prefix in the specified project.

    The network resources to be removed are ports, routers, and networks.
    """
    # Delete ports and routers
    for router in conn.network.routers(project_id=project_id):
        if router.name.startswith(RESOURCE_PREFIX):
            # Ports attached via the external gateway info
            # cannot be removed/deleted via the ports api,
            # so external_gateway_info must be unset before removing other ports.
            if router.external_gateway_info:
                conn.network.update_router(router, external_gateway_info=None)
            for port in conn.network.ports(device_id=router.id):
                conn.network.remove_interface_from_router(
                    router, port_id=port.id
                )
            conn.network.delete_router(router.id)

    # Delete networks
    for network in conn.network.networks(project_id=project_id):
        if network.name.startswith(RESOURCE_PREFIX):
            conn.network.delete_network(network.id)


def _cleanup_stacks(conn: Connection, project_id: str) -> None:
    """Delete stacks with names starting with prefix and owned by the specified project.

    If Heat service is not found in the cloud, this clean-up will be skipped.
    """
    try:
        for stack in conn.orchestration.stacks(project_id=project_id):
            if stack.name.startswith(RESOURCE_PREFIX):
                conn.orchestration.delete_stack(stack.id)
    except EndpointNotFound:
        # do nothing if the heat endpoint is not found
        pass


def _cleanup_users(
    conn: Connection, domain_id: str, exclude_users: set[str] = set()
) -> None:
    """Delete users with names starting with prefix in the specified domain.

    If exclude_users is specified, users in exclude_users will not be removed.
    """
    for user in conn.identity.users(domain_id=domain_id):
        if (
            user.name.startswith(RESOURCE_PREFIX)
            and user.name not in exclude_users
        ):
            conn.identity.delete_user(user.id)


def _cleanup_project(conn: Connection, project_id: str) -> None:
    """Delete a project given its id."""
    conn.identity.delete_project(project_id)


def _run_cleanup_functions(
    conn: Connection, projects: list[str], functions: list[Callable]
) -> list[str]:
    """Run clean-up function on a list of projects."""
    failure_message = []
    for project_id in projects:
        for func in functions:
            try:
                func(conn, project_id)
            except Exception as e:
                failure_message.append(f"Error calling {func.__name__}: {e}")
    return failure_message


def run_quick_cleanup(env: dict) -> None:
    """Perform the quick cleanup of tempest resources under a specific domain.

    This clean-up removes compute instances, keypairs, volumes, snapshots,
    images, networks, projects, users, and stacks that are not associated with
    the pre-generated test accounts and projects defined in account_file.

    Note that this clean up will only work in workload container as it needs
    access to the generated test accounts file.
    """
    conn = _connect_to_os(env)
    cleanup_funcs = [
        _cleanup_compute_resources,
        _cleanup_block_resources,
        _cleanup_images,
        _cleanup_stacks,
    ]
    filtered_cleanup_funcs = [
        _cleanup_networks_resources,
        _cleanup_project,
    ]
    exclude_resources = _get_exclusion_resources(env["TEMPEST_TEST_ACCOUNTS"])
    failure_message = []

    try:
        # get all projects with prefix in domain
        test_projects = _get_test_projects_in_domain(conn, env["OS_DOMAIN_ID"])

        # get projects that are not found in account file
        filtered_test_projects = _get_test_projects_in_domain(
            conn, env["OS_DOMAIN_ID"], exclude_resources["projects"]
        )
    except (ForbiddenException, Unauthorized) as e:
        raise CleanUpError("Operation not authorized.") from e

    failure_message.extend(
        _run_cleanup_functions(conn, test_projects, cleanup_funcs)
    )
    failure_message.extend(
        _run_cleanup_functions(
            conn, filtered_test_projects, filtered_cleanup_funcs
        )
    )

    try:
        _cleanup_users(conn, env["OS_DOMAIN_ID"], exclude_resources["users"])
    except Exception as e:
        failure_message.append(f"\nError cleaning up users: {e}")

    if failure_message:
        raise CleanUpError("\n".join(failure_message))


def run_extensive_cleanup(env: dict) -> None:
    """Perform the extensive cleanup of tempest resources under a specific domain.

    This clean-up removes compute instances, keypairs, volumes, snapshots,
    images, and stacks, as well as generated test accounts, projects, and the
    network resources associated with them.
    """
    conn = _connect_to_os(env)
    cleanup_funcs = [
        _cleanup_compute_resources,
        _cleanup_block_resources,
        _cleanup_images,
        _cleanup_stacks,
        _cleanup_networks_resources,
        _cleanup_project,
    ]
    failure_message = []

    try:
        # get all projects with prefix in domain
        projects = _get_test_projects_in_domain(conn, env["OS_DOMAIN_ID"])
    except (ForbiddenException, Unauthorized) as e:
        raise CleanUpError("Operation not authorized.") from e

    failure_message.extend(
        _run_cleanup_functions(conn, projects, cleanup_funcs)
    )

    try:
        _cleanup_users(conn, env["OS_DOMAIN_ID"])
    except Exception as e:
        failure_message.append(f"\nError cleaning up users: {e}")

    if failure_message:
        raise CleanUpError("\n".join(failure_message))


def parse_command_line() -> argparse.Namespace:
    """Parse command line interface."""
    parser = argparse.ArgumentParser(
        description="Clean up OpenStack resources created by tempest or discover-tempest-conf."
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser(
        "quick",
        description="Run a quick cleanup, suitable in between tempest test runs.",
        help="run a quick cleanup, suitable in between tempest test runs",
    )
    subparsers.add_parser(
        "extensive",
        description="Run an extensive cleanup, suitable for a clean environment before creating initial tempest resources.",
        help="run an extensive cleanup, suitable for a clean environment before creating initial tempest resources",
    )
    return parser.parse_args()


def main() -> None:
    """Entrypoint for executing the script directly.

    This will be used in periodic test runs.
    Quick cleanup will be performed.
    """
    env = {
        "OS_CACERT": os.getenv("OS_CACERT", ""),
        "OS_AUTH_URL": os.getenv("OS_AUTH_URL", ""),
        "OS_USERNAME": os.getenv("OS_USERNAME", ""),
        "OS_PASSWORD": os.getenv("OS_PASSWORD", ""),
        "OS_PROJECT_NAME": os.getenv("OS_PROJECT_NAME", ""),
        "OS_DOMAIN_ID": os.getenv("OS_DOMAIN_ID", ""),
        "OS_USER_DOMAIN_ID": os.getenv("OS_USER_DOMAIN_ID", ""),
        "OS_PROJECT_DOMAIN_ID": os.getenv("OS_PROJECT_DOMAIN_ID", ""),
        "TEMPEST_TEST_ACCOUNTS": os.getenv("TEMPEST_TEST_ACCOUNTS", ""),
    }

    args = parse_command_line()

    if args.command == "quick":
        run_quick_cleanup(env)
    else:
        run_extensive_cleanup(env)


if __name__ == "__main__":
    main()
