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
 * Service to help with StorageDriver related tasks
 */
define([ 'knockout'
], function(ko) {

    /**
     * Returns a singleton instance of this service (same instance is served throughout the application)
     */
    function StorageDriverService(){
        var self = this;

        // Constants
        self.minNonDisposableScosFactor = 1.5;
        self.defaultNumberOfScosInTlog = 4;

        // Functions
        /**
         * Calculate the number of scos in tlog and the non disposable scos factor
         * This uses the mapping to have the simple mode available
         * @return {object}
         */
        self.calculateAdvancedFactors = function(sco_size, write_buffer, scos_per_tlog) {
            write_buffer = ko.utils.unwrapObservable(write_buffer);
            scos_per_tlog = ko.utils.unwrapObservable(scos_per_tlog);
            sco_size = ko.utils.unwrapObservable(sco_size);
            var nonDisposableScoFactor = write_buffer / scos_per_tlog / sco_size;
            return nonDisposableScoFactor
        };
        /**
         * Calculate the number of scos in tlog and the non disposable scos factor
         * @return {object}
         */
        self.calculateVolumeWriteBuffer = function(numberOfScosInTlog, nonDisposableScoFactor, scoSize) {
            return nonDisposableScoFactor * (numberOfScosInTlog * scoSize);
        };
    }
    return new StorageDriverService()
});