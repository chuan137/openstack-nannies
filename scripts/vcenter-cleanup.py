#!/usr/bin/env python
#
# Copyright (c) 2018 SAP SE
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
#

import atexit
import click
import logging
import re
import os
import six
import ssl
import time

from pyVim.connect import SmartConnect, Disconnect
from pyVim.task import WaitForTask, WaitForTasks
from pyVmomi import vim, vmodl
from openstack import connection, exceptions
# prometheus export functionality
from prometheus_client import start_http_server, Gauge

uuid_re = re.compile('[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', re.IGNORECASE)

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)-15s %(message)s')

vms_to_be_suspended = dict()
vms_to_be_poweredoff = dict()
vms_to_be_unregistered = dict()
vms_seen = dict()
files_to_be_deleted = dict()
files_to_be_renamed = dict()
files_seen = dict()

tasks = []

state_to_name_map = dict()

gauge_value_empty_vvol_folders = 0
gauge_value_vcenter_connection_problems = 0
gauge_value_vcenter_get_properties_problems = 0

gauge_value = dict()
gauge_suspend_vm = Gauge('vcenter_nanny_suspend_vm', 'vm suspends of the vcenter nanny', ['kind'])
gauge_power_off_vm = Gauge('vcenter_nanny_power_off_vm', 'vm power offs of the vcenter nanny', ['kind'])
gauge_unregister_vm = Gauge('vcenter_nanny_unregister_vm', 'vm unregisters of the vcenter nanny', ['kind'])
gauge_rename_ds_path = Gauge('vcenter_nanny_rename_ds_path', 'ds path renames of the vcenter nanny', ['kind'])
gauge_delete_ds_path = Gauge('vcenter_nanny_delete_ds_path', 'ds path deletes of the vcenter nanny', ['kind'])
gauge_ghost_volumes = Gauge('vcenter_nanny_ghost_volumes', 'number of possible ghost volumes')
gauge_eph_shadow_vms = Gauge('vcenter_nanny_eph_shadow_vms', 'number of possible shadow vms on eph storage')
gauge_datastore_no_access = Gauge('vcenter_nanny_datastore_no_access', 'number of non accessible datastores')
gauge_empty_vvol_folders = Gauge('vcenter_nanny_empty_vvol_folders', 'number of empty vvols')
gauge_vcenter_connection_problems = Gauge('vcenter_nanny_vcenter_connection_problems', 'number of connection problems to the vcenter')
gauge_vcenter_get_properties_problems = Gauge('vcenter_nanny_get_properties_problems', 'number of get properties problems from the vcenter')
gauge_openstack_connection_problems = Gauge('vcenter_nanny_openstack_connection_problems', 'number of connection problems to openstack')
gauge_unknown_vcenter_templates = Gauge('vcenter_nanny_unknown_vcenter_templates', 'number of templates unknown to openstack')
gauge_complete_orphans = Gauge('vcenter_nanny_complete_orphans', 'number of possibly completely orphan vms')

# find vmx and vmdk files with a uuid name pattern
def _uuids(task):
    global gauge_value_empty_vvol_folders
    for searchresult in task.info.result:
        folder_path = searchresult.folderPath
        # no files in the folder
        if not searchresult.file:
            log.warn("- PLEASE CHECK MANUALLY - empty folder: %s", folder_path)
            gauge_value_empty_vvol_folders += 1
        else:
            # its ugly to do it in two loops, but an easy way to make sure to have the vms before the vmdks in the list
            for f in searchresult.file:
                if f.path.lower().endswith(".vmx") or f.path.lower().endswith(".vmx.renamed_by_vcenter_nanny"):
                    match = uuid_re.search(f.path)
                    if match:
                        yield match.group(0), {'folderpath': folder_path, 'filepath': f.path}
            for f in searchresult.file:
                if f.path.lower().endswith(".vmdk") or f.path.lower().endswith(".vmdk.renamed_by_vcenter_nanny"):
                    match = uuid_re.search(f.path)
                    if match:
                        yield match.group(0), {'folderpath': folder_path, 'filepath': f.path}


# cmdline handling
@click.command()
# vcenter host, user and password
@click.option('--host', help='Host to connect to.')
@click.option('--username', prompt='Your name')
@click.option('--password', prompt='The password')
# every how many minutes the check should be preformed
@click.option('--interval', prompt='Interval in minutes')
# how often a vm should be continously a candidate for some action (delete etc.) before
# we actually do it - the idea behind is that we want to avoid actions due to short
# temporary technical problems of any kind ... another idea is to do the actions step
# by step (i.e. suspend - iterations - power-off - iterations - unlink - iterations -
# delete file path) for vms or rename folder (eph storage) or files (vvol storage), so
# that we have a chance to still roll back in case we notice problems due to some wrong
# action done
@click.option('--iterations', prompt='Iterations')
# dry run mode - only say what we would do without actually doing it
@click.option('--dry-run', is_flag=True)
# do not power off vms
@click.option('--power-off', is_flag=True)
# do not unregister vms
@click.option('--unregister', is_flag=True)
# do not delete datastore files or folders
@click.option('--delete', is_flag=True)
# port to use for prometheus exporter, otherwise we use 9456 as default
@click.option('--port')
def run_me(host, username, password, interval, iterations, dry_run, power_off, unregister, delete, port):

    global gauge_value_vcenter_connection_problems

    # Start http server for exported data
    if port:
        prometheus_exporter_port = port
    else:
        prometheus_exporter_port = 9456
    try:
        start_http_server(prometheus_exporter_port)
    except Exception as e:
        logging.error("failed to start prometheus exporter http server: " + str(e))

    while True:

        gauge_value_vcenter_connection_problems = 0

        # vcenter connection
        if hasattr(ssl, '_create_unverified_context'):
            context = ssl._create_unverified_context()

            try:
                service_instance = SmartConnect(host=host,
                                            user=username,
                                            pwd=password,
                                            port=443,
                                            sslContext=context)
            except Exception as e:
                log.warn("- PLEASE CHECK MANUALLY - problems connecting to vcenter: %s - retrying in next loop run",
                    str(e))
                gauge_value_vcenter_connection_problems += 1

            else:
                atexit.register(Disconnect, service_instance)

                content = service_instance.content
                dc = content.rootFolder.childEntity[0]

                # iterate through all vms and get the config.hardware.device properties (and some other)
                # get vm containerview
                # TODO: destroy the view again
                view_ref = content.viewManager.CreateContainerView(
                    container=content.rootFolder,
                    type=[vim.VirtualMachine],
                    recursive=True
                )

                # define the state to verbal name mapping
                state_to_name_map["suspend_vm"] = "suspend of former os server"
                state_to_name_map["power_off_vm"] = "power off of former os server"
                state_to_name_map["unregister_vm"] = "unregister of former os server"
                state_to_name_map["rename_ds_path"] = "rename of ds path"
                state_to_name_map["delete_ds_path"] = "delete of ds path"

                # do the cleanup work
                cleanup_items(host, username, password, iterations, dry_run, power_off, unregister, delete,
                              service_instance,
                              content, dc, view_ref)

                # disconnect from vcenter
                Disconnect(service_instance)

        else:
            raise Exception("maybe too old python version with ssl problems?")

        # wait the interval time
        time.sleep(60 * int(interval))

# init dict of all vms or files we have seen already
def init_seen_dict(seen_dict):
    for i in seen_dict:
        seen_dict[i] = 0


# reset dict of all vms or files we plan to do something with (delete etc.)
def reset_to_be_dict(to_be_dict, seen_dict):
    for i in seen_dict:
        # if a machine we planned to delete no longer appears as canditate for delettion, remove it from the list
        if seen_dict[i] == 0:
            to_be_dict[i] = 0


# here we decide to wait longer before doings something (delete etc.) or finally doing it
# id here is the corresponding old openstack uuid of vm (for vms) or the file-/dirname on the
# datastore (for files and folders on the datastore)
def now_or_later(id, to_be_dict, seen_dict, what_to_do, iterations, dry_run, power_off, unregister, delete, vm, dc,
                 content, detail):
    default = 0
    seen_dict[id] = 1
    if to_be_dict.get(id, default) <= int(iterations):
        if to_be_dict.get(id, default) == int(iterations):
            if dry_run:
                log.info("- dry-run: %s %s", what_to_do, id)
                log.info("           [ %s ]", detail)
                gauge_value[('dry_run', what_to_do)] += 1
            else:
                if what_to_do == "suspend_vm":
                    log.info("- action: %s %s", state_to_name_map[what_to_do], id)
                    log.info("          [ %s ]", detail)
                    tasks.append(vm.SuspendVM_Task())
                    gauge_value[('done', what_to_do)] += 1
                elif what_to_do == "power_off_vm":
                    if power_off:
                        log.info("- action: %s %s", state_to_name_map[what_to_do], id)
                        log.info("          [ %s ]", detail)
                        tasks.append(vm.PowerOffVM_Task())
                        gauge_value[('done', what_to_do)] += 1
                elif what_to_do == "unregister_vm":
                    if unregister:
                        log.info("- action: %s %s", state_to_name_map[what_to_do], id)
                        log.info("          [ %s ]", detail)
                        vm.UnregisterVM()
                        gauge_value[('done', what_to_do)] += 1
                elif what_to_do == "rename_ds_path":
                    log.info("- action: %s %s", state_to_name_map[what_to_do], id)
                    log.info("          [ %s ]", detail)
                    newname = id.rstrip('/') + ".renamed_by_vcenter_nanny"
                    tasks.append(content.fileManager.MoveDatastoreFile_Task(sourceName=id, sourceDatacenter=dc,
                                                                            destinationName=newname,
                                                                            destinationDatacenter=dc))
                    gauge_value[('done', what_to_do)] += 1
                elif what_to_do == "delete_ds_path":
                    if delete:
                        log.info("- action: %s %s", state_to_name_map[what_to_do], id)
                        log.info("          [ %s ]", detail)
                        tasks.append(content.fileManager.DeleteDatastoreFile_Task(name=id, datacenter=dc))
                        gauge_value[('done', what_to_do)] += 1
                else:
                    log.warn("- PLEASE CHECK MANUALLY - unsupported action requested for id: %s", id)
        else:
            log.info("- plan: %s %s", state_to_name_map[what_to_do], id)
            log.info("        [ %s ] (%i/%i)", detail, to_be_dict.get(id, default) + 1, int(iterations))
            gauge_value[('plan', what_to_do)] += 1
        to_be_dict[id] = to_be_dict.get(id, default) + 1


# Shamelessly borrowed from:
# https://github.com/dnaeon/py-vconnector/blob/master/src/vconnector/core.py
def collect_properties(service_instance, view_ref, obj_type, path_set=None,
                       include_mors=False):
    """
    Collect properties for managed objects from a view ref
    Check the vSphere API documentation for example on retrieving
    object properties:
        - http://goo.gl/erbFDz
    Args:
        si          (ServiceInstance): ServiceInstance connection
        view_ref (vim.view.*): Starting point of inventory navigation
        obj_type      (vim.*): Type of managed object
        path_set               (list): List of properties to retrieve
        include_mors           (bool): If True include the managed objects
                                       refs in the result
    Returns:
        A list of properties for the managed objects
    """

    global gauge_value_vcenter_get_properties_problems

    collector = service_instance.content.propertyCollector

    # Create object specification to define the starting point of
    # inventory navigation
    obj_spec = vmodl.query.PropertyCollector.ObjectSpec()
    obj_spec.obj = view_ref
    obj_spec.skip = True

    # Create a traversal specification to identify the path for collection
    traversal_spec = vmodl.query.PropertyCollector.TraversalSpec()
    traversal_spec.name = 'traverseEntities'
    traversal_spec.path = 'view'
    traversal_spec.skip = False
    traversal_spec.type = view_ref.__class__
    obj_spec.selectSet = [traversal_spec]

    # Identify the properties to the retrieved
    property_spec = vmodl.query.PropertyCollector.PropertySpec()
    property_spec.type = obj_type

    if not path_set:
        property_spec.all = True

    property_spec.pathSet = path_set

    # Add the object and property specification to the
    # property filter specification
    filter_spec = vmodl.query.PropertyCollector.FilterSpec()
    filter_spec.objectSet = [obj_spec]
    filter_spec.propSet = [property_spec]

    # initialize data hete, so that we can check for an empty data later in case of an exception while getting the properties
    data = []
    # Retrieve properties
    try:
        props = collector.RetrieveContents([filter_spec])
    except vmodl.fault.ManagedObjectNotFound as e:
        log.warn("- PLEASE CHECK MANUALLY - problems retrieving properties from vcenter: %s - retrying in next loop run",
                 str(e))
        gauge_value_vcenter_get_properties_problems += 1
        # wait a moment before retrying
        time.sleep(600)
        return data

    for obj in props:
        properties = {}
        for prop in obj.propSet:
            properties[prop.name] = prop.val

        if include_mors:
            properties['obj'] = obj.obj

        data.append(properties)
    return data


# main cleanup function
def cleanup_items(host, username, password, iterations, dry_run, power_off, unregister, delete, service_instance,
                  content, dc, view_ref):
    # openstack connection
    conn = connection.Connection(auth_url=os.getenv('OS_AUTH_URL'),
                                 project_name=os.getenv('OS_PROJECT_NAME'),
                                 project_domain_name=os.getenv('OS_PROJECT_DOMAIN_NAME'),
                                 username=os.getenv('OS_USERNAME'),
                                 user_domain_name=os.getenv('OS_USER_DOMAIN_NAME'),
                                 password=os.getenv('OS_PASSWORD'))

    known = dict()
    template = dict()

    global gauge_value_empty_vvol_folders
    global gauge_value_vcenter_connection_problems
    global gauge_value_vcenter_get_properties_problems

    # reset all gauge counters
    for kind in [ "plan", "dry_run", "done"]:
        for what in state_to_name_map:
            gauge_value[(kind, what)] = 0
    gauge_value_ghost_volumes = 0
    gauge_value_eph_shadow_vms = 0
    gauge_value_datastore_no_access = 0
    gauge_value_empty_vvol_folders = 0
    gauge_value_vcenter_get_properties_problems = 0
    gauge_value_openstack_connection_problems = 0
    gauge_value_unknown_vcenter_templates = 0
    gauge_value_complete_orphans = 0


    # get all servers, volumes, snapshots and images from openstack to compare the resources we find on the vcenter against
    try:
        service = "nova"
        for server in conn.compute.servers(details=False, all_tenants=1):
            known[server.id] = server
        service = "cinder"
        for volume in conn.block_store.volumes(details=False, all_tenants=1):
            known[volume.id] = volume
        service = "cinder"
        for snapshot in conn.block_store.snapshots(details=False, all_tenants=1):
            known[snapshot.id] = snapshot
        service = "glance"
        for image in conn.image.images(details=False, all_tenants=1):
            known[image.id] = image
    except exceptions.HttpException as e:
        log.warn(
            "- PLEASE CHECK MANUALLY - problems retrieving information from openstack %s: %s - retrying in next loop run",
            service, str(e))
        gauge_value_openstack_connection_problems += 1
        # wait a moment before retrying
        time.sleep(600)
        return
    except exceptions.SDKException as e:
        log.warn(
            "- PLEASE CHECK MANUALLY - problems retrieving information from openstack %s: %s - retrying in next loop run",
            service, str(e))
        gauge_value_openstack_connection_problems += 1
        # wait a moment before retrying
        time.sleep(600)
        return

    # the properties we want to collect - some of them are not yet used, but will at a later
    # development stage of this script to validate the volume attachments with cinder and nova
    vm_properties = [
        "config.hardware.device",
        "config.name",
        "config.uuid",
        "config.instanceUuid",
        "config.template"
    ]

    # collect the properties for all vms
    data = collect_properties(service_instance, view_ref, vim.VirtualMachine,
                              vm_properties, True)
    # in case we have problems getting the properties from the vcenter, start over from the beginning
    if data is None:
        return

    # create a dict of volumes mounted to vms to compare the volumes we plan to delete against
    # to find possible ghost volumes
    vcenter_mounted = dict()
    # iterate over the list of vms
    for k in data:
        if k.get('config.instanceUuid'):
            if k.get('config.template'):
                template[k['config.instanceUuid']] = k['config.template']
            log.debug("uuid: %s - template: %s", str(k['config.instanceUuid']), str(k['config.template']))
        # get the config.hardware.device property out of the data dict and iterate over its elements
        # for j in k['config.hardware.device']:
        # this check seems to be required as in one bb i got a key error otherwise - looks like a vm without that property
        if k.get('config.hardware.device'):
            for j in k.get('config.hardware.device'):
                # we are only interested in disks - TODO: maybe the range needs to be adjusted
                if 2001 <= j.key <= 2010:
                    vcenter_mounted[j.backing.uuid] = k['config.instanceUuid']
                    log.debug("==> mount - instance: %s - volume: %s", str(k['config.instanceUuid']), str(j.backing.uuid))

    # do the check from the other end: see for which vms or volumes in the vcenter we do not have any openstack info
    missing = dict()

    # iterate through all datastores in the vcenter
    for ds in dc.datastore:
        # only consider eph and vvol datastores
        if ds.name.lower().startswith('eph') or ds.name.lower().startswith('vvol'):
            log.info("- datacenter / datastore: %s / %s", dc.name, ds.name)

            # get all files and folders recursively from the datastore
            task = ds.browser.SearchDatastoreSubFolders_Task(datastorePath="[%s] /" % ds.name,
                                                             searchSpec=vim.HostDatastoreBrowserSearchSpec(
                                                                 matchPattern="*"))
            # matchPattern = ["*.vmx", "*.vmdk", "*.vmx.renamed_by_vcenter_nanny", "*,vmdk.renamed_by_vcenter_nanny"]))

            try:
                # wait for the async task to finish and then find vms and vmdks with openstack uuids in the name and
                # compare those uuids to all the uuids we know from openstack
                WaitForTask(task, si=service_instance)
                for uuid, location in _uuids(task):
                    if uuid not in known:
                        # only handle uuids which are not templates in the vcenter - otherwise theny might confuse the nanny
                        if template.get(uuid) is True:
                            log.warn("- PLEASE CHECK MANUALLY - uuid %s is a vcenter template and unknown to openstack",
                                     uuid)
                            gauge_value_unknown_vcenter_templates += 1
                        else:
                            # multiple locations are possible for one uuid, thus we need to put the locations into a list
                            if uuid in missing:
                                missing[uuid].append(location)
                            else:
                                missing[uuid] = [location]
            except vim.fault.InaccessibleDatastore as e:
                log.warn("- PLEASE CHECK MANUALLY - something went wrong trying to access this datastore: %s", e.msg)
                gauge_value_datastore_no_access += 1
            except vim.fault.FileNotFound as e:
                log.warn("- PLEASE CHECK MANUALLY - something went wrong trying to access this datastore: %s", e.msg)
                gauge_value_datastore_no_access += 1
            except vim.fault.NoHost as e:
                log.warn("- PLEASE CHECK MANUALLY - something went wrong trying to access this datastore: %s", e.msg)
                gauge_value_datastore_no_access += 1
            except task.info.error as e:
                log.warn("- PLEASE CHECK MANUALLY - something went wrong trying to access this datastore: %s", e.msg)
                gauge_value_datastore_no_access += 1

    init_seen_dict(vms_seen)
    init_seen_dict(files_seen)

    # needed to mark folder paths and full paths we already dealt with
    vmxmarked = {}
    vmdkmarked = {}
    vvolmarked = {}

    # iterate over all entities we have on the vcenter which have no relation to openstack anymore
    for item, locationlist in six.iteritems(missing):
        # none of the uuids we do not know anything about on openstack side should be mounted anywhere in vcenter
        # so we should neither see it as vmx (shadow vm) or datastore file
        if vcenter_mounted.get(item):
            if template.get(vcenter_mounted[item]) is True:
                log.warn("- PLEASE CHECK MANUALLY - volume %s is mounted on vcenter template %s", item,
                         vcenter_mounted[item])
                gauge_value_template_mount += 1
            else:
                log.warn("- PLEASE CHECK MANUALLY - possibly mounted ghost volume: %s mounted on %s", item,
                         vcenter_mounted[item])
                gauge_value_ghost_volumes += 1
        else:
            for location in locationlist:
                # foldername on datastore
                path = "{folderpath}".format(**location)
                # filename on datastore
                filename = "{filepath}".format(**location)
                fullpath = path + filename
                # in the case of a vmx file we check if the vcenter still knows about it
                if location["filepath"].lower().endswith(".vmx"):
                    vmx_path = "{folderpath}{filepath}".format(**location)
                    vm = content.searchIndex.FindByDatastorePath(path=vmx_path, datacenter=dc)
                    # there is a vm for that file path we check what to do with it
                    if vm:
                        # maybe there is a better way to get the moid ...
                        vm_moid = str(vm).strip('"\'').split(":")[1]
                        power_state = vm.runtime.powerState
                        # is the vm located on vvol storage - needed later to check if its a volume shadow vm
                        if vm.config.files.vmPathName.lower().startswith('[vvol'):
                            is_vvol = True
                        else:
                            is_vvol = False
                        # check if the vm has a nic configured
                        for j in vm.config.hardware.device:
                            if j.key == 4000:
                                has_no_nic = False
                            else:
                                has_no_nic = True
                        # we store the openstack project id in the annotations of the vm
                        annotation = vm.config.annotation or ''
                        items = dict([line.split(':', 1) for line in annotation.splitlines()])
                        # we search for either vms with a project_id in the annotation (i.e. real vms) or
                        # for powered off vms with 128mb, one cpu and no nic which are stored on vvol (i.e. shadow vm for a volume)
                        if 'projectid' in items or (
                                vm.config.hardware.memoryMB == 128 and vm.config.hardware.numCPU == 1 and power_state == 'poweredOff' and is_vvol and has_no_nic):
                            # if still powered on the planned action is to suspend it
                            if power_state == 'poweredOn':
                                # mark that path as already dealt with, so that we ignore it when we see it again
                                # with vmdks later maybe
                                vmxmarked[path] = True
                                now_or_later(vm.config.instanceUuid, vms_to_be_suspended, vms_seen, "suspend_vm",
                                             iterations,
                                             dry_run, power_off, unregister, delete, vm, dc, content, filename + " / " + vm_moid + " / " + vm.config.name)
                            # if already suspended the planned action is to power off the vm
                            elif power_state == 'suspended':
                                vmxmarked[path] = True
                                now_or_later(vm.config.instanceUuid, vms_to_be_poweredoff, vms_seen, "power_off_vm",
                                             iterations,
                                             dry_run, power_off, unregister, delete, vm, dc, content, filename + " / " + vm_moid + " / " + vm.config.name)
                            # if already powered off the planned action is to unregister the vm
                            elif power_state == 'poweredOff':
                                vmxmarked[path] = True
                                now_or_later(vm.config.instanceUuid, vms_to_be_unregistered, vms_seen,
                                             "unregister_vm",
                                             iterations,
                                             dry_run, power_off, unregister, delete, vm, dc, content, filename + " / " + vm_moid + " / " + vm.config.name)
                        # this should not happen
                        elif (
                                vm.config.hardware.memoryMB == 128 and vm.config.hardware.numCPU == 1 and power_state == 'poweredOff' and not is_vvol and has_no_nic):
                            log.warn("- PLEASE CHECK MANUALLY - possible orphan shadow vm on eph storage: %s", path)
                            gauge_value_eph_shadow_vms += 1
                        # this neither
                        else:
                            log.warn(
                                "- PLEASE CHECK MANUALLY - this vm seems to be neither a former openstack vm nor an orphan shadow vm: %s",
                                path)
                            gauge_value_complete_orphans += 1

                    # there is no vm anymore for the file path - planned action is to delete the file
                    elif not vmxmarked.get(path, False):
                        vmxmarked[path] = True
                        if path.lower().startswith("[eph"):
                            if path.endswith(".renamed_by_vcenter_nanny/"):
                                # if already renamed finally delete
                                now_or_later(str(path), files_to_be_deleted, files_seen, "delete_ds_path",
                                             iterations, dry_run, power_off, unregister, delete, vm, dc, content,
                                             filename)
                            else:
                                # first rename the file before deleting them later
                                now_or_later(str(path), files_to_be_renamed, files_seen, "rename_ds_path",
                                             iterations, dry_run, power_off, unregister, delete, vm, dc, content,
                                             filename)
                        else:
                            # vvol storage
                            # for vvols we have to mark based on the full path, as we work on them file by file
                            # and not on a directory base
                            vvolmarked[fullpath] = True
                            if fullpath.endswith(".renamed_by_vcenter_nanny/"):
                                now_or_later(str(fullpath), files_to_be_deleted, files_seen, "delete_ds_path",
                                             iterations, dry_run, power_off, unregister, delete, vm, dc, content,
                                             filename)
                            else:
                                now_or_later(str(fullpath), files_to_be_renamed, files_seen, "rename_ds_path",
                                             iterations, dry_run, power_off, unregister, delete, vm, dc, content,
                                             filename)

                    if len(tasks) % 8 == 0:
                        try:
                            WaitForTasks(tasks[-8:], si=service_instance)
                        except vmodl.fault.ManagedObjectNotFound as e:
                            log.warn("- PLEASE CHECK MANUALLY - problems running vcenter tasks: %s - they will run next time then", str(e))
                            gauge_value_vcenter_task_problems += 1

                # in case of a vmdk or vmx.renamed_by_vcenter_nanny
                # eph storage case - we work on directories
                elif path.lower().startswith("[eph") and not vmxmarked.get(path, False) and not vmdkmarked.get(path,
                                                                                                               False):
                    # mark to not redo it for other vmdks as we are working on the dir at once
                    vmdkmarked[path] = True
                    if path.endswith(".renamed_by_vcenter_nanny/"):
                        now_or_later(str(path), files_to_be_deleted, files_seen, "delete_ds_path",
                                     iterations, dry_run, power_off, unregister, delete, None, dc, content, filename)
                    else:
                        now_or_later(str(path), files_to_be_renamed, files_seen, "rename_ds_path",
                                     iterations, dry_run, power_off, unregister, delete, None, dc, content, filename)
                # vvol storage case - we work file by file as we can't rename or delete the vvol folders
                elif path.lower().startswith("[vvol") and not vvolmarked.get(fullpath, False):
                    # vvol storage
                    if fullpath.endswith(".renamed_by_vcenter_nanny"):
                        now_or_later(str(fullpath), files_to_be_deleted, files_seen, "delete_ds_path",
                                     iterations, dry_run, power_off, unregister, delete, None, dc, content, filename)
                    else:
                        now_or_later(str(fullpath), files_to_be_renamed, files_seen, "rename_ds_path",
                                     iterations, dry_run, power_off, unregister, delete, None, dc, content, filename)

                if len(tasks) % 8 == 0:
                    try:
                        WaitForTasks(tasks[-8:], si=service_instance)
                    except vmodl.fault.ManagedObjectNotFound as e:
                        log.warn("- PLEASE CHECK MANUALLY - problems running vcenter tasks: %s - they will run next time then", str(e))
                        gauge_value_vcenter_task_problems += 1

    # send the counters to the prometheus exporter - ugly for now, will change
    for kind in [ "plan", "dry_run", "done"]:
        gauge_suspend_vm.labels(kind).set(float(gauge_value[(kind, "suspend_vm")]))
        gauge_power_off_vm.labels(kind).set(float(gauge_value[(kind, "power_off_vm")]))
        gauge_unregister_vm.labels(kind).set(float(gauge_value[(kind, "unregister_vm")]))
        gauge_rename_ds_path.labels(kind).set(float(gauge_value[(kind, "rename_ds_path")]))
        gauge_delete_ds_path.labels(kind).set(float(gauge_value[(kind, "delete_ds_path")]))
    gauge_ghost_volumes.set(float(gauge_value_ghost_volumes))
    gauge_eph_shadow_vms.set(float(gauge_value_eph_shadow_vms))
    gauge_datastore_no_access.set(float(gauge_value_datastore_no_access))
    gauge_empty_vvol_folders.set(float(gauge_value_empty_vvol_folders))
    gauge_vcenter_connection_problems.set(float(gauge_value_vcenter_connection_problems))
    gauge_vcenter_get_properties_problems.set(float(gauge_value_vcenter_get_properties_problems))
    gauge_openstack_connection_problems.set(float(gauge_value_openstack_connection_problems))
    gauge_unknown_vcenter_templates.set(float(gauge_value_unknown_vcenter_templates))
    gauge_complete_orphans.set(float(gauge_value_complete_orphans))

    # reset the dict of vms or files we plan to do something with for all machines we did not see or which disappeared
    reset_to_be_dict(vms_to_be_suspended, vms_seen)
    reset_to_be_dict(vms_to_be_poweredoff, vms_seen)
    reset_to_be_dict(vms_to_be_unregistered, vms_seen)
    reset_to_be_dict(files_to_be_deleted, files_seen)
    reset_to_be_dict(files_to_be_renamed, files_seen)


if __name__ == '__main__':
    while True:
        run_me()
