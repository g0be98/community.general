# -*- coding: utf-8 -*-
# Refactored to use XenAPI instead of websocket

from __future__ import annotations

DOCUMENTATION = r"""
name: xen_orchestra
short_description: Xen Orchestra inventory source
version_added: 4.1.0
author:
  - Dom Del Nano (@ddelnano) <ddelnano@gmail.com>
  - Samori Gorse (@shinuza) <samorigorse@gmail.com>
requirements:
  - XenAPI
description:
  - Get inventory hosts from a Xen Orchestra deployment.
  - Uses a configuration file as an inventory source, it must end in C(.xen_orchestra.yml) or C(.xen_orchestra.yaml).
extends_documentation_fragment:
  - constructed
  - inventory_cache
options:
  plugin:
    description: The name of this plugin, it should always be set to V(community.general.xen_orchestra) for this plugin to
      recognize it as its own.
    required: true
    choices: ['community.general.xen_orchestra']
    type: str
  api_host:
    description:
      - API host to XenServer.
      - If the value is not specified in the inventory configuration, the value of environment variable E(ANSIBLE_XO_HOST)
        will be used instead.
    type: str
    env:
      - name: ANSIBLE_XO_HOST
  user:
    description:
      - Xen Orchestra user.
      - If the value is not specified in the inventory configuration, the value of environment variable E(ANSIBLE_XO_USER)
        will be used instead.
    required: true
    type: str
    env:
      - name: ANSIBLE_XO_USER
  password:
    description:
      - Xen Orchestra password.
      - If the value is not specified in the inventory configuration, the value of environment variable E(ANSIBLE_XO_PASSWORD)
        will be used instead.
    required: true
    type: str
    env:
      - name: ANSIBLE_XO_PASSWORD
  validate_certs:
    description: Verify TLS certificate if using HTTPS.
    type: boolean
    default: true
  use_vm_uuid:
    description:
      - Import Xen VMs to inventory using their UUID as the VM entry name.
      - If set to V(false) use VM name labels instead of UUIDs.
    type: boolean
    default: true
    version_added: 10.4.0
  use_host_uuid:
    description:
      - Import Xen Hosts to inventory using their UUID as the Host entry name.
      - If set to V(false) use Host name labels instead of UUIDs.
    type: boolean
    default: true
    version_added: 10.4.0
"""

import XenAPI
import ssl
from ansible.errors import AnsibleError
from ansible.plugins.inventory import BaseInventoryPlugin, Constructable, Cacheable

HALTED = 'Halted'
PAUSED = 'Paused'
RUNNING = 'Running'
SUSPENDED = 'Suspended'
POWER_STATES = [RUNNING, HALTED, SUSPENDED, PAUSED]
HOST_GROUP = 'xo_hosts'
POOL_GROUP = 'xo_pools'


def clean_group_name(label):
    return label.lower().replace(' ', '-').replace('-', '_')


class InventoryModule(BaseInventoryPlugin, Constructable, Cacheable):
    ''' Host inventory parser for ansible using a XenServer and XenApi as source. '''

    NAME = 'community.general.xen_server'

    def __init__(self):
        super(InventoryModule, self).__init__()
        # from config
        self.counter = -1
        self.session = None
        self.cache_key = None
        self.use_cache = None

    @property
    def pointer(self):
        self.counter += 1
        return self.counter

    def create_connection(self, xoa_api_host, user, password):
        try:
            # Determine protocol based on user configuration
            use_ssl = self.get_option('use_ssl')
            validate_certs = self.get_option('validate_certs')
            protocol = "https" if use_ssl else "http"

            # Handle SSL validation
            if use_ssl and not validate_certs:
                # Disable SSL certificate validation globally
                ssl._create_default_https_context = ssl._create_unverified_context

            # Create the XenAPI session
            self.session = XenAPI.Session(f"{protocol}://{xoa_api_host}")

            # Login to the XenServer API
            self.session.xenapi.login_with_password(user, password)
        except XenAPI.Failure as e:
            raise AnsibleError(f"Failed to connect to Xen Orchestra: {e.details}")

    def get_objects(self, object_type):
        try:
            return self.session.xenapi.__getattr__(f"{object_type}.get_all_records")()
        except XenAPI.Failure as e:
            raise AnsibleError(f"Failed to retrieve {object_type} objects: {e.details}")

    def _apply_constructable(self, name, variables):
        strict = self.get_option('strict')
        self._add_host_to_composed_groups(self.get_option('groups'), variables, name, strict=strict)
        self._add_host_to_keyed_groups(self.get_option('keyed_groups'), variables, name, strict=strict)
        self._set_composite_vars(self.get_option('compose'), variables, name, strict=strict)

    def _add_vms(self, vms, hosts, pools):
        for uuid, vm in vms.items():
            entry_name = vm['name_label'] if not self.get_option('use_vm_uuid') else uuid
            group = 'with_ip' if vm.get('networks') else 'without_ip'

            self.inventory.add_host(entry_name)
            self.inventory.add_group(group)
            self.inventory.add_child(group, entry_name)

            self.inventory.set_variable(entry_name, 'uuid', uuid)
            self.inventory.set_variable(entry_name, 'power_state', vm['power_state'].lower())
            self.inventory.set_variable(entry_name, 'name_label', vm['name_label'])
            self.inventory.set_variable(entry_name, 'memory', vm['memory_static_max'])
            self.inventory.set_variable(entry_name, 'cpus', vm['VCPUs_max'])
            self.inventory.set_variable(entry_name, 'tags', vm.get('tags', []))

    def _add_hosts(self, hosts):
        for uuid, host in hosts.items():
            entry_name = host['name_label'] if not self.get_option('use_host_uuid') else uuid
            group_name = f"xo_host_{clean_group_name(host['name_label'])}"

            self.inventory.add_group(group_name)
            self.inventory.add_host(entry_name)
            self.inventory.add_child(HOST_GROUP, entry_name)

            self.inventory.set_variable(entry_name, 'uuid', uuid)
            self.inventory.set_variable(entry_name, 'hostname', host['hostname'])
            self.inventory.set_variable(entry_name, 'memory', host['memory_total'])
            self.inventory.set_variable(entry_name, 'cpus', host['cpu_count'])
            self.inventory.set_variable(entry_name, 'tags', host.get('tags', []))

    def _add_pools(self, pools):
        for uuid, pool in pools.items():
            group_name = f"xo_pool_{clean_group_name(pool['name_label'])}"
            self.inventory.add_group(group_name)

    def _populate(self, objects):
        self.inventory.add_group(HOST_GROUP)
        self.inventory.add_group(POOL_GROUP)
        for group in POWER_STATES:
            self.inventory.add_group(group.lower())

        self._add_pools(objects['pools'])
        self._add_hosts(objects['hosts'])
        self._add_vms(objects['vms'], objects['hosts'], objects['pools'])

    def parse(self, inventory, loader, path, cache=True):
        super(InventoryModule, self).parse(inventory, loader, path)

        self._read_config_data(path)
        self.inventory = inventory
        
        self.cache_key = self.get_cache_key(path)
        self.use_cache = cache and self.get_option('cache')

        xoa_api_host = self.get_option('api_host')
        xoa_user = self.get_option('user')
        xoa_password = self.get_option('password')

        self.create_connection(xoa_api_host, xoa_user, xoa_password)

        objects = {
            'vms': self.get_objects('VM'),
            'hosts': self.get_objects('host'),
            'pools': self.get_objects('pool'),
        }

        self._populate(objects)