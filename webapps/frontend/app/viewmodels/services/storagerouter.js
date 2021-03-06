// Copyright (C) 2017 iNuron NV
//
// This file is part of Open vStorage Open Source Edition (OSE),
// as available from
//
//      http://www.openvstorage.org and
//      http://www.openvstorage.com.
//
// This file is free software; you can redistribute it and/or modify it
// under the terms of the GNU Affero General Public License v3 (GNU AGPLv3)
// as published by the Free Software Foundation, in version 3 as it comes
// in the LICENSE.txt file of the Open vStorage OSE distribution.
//
// Open vStorage is distributed in the hope that it will be useful,
// but WITHOUT ANY WARRANTY of any kind.
/*global define */
/**
 * Service to help with backend related tasks
 */
define([
    'jquery', 'knockout',
    'ovs/api', 'ovs/shared'
], function ($, ko, api, shared) {

    function StorageRouterService() {
        var self = this;
        /**
         * Loads in all StorageRouters for the current supplied data
         * @param queryParams: Additional query params. Defaults to no params
         * @returns {Deferred}
         */
        self.loadStorageRouters = function(queryParams) {
            queryParams = (typeof queryParams !== 'undefined') ? queryParams : {};
            return api.get('storagerouters', {queryparams: queryParams})
        };

        /**
         * Fetches metadata of a StorageRouter
         * @param storageRouterGuid: Guid of the StorageRouter
         * @return {Deferred}
         */
        self.getMetadata = function(storageRouterGuid) {
            if (storageRouterGuid === undefined) {
                throw new Error('A guid of an existing StorageRouter should be supplied')
            }
            // Task id api, resolve it within the service and return the result
            return api.post('storagerouters/' + storageRouterGuid + '/get_metadata')
                .then(shared.tasks.wait)
        };

    }
    return new StorageRouterService();
});