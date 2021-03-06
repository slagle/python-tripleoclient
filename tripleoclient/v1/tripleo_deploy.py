#   Copyright 2016 Red Hat, Inc.
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
from __future__ import print_function

import argparse
import logging
import netaddr
import os
import pwd
import re
import shutil
import six
import sys
import tarfile
import tempfile
import yaml

from cliff import command
from datetime import datetime
from heatclient.common import event_utils
from heatclient.common import template_utils
from osc_lib.i18n import _
from six.moves import configparser

from tripleoclient import constants
from tripleoclient import exceptions
from tripleoclient import heat_launcher
from tripleoclient import utils

from tripleo_common.image import kolla_builder
from tripleo_common.utils import passwords as password_utils

# For ansible download
from tripleo_common.inventory import TripleoInventory
from tripleo_common.utils import config

DEPLOY_FAILURE_MESSAGE = """
##########################################################
containerized undercloud deployment failed.

ERROR: Heat log files: {0}

See the previous output for details about what went wrong.

##########################################################
"""
DEPLOY_COMPLETION_MESSAGE = """
########################################################
containerized undercloud deployment complete.

Useful files:

Password file is at {0}
The stackrc file is at {1}

Use these files to interact with OpenStack services, and
ensure they are secured.

########################################################
"""


class Deploy(command.Command):
    """Deploy containerized Undercloud"""

    log = logging.getLogger(__name__ + ".Deploy")
    auth_required = False
    heat_pid = None
    tht_render = None
    output_dir = None
    tmp_ansible_dir = None
    roles_file = None
    roles_data = None
    stack_update_mark = None
    stack_action = 'CREATE'

    def _set_roles_file(self, file_name=None, templates_dir=None):
        """Set the roles file for the deployment

        If the file_name is a full path, it will be used. If the file name
        passed in is not a full path, we will join it with the templates
        dir and use that instead.

        :param file_name: (String) role file name to use, can be relative to
         templates directory
        :param templates_dir:
        """
        if os.path.exists(file_name):
            self.roles_file = file_name
        else:
            self.roles_file = os.path.join(templates_dir, file_name)

    def _get_roles_data(self):
        """Load the roles data for deployment"""
        # only load once
        if self.roles_data:
            return self.roles_data

        if self.roles_file and os.path.exists(self.roles_file):
            with open(self.roles_file) as f:
                self.roles_data = yaml.safe_load(f)
        elif self.roles_file:
            self.log.warning("roles_data '%s' is not found" % self.roles_file)

        return self.roles_data

    def _get_primary_role_name(self):
        """Return the primary role name"""
        roles_data = self._get_roles_data()
        if not roles_data:
            # TODO(aschultz): should this be Undercloud instead?
            return 'Controller'

        for r in roles_data:
            if 'tags' in r and 'primary' in r['tags']:
                return r['name']
        self.log.warning('No primary role found in roles_data, using '
                         'first defined role')
        return roles_data[0]['name']

    def _get_tar_filename(self):
        """Return tarball name for the install artifacts"""
        return '%s/undercloud-install-%s.tar.bzip2' % \
               (self.output_dir,
                datetime.utcnow().strftime('%Y%m%d%H%M%S'))

    def _create_install_artifact(self):
        """Create a tarball of the temporary folders used"""
        self.log.debug(_("Preserving deployment artifacts"))

        def remove_output_dir(info):
            """Tar filter to remove output dir from path"""
            # leading path to tar is home/stack/ rather than /home/stack
            leading_path = self.output_dir[1:] + '/'
            info.name = info.name.replace(leading_path, '')
            return info

        # tar up working data and put in
        # output_dir/undercloud-install-TS.tar.bzip2
        tar_filename = self._get_tar_filename()
        try:
            tf = tarfile.open(tar_filename, 'w:bz2')
            tf.add(self.tht_render, recursive=True, filter=remove_output_dir)
            tf.add(self.tmp_ansible_dir, recursive=True,
                   filter=remove_output_dir)
            tf.close()
        except Exception as ex:
            msg = _("Unable to create artifact tarball, %s") % ex.message
            self.log.error(msg)
            raise exceptions.DeploymentError(msg)
        return tar_filename

    def _create_persistent_dirs(self):
        """Creates temporary working directories"""
        if not os.path.exists(constants.STANDALONE_EPHEMERAL_STACK_VSTATE):
            os.mkdir(constants.STANDALONE_EPHEMERAL_STACK_VSTATE)

    def _create_working_dirs(self):
        """Creates temporary working directories"""
        if self.output_dir and not os.path.exists(self.output_dir):
            os.mkdir(self.output_dir)
        if not self.tht_render:
            self.tht_render = os.path.join(self.output_dir,
                                           'tripleo-heat-installer-templates')
            # Clear dir since we're using a static name and shutils.copytree
            # needs the fodler to not exist. We'll generate the
            # contents each time. This should clear the folder on the first
            # run of this function.
            shutil.rmtree(self.tht_render, ignore_errors=True)
        if not self.tmp_ansible_dir:
            self.tmp_ansible_dir = tempfile.mkdtemp(
                prefix='undercloud-ansible-', dir=self.output_dir)

    def _populate_templates_dir(self, source_templates_dir):
        """Creates template dir with templates

        * Copy --templates content into a working dir
          created as 'output_dir/tripleo-heat-installer-templates'.

        :param source_templates_dir: string to a directory containing our
                                     source templates
        """
        self._create_working_dirs()
        if not os.path.exists(source_templates_dir):
            raise exceptions.NotFound("%s template director does not exists" %
                                      source_templates_dir)
        if not os.path.exists(self.tht_render):
            shutil.copytree(source_templates_dir, self.tht_render,
                            symlinks=True)

    def _cleanup_working_dirs(self, cleanup=False):
        """Cleanup temporary working directories

        :param cleanup: Set to true if you DO want to cleanup the dirs
        """
        if cleanup:
            if self.tht_render and os.path.exists(self.tht_render):
                shutil.rmtree(self.tht_render, ignore_errors=True)

            self.tht_render = None
            if self.tmp_ansible_dir and os.path.exists(self.tmp_ansible_dir):
                shutil.rmtree(self.tmp_ansible_dir)
                self.tmp_ansible_dir = None
        else:
            self.log.warning(_("Not cleaning working directory %s")
                             % self.tht_render)
            self.log.warning(_("Not cleaning ansible directory %s")
                             % self.tmp_ansible_dir)

    def _configure_puppet(self):
        self.log.info(_('Configuring puppet modules symlinks ...'))
        utils.bulk_symlink(self.log, constants.TRIPLEO_PUPPET_MODULES,
                           constants.PUPPET_MODULES,
                           constants.PUPPET_BASE)

    def _update_passwords_env(self, output_dir, passwords=None):
        pw_file = os.path.join(output_dir, 'tripleo-undercloud-passwords.yaml')
        undercloud_pw_file = os.path.join(output_dir,
                                          'undercloud-passwords.conf')
        stack_env = {'parameter_defaults': {}}

        # Getting passwords that were managed by instack-undercloud so
        # we can upgrade to a containerized undercloud and keep old passwords.
        legacy_env = {}
        if os.path.exists(undercloud_pw_file):
            config = configparser.ConfigParser()
            config.read(undercloud_pw_file)
            for k, v in config.items('auth'):
                # Manage exceptions
                if k == 'undercloud_db_password':
                    k = 'MysqlRootPassword'
                elif k == 'undercloud_rabbit_username':
                    k = 'RpcUserName'
                elif k == 'undercloud_rabbit_password':
                    try:
                        # NOTE(aschultz): Only save rabbit password to rpc
                        # if it's not already defined for the upgrade case.
                        # The passwords are usually different so we don't
                        # want to overwrite it if it already exists because
                        # we'll end up rewriting the passwords later and
                        # causing problems.
                        config.get('auth', 'undercloud_rpc_password')
                    except Exception:
                        legacy_env['RpcPassword'] = v
                    k = 'RabbitPassword'
                elif k == 'undercloud_rabbit_cookie':
                    k = 'RabbitCookie'
                elif k == 'undercloud_heat_encryption_key':
                    k = 'HeatAuthEncryptionKey'
                elif k == 'undercloud_libvirt_tls_password':
                    k = 'LibvirtTLSPassword'
                elif k == 'undercloud_ha_proxy_stats_password':
                    k = 'HAProxyStatsPassword'
                else:
                    k = ''.join(i.capitalize() for i in k.split('_')[1:])
                legacy_env[k] = v

        if os.path.exists(pw_file):
            with open(pw_file) as pf:
                stack_env = yaml.safe_load(pf.read())

        pw = password_utils.generate_passwords(stack_env=stack_env)
        stack_env['parameter_defaults'].update(pw)
        # Override what has been generated by tripleo-common with old passwords
        # if any.
        stack_env['parameter_defaults'].update(legacy_env)

        if passwords:
            # These passwords are the DefaultPasswords so we only
            # update if they don't already exist in stack_env
            for p, v in passwords.items():
                if p not in stack_env['parameter_defaults']:
                    stack_env['parameter_defaults'][p] = v

        # Write out the password file in yaml for heat.
        # This contains sensitive data so ensure it's not world-readable
        with open(pw_file, 'w') as pf:
            yaml.safe_dump(stack_env, pf, default_flow_style=False)
        # Using chmod here instead of permissions on the open above so we don't
        # have to fight with umask.
        os.chmod(pw_file, 0o600)
        # Write out an instack undercloud compatible version.
        # This contains sensitive data so ensure it's not world-readable
        with open(undercloud_pw_file, 'w') as pf:
            pf.write('[auth]\n')
            for p, v in stack_env['parameter_defaults'].items():
                if 'Password' in p or 'Token' in p or p.endswith('Kek'):
                    # Convert camelcase from heat templates into the underscore
                    # format used by instack undercloud.
                    s1 = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', p)
                    pw_key = re.sub('([a-z0-9])([A-Z])',
                                    r'\1_\2', s1).lower()
                    pf.write('undercloud_%s: %s\n' % (pw_key, v))
        os.chmod(undercloud_pw_file, 0o600)

        return pw_file

    def _generate_hosts_parameters(self, parsed_args, p_ip):
        hostname = utils.get_short_hostname()
        domain = parsed_args.local_domain

        data = {
            'CloudName': p_ip,
            'CloudDomain': domain,
            'CloudNameInternal': '%s.internalapi.%s' % (hostname, domain),
            'CloudNameStorage': '%s.storage.%s' % (hostname, domain),
            'CloudNameStorageManagement': ('%s.storagemgmt.%s'
                                           % (hostname, domain)),
            'CloudNameCtlplane': '%s.ctlplane.%s' % (hostname, domain),
        }
        return data

    def _generate_portmap_parameters(self, ip_addr, ip_nw, ctlplane_vip_addr,
                                     public_vip_addr, stack_name='Undercloud',
                                     role_name='Undercloud'):
        hostname = utils.get_short_hostname()

        # in order for deployed server network information to match correctly,
        # we need to ensure the HostnameMap matches our hostname
        hostname_map_name = "%s-%s-0" % (stack_name.lower(), role_name.lower())
        data = {
            'ControlPlaneSubnetCidr': '%s' % ip_nw.prefixlen,
            'HostnameMap': {
                hostname_map_name: '%s' % hostname
            },
            # The settings below allow us to inject a custom public
            # VIP. This requires use of the generated
            # ../network/ports/external_from_pool.yaml resource in t-h-t.
            'IPPool': {
                'external': [public_vip_addr]
            },
            'ExternalNetCidr': '%s/%s' % (public_vip_addr, ip_nw.prefixlen),
            # This requires use of the
            # ../deployed-server/deployed-neutron-port.yaml resource in t-h-t
            # We use this for the control plane VIP and also via
            # the environments/deployed-server-noop-ctlplane.yaml
            # for the server IP itself
            'DeployedServerPortMap': {
                ('%s-ctlplane' % hostname): {
                    'fixed_ips': [{'ip_address': ip_addr}],
                    'subnets': [{'cidr': str(ip_nw.cidr)}]
                },
                'control_virtual_ip': {
                    'fixed_ips': [{'ip_address': ctlplane_vip_addr}],
                    'subnets': [{'cidr': str(ip_nw.cidr)}]
                },
                'public_virtual_ip': {
                    'fixed_ips': [{'ip_address': public_vip_addr}],
                    'subnets': [{'cidr': str(ip_nw.cidr)}]
                }
            }
        }
        return data

    def _kill_heat(self, parsed_args):
        """Tear down heat installer and temp files

        Kill the heat launcher/installer process.
        Teardown temp files created in the deployment process,
        when cleanup is requested.

        """
        if self.heat_pid:
            self.heat_launch.kill_heat(self.heat_pid)
            pid, ret = os.waitpid(self.heat_pid, 0)
            self.heat_pid = None

    def _launch_heat(self, parsed_args):
        # we do this as root to chown config files properly for docker, etc.
        if parsed_args.heat_native:
            self.heat_launch = heat_launcher.HeatNativeLauncher(
                parsed_args.heat_api_port,
                parsed_args.heat_container_image,
                parsed_args.heat_user)
        else:
            self.heat_launch = heat_launcher.HeatDockerLauncher(
                parsed_args.heat_api_port,
                parsed_args.heat_container_image,
                parsed_args.heat_user)

        # NOTE(dprince): we launch heat with fork exec because
        # we don't want it to inherit our args. Launching heat
        # as a "library" would be cool... but that would require
        # more refactoring. It runs a single process and we kill
        # it always below.
        self.heat_pid = os.fork()
        if self.heat_pid == 0:
            if parsed_args.heat_native:
                try:
                    uid = pwd.getpwnam(parsed_args.heat_user).pw_uid
                    gid = pwd.getpwnam(parsed_args.heat_user).pw_gid
                except KeyError:
                    msg = _(
                        "Please create a %s user account before "
                        "proceeding.") % parsed_args.heat_user
                    self.log.error(msg)
                    raise exceptions.DeploymentError(msg)
                os.setgid(gid)
                os.setuid(uid)
            self.heat_launch.heat_db_sync()
            # Exec() never returns.
            self.heat_launch.launch_heat()

        # NOTE(dprince): we use our own client here because we set
        # auth_required=False above because keystone isn't running when this
        # command starts
        tripleoclients = self.app.client_manager.tripleoclient
        orchestration_client = \
            tripleoclients.local_orchestration(parsed_args.heat_api_port)

        return orchestration_client

    def _normalize_user_templates(self, user_tht_root, tht_root, env_files=[]):
        """copy environment files into tht render path

        This assumes any env file that includes user_tht_root has already
        been copied into tht_root.

        :param user_tht_root: string path to the user's template dir
        :param tht_root: string path to our deployed tht_root
        :param env_files: list of paths to environment files
        :return list of absolute pathed environment files that exist in
                tht_root
        """
        environments = []
        # normalize the user template path to ensure it doesn't have a trailing
        # slash
        user_tht = os.path.abspath(user_tht_root)
        for env_path in env_files:
            self.log.debug("Processing file %s" % env_path)
            abs_env_path = os.path.abspath(env_path)
            if (abs_env_path.startswith(user_tht_root) and
                    ((user_tht + '/') in env_path or
                     (user_tht + '/') in abs_env_path or
                     user_tht == abs_env_path or
                     user_tht == env_path)):
                # file is in tht and will be copied, so just update path
                new_env_path = env_path.replace(user_tht + '/',
                                                tht_root + '/')
                self.log.debug("Redirecting %s to %s"
                               % (abs_env_path, new_env_path))
                environments.append(new_env_path)
            elif abs_env_path.startswith(tht_root):
                self.log.debug("File already in tht_root %s")
                environments.append(abs_env_path)
            else:
                self.log.debug("File outside of tht_root %s, copying in")
                # file is outside of THT, just copy it in
                # TODO(aschultz): probably shouldn't be flattened?
                target_dest = os.path.join(tht_root,
                                           os.path.basename(abs_env_path))
                if os.path.exists(target_dest):
                    raise exceptions.DeploymentError("%s already exists, "
                                                     "please rename the "
                                                     "file to something else"
                                                     % target_dest)
                shutil.copy(abs_env_path, tht_root)
                environments.append(target_dest)
        return environments

    def _setup_heat_environments(self, parsed_args):
        """Process tripleo heat templates with jinja and deploy into work dir

        * Process j2/install additional templates there
        * Return the environments list for futher processing as a new base.

        The first two items are reserved for the
        overcloud-resource-registry-puppet.yaml and passwords files.
        """

        self.log.warning(_("** Handling template files **"))
        env_files = []

        # TODO(aschultz): in overcloud deploy we have a --environments-dir
        # we might want to handle something similar for this
        # (shardy) alternatively perhaps we should rely on the plan-environment
        # environments list instead?
        if parsed_args.environment_files:
            env_files.extend(parsed_args.environment_files)

        # ensure any user provided templates get copied into tht_render
        user_environments = self._normalize_user_templates(
            parsed_args.templates, self.tht_render, env_files)

        # generate jinja templates by its work dir location
        self.log.debug(_("Using roles file %s") % self.roles_file)
        process_templates = os.path.join(parsed_args.templates,
                                         'tools/process-templates.py')
        args = ['python', process_templates, '--roles-data',
                self.roles_file, '--output-dir', self.tht_render]
        if utils.run_command_and_log(self.log, args, cwd=self.tht_render) != 0:
            # TODO(aschultz): improve error messaging
            msg = _("Problems generating templates.")
            self.log.error(msg)
            raise exceptions.DeploymentError(msg)

        # NOTE(aschultz): the next set of environment files are system included
        # so we have to include them at the front of our environment list so a
        # user can override anything in them.

        # Include any environments from the plan-environment.yaml
        plan_env_path = utils.rel_or_abs_path(
            self.tht_render, parsed_args.plan_environment_file)
        with open(plan_env_path, 'r') as f:
            plan_env_data = yaml.safe_load(f)
        environments = [utils.rel_or_abs_path(self.tht_render, e.get('path'))
                        for e in plan_env_data.get('environments', {})]

        # this will allow the user to overwrite passwords with custom envs
        pw_file = self._update_passwords_env(self.output_dir)
        environments.append(pw_file)

        # use deployed-server because we run os-collect-config locally
        deployed_server_env = os.path.join(
            self.tht_render, 'environments',
            'config-download-environment.yaml')
        environments.append(deployed_server_env)

        # use deployed-server because we run os-collect-config locally
        deployed_server_env = os.path.join(
            self.tht_render, 'environments',
            'deployed-server-noop-ctlplane.yaml')
        environments.append(deployed_server_env)

        self.log.info(_("Deploying templates in the directory {0}").format(
            os.path.abspath(self.tht_render)))

        maps_file = os.path.join(self.tht_render,
                                 'tripleoclient-hosts-portmaps.yaml')
        ip_nw = netaddr.IPNetwork(parsed_args.local_ip)
        ip = str(ip_nw.ip)

        if parsed_args.control_virtual_ip:
            c_ip = parsed_args.control_virtual_ip
        else:
            c_ip = ip

        if parsed_args.public_virtual_ip:
            p_ip = parsed_args.public_virtual_ip
        else:
            p_ip = ip

        tmp_env = self._generate_hosts_parameters(parsed_args, p_ip)
        tmp_env.update(self._generate_portmap_parameters(
            ip, ip_nw, c_ip, p_ip,
            stack_name=parsed_args.stack,
            role_name=self._get_primary_role_name()))

        with open(maps_file, 'w') as env_file:
            yaml.safe_dump({'parameter_defaults': tmp_env}, env_file,
                           default_flow_style=False)
        environments.append(maps_file)

        # NOTE(aschultz): this doesn't get copied into tht_root but
        # we always include the hieradata override stuff last.
        if parsed_args.hieradata_override:
            environments.append(self._process_hieradata_overrides(
                parsed_args.hieradata_override,
                parsed_args.standalone_role))

        # Create a persistent drop-in file to indicate the stack
        # virtual state changes
        stack_vstate_dropin = os.path.join(self.tht_render,
                                           '%s-stack-vstate-dropin.yaml' %
                                           parsed_args.stack)
        with open(stack_vstate_dropin, 'w') as dropin_file:
            yaml.safe_dump(
                {'parameter_defaults': {'StackAction': self.stack_action}},
                dropin_file, default_flow_style=False)
        environments.append(stack_vstate_dropin)

        return environments + user_environments

    def _prepare_container_images(self, env):
        roles_data = self._get_roles_data()
        image_params = kolla_builder.container_images_prepare_multi(
            env, roles_data, dry_run=True)

        # use setdefault to ensure every needed image parameter is
        # populated without replacing user-set values
        if image_params:
            pd = env.get('parameter_defaults', {})
            for k, v in image_params.items():
                pd.setdefault(k, v)

    def _deploy_tripleo_heat_templates(self, orchestration_client,
                                       parsed_args):
        """Deploy the fixed templates in TripleO Heat Templates"""

        # sets self.tht_render to the working dir with deployed templates
        environments = self._setup_heat_environments(parsed_args)

        # rewrite paths to consume t-h-t env files from the working dir
        self.log.debug(_("Processing environment files %s") % environments)
        env_files, env = utils.process_multiple_environments(
            environments, self.tht_render, parsed_args.templates,
            cleanup=parsed_args.cleanup)

        self._prepare_container_images(env)

        self.log.debug(_("Getting template contents"))
        template_path = os.path.join(self.tht_render, 'overcloud.yaml')
        template_files, template = \
            template_utils.get_template_contents(template_path)

        files = dict(list(template_files.items()) + list(env_files.items()))

        stack_name = parsed_args.stack

        self.log.debug(_("Deploying stack: %s") % stack_name)
        self.log.debug(_("Deploying template: %s") % template)
        self.log.debug(_("Deploying environment: %s") % env)
        self.log.debug(_("Deploying files: %s") % files)

        stack_args = {
            'stack_name': stack_name,
            'template': template,
            'environment': env,
            'files': files,
        }

        if parsed_args.timeout:
            stack_args['timeout_mins'] = parsed_args.timeout

        self.log.warning(_("** Performing Heat stack create.. **"))
        stack = orchestration_client.stacks.create(**stack_args)
        stack_id = stack['stack']['id']

        return "%s/%s" % (stack_name, stack_id)

    def _download_ansible_playbooks(self, client, stack_name,
                                    tripleo_role_name='Standalone'):
        stack_config = config.Config(client)
        self._create_working_dirs()

        self.log.warning(_('** Downloading {0} ansible.. **').format(
            stack_name))
        # python output buffering is making this seem to take forever..
        sys.stdout.flush()
        stack_config.write_config(stack_config.fetch_config(stack_name),
                                  stack_name,
                                  self.tmp_ansible_dir)

        inventory = TripleoInventory(
            hclient=client,
            plan_name=stack_name,
            ansible_ssh_user='root')

        inv_path = os.path.join(self.tmp_ansible_dir, 'inventory.yaml')
        extra_vars = {tripleo_role_name: {'ansible_connection': 'local'}}
        inventory.write_static_inventory(inv_path, extra_vars)

        self.log.info(_('** Downloaded {0} ansible to {1} **').format(
                      stack_name, self.tmp_ansible_dir))
        sys.stdout.flush()
        return self.tmp_ansible_dir

    # Never returns, calls exec()
    def _launch_ansible_deploy(self, ansible_dir):
        self.log.warning(_('** Running ansible deploy tasks **'))
        os.chdir(ansible_dir)
        playbook_inventory = os.path.join(ansible_dir, 'inventory.yaml')
        cmd = ['ansible-playbook', '-i', playbook_inventory,
               'deploy_steps_playbook.yaml']
        self.log.debug('Running Ansible Deploy tasks: %s' % (' '.join(cmd)))
        return utils.run_command_and_log(self.log, cmd)

    def _launch_ansible_upgrade(self, ansible_dir):
        self.log.warning('** Running ansible upgrade tasks **')
        os.chdir(ansible_dir)
        playbook_inventory = os.path.join(ansible_dir, 'inventory.yaml')
        cmd = ['ansible-playbook', '-i', playbook_inventory,
               'upgrade_steps_playbook.yaml', '--skip-tags', 'validation']
        self.log.debug('Running Ansible Upgrade tasks: %s' % (' '.join(cmd)))
        return utils.run_command_and_log(self.log, cmd)

    def get_parser(self, prog_name):
        parser = argparse.ArgumentParser(
            description=self.get_description(),
            prog=prog_name,
            add_help=False
        )
        parser.add_argument(
            '--templates', nargs='?', const=constants.TRIPLEO_HEAT_TEMPLATES,
            help=_("The directory containing the Heat templates to deploy"),
        )
        parser.add_argument('--standalone', default=False, action='store_true',
                            help=_("Run deployment as a standalone deployment "
                                   "with no undercloud."))
        parser.add_argument('--upgrade', default=False, action='store_true',
                            help=_("Upgrade an existing deployment."))
        parser.add_argument('-y', '--yes', default=False, action='store_true',
                            help=_("Skip yes/no prompt (assume yes)."))
        parser.add_argument('--stack',
                            help=_("Name for the ephemeral (one-time create "
                                   "and forget) heat stack."),
                            default='standalone')
        parser.add_argument('--force-stack-update',
                            dest='force_stack_update',
                            action='store_true',
                            default=False,
                            help=_("Do a virtual update of the ephemeral "
                                   "heat stack (it cannot take real updates). "
                                   "New or failed deployments "
                                   "always have the stack_action=CREATE. This "
                                   "option enforces stack_action=UPDATE."),
                            )
        parser.add_argument('--output-dir',
                            dest='output_dir',
                            help=_("Directory to output state, processed heat "
                                   "templates, ansible deployment files."),
                            default=constants.UNDERCLOUD_OUTPUT_DIR)
        parser.add_argument('--output-only',
                            dest='output_only',
                            action='store_true',
                            default=False,
                            help=_("Do not execute the Ansible playbooks. By"
                                   " default the playbooks are saved to the"
                                   " output-dir and then executed.")),
        parser.add_argument('--standalone-role', default='Standalone',
                            help=_("The role to use for standalone "
                                   "configuration when populating the "
                                   "deployment actions."))
        parser.add_argument('-t', '--timeout', metavar='<TIMEOUT>',
                            type=int, default=30,
                            help=_('Deployment timeout in minutes.'))
        parser.add_argument(
            '-e', '--environment-file', metavar='<HEAT ENVIRONMENT FILE>',
            action='append', dest='environment_files',
            help=_('Environment files to be passed to the heat stack-create '
                   'or heat stack-update command. (Can be specified more than '
                   'once.)')
        )
        parser.add_argument(
            '--roles-file', '-r', dest='roles_file',
            help=_('Roles file, overrides the default %s in the --templates '
                   'directory') % constants.UNDERCLOUD_ROLES_FILE,
            default=constants.UNDERCLOUD_ROLES_FILE
        )
        parser.add_argument(
            '--plan-environment-file', '-p',
            help=_('Plan Environment file, overrides the default %s in the '
                   '--templates directory') % constants.PLAN_ENVIRONMENT,
            default=constants.PLAN_ENVIRONMENT
        )
        parser.add_argument(
            '--heat-api-port', metavar='<HEAT_API_PORT>',
            dest='heat_api_port',
            default='8006',
            help=_('Heat API port to use for the installers private'
                   ' Heat API instance. Optional. Default: 8006.)')
        )
        parser.add_argument(
            '--heat-user', metavar='<HEAT_USER>',
            dest='heat_user',
            default='heat',
            help=_('User to execute the non-priveleged heat-all process. '
                   'Defaults to heat.')
        )
        parser.add_argument(
            '--heat-container-image', metavar='<HEAT_CONTAINER_IMAGE>',
            dest='heat_container_image',
            default='tripleomaster/centos-binary-heat-all',
            help=_('The container image to use when launching the heat-all '
                   'process. Defaults to: '
                   'tripleomaster/centos-binary-heat-all')
        )
        parser.add_argument(
            '--heat-native',
            action='store_true',
            default=True,
            help=_('Execute the heat-all process natively on this host. '
                   'This option requires that the heat-all binaries '
                   'be installed locally on this machine. '
                   'This option is enabled by default which means heat-all is '
                   'executed on the host OS directly.')
        )
        parser.add_argument(
            '--local-ip', metavar='<LOCAL_IP>',
            dest='local_ip',
            help=_('Local IP/CIDR for undercloud traffic. Required.')
        )
        parser.add_argument(
            '--control-virtual-ip', metavar='<CONTROL_VIRTUAL_IP>',
            dest='control_virtual_ip',
            help=_('Control plane VIP. This allows the undercloud installer '
                   'to configure a custom VIP on the control plane.')
        )
        parser.add_argument(
            '--public-virtual-ip', metavar='<PUBLIC_VIRTUAL_IP>',
            dest='public_virtual_ip',
            help=_('Public nw VIP. This allows the undercloud installer '
                   'to configure a custom VIP on the public (external) NW.')
        )
        parser.add_argument(
            '--local-domain', metavar='<LOCAL_DOMAIN>',
            dest='local_domain',
            default='undercloud',
            help=_('Local domain for undercloud and its API endpoints')
        )
        parser.add_argument(
            '--cleanup',
            action='store_true', default=False,
            help=_('Cleanup temporary files. Using this flag will '
                   'remove the temporary files used during deployment in '
                   'after the command is run.'),

        )
        parser.add_argument(
            '--hieradata-override', nargs='?',
            help=_('Path to hieradata override file. When it points to a heat '
                   'env file, it is passed in t-h-t via --environment-file. '
                   'When the file contains legacy instack data, '
                   'it is wrapped with <role>ExtraConfig and also '
                   'passed in for t-h-t as a temp file created in '
                   '--output-dir. Note, instack hiera data may be '
                   'not t-h-t compatible and will highly likely require a '
                   'manual revision.')
        )
        return parser

    def _process_hieradata_overrides(self, override_file=None,
                                     tripleo_role_name='Standalone'):
        """Count in hiera data overrides including legacy formats

        Return a file name that points to processed hiera data overrides file
        """
        if not override_file or not os.path.exists(override_file):
            # we should never get here because there's a check in
            # undercloud_conf but stranger things have happened.
            msg = (_('hieradata_override file could not be found %s') %
                   override_file)
            self.log.error(msg)
            raise exceptions.DeploymentError(msg)

        target = override_file
        data = open(target, 'r').read()
        hiera_data = yaml.safe_load(data)
        if not hiera_data:
            msg = (_('Unsupported data format in hieradata override %s') %
                   target)
            self.log.error(msg)
            raise exceptions.DeploymentError(msg)
        self._create_working_dirs()

        # NOTE(bogdando): In t-h-t, hiera data should come in wrapped as
        # {parameter_defaults: {UndercloudExtraConfig: ... }}
        extra_config_var = '%sExtraConfig' % tripleo_role_name
        if (extra_config_var not in hiera_data.get('parameter_defaults', {})):
            hiera_override_file = os.path.join(
                self.tht_render, 'tripleo-hieradata-override.yaml')
            self.log.info('Converting hiera overrides for t-h-t from '
                          'legacy format into a file %s' %
                          hiera_override_file)
            with open(hiera_override_file, 'w') as override:
                yaml.safe_dump(
                    {'parameter_defaults': {
                     extra_config_var: hiera_data}},
                    override,
                    default_flow_style=False)
            target = hiera_override_file
        return target

    def _standalone_deploy(self, parsed_args):
        if not parsed_args.local_ip:
            msg = _('Please set --local-ip to the correct '
                    'ipaddress/cidr for this machine.')
            self.log.error(msg)
            raise exceptions.DeploymentError(msg)

        if not os.environ.get('HEAT_API_PORT'):
            os.environ['HEAT_API_PORT'] = parsed_args.heat_api_port

        # The main thread runs as root and we drop privs for forked
        # processes below. Only the heat deploy/os-collect-config forked
        # process runs as root.
        if os.geteuid() != 0:
            msg = _("Please run as root.")
            self.log.error(msg)
            raise exceptions.DeploymentError(msg)

        # prepare working spaces
        self.output_dir = os.path.abspath(parsed_args.output_dir)
        self._create_working_dirs()
        # The state that needs to be persisted between serial deployments
        # and cannot be contained in ephemeral heat stacks or working dirs
        self._create_persistent_dirs()

        # configure puppet
        self._configure_puppet()

        # copy the templates dir in place
        self._populate_templates_dir(parsed_args.templates)

        # configure our roles data
        self._set_roles_file(parsed_args.roles_file, self.tht_render)
        self._get_roles_data()

        rc = 1
        try:
            # NOTE(bogdando): Look for the unique virtual update mark matching
            # the heat stack name we are going to create below. If found the
            # mark, consider the stack action is UPDATE instead of CREATE.
            mark_uuid = '_'.join(['update_mark', parsed_args.stack])
            self.stack_update_mark = os.path.join(
                constants.STANDALONE_EPHEMERAL_STACK_VSTATE,
                mark_uuid)

            # Prepare the heat stack action we want to start deployment with
            if (os.path.isfile(self.stack_update_mark) or
               parsed_args.force_stack_update):
                self.stack_action = 'UPDATE'

            self.log.warning(
                _('The heat stack {0} action is {1}').format(
                    parsed_args.stack, self.stack_action))

            # Launch heat.
            orchestration_client = self._launch_heat(parsed_args)
            # Wait for heat to be ready.
            utils.wait_api_port_ready(parsed_args.heat_api_port)
            # Deploy TripleO Heat templates.
            stack_id = \
                self._deploy_tripleo_heat_templates(orchestration_client,
                                                    parsed_args)

            # Wait for complete..
            status, msg = event_utils.poll_for_events(
                orchestration_client, stack_id, nested_depth=6)
            if status != "CREATE_COMPLETE":
                message = _("Stack create failed; %s") % msg
                self.log.error(message)
                raise exceptions.DeploymentError(message)

            # download the ansible playbooks and execute them.
            ansible_dir = \
                self._download_ansible_playbooks(orchestration_client,
                                                 parsed_args.stack,
                                                 parsed_args.standalone_role)
            # Kill heat, we're done with it now.
            self._kill_heat(parsed_args)
            if not parsed_args.output_only:
                # Run Upgrade tasks before the deployment
                if parsed_args.upgrade:
                    rc = self._launch_ansible_upgrade(ansible_dir)
                    if rc != 0:
                        raise exceptions.DeploymentError('Upgrade failed')
                rc = self._launch_ansible_deploy(ansible_dir)
        except Exception as e:
            self.log.error("Exception: %s" % six.text_type(e))
            raise exceptions.DeploymentError(six.text_type(e))
        finally:
            self._kill_heat(parsed_args)
            tar_filename = self._create_install_artifact()
            self._cleanup_working_dirs(cleanup=parsed_args.cleanup)
            if tar_filename:
                self.log.warning('Install artifact is located at %s' %
                                 tar_filename)
            if not parsed_args.output_only and rc != 0:
                # We only get here on error.
                # Alter the stack virtual state for failed deployments
                if (self.stack_update_mark and
                   not parsed_args.force_stack_update and
                   os.path.isfile(self.stack_update_mark)):
                    self.log.warning(
                        _('The heat stack %s virtual state/action is '
                          'is reset to CREATE. Use "--force-stack-update" to '
                          ' set it forcefully to UPDATE') % parsed_args.stack)
                    self.log.warning(
                        _('Removing the stack virtual update mark file %s') %
                        self.stack_update_mark)
                    os.remove(self.stack_update_mark)

                self.log.error(DEPLOY_FAILURE_MESSAGE.format(
                    self.heat_launch.install_tmp
                    ))
                raise exceptions.DeploymentError('Deployment failed.')
            else:
                # We only get here if no errors
                self.log.warning(DEPLOY_COMPLETION_MESSAGE.format(
                    '~/undercloud-passwords.conf',
                    '~/stackrc'
                    ))

                if (self.stack_update_mark and
                   (not parsed_args.output_only or
                       parsed_args.force_stack_update)):
                    # Persist the unique mark file for this stack
                    # Do not update its atime file system attribute to keep its
                    # genuine timestamp for the 1st time the stack state had
                    # been (virtually) changed to match stack_action UPDATE
                    self.log.warning(
                        _('Writing the stack virtual update mark file %s') %
                        self.stack_update_mark)
                    open(self.stack_update_mark, 'wa').close()
                elif parsed_args.output_only:
                    self.log.warning(
                        _('Not creating the stack %s virtual update mark file '
                          'in the --output-only mode! Re-run with '
                          '--force-stack-update, if you want to enforce it.') %
                        parsed_args.stack)
                else:
                    self.log.warning(
                        _('Not creating the stack %s virtual update mark '
                          'file') % parsed_args.stack)

            return rc

    def take_action(self, parsed_args):
        self.log.debug("take_action(%s)" % parsed_args)

        try:
            if parsed_args.upgrade and (
                    not parsed_args.yes and sys.stdin.isatty()):
                prompt_response = six.moves.input(
                    ('It is strongly recommended to perform a backup '
                     'before the upgrade. Are you sure you want to '
                     'upgrade [y/N]?')
                ).lower()
                if not prompt_response.startswith('y'):
                    self.log.info('User did not confirm upgrade so '
                                  'taking no action.')
                    return
        except KeyboardInterrupt:  # ctrl-c
            self.log.info('User did not confirm upgrade '
                          '(ctrl-c) so taking no action.')
            return
        except EOFError:  # ctrl-d
            self.log.info('User did not confirm upgrade '
                          '(ctrl-d) so taking no action.')
            return

        if parsed_args.standalone:
            if self._standalone_deploy(parsed_args) != 0:
                msg = _('Deployment failed.')
                self.log.error(msg)
                raise exceptions.DeploymentError(msg)
        else:
            msg = _('Non-standalone is currently not supported')
            self.log.error(msg)
            raise exceptions.DeploymentError(msg)
