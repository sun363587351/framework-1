# Copyright (C) 2017 iNuron NV
#
# This file is part of Open vStorage Open Source Edition (OSE),
# as available from
#
#      http://www.openvstorage.org and
#      http://www.openvstorage.com.
#
# This file is free software; you can redistribute it and/or modify it
# under the terms of the GNU Affero General Public License v3 (GNU AGPLv3)
# as published by the Free Software Foundation, in version 3 as it comes
# in the LICENSE.txt file of the Open vStorage OSE distribution.
#
# Open vStorage is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY of any kind.

#@todo remove
import ovs.deadlocker as stacktracer
stacktracer.trace_start("/tmp/trace.html")
"""
Scrubber Module
"""
import json
import time
import uuid
import itertools
from Queue import Empty, Queue
from random import randint
from threading import Thread
from ovs.dal.dataobject import DataObject
from ovs.dal.hybrids.diskpartition import DiskPartition
from ovs.dal.hybrids.storagerouter import StorageRouter
from ovs.dal.hybrids.vdisk import VDisk
from ovs.dal.hybrids.vpool import VPool
from ovs.dal.lists.storagedriverlist import StorageDriverList
from ovs.dal.lists.storagerouterlist import StorageRouterList
from ovs.dal.lists.vpoollist import VPoolList
from ovs.extensions.generic.configuration import Configuration
from ovs_extensions.generic.remote import remote
from ovs.extensions.generic.sshclient import SSHClient, UnableToConnectException
from ovs.extensions.generic.system import System
from ovs.extensions.generic.volatilemutex import volatile_mutex, NoLockAvailableException
from ovs.extensions.services.servicefactory import ServiceFactory
from ovs_extensions.storage.exceptions import AssertException
from ovs.extensions.storage.volatilefactory import VolatileFactory
from ovs.extensions.storage.persistentfactory import PersistentFactory
from ovs.lib.mdsservice import MDSServiceController
from ovs.log.log_handler import LogHandler


class ScrubShared(object):
    """
    Class which has functions to ensure consistency
    """
    _logger = LogHandler.get('lib', name='generic tasks scrub')

    _SCRUB_KEY = 'ovs/framework/jobs/scrub'  # Parent key for all scrub related jobs
    _SCRUB_NAMESPACE = 'ovs_jobs_scrub'
    _SCRUB_VDISK_KEY = '{0}_{{0}}_vdisks'.format(_SCRUB_NAMESPACE)  # Second format should be the vpool name
    _SCRUB_PROXY_KEY = '{0}_{{0}}'.format(_SCRUB_NAMESPACE)  # Second format should be the proxy name

    def __init__(self, job_id):
        self.job_id = job_id
        self._persistent = PersistentFactory.get_client()
        self._service_manager = ServiceFactory.get_manager()

        self._relevant_work_items = None  # All relevant scrubbing items
        self._fetched_work_items = None  # All retrieved scrubbing items

        # Required to be overruled
        self.worker_contexts = None

        self._key = None
        self._log = None  # To be overruled

    @property
    def worker_context(self):
        """
        Return the own worker context
        :return: Worker context
        :rtype: dict
        """
        if self.worker_contexts is None:
            raise NotImplementedError('No worker_contexts have yet been implemented')
        return self.worker_contexts[System.get_my_storagerouter()]

    def _safely_store(self, key, value, expected_value, logging_start, key_not_exist=False, max_retries=20, hooks=None):
        """
        Safely store a key/value pair within the persistent storage
        :param key: Key to store
        :type key: str
        :param value: Value to store
        :type value: any
        :param expected_value: Current value on the storage side
        :type expected_value: any
        :param logging_start: Start of the logging line
        :type logging_start: str
        :param max_retries: Number of retries to attempt
        :type max_retries: int
        :param key_not_exist: Only store if the key does not exist
        :type key_not_exist: bool
        :param hooks: Hooks to execute on certain conditions:
        - assert_fail_before: function to be called before other assert_fail hooks would be called
        - assert_fail_value: function to be called after the assert fail which changes the value to be saved
        - assert_fail_expected: function to be called after the assert fail which changes the 'expect_value' to be asserted
        :return: Stored value or the current value if key_not_exists is True and the key is already present
        :rtype: any
        :raises: AssertException:
        - When the save could not happen
        """
        def _execute_hook(hook, args=None, kwargs=None):
            if kwargs is None:
                kwargs = {}
            if args is None:
                args = []
            func = hooks.get(hook)
            if func is not None and callable(func):
                return func(*args, **kwargs)

        if hooks is None:
            hooks = {}
        tries = 0
        success = False
        last_exception = None
        return_value = value
        if key_not_exist is True and self._persistent.exists(key) is True:
            return_value = self._persistent.get(key)
            success = True
            self._logger.debug('{0} - key {1} is already present and key_not_exist given. Not saving and returning current value'.format(logging_start, key))
        while success is False:
            transaction = self._persistent.begin_transaction()
            return_value = value  # Value might change because of hooking
            tries += 1
            if tries > max_retries:
                raise last_exception
            self._persistent.assert_value(key, expected_value, transaction=transaction)
            self._persistent.set(key, value, transaction=transaction)
            try:
                self._persistent.apply_transaction(transaction)
                success = True
            except AssertException as ex:
                self._logger.debug('{0} - Asserting failed for key {1}. Retrying {2} more times'.format(logging_start, key, max_retries - tries))
                last_exception = ex
                time.sleep(randint(0, 25) / 100.0)
                _execute_hook('assert_fail_before')
                expected_value = _execute_hook('assert_fail_expected')
                value = _execute_hook('assert_fail_value')
        return return_value

    def _get_relevant_items(self, relevant_values, relevant_keys):
        """
        Retrieves all scrub work currently being done based on relevant values and the relevant format
        - Filters out own data
        - Filters out relevant data
        - Removes obsolete data
        :param relevant_values: The values to check relevancy on (Only supporting dict types)
        :type relevant_values: list[dict]
        :param relevant_keys: The keys that are relevant for checking relevancy
        (found items will strip keys to match to this format) (this format will be used to check in relevant values)
        :type relevant_keys: list
        :return: All pending scrub work in list of vdisk guids and all items to be removed
        :rtype: tuple(list, list)
        :raises: ValueError: When an irregular item has been detected
        """
        if any(not isinstance(v, dict )for v in relevant_values):
            raise ValueError('Not all relevant values are a dict')
        if not isinstance(relevant_keys, list):
            raise ValueError('The relevant keys should be a list of keys that are relevant')
        if any(set(v.keys()) != set(relevant_keys) for v in relevant_values):
            raise ValueError('The relevant values do not match the relevant format')
        # Fetch all items, yield empty list if None
        all_items = self._fetch_registered_items() or []
        self._relevant_work_items = []
        # Filter out the relevant items
        try:
            for item in all_items:
                # Extract relevant context. Note this is just a shallow copy and when retrieving items with lists/dicts we should not modify these in any way
                # because the referance is kept in _relevant_work_items
                relevant_context = dict((k, v) for k, v in item.iteritems() if k in relevant_keys)
                if relevant_context in relevant_values:
                    self._relevant_work_items.append(item)
                else:
                    # Not a item for the current scrubbing context. Possible remnant of an aborted scrub job so it will be removed when re-saving all total work items
                    self._logger.debug('{0} - Will be removing {1} on the next save as it is no longer relevant'.format(self._log, item))
        except KeyError:
            raise ValueError('{0} - Someone is registering keys different to worker context + vdisk guid'.format(self._log))
        return self._relevant_work_items

    def _fetch_registered_items(self):
        """
        Fetches all items currently registered on the key
        Saves them under _fetched_work_items for caching purposes. When None is returned, an empty list is set
        :return: All current items (None if the key has not yet been registered)
        """
        if self._key is None:
            raise ValueError('self._key has no value. Nothing to fetch')
        if self._persistent.exists(self._key) is True:
            items = self._persistent.get(self._key)
        else:
            items = None
        self._fetched_work_items = items
        return items

    @classmethod
    def _covert_data_objects(cls, item):
        # Change all data objects to their GUID
        if isinstance(item, list):
            return [cls._covert_data_objects(i) for i in item]
        elif isinstance(item, dict):
            return dict((cls._covert_data_objects(k), cls._covert_data_objects(v)) for k, v in item.iteritems())
        elif isinstance(item, DataObject):
            return item.guid
        return item

    def _format_message(self, message):
        """
        Formats a message with the set _log property in front (to remove redundancy)
        :param message: The message to format
        :return: Newly formatted message
        """
        if self._log is None:
            raise ValueError('_log property has no value. Nothing to format with')
        return '{0} - {1}'.format(self._log, message)


class StackWorkHandler(ScrubShared):
    """
    Handles generation, unregistering and saving of vpool stack work
    """
    def __init__(self, job_id, vpool, vdisks, worker_contexts):
        """
        Initialize
        :param vpool: vPool to generate work for
        :type vpool: ovs.dal.hybrids.vpool.VPool
        :param vdisks: vDisks to include in the work
        :type vdisks: list
        :param worker_contexts: Contexts about the workers
        :type worker_contexts: dict
        """
        super(StackWorkHandler, self).__init__(job_id)

        self.vpool = vpool
        self.vdisks = vdisks
        self.worker_contexts = worker_contexts
        self.work_queue = Queue()

        self._relevant_work_items = None  # All relevant scrubbing items
        self._fetched_work_items = None  # All retrieved scrubbing items

        self._key = self._SCRUB_VDISK_KEY.format(self.vpool.name)  # Key to register items under
        self._log = 'Scrubber - vPool {0}'.format(self.vpool.name)

    def generate_save_scrub_work(self):
        """
        Generates applicable scrub work and saves the scrub work consistently
        :return: Queue of work items
        """
        work_queue = self._generate_scrub_work()
        if work_queue.qsize() > 0:
            work_queue = self._save_work()
        return work_queue

    def _generate_scrub_work(self):
        """
        Generates scrub work to be done
        :return: Queue of work items
        :rtype: Queue
        """
        self._get_pending_scrub_work()
        registered_vdisks = [item['vdisk_guid'] for item in self._relevant_work_items]
        # Clear current queue
        with self.work_queue.mutex:
            self.work_queue.queue.clear()
        for vd in self.vdisks:
            logging_start_vd = '{0} - vDisk {1} {2}'.format(self._log, vd.guid, vd.name)
            if vd.guid in registered_vdisks:
                self._logger.info('{0} - has already been registered to get scrubbed, not queueing again'.format(logging_start_vd))
                continue
            if vd.is_vtemplate is True:
                self._logger.info('{0} - Is a template, not scrubbing'.format(logging_start_vd))
                continue
            vd.invalidate_dynamics('storagedriver_id')
            if not vd.storagedriver_id:
                self._logger.warning('{0} - No StorageDriver ID found'.format(logging_start_vd))
                continue
            self.work_queue.put(vd.guid)
        return self.work_queue

    def _wrap_data(self, vdisk_guid):
        """
        Wrap the vdisk guid in a dict with some other metadata
        Saving the worker context allows the other scrub jobs to determine what is still valid scrub data
        :param vdisk_guid: The guid of the vDisk to store
        :return: The wrapped data
        :rtype: dict
        """
        data = {'vdisk_guid': vdisk_guid}
        data.update(self.worker_context)
        return data

    def _get_pending_scrub_work(self):
        """
        Retrieves all scrub work currently being done
        - Filters out own data
        - Filters out relevant data
        - Removes obsolete data
        :return: All pending scrub work in list of vdisk guids and all items to be removed
        :rtype: tuple(list, list)
        :raises: ValueError: When an irregular item has been detected
        """
        # This will strip out the vdisk_guid to check the relevancy of the item (keeping the worker context)
        return self._get_relevant_items(relevant_values=self.worker_contexts.values(),
                                        relevant_keys=self.worker_context.keys())

    def _save_work(self):
        """
        Consistently save the generated scrub work
        :return: Current work to be done
        :rtype: Queue
        """
        # Add current queue items to the pool
        def get_total_items(*args, **kwargs):
            _ = args, kwargs
            return list(itertools.chain(self._relevant_work_items, (self._wrap_data(item) for item in self.work_queue.queue)))
        # Register some hooks to combat race conditions: Scrubbing vdisks might have changed (some removed, some added)
        # To combat this, regenerate our work and apply
        hooks = {'assert_fail_before': self._generate_scrub_work,
                 'assert_fail_value': get_total_items,  # Total items will be changed as registered items are fetched again and work items generated again
                 'assert_fail_expected': lambda *args, **kwargs: self._fetched_work_items}  # Total fetched items will be changed as the generate_scrub_work needs this info
        # Attempt to save with all fetched data during work generation, expect the current key to not have changed
        self._safely_store(self._key, get_total_items(),
                           expected_value=self._fetched_work_items,
                           logging_start=self._log,
                           hooks=hooks)
        # The queue might be different of when the function got called due to the hooking in the save
        return self.work_queue

    def remove_vdisk(self, vdisk_guid, retries=5):
        """
        Safely removes a vdisk from the pool
        :param vdisk_guid: Guid of the vDisk to remove
        :type vdisk_guid: basestring
        :param retries: Amount of retries to do
        :type retries: int
        :return: Remaining items
        :rtype: list
        """
        # @todo fix that remaining items are not diminishing!!
        def get_remaining_items():
            try:
                self._relevant_work_items.remove(self._wrap_data(vdisk_guid))
            except ValueError as ex:
                raise ValueError('The registering data is not in the list. Something must have removed it! ({0})'.format(str(ex)))
            return self._relevant_work_items
        self._logger.debug('{0} - Unregistering vDisk {1}'.format(self._log, vdisk_guid))
        hooks = {'assert_fail_before': self._get_pending_scrub_work,
                 'assert_fail_value': get_remaining_items,  # Total items will be changed as registered items are fetched again by the hook
                 'assert_fail_expected': lambda *args, **kwargs: self._fetched_work_items}  # Registered items will be fetched again by the hook

        self._get_pending_scrub_work()  # Fetch the data before generating the total items to be up to date
        # Attempt to save with all fetched data during work generation, expect the current key to not have changed
        self._safely_store(self._key, get_remaining_items(),
                           expected_value=self._fetched_work_items,
                           logging_start=self._log,
                           hooks=hooks,
                           max_retries=retries)
        return self._relevant_work_items


class StackWorker(ScrubShared):
    """
    This class represents a worker of the scrubbing stack
    """
    def __init__(self, job_id, queue, vpool, scrub_info, error_messages, stack_work_handler, worker_contexts, stacks_to_spawn, stack_number):
        """
        :param queue: a Queue with vDisk guids that need to be scrubbed (they should only be member of a single vPool)
        :type queue: Queue
        :param vpool: the vPool object of the vDisks
        :type vpool: VPool
        :param scrub_info: A dict containing scrub information:
                           `scrub_path` with the path where to scrub
                           `storage_router` with the StorageRouter that needs to do the work
        :type scrub_info: dict
        :param error_messages: A list of error messages to be filled (by reference)
        :type error_messages: list
        :param stack_work_handler: A stack work handler instance
        :type stack_work_handler: StackWorkHandler
        :param worker_contexts: Context of all ovs-worker services (dict with storagerouter guid, worker_pid and worker_start)
        :type worker_contexts: dict
        :param stacks_to_spawn: Amount of stacks that were spawned. Required to resolve racing with retries
        :type stacks_to_spawn: int
        :param stack_number: Number given by the stack spawner
        :type stack_number: int
        """
        super(StackWorker, self).__init__(job_id)
        self.queue = queue
        self.vpool = vpool
        self.error_messages = error_messages
        self.worker_contexts = worker_contexts
        self.stack_number = stack_number
        self.stacks_to_spawn = stacks_to_spawn
        self.stack_work_handler = stack_work_handler

        self.queue_size = self.queue.qsize()
        self.stack_id = str(uuid.uuid4())
        self.storagerouter = scrub_info['storagerouter']
        self.partition_guid = scrub_info['partition_guid']
        self.alba_proxy_service = 'ovs-albaproxy_{0}_{1}_{2}_scrub'.format(self.vpool.name, self.storagerouter.name, self.partition_guid)
        self.scrub_directory = '{0}/scrub_work_{1}'.format(scrub_info['scrub_path'], uuid.uuid4())
        self.scrub_config_key = 'ovs/vpools/{0}/proxies/scrub/scrub_config_{1}'.format(vpool.guid, self.partition_guid)
        self.backend_config_key = 'ovs/vpools/{0}/proxies/scrub/backend_config_{1}'.format(vpool.guid, self.partition_guid)
        self.lock_time = 5 * 60
        self.lock_key = 'ovs_albaproxy_scrub_{0}'.format(self.alba_proxy_service)
        self.registered_proxy = False
        self.registering_data = {'job_id': self.job_id, 'stack_id': self.stack_id}  # Data to register within Arakoon about this StackWorker
        self.registering_data.update(self.worker_context)

        self._client = None
        self._log = 'Scrubber - vPool {0} - StorageRouter {1} - Stack {2}'.format(self.vpool.name, self.storagerouter.name, self.stack_number)
        self._key = self._SCRUB_PROXY_KEY.format(self.alba_proxy_service)
        self._state_key = '{0}_state'.format(self._key)

    def deploy_stack_and_scrub(self):
        """
        Executes scrub work for a given vDisk queue and vPool, based on scrub_info
        - Sets up a scrubbing proxy (if the proxy is not present yet)
        - Launches scrubbing threads (up to 20)
        - Cleans up the scrubbing proxy (if the proxy is no longer used)
        :return: None
        :rtype: NoneType
        """
        if len(self.vpool.storagedrivers) == 0 or not self.vpool.storagedrivers[0].storagedriver_id:
            self.error_messages.append('vPool {0} does not have any valid StorageDrivers configured'.format(self.vpool.name))
            return

        try:
            # Deploy a proxy
            self._deploy_proxy()

            # Execute the actual scrubbing
            threads = []
            threads_key = '/ovs/framework/hosts/{0}/config|scrub_stack_threads'.format(self.storagerouter.machine_id)
            amount_threads = Configuration.get(key=threads_key) if Configuration.exists(key=threads_key) else 2
            if not isinstance(amount_threads, int):
                self.error_messages.append('Amount of threads to spawn must be an integer for StorageRouter with ID {0}'.format(self.storagerouter.machine_id))
                return

            amount_threads = max(amount_threads, 1)  # Make sure amount_threads is at least 1
            amount_threads = min(min(self.queue.qsize(), amount_threads), 20)  # Make sure amount threads is max 20
            self._logger.info('{0} - Spawning {1} threads for proxy service {2}'.format(self._log, amount_threads, self.alba_proxy_service))
            for index in range(amount_threads):
                thread = Thread(name='execute_scrub_{0}_{1}_{2}'.format(self.vpool.guid, self.partition_guid, index),
                                target=self._execute_scrub,
                                args=())
                thread.start()
                threads.append(thread)
            for thread in threads:
                thread.join()

            # Delete the proxy again
            self._remove_proxy()
        finally:
            pass
            # # Attempt to remove all registered items
            # for vdisk_guid in self.queue.queue:
            #     self.stack_work_handler.remove_vdisk(vdisk_guid)

    def _execute_scrub(self):
        """
        Executes the actual scrubbing
        - Ask the Volumedriver what scrub work is to be done
        - Ask to Volumedriver to do scrub work
        - Ask the Volumedriver to apply this work (when not applied, scrubbing leaves garbage on the backend)
        :return: None
        :rtype: NoneType
        """
        def _verify_mds_config(current_vdisk):
            current_vdisk.invalidate_dynamics('info')
            vdisk_configs = current_vdisk.info['metadata_backend_config']
            if len(vdisk_configs) == 0:
                raise RuntimeError('Could not load MDS configuration')
            return vdisk_configs

        volatile_client = VolatileFactory.get_client()
        try:
            # Empty the queue with vDisks to scrub
            with remote(self.storagerouter.ip, [VDisk]) as rem:
                while True:
                    vdisk = None
                    vdisk_guid = self.queue.get(False)  # Raises Empty Exception when queue is empty, so breaking the while True loop
                    volatile_key = 'ovs_scrubbing_vdisk_{0}'.format(vdisk_guid)
                    try:
                        # Check MDS master is local. Trigger MDS handover if necessary
                        vdisk = rem.VDisk(vdisk_guid)
                        self._logger.info('{0} - vDisk {1} - Started scrubbing at location {2}'.format(self._log, vdisk.name, self.scrub_directory))
                        configs = _verify_mds_config(current_vdisk=vdisk)
                        storagedriver = StorageDriverList.get_by_storagedriver_id(vdisk.storagedriver_id)
                        if configs[0].get('ip') != storagedriver.storagerouter.ip:
                            self._logger.info('{0} - vDisk {1} - MDS master is not local, trigger handover'.format(self._log, vdisk.name))
                            MDSServiceController.ensure_safety(vdisk_guid=vdisk_guid)  # Do not use a remote VDisk instance here
                            configs = _verify_mds_config(current_vdisk=vdisk)
                            if configs[0].get('ip') != storagedriver.storagerouter.ip:
                                self._logger.warning('{0} - vDisk {1} - Skipping because master MDS still not local'.format(self._log, vdisk.name))
                                continue

                        # Check if vDisk is already being scrubbed
                        if volatile_client.add(key=volatile_key, value=volatile_key, time=24 * 60 * 60) is False:
                            self._logger.warning('{0} - vDisk {1} - Skipping because vDisk is already being scrubbed'.format(self._log, vdisk.name))
                            continue

                        # Do the actual scrubbing
                        with vdisk.storagedriver_client.make_locked_client(str(vdisk.volume_id)) as locked_client:
                            self._logger.info('{0} - vDisk {1} - Retrieve and apply scrub work'.format(self._log, vdisk.name))
                            work_units = locked_client.get_scrubbing_workunits()
                            for work_unit in work_units:
                                res = locked_client.scrub(work_unit=work_unit,
                                                          scratch_dir=self.scrub_directory,
                                                          log_sinks=[LogHandler.get_sink_path('scrubber_{0}'.format(self.vpool.name), allow_override=True, forced_target_type='file')],
                                                          backend_config=Configuration.get_configuration_path(self.backend_config_key))
                                locked_client.apply_scrubbing_result(scrubbing_work_result=res)
                            if work_units:
                                self._logger.info('{0} - vDisk {1} - {2} work units successfully applied'.format(self._log, vdisk.name, len(work_units)))
                            else:
                                self._logger.info('{0} - vDisk {1} - No scrubbing required'.format(self._log, vdisk.name))
                    except Exception:
                        if vdisk is None:
                            message = '{0} - vDisk with guid {1} could not be found'.format(self._log, vdisk_guid)
                        else:
                            message = '{0} - vDisk {1} - Scrubbing failed'.format(self._log, vdisk.name)
                        self.error_messages.append(message)
                        self._logger.exception(message)
                    finally:
                        # Remove vDisk from volatile memory and scrub work
                        volatile_client.delete(volatile_key)
                        self.stack_work_handler.remove_vdisk(vdisk_guid, self.queue_size)

        except Empty:  # Raised when all items have been fetched from the queue
            self._logger.info('{0} - Queue completely processed'.format(self._log))
        except Exception:
            message = '{0} - Scrubbing failed'.format(self._log)
            self.error_messages.append(message)
            self._logger.exception(message)

    def _get_registered_proxy_users(self):
        """
        Retrieves all stacks using a certain proxy
        - Discards obsolete data
        - Removes obsolete data keys
        :return: List of stacks using the proxy
        :rtype: list
        """
        # This will strip out the job_id, stack_id to check the relevancy of the item (keeping the worker context)
        return self._get_relevant_items(relevant_values=self.worker_contexts.values(),
                                        relevant_keys=self.worker_context.keys())

    def _register_proxy_usage(self):
        """
        Registers that this worker is using the proxy
        :return: Current list of items
        :rtype: list
        """
        def get_total_items():
            self._relevant_work_items.append(self.registering_data)
            return self._relevant_work_items

        self._logger.debug('{0} - Registering usage of {1}'.format(self._log, self.alba_proxy_service))
        hooks = {'assert_fail_before': self._get_registered_proxy_users,
                 'assert_fail_value': get_total_items,  # Total items will be changed as registered items are fetched again by the hook
                 'assert_fail_expected': lambda *args, **kwargs: self._fetched_work_items}  # Registered items will be fetched again by the hook

        self._get_registered_proxy_users()  # Fetch the data before generating the total items to be up to date
        # Attempt to save with all fetched data during work generation, expect the current key to not have changed
        self._safely_store(self._key, get_total_items(),
                           expected_value=self._fetched_work_items,
                           logging_start=self._log,
                           hooks=hooks)
        self.registered_proxy = True
        return self._relevant_work_items

    def _unregister_proxy_usage(self):
        """
        Unregisters that this worker is using this proxy
        :return: The remaining data entires
        :rtype: list
        """
        def get_remaining_items():
            try:
                self._relevant_work_items.remove(self.registering_data)
            except ValueError as ex:
                raise ValueError('The registering data is not in the list. Something must have removed it! ({0})'.format(str(ex)))
            return self._relevant_work_items
        self._logger.debug('{0} - Unregistering usage of {1}'.format(self._log, self.alba_proxy_service))
        hooks = {'assert_fail_before': self._get_registered_proxy_users,
                 'assert_fail_value': get_remaining_items,  # Total items will be changed as registered items are fetched again by the hook
                 'assert_fail_expected': lambda *args, **kwargs: self._fetched_work_items}  # Registered items will be fetched again by the hook

        self._get_registered_proxy_users()  # Fetch the data before generating the total items to be up to date
        if self.registered_proxy is True:
            # Attempt to save with all fetched data during work generation, expect the current key to not have changed
            self._safely_store(self._key, get_remaining_items(),
                               expected_value=self._fetched_work_items,
                               logging_start=self._log,
                               hooks=hooks,
                               max_retries=self.stacks_to_spawn)
        return self._relevant_work_items

    def _long_poll_proxy_state(self, state, timeout=None):
        """
        Start long polling for the proxy state. This state is not fetched by the service manager but rather by the threads injecting a key in Arakoon
        This function will return once the requested state has been achieved
        :param state: State of the key
        :type state: Possible states are:
        - 'deploying': proxy is still being deployed
        - 'deployed': proxy has been deployed
        - 'removing': proxy is being removed
        - 'removed': proxy has been removed
        :param timeout: Amount of seconds to wait (Defaults to 5 minutes)
        :return: None
        :rtype: NoneType
        """
        if timeout is None:
            timeout = self.lock_time
        if self._persistent.exists(self._state_key) is True:
            start = time.time()
            while True:
                current_state = self._persistent.get(self._state_key)
                if current_state == state:
                    return
                if time.time() - start > timeout:
                    raise RuntimeError('Long polling for the state has timed out')
                self._logger.debug(self._format_message('Proxy {0}\'s state does not match {1}  (Current: {2})'.format(self.alba_proxy_service, state, current_state)))
                time.sleep(1)
        return

    def _set_proxy_state(self, state):
        """
        Sets the state of the proxy
        :param state: State of key
        :return: The state that was set
        """
        self._persistent.set(self._state_key, state)

    def _deploy_proxy(self):
        """
        Deploy a scrubbing proxy
        - Validates if a proxy is already present
        - Sets up a proxy consistently
        :return: None
        :rtype: NoneType
        """
        try:
            self._logger.info(self._format_message('Checking for ALBA proxy {0} deployment'.format(self.alba_proxy_service)))
            registered_users = self._register_proxy_usage()
            self._logger.info(self._format_message('Current registered users: {0}'.format(registered_users)))
            if len(registered_users) == 1:
                self._logger.info(self._format_message('Deploying ALBA proxy {0}'.format(self.alba_proxy_service)))
                self._set_proxy_state('deploying')
                # First to register, allowed to deploy the proxy
                # Check the proxy status - could be that it is removing
                self._client = SSHClient(self.storagerouter, 'root')
                self._client.dir_create(self.scrub_directory)
                self._client.dir_chmod(self.scrub_directory, 0777)  # Celery task executed by 'ovs' user and should be able to write in it
                machine_id = System.get_my_machine_id(self._client)
                port_range = Configuration.get('/ovs/framework/hosts/{0}/ports|storagedriver'.format(machine_id))
                port = System.get_free_ports(selected_range=port_range, nr=1, client=self._client)[0]
                scrub_config = Configuration.get('ovs/vpools/{0}/proxies/scrub/generic_scrub'.format(self.vpool.guid))
                scrub_config['port'] = port
                scrub_config['transport'] = 'tcp'
                Configuration.set(self.scrub_config_key, json.dumps(scrub_config, indent=4), raw=True)

                params = {'VPOOL_NAME': self.vpool.name,
                          'LOG_SINK': LogHandler.get_sink_path(self.alba_proxy_service),
                          'CONFIG_PATH': Configuration.get_configuration_path(self.scrub_config_key)}
                self._service_manager.add_service(name='ovs-albaproxy', params=params, client=self._client, target_name=self.alba_proxy_service)
                self._service_manager.start_service(name=self.alba_proxy_service, client=self._client)
                self._logger.info(self._format_message('Deployed ALBA proxy {0} (Config: {1})'.format(self.alba_proxy_service, scrub_config)))
                # Backend config is tied to the proxy, so only need to register while the proxy has to be deployed
                self._logger.info(self._format_message('Setting up backend config'))
                backend_config = Configuration.get('ovs/vpools/{0}/hosts/{1}/config'.format(self.vpool.guid, self.vpool.storagedrivers[0].storagedriver_id))['backend_connection_manager']
                if backend_config.get('backend_type') != 'MULTI':
                    backend_config['alba_connection_host'] = '127.0.0.1'
                    backend_config['alba_connection_port'] = scrub_config['port']
                else:
                    for value in backend_config.itervalues():
                        if isinstance(value, dict):
                            value['alba_connection_host'] = '127.0.0.1'
                            value['alba_connection_port'] = scrub_config['port']
                # Copy backend connection manager information in separate key
                Configuration.set(self.backend_config_key, json.dumps({"backend_connection_manager": backend_config}, indent=4), raw=True)
                self._logger.info(self._format_message('Backend config was set up'))
                self._set_proxy_state('deployed')
            else:
                # Long polling for the status
                self._logger.info(self._format_message('Re-using existing proxy service {0}'.format(self.alba_proxy_service)))
                self._logger.info(self._format_message('Waiting for proxy service {0} to be deployed'.format(self.alba_proxy_service)))
                self._long_poll_proxy_state(state='deployed')
                self._logger.info(self._format_message('Proxy service {0} was deployed'.format(self.alba_proxy_service)))
        except Exception:
            message = '{0} - An error occurred deploying ALBA proxy {1}. Removing...'.format(self._log, self.alba_proxy_service)
            self.error_messages.append(message)
            self._logger.exception(message)
            # Locking the removal here because others might register a proxy to be used
            self._remove_proxy()
            # No proxy could be set so no scrubbing would be able to happen
            raise

    def _remove_proxy(self):
        """
        Removes the proxy that was used
        :return: None
        """
        try:
            # No longer using the proxy as we want to remove it
            proxy_users = self._unregister_proxy_usage()
            self._client = SSHClient(self.storagerouter, 'root')
            if len(proxy_users) > 0:
                self._logger.info('{0} - Service {1} still in use by others'.format(self._log, self.alba_proxy_service))
                return
            self._logger.info('{0} - Removing service {1}'.format(self._log, self.alba_proxy_service))
            self._set_proxy_state('removing')
            self._client.dir_delete(self.scrub_directory)
            if self._service_manager.has_service(self.alba_proxy_service, client=self._client) is True:
                if self._service_manager.get_service_status(name=self.alba_proxy_service,client=self._client) == 'active':
                    self._service_manager.stop_service(self.alba_proxy_service, client=self._client)
                self._service_manager.remove_service(self.alba_proxy_service, client=self._client)
            if Configuration.exists(self.scrub_config_key):
                Configuration.delete(self.scrub_config_key)
            self._logger.info('{0} - Removed service {1}'.format(self._log, self.alba_proxy_service))
        except Exception:
            message = '{0} - Removing service {1} failed'.format(self._log, self.alba_proxy_service)
            self.error_messages.append(message)
            self._logger.exception(message)
            raise
        finally:
            # Not sure if this is the right call. Proxy deployment/removal errors should be looked after
            # This will cause the next proxy setup to fail as the service already exists and that one will attempt to clean up again
            self._set_proxy_state('removed')


class Scrubber(ScrubShared):
    """
    This class represents a scrub job being handled
    It will:
    - Divide the scrub work among all StorageRouters with a SCRUB partition
    - Create a job entry into ovs/framework/jobs/scrub
    - Executes scrub work for a given vDisk queue and vPool, based on scrub_info
      - Will re-use already deployed proxies
      - Keep internal track of items to scrub
    - Cleanup stale job entries
    """
    _KEY_LIFETIME = 7 * 24 * 60 * 60  # All job keys are kept for 7 days and after that the next scrubbing job will remove the outdated ones

    def __init__(self, vpool_guids=None, vdisk_guids=None, storagerouter_guid=None, manual=False, task_id=None):
        """
        :param vpool_guids: Guids of the vPools that need to be scrubbed completely
        :type vpool_guids: list
        :param vdisk_guids: Guids of the vDisks that need to be scrubbed
        :type vdisk_guids: list
        :param storagerouter_guid: Guid of the StorageRouter to execute the scrub work on
        :type storagerouter_guid: str
        :param manual: Indicator whether the execute_scrub is called manually or as scheduled task (automatically)
        :type manual: bool
        :param task_id: An ID for the current scrub task (this can be the current celery job id or None for a generated one)
        """
        # Validation
        if vdisk_guids is None:
            vdisk_guids = []
        if vpool_guids is None:
            vpool_guids = []
        if not isinstance(vpool_guids, list):
            raise ValueError('vpool_guids should be a list')
        if not isinstance(vdisk_guids, list):
            raise ValueError('vdisk_guids should be a list')
        if storagerouter_guid is not None and not isinstance(storagerouter_guid, basestring):
            raise ValueError('storagerouter_guid should be a str')

        if manual is False and (len(vpool_guids) > 0 or len(vdisk_guids) > 0):
            raise ValueError('When specifying vDisks or vPools, "manual" must be True')

        super(Scrubber, self).__init__(task_id or str(uuid.uuid4()))

        self.task_id = task_id  # Be able to differentiate between directly executed ones for debugging purposes
        self.vdisk_guids = vdisk_guids
        self.vpool_guids = vpool_guids
        self.storagerouter_guid = storagerouter_guid
        self.manual = manual

        self._service_manager = ServiceFactory.get_manager()
        self._log = 'Scrubber'

        self.time_start = None
        self.time_end = None
        self.vpool_vdisk_map = self.generate_vpool_vdisk_map(vpool_guids=vpool_guids, vdisk_guids=vdisk_guids, manual=manual)
        self.scrub_locations = self.get_scrub_locations(storagerouter_guid)
        self.worker_contexts = self.get_worker_contexts()  # Could give off an outdated view but that would be picked up by the next scrub job

        # Scrubbing stack
        self.error_messages = []  # Keep track of all messages that might occur
        self.max_stacks_per_vpool = None
        self.stacks = {}
        self.stack_threads = []

    def get_worker_contexts(self):
        """
        Retrieves information about the all workers (where it is executed and under what PID)
        :return: Information about the current workers
        :rtype: dict
        """
        workers_context = {}
        for storagerouter in StorageRouterList.get_storagerouters():
            try:
                client = SSHClient(storagerouter, username='root')
            except:
                self._logger.warning('Unable to connect to StorageRouter {0}'.format(storagerouter.guid))
                continue
            worker_pid = self._service_manager.get_service_pid(name='ovs-workers', client=client)
            worker_start = None
            if worker_pid == 0:
                self._logger.warning('The workers are down on StorageRouter {0}'.format(storagerouter.guid))
            else:
                try:
                    # Retrieve the current start time of the process (used to create a unique key)
                    # Output of the command:
                    #                  STARTED   PID
                    # Mon Jan 22 11:49:04 2018 22287
                    worker_start = client.run(['ps', '-o', 'lstart', '-p', worker_pid]).strip().splitlines()[-1].replace(' ', '-')
                except Exception:
                    self._logger.warning('Unable to retrieve start time of the worker')
            workers_context[storagerouter] = {'storagerouter_guid': storagerouter.guid,
                                              'worker_pid': worker_pid,
                                              'worker_start': worker_start}
        return workers_context

    def execute_scrubbing(self):
        """
        Execute the scrubbing work
        Every vpool will have its own set of stacks to scrub. These stacks deploy scrubbing threads internally
        The number of stacks for every vpool is calculated based on the number of vpools to scrub in total ( 6+ -> 1/vpool, 6>x>=3 -> 2/vpool, 3> -> 3/vpool)
        :return: None
        :rtype: NoneType
        """
        number_of_vpools = len(self.vpool_vdisk_map)
        if number_of_vpools >= 6:
            self.max_stacks_per_vpool = 1
        elif number_of_vpools >= 3:
            self.max_stacks_per_vpool = 2
        else:
            self.max_stacks_per_vpool = 5

        self.time_start = time.time()
        self.set_main_job_info()
        counter = 0
        for vp, vdisks in self.vpool_vdisk_map.iteritems():
            logging_start = '{0} - vPool {1}'.format(self._log, vp.name)
            # Verify amount of vDisks on vPool
            self._logger.info('{0} - Checking scrub work'.format(logging_start))
            stack_work_handler = StackWorkHandler(vpool=vp, vdisks=vdisks, worker_contexts=self.worker_contexts, job_id=self.job_id)
            vpool_queue = stack_work_handler.generate_save_scrub_work()
            if vpool_queue.qsize() == 0:
                self._logger.info('{0} - No scrub work'.format(logging_start))
                continue
            stacks_to_spawn = min(self.max_stacks_per_vpool, len(self.scrub_locations))
            self._logger.info('{0} - Spawning {1} stack{2}'.format(logging_start, stacks_to_spawn, '' if stacks_to_spawn == 1 else 's'))
            for stack_number in xrange(stacks_to_spawn):
                scrub_target = self.scrub_locations[counter % len(self.scrub_locations)]
                stack_worker = StackWorker(queue=vpool_queue,
                                           vpool=vp,
                                           scrub_info=scrub_target,
                                           error_messages=self.error_messages,
                                           worker_contexts=self.worker_contexts,
                                           stack_work_handler=stack_work_handler,
                                           job_id=self.job_id,
                                           stacks_to_spawn=stacks_to_spawn,
                                           stack_number=stack_number)
                stack = Thread(target=stack_worker.deploy_stack_and_scrub,
                               args=())
                stack.start()
                self.stack_threads.append(stack)
                counter += 1

        for thread in self.stack_threads:
            thread.join()

        # Update the job info
        self.time_end = time.time()
        self.set_main_job_info()

        self._cleanup_job_entries()

        if len(self.error_messages) > 0:
            raise Exception(self._format_message('Errors occurred while scrubbing:\n  - {0}'.format('\n  - '.join(self.error_messages))))

    def set_main_job_info(self):
        """
        Registers the current scrubbing job within Configuration
        This allows for better debugging / linking jobs
        :return: None
        :rtype: NoneType
        """
        # Validation
        if any(item is None for item in [self.max_stacks_per_vpool, self.time_start]):
            raise ValueError('Scrubbing has not been executed yet. Not registering the current job')

        job_key = '{0}/{1}/job_info'.format(self._SCRUB_KEY, self.job_id)
        job_info = {'scrub_locations': [self._covert_data_objects(x) for x in self.scrub_locations],
                    'task_id': self.task_id,
                    'max_stacks_per_vpool': self.max_stacks_per_vpool,
                    'vpool_vdisk_map': self._covert_data_objects(self.vpool_vdisk_map),
                    'time_start': self.time_start,
                    'time_end': self.time_end,
                    'worker_contexts': self._covert_data_objects(self.worker_contexts)}
        Configuration.set(job_key, json.dumps(job_info, indent=4), raw=True)

    @staticmethod
    def generate_vpool_vdisk_map(vpool_guids=None, vdisk_guids=None, manual=False):
        """
        Generates a mapping between the provided vpools and vdisks
        :param vpool_guids: Guids of the vPools
        :type vpool_guids: list
        :param vdisk_guids: Guids of the vdisks
        :type vdisk_guids: list
        :param manual: Indicator whether the execute_scrub is called manually or as scheduled task (automatically)
        :type manual: bool
        :return: The mapping
        :rtype: dict
        """
        if vdisk_guids is None:
            vdisk_guids = []
        if vpool_guids is None:
            vpool_guids = []
        if manual is True:
            vpool_vdisk_map = {}
            for vpool_guid in set(vpool_guids):
                vpool = VPool(vpool_guid)
                vpool_vdisk_map[vpool] = list(vpool.vdisks)
            for vdisk_guid in set(vdisk_guids):
                vdisk = VDisk(vdisk_guid)
                if vdisk.vpool not in vpool_vdisk_map:
                    vpool_vdisk_map[vdisk.vpool] = []
                if vdisk not in vpool_vdisk_map[vdisk.vpool]:
                    vpool_vdisk_map[vdisk.vpool].append(vdisk)
        else:
            vpool_vdisk_map = dict((vpool, list(vpool.vdisks)) for vpool in VPoolList.get_vpools())
        return vpool_vdisk_map

    def get_scrub_locations(self, storagerouter_guid=None):
        """
        Retrieve all scrub locations
        :param storagerouter_guid: Guid of the StorageRouter to execute the scrub work on
        :type storagerouter_guid: str
        :raises: ValueError when no scrub locations could be found
        :return: Scrubbing location info (Example: [{'scrub_path': FOLDER, 'partition_guid': GUID, 'storagerouter': StorageRouter object}]
        :rtype: list[dict]
        """
        scrub_locations = []
        storagerouters = StorageRouterList.get_storagerouters() if storagerouter_guid is None else [
            StorageRouter(storagerouter_guid)]
        for storagerouter in storagerouters:
            scrub_partitions = storagerouter.partition_config.get(DiskPartition.ROLES.SCRUB, [])
            if len(scrub_partitions) == 0:
                continue
            try:
                SSHClient(endpoint=storagerouter, username='root')
                for partition_guid in scrub_partitions:
                    partition = DiskPartition(partition_guid)
                    self._logger.info(self._format_message('Storage Router {0} has {1} partition at {2}'.format(storagerouter.ip, DiskPartition.ROLES.SCRUB, partition.folder)))
                    scrub_locations.append({'scrub_path': str(partition.folder),
                                            'partition_guid': partition.guid,
                                            'storagerouter': storagerouter})
            except UnableToConnectException:
                self._logger.warning(self._format_message('Storage Router {0} is not reachable'.format(storagerouter.ip)))

        if len(scrub_locations) == 0:
            raise ValueError('No scrub locations found, cannot scrub')
        return scrub_locations

    def _cleanup_job_entries(self):
        """
        Clean up job entries which have been stored longer than the _KEY_LIFETIME number of seconds
        :return: List of removed keys
        :rtype: list
        """
        removed_keys = []
        try:
            with volatile_mutex('scrubber_clean_entries', wait=30):
                for key in Configuration.list(self._SCRUB_KEY):
                    full_key = '{0}/{1}'.format(self._SCRUB_KEY, key)
                    job_info = Configuration.get('{0}/job_info'.format(full_key))
                    time_start = job_info.get('time_start')
                    time_end = job_info.get('time_end')
                    if time_start is None or (time_end is not None and time_end - time_start >= self._KEY_LIFETIME):
                        Configuration.delete(full_key)
                        removed_keys.append(full_key)
                if len(removed_keys) > 0:
                    self._logger.info(self._format_message('Cleaned up the following outdated scrub keys: {0}'.format('\n - '.join(removed_keys))))
        except NoLockAvailableException:
            self._logger.warning(self._format_message('Could not get the lock to clean entries'))
        return removed_keys
