#   Copyright 2015 Red Hat, Inc.
#
#   Licensed under the Apache License, Version 2.0 (the "License"); you may
#   not use this file except in compliance with the License. You may obtain
#   a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#   WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#   License for the specific language governing permissions and limitations
#   under the License.
#
from oslo_config import cfg
from oslo_log import log as logging

from osc_lib.i18n import _
from osc_lib import utils

from tripleoclient import command
from tripleoclient import constants
from tripleoclient import utils as oooutils
from tripleoclient.v1.overcloud_deploy import DeployOvercloud
from tripleoclient.workflows import package_update

CONF = cfg.CONF
logging.register_options(CONF)
logging.setup(CONF, '')


class UpdatePrepare(DeployOvercloud):
    """Run heat stack update for overcloud nodes to refresh heat stack outputs.

       The heat stack outputs are what we use later on to generate ansible
       playbooks which deliver the minor update workflow. This is used as the
       first step for a minor update of your overcloud.
    """

    log = logging.getLogger(__name__ + ".MinorUpdatePrepare")

    def get_parser(self, prog_name):
        parser = super(UpdatePrepare, self).get_parser(prog_name)
        parser.add_argument('--container-registry-file',
                            dest='container_registry_file',
                            default=None,
                            help=_("File which contains the container "
                                   "registry data for the update"),
                            )
        parser.add_argument('--ceph-ansible-playbook',
                            action="store",
                            default="/usr/share/ceph-ansible"
                                    "/site-docker.yml.sample",
                            help=_('Path to switch the ceph-ansible playbook '
                                   'used for update. '),
                            )
        return parser

    def take_action(self, parsed_args):
        self.log.debug("take_action(%s)" % parsed_args)
        clients = self.app.client_manager

        stack = oooutils.get_stack(clients.orchestration,
                                   parsed_args.stack)

        stack_name = stack.stack_name
        registry = oooutils.load_container_registry(
            self.log, parsed_args.container_registry_file)

        # Run update
        ceph_ansible_playbook = parsed_args.ceph_ansible_playbook
        # Run Overcloud deploy (stack update)
        # In case of update and upgrade we need to force the
        # update_plan_only. The heat stack update is done by the
        # packag_update mistral action
        parsed_args.update_plan_only = True

        # Add the update-prepare.yaml environment to set noops etc
        templates_dir = (parsed_args.templates or
                         constants.TRIPLEO_HEAT_TEMPLATES)
        parsed_args.environment_files = oooutils.prepend_environment(
            parsed_args.environment_files, templates_dir,
            constants.UPDATE_PREPARE_ENV)

        super(UpdatePrepare, self).take_action(parsed_args)
        package_update.update(clients, container=stack_name,
                              container_registry=registry,
                              ceph_ansible_playbook=ceph_ansible_playbook)
        package_update.get_config(clients, container=stack_name)
        self.log.info("Update init on stack {0} complete.".format(
                      parsed_args.stack))


class UpdateRun(command.Command):
    """Run minor update ansible playbooks on Overcloud nodes"""

    log = logging.getLogger(__name__ + ".MinorUpdateRun")

    def get_parser(self, prog_name):
        parser = super(UpdateRun, self).get_parser(prog_name)
        nodes_or_roles = parser.add_mutually_exclusive_group(required=True)
        nodes_or_roles.add_argument(
            '--nodes', action="store", help=_(
                "A string that identifies a single node "
                "or comma-separated list of nodes to be "
                "updated in parallel in this minor update "
                "run invocation. For example: --nodes "
                "\"compute-0, compute-1, compute-5\". "
                "NOTE: Using this parameter with nodes of "
                "controlplane roles (e.g. \"--nodes "
                "controller-1\") is NOT supported and WILL "
                "end badly unless you include ALL nodes of "
                "that role as a comma separated string. You "
                "should instead use the --roles parameter "
                "for controlplane roles and specify the "
                "role name.")
        )
        nodes_or_roles.add_argument(
            '--roles', action="store", help=_(
                "A string that identifies the role or "
                "comma-separated list of roles to be "
                "updated in this minor update run "
                "invocation. "
                "NOTE: Nodes of specified role(s) are "
                "updated in parallel. This is REQUIRED for "
                "controlplane roles (e.g., \"Compute\"), "
                "you may consider instead using the --nodes "
                "argument to limit the upgrade to a "
                "specific node or list (comma separated "
                "string) of nodes.")
        )
        parser.add_argument('--playbook',
                            action="store",
                            default="all",
                            help=_("Ansible playbook to use for the minor "
                                   "update. Defaults to the special value "
                                   "\'all\' which causes all the update "
                                   "playbooks to be executed. That is the "
                                   "update_steps_playbook.yaml and then the"
                                   "deploy_steps_playbook.yaml. "
                                   "Set this to each of those playbooks in "
                                   "consecutive invocations of this command "
                                   "if you prefer to run them manually. Note: "
                                   "make sure to run both those playbooks so "
                                   "that all services are updated and running "
                                   "with the target version configuration.")
                            )
        parser.add_argument("--ssh-user",
                            dest="ssh_user",
                            action="store",
                            default="heat-admin",
                            help=_("The ssh user name for connecting to "
                                   "the overcloud nodes.")
                            )
        parser.add_argument('--static-inventory',
                            dest='static_inventory',
                            action="store",
                            default=None,
                            help=_('Path to an existing ansible inventory to '
                                   'use. If not specified, one will be '
                                   'generated in '
                                   '~/tripleo-ansible-inventory.yaml')
                            )
        parser.add_argument('--stack', dest='stack',
                            help=_('Name or ID of heat stack '
                                   '(default=Env: OVERCLOUD_STACK_NAME)'),
                            default=utils.env('OVERCLOUD_STACK_NAME',
                                              default='overcloud')
                            )

        return parser

    def take_action(self, parsed_args):
        self.log.debug("take_action(%s)" % parsed_args)
        clients = self.app.client_manager
        stack = parsed_args.stack

        # Run ansible:
        roles = parsed_args.roles
        nodes = parsed_args.nodes
        limit_hosts = roles or nodes
        playbook = parsed_args.playbook
        inventory = oooutils.get_tripleo_ansible_inventory(
            parsed_args.static_inventory, parsed_args.ssh_user, stack)
        oooutils.run_update_ansible_action(self.log, clients, limit_hosts,
                                           inventory, playbook,
                                           constants.UPDATE_QUEUE,
                                           constants.MINOR_UPDATE_PLAYBOOKS,
                                           package_update,
                                           parsed_args.ssh_user)


class UpdateConverge(DeployOvercloud):
    """Converge the update on Overcloud nodes.

    This restores the plan and stack so that normal deployment
    workflow is back in place.
    """

    log = logging.getLogger(__name__ + ".UpdateConverge")

    def take_action(self, parsed_args):
        self.log.debug("take_action(%s)" % parsed_args)

        # Add the update-converge.yaml environment to unset noops
        templates_dir = (parsed_args.templates or
                         constants.TRIPLEO_HEAT_TEMPLATES)
        parsed_args.environment_files = oooutils.prepend_environment(
            parsed_args.environment_files, templates_dir,
            constants.UPDATE_CONVERGE_ENV)

        super(UpdateConverge, self).take_action(parsed_args)
        self.log.info("Update converge on stack {0} complete.".format(
                      parsed_args.stack))
