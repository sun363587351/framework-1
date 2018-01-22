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
OSD Backend module
"""
from ovs.dal.dataobject import DataObject
from ovs.dal.structures import Relation
from ovs.dal.hybrids.storagedriver import StorageDriver
from ovs.dal.hybrids.service import Service


class OSDBackend(DataObject):
   """
   The OSDBackend class represents the junction table between the (alba)Backend and OSD.
   Examples:
   * osd.alba_backends[0]
   * alba.backend.osds[0]
   """
   __properties = []
   __relations = [Relation('alba_backend', StorageDriver, 'osds'),
                  Relation('alba_osd', Service, 'alba_backends')]
   __dynamics = []