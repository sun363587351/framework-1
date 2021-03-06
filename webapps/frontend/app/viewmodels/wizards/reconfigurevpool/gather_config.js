// Copyright (C) 2016 iNuron NV
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
define([
    'jquery', 'knockout', 'ovs/generic', './data'
], function ($, ko, generic, data) {
    "use strict";
    return function () {
        var self = this;

        // Variables
        self.activated = false;
        self.data = data;

        // Observables
        self.dtlModes = ko.observableArray([]);
        self.dtlTransportModes = ko.observableArray([]);

        // Computed
        self.canContinue = ko.pureComputed(function () {
            var reasons = [], fields = [];
            return {value: reasons.length === 0, reasons: reasons, fields: fields};
        });
        self.dtlMode = ko.computed({
            deferEvaluation: true,  // Wait with computing for an actual subscription
            write: function (mode) {
                if (mode.name === 'no_sync') {
                    self.data.dtlEnabled(false);
                } else {
                    self.data.dtlEnabled(true);
                }
                self.data.dtlMode(mode);
            },
            read: function () {
                if (self.data.configParams.dtlEnabled() === false) {
                    return {name: 'no_sync', disabled: false};
                }
                return self.data.dtlMode();
            }
        });
        self.globalWriteBufferOverAllocation = ko.pureComputed(function() {
           return self.data.globalWriteBufferMax() < self.data.globalWriteBuffer();
        });
        self.canContinue = ko.pureComputed(function () {
            var reasons = [], fields = [];
            if (self.globalWriteBufferOverAllocation() === true) {
                fields.push('writeBufferGlobal');
                reasons.push($.t('ovs:wizards.add_vpool.gather_config.over_allocation'));
            }
            // Verify amount of proxies to deploy is possible
            var total_available = 0 ;
            var largest_ssd = 0 ;
            var largest_sata = 0;
            var amount_of_proxies = self.data.proxyAmount();
            var maximum = amount_of_proxies;
            if (self.data.srPartitions() === undefined || self.data.srPartitions()['WRITE'] === undefined) {
                fields.push('writeBufferGlobal');
                reasons.push($.t('ovs:wizards.add_vpool.gather_config.noMetadata'));
            } else {
                $.each(self.data.srPartitions()['WRITE'], function(index, value) {
                    total_available += value['available'];
                    if (value['ssd'] === true && value['available'] > largest_ssd) {
                        largest_ssd = value['available']
                    } else if (value['ssd'] === false && value['available'] > largest_sata) {
                        largest_sata = value['available']
                    }
                });
                var useLocalFC = self.data.cachingData.fragment_cache.isUsed() && !self.data.cachingData.fragment_cache.is_backend();
                var useLocalBC = self.data.cachingData.block_cache.isUsed() && !self.data.cachingData.block_cache.is_backend();
                if (useLocalFC || useLocalBC) {
                    var proxies = useLocalFC && useLocalBC ? amount_of_proxies * 2 : amount_of_proxies;
                    var proportion = (largest_ssd || largest_sata) * 100.0 / total_available ;
                    var available = proportion * self.data.globalWriteBuffer() / 100 * 0.10;  // Only 10% is used on the largest WRITE partition for fragment caching
                    var fragment_size = available / proxies;
                    if (fragment_size < Math.pow(1024, 3)) {
                        while (maximum > 0) {
                            if (available / maximum > Math.pow(1024, 3)) {
                                break;
                            }
                            maximum -= 1;
                        }
                        var thisMaximum = proxies > amount_of_proxies ? Math.floor(maximum / 2) : maximum;
                        fields.push('writeBufferGlobal');
                        if (thisMaximum === 0) {
                            reasons.push($.t('ovs:wizards.add_vpool.gather_config.cache_no_proxies'));
                        } else {
                            reasons.push($.t('ovs:wizards.add_vpool.gather_config.cache_too_small', {amount: thisMaximum, multiple: thisMaximum === 1 ? 'y' : 'ies'}));
                        }
                    }
                }
            }
            return { value: reasons.length === 0, reasons: reasons, fields: fields };
        });

        // Durandal
        self.activate = function () {
            if (self.activated === true){
                return
            }
            var configParams = self.data.configParams;
            $.each(configParams.dtlModes, function (index, dtlMode) {
                self.dtlModes.push(dtlMode);
            });
            $.each(configParams.dtlTransportModes, function (index, dtlTransportMode) {
                // Check if RDMA option should be disabled
                if (dtlTransportMode === 'rdma' && !self.data.storageRouter().rdmaCapable() === true) {
                    return true // Continue
                }
                self.dtlTransportModes.push(dtlTransportMode);
            });
            self.activated = true;
        };
    };
});
