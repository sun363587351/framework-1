# Copyright (C) 2018 iNuron NV
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

"""
ArakoonHelper module
"""
import os
import json
import Queue
from threading import Thread
from ovs.dal.hybrids.servicetype import ServiceType
from ovs.dal.lists.servicelist import ServiceList
from ovs.dal.lists.storagerouterlist import StorageRouterList
from ovs.extensions.db.arakooninstaller import ArakoonClusterConfig, ArakoonInstaller
from ovs.extensions.generic.configuration import Configuration
from ovs.extensions.generic.logger import Logger
from ovs.extensions.generic.sshclient import SSHClient
from ovs_extensions.generic.toolbox import ExtensionsToolbox


class ArakoonHelper(object):
    """
    Arakoon helper class
    """
    _logger = Logger('lib')

    @staticmethod
    def _get_cluster_ip(cluster_name):
        """
        Checks if an arakoon is internally managed. If so, return a list of ips on which the cluster runs. Returns None if the cluster is not internally managed.
        :param cluster_name: Name of an arakooncluster
        :return: list of ips the arakoon cluster is deployed on
        :rtype: list
        """
        if len(ServiceList.get_services()) == 0:
            ArakoonHelper._logger.exception('No services are running on OVS cluster!')
        for service in ServiceList.get_services():
            if service.name.endswith(cluster_name):
                if service.is_internal is True and service.type.name in (ServiceType.SERVICE_TYPES.ARAKOON,
                                                                         ServiceType.SERVICE_TYPES.NS_MGR,
                                                                         ServiceType.SERVICE_TYPES.ALBA_MGR):
                    return [sr.ip for sr in StorageRouterList.get_storagerouters()]
                else:
                    return None
        ArakoonHelper._logger.exception('Cluster {0} not found in running services!'.format(cluster_name))

    @staticmethod
    def get_arakoon_clusters():
        """
        Retrieves all Arakoon clusters registered in this OVSCluster
        :return: Dict with the Arakoon cluster types as key and list with dicts which contain cluster names and pyrakoon clients
        :rtype: dict(arakoon_name: [{'client': ovs_extensions.db.arakoon.pyrakoon.client.PyrakoonClient,
                                    'clustertype': str,
                                    'ips:node_ids: {ip: node_id,
                                                    ip: node_id}
                                    'config': ovs.extensions.db.arakooninstaller.ArakoonClusterConfig])
        """
        basic_config = ArakoonHelper.get_basic_config()

        for cluster_name in list(Configuration.list('/ovs/arakoon')) + ['cacc']:
            # Determine Arakoon type
            is_cacc = cluster_name == 'cacc'
            arakoon_config = basic_config[cluster_name]['config']
            if is_cacc is True:
                with open(Configuration.CACC_LOCATION) as config_file:
                    contents = config_file.read()
                arakoon_config.read_config(contents=contents)
                cluster_type = ServiceType.ARAKOON_CLUSTER_TYPES.CFG
                arakoon_client = ArakoonInstaller.build_client(arakoon_config)
            else:
                arakoon_client = ArakoonInstaller.build_client(arakoon_config)
                metadata = json.loads(arakoon_client.get(ArakoonInstaller.METADATA_KEY))
                cluster_type = metadata['cluster_type']

            basic_config[cluster_name].update({'client': arakoon_client,
                                               'cluster_type': cluster_type})
        return basic_config

    @classmethod
    def retrieve_collapse_stats(cls, arakoon_clusters, batch_size=10):
        """
        Retrieve tlog/tlx stat information for a Arakoon cluster concurrently
        Note: this will mutate the given arakoon_clusters dict
        Note: does NOT collapse arakoon clusters
        :param arakoon_clusters: Information about all arakoon clusters, sorted by name. An ArakoonClusterConfig object is required for this function to work
        Example input: {'cacc': {'config': ovs.extensions.db.arakooninstaller.ArakoonClusterConfig},
                        'ovsdb': {'config': ovs.extensions.db.arakooninstaller.ArakoonClusterConfig}
        :type arakoon_clusters: dict
        :param batch_size: Amount of workers to collect the Arakoon information.
        The amount of workers are dependant on the MaxSessions in the sshd_config
        :return: Dict with tlog/tlx contents for every node config
        Example return:
            dict(arakoon_name: {'config': ovs.extensions.db.arakooninstaller.ArakoonClusterConfig]
                                'collapse_result: { ovs_extensions.db.arakoon.arakooninstaller.ArakoonNodeConfig object: 'errors': [],
                                                                                                                         'result': { 'tlx': [[timestamp, path, size]],
                                                                                                                                             'tlog': [[timestamp, path, size]],
                                                                                                                                             'avail_size: int,
                                                                                                                                             'headDB: [[timestamp, path, size]]})                                                                                                                                              'errors': []}}}
        :rtype: dict
        """
        # Validation
        if isinstance(arakoon_clusters, dict) is False:
            ArakoonHelper._logger.exception('Argument `arakoon_clusters` must be of type dict, {0} given'.format(type(arakoon_clusters)))
        for cluster_name, cluster in arakoon_clusters.iteritems():
            try:
                isinstance(cluster['config'], ArakoonInstaller)
            except KeyError as ex:
                ArakoonHelper._logger.exception(ex)
        if not isinstance(batch_size, int ):
            ArakoonHelper._logger.exception('Argument `batch_size` must be of type int, {0} given'.format(type(batch_size)))

        # Prep work
        queue = Queue.Queue()
        clients = {}
        for cluster_name, cluster in arakoon_clusters.iteritems():
            arakoon_config = cluster['config']
            cluster['collapse_result'] = {}
            for node_config in arakoon_config.nodes:
                result = {'errors': [],
                          'result': {'tlx': [],
                                     'tlog': [],
                                     'headDB': [],
                                     'avail_size': None}}
                cluster['collapse_result'][node_config] = result
                # Build SSHClients outside the threads to avoid GIL
                try:
                    client = clients.get(node_config.ip)
                    if client is None:
                        client = SSHClient(node_config.ip, timeout=5)
                        clients[node_config.ip] = client
                except Exception as ex:
                    result['errors'].append(('build_client', ex))
                    continue
                queue.put((cluster_name, node_config, result))

        for _ in xrange(batch_size):
            thread = Thread(target=ArakoonHelper._collapse_worker, args=(queue, clients))
            thread.setDaemon(True)  # Setting threads as "daemon" allows main program to exit eventually even if these don't finish correctly.
            thread.start()
        # Wait for all results
        queue.join()
        return arakoon_clusters

    @staticmethod
    def _collapse_worker(queue, clients):
        """
        Worker method to retrieve file descriptors
        :param queue: Queue to use
        :param clients: SSHClients to choose from
        :return: None
        :rtype: NoneType
        """
        while not queue.empty():
            cluster_name, _node_config, _results = queue.get()
            errors = _results['errors']
            output = _results['result']
            identifier = 'Arakoon cluster {0} on node {1}'.format(cluster_name, _node_config.ip)

            try:
                _client = clients[_node_config.ip]
                tlog_dir = _node_config.tlog_dir
                path = os.path.join(tlog_dir, '*')
                if os.environ.get('RUNNING_UNITTESTS') == 'True':
                    timestamp_files = '1517569218 /opt/OpenvStorage/db/arakoon/config/tlogs/000.tlog 41766'
                    output['avail_size'] = 64073932
                else:
                    try:
                        # List the contents of the tlog directory and sort by oldest modification date
                        # Example output: (timestamp, name, size (bits)
                        # 01111 file.tlog 101
                        # 01112 file2.tlog 102
                        timestamp_files = _client.run('stat -c "%Y %n %s" {0}'.format(path), allow_insecure=True)
                        output['avail_size'] = _client.run("df {0} | tail -1 | awk '{{print $4}}'".format(path), allow_insecure=True)
                    except Exception as _ex:
                        errors.append(('stat_dir', _ex))
                        raise
                # Sort and separate the timestamp item files
                for split_entry in sorted((timestamp_file.split() for timestamp_file in timestamp_files.splitlines()), key=lambda split: int(split[0])):
                    file_name = split_entry[1]
                    if file_name.endswith('tlx'):
                        output['tlx'].append(split_entry)
                    elif file_name.endswith('tlog'):
                        output['tlog'].append(split_entry)
                    elif file_name.rsplit('/')[-1].startswith('head.db'):
                        output['headDB'].append(split_entry)
            except Exception as _ex:
                ArakoonHelper._logger.warning('Could not retrieve the collapse information for {0} ({1})'.format(identifier, str(_ex)))
            finally:
                queue.task_done()

    @staticmethod
    def get_basic_config():
        """
        Return the basic distribution of all arakoon clusters in the OVS environment
        :return: {cluster_name: {'ips:node_ids':{ip: node_id,
                                                 ip: node_id},
                                  'config: ovs.extensions.db.arakooninstaller.ArakoonClusterConfig}
        """
        storagerouters = StorageRouterList.get_storagerouters()
        cluster_info = {}
        if os.environ.get('RUNNING_UNITTESTS') != 'True':
            cluster_info['cacc'] = storagerouters[0]

        arakoon_clusters = {}

        for service in ServiceList.get_services():
            if service.is_internal is True and service.type.name in (ServiceType.SERVICE_TYPES.ARAKOON,
                                                                     ServiceType.SERVICE_TYPES.NS_MGR,
                                                                     ServiceType.SERVICE_TYPES.ALBA_MGR):
                cluster = ExtensionsToolbox.remove_prefix(service.name, 'arakoon-')
                if cluster in cluster_info and cluster not in ['cacc', 'unittest-cacc']:
                    continue
                cluster_info[cluster] = service.storagerouter
        for cluster_name, sr in cluster_info.iteritems():
            ArakoonHelper._logger.debug('  Collecting info for cluster {0}'.format(cluster_name))

            arakoon_clusters[cluster_name] = {'ips:node_ids': {}}
            ip = sr.ip if cluster_name in ['cacc', 'unittest-cacc'] else None
            try:
                arakoon_config = ArakoonClusterConfig(cluster_id=cluster_name, source_ip=ip)
                arakoon_clusters[cluster_name]['config'] = arakoon_config
            except Exception:
                ArakoonHelper._logger.exception('  Retrieving cluster information on {0} for {1} failed'.format(ip, cluster_name))

                continue
            for node in arakoon_config.nodes:
                if node.ip not in arakoon_clusters[cluster_name]['ips:node_ids']:
                    arakoon_clusters[cluster_name]['ips:node_ids'][node.ip] = node.name

        return arakoon_clusters
