# Copyright (c) 2024 Canonical Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging

from zaza import model
from zaza.openstack.charm_tests import test_utils
from zaza.openstack.utilities import openstack as openstack_utils


logger = logging.getLogger(__name__)


def wait_for_application_state(
    model: model, app: str, status: str, message_regex: str
):
    """Block until all units of app reach desired state.

    Blocks until all units of the application have:
    - unit status matches status
    - unit status message matches the message_regex
    - unit agent is idle
    """
    for unit in model.get_units(app):
        model.block_until_unit_wl_status(unit.name, status)
        model.block_until_unit_wl_message_match(unit.name, message_regex)
        model.wait_for_unit_idle(unit.name)


class TempestK8sTest(test_utils.BaseCharmTest):
    """Charm tests for tempest-k8s."""

    @classmethod
    def setUpClass(cls):
        """Run class setup for running tests."""
        super(TempestK8sTest, cls).setUpClass(application_name="tempest")

        # Connect to the OpenStack cloud
        keystone_session = openstack_utils.get_overcloud_keystone_session()
        cls.keystone_client = openstack_utils.get_keystone_session_client(keystone_session)
        cls.glance_client = openstack_utils.get_glance_session_client(keystone_session)
        cls.neutron_client = openstack_utils.get_neutron_session_client(keystone_session)

    def get_tempest_init_resources(self, domain_id):
        """Get the test accounts and associated resources generated by tempest.

        Return a dict of resources containing users, projects, and networks created
        at tempest init stage.
        """
        test_accounts_resources = dict()

        test_accounts_resources["projects"] = [
            project.name
            for project in self.keystone_client.projects.list(domain=domain_id)
            if project.name.startswith('tempest-')
        ]
        test_accounts_resources["users"] = [
            user.name
            for user in self.keystone_client.users.list(domain=domain_id)
            if user.name.startswith('tempest-')
        ]
        test_accounts_resources["networks"] = [
            network["name"]
            for network in self.neutron_client.list_networks()['networks']
            if network["name"].startswith('tempest-')
        ]

        return test_accounts_resources

    def get_domain_id(self):
        """Get tempest domain id."""
        return openstack_utils.get_domain_id(
            self.keystone_client,
            domain_name="CloudValidation-b82746a08d"
        )

    def check_charm_created_resources(self, domain_id):
        """Check charm created resources exists."""
        self.assertTrue(domain_id)

        projects = [
            project.name
            for project in self.keystone_client.projects.list(domain=domain_id)
            if project.name == "CloudValidation-test-project"
        ]
        users = [
            user.name
            for user in self.keystone_client.users.list(domain=domain_id)
            if user.name == "CloudValidation-test-user"
        ]

        self.assertTrue(projects)
        self.assertTrue(users)

    def test_get_lists(self):
        """Verify that the get-lists action returns list names as expected."""
        action = model.run_action_on_leader(self.application_name, "get-lists")
        lists = action.data["results"]["stdout"].splitlines()
        self.assertIn("readonly-quick", lists)
        self.assertIn("refstack-2022.11", lists)

    def test_bounce_keystone_relation_with_extensive_cleanup(self):
        """Test removing and re-adding the keystone relation.

        Extensive cleanup should be triggered upon keystone relation break to
        remove all resources created by tempest init. Charm-created
        resources gets removed and re-created upon keystone relation rejoin.
        """
        # Verify the existance of charm-created resources
        domain_id = self.get_domain_id()
        self.check_charm_created_resources(domain_id)

        # Verify the existance of resources created by tempest init
        # when keystone relation is joined
        test_accounts_resources = self.get_tempest_init_resources(domain_id)
        self.assertTrue(test_accounts_resources["projects"])
        self.assertTrue(test_accounts_resources["users"])
        self.assertTrue(test_accounts_resources["networks"])

        # Verify that the application is blocked when keystone is missing
        model.remove_relation("tempest", "identity-ops", "keystone")
        wait_for_application_state(
            model,
            "tempest",
            "blocked",
            r"^\(identity-ops\) integration missing$",
        )
        wait_for_application_state(
            model,
            "keystone",
            "active",
            r"^$",
        )

        # Verify that charm-created resources remain in the cloud
        self.assertEqual(domain_id, self.get_domain_id())
        self.check_charm_created_resources(domain_id)

        # Verify that there are no more resources created by tempest init
        # when keystone relation is removed
        test_accounts_resources = self.get_tempest_init_resources(domain_id)
        self.assertFalse(test_accounts_resources["projects"])
        self.assertFalse(test_accounts_resources["users"])
        self.assertFalse(test_accounts_resources["networks"])

        # And then verify that adding it back
        # results in reaching active/idle state again.
        # ie. successful tempest init again.
        model.add_relation("tempest", "identity-ops", "keystone")
        wait_for_application_state(model, "tempest", "active", r"^$")
        wait_for_application_state(
            model,
            "keystone",
            "active",
            r"^$",
        )

        # Verify that a new domain (with project and user) is created which
        # replaces the old one
        new_domain_id = self.get_domain_id()
        self.assertNotEqual(new_domain_id, domain_id)
        self.check_charm_created_resources(new_domain_id)

        # Verify that tempest init re-created projects, users and networks when
        # keystone relation is re-joined
        test_accounts_resources = self.get_tempest_init_resources(new_domain_id)
        self.assertTrue(test_accounts_resources["projects"])
        self.assertTrue(test_accounts_resources["users"])
        self.assertTrue(test_accounts_resources["networks"])

    def test_quick_cleanup_in_between_tests(self):
        """Verify that quick cleanup in between tests are behaving correctly.

        Test-created resources should be removed. Resources generated by charm and
        tempest init remain in the cloud.
        """
        # Get the list of images before test is run. Note that until an upstream
        # fix [1] lands and releases, we cannot use `tempest-` prefix as the filter.
        # Instead, we compare the list of all images before and after a test run.
        # [1]: https://review.opendev.org/c/openstack/tempest/+/908358
        before_images = [i.name for i in self.glance_client.images.list()]

        # Get the resources (domain, projects, users, and networks) generated by
        # the charm and tempest init
        domain_id = self.get_domain_id()
        self.check_charm_created_resources(domain_id)
        before_test_accounts_resources = self.get_tempest_init_resources(domain_id)

        # Run a test that will create an image in the cloud
        action = model.run_action_on_leader(
            self.application_name, "validate",
            action_params={
                "regex": "test_image_web_download_import_with_bad_url",
            }
        )
        logger.info("action.data = %s", action.data)
        summary = action.data["results"]["summary"]

        # Verify that the test is successfully ran and passed.
        # Successul test run means the image resource has been created.
        self.assertIn("Ran: 1 tests", summary)
        self.assertIn("Passed: 1", summary)

        # Verify that the image createby test is removed
        after_images = [i.name for i in self.glance_client.images.list()]
        self.assertEqual(after_images, before_images)

        # Verify that the resources created by charm and tempest init
        # (domain, projects, users, and networks) remain intact.
        self.assertEqual(domain_id, self.get_domain_id())
        self.check_charm_created_resources(domain_id)
        after_test_accounts_resources = self.get_tempest_init_resources(domain_id)
        self.assertEqual(after_test_accounts_resources, before_test_accounts_resources)

    def test_validate_with_readonly_quick_tests(self):
        """Verify that the validate action runs tests as expected."""
        action = model.run_action_on_leader(
            self.application_name, "validate",
            action_params={
                "test-list": "readonly-quick",
            }
        )
        # log the data so we can debug failures
        logger.info("action.data = %s", action.data)
        summary = action.data["results"]["summary"]

        # No tests should fail, and all the summary items should be present and populated
        self.assertRegex(summary, "Ran: [1-9]\d* test") # at least one test should run
        self.assertRegex(summary, "Passed: \d+")
        self.assertRegex(summary, "Skipped: \d+")
        self.assertIn("Expected Fail: 0", summary)
        self.assertIn("Unexpected Success: 0", summary)
        self.assertIn("Failed: 0", summary)
