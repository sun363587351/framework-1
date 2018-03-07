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
    'jquery', 'knockout', 'durandal/app', 'ovs/generic',
    'viewmodels/containers/shared/base_container', 'viewmodels/services/storagedriver'
], function($, ko, app, generic, BaseModel, storageDriverService) {
    "use strict";
    // Configuration viewModel which is parsed from JS
    // Return a constructor for a nested viewModel
    var configurationMapping = {
       mds_config: {
            create: function (options) {
                if (options.data !== null) return new MDSConfigModel(options.data);
            }
        },
        include: ["non_disposable_scos_factor"],
        ignore: ["clusterSizes", "dtlModes", "dtlTransportModes", "scoSizes"]
    };
    var ConfigurationViewModel = function(data) {
        var self = this;
        // Inherit
        BaseModel.call(self);

        // Properties
        self.settings  = undefined;  // Used for caching
        // Observables
        self.dtl_config_mode    = ko.observable();
        self.dtl_enabled        = ko.observable();
        self.tlog_multiplier    = ko.observable();  // Number of sco's in tlog - returned by the vpool metadata
        self.write_buffer       = ko.observable()   // Volume Write buffer
            .extend({numeric: {min: 128, allowUndefined: false},
                     rateLimit: { method: "notifyWhenChangesStop", timeout: 400}});
        self.scos_per_tlog = ko.observable()
            .extend({numeric: {min: 4, allowUndefined: false},
                     rateLimit: { method: "notifyWhenChangesStop", timeout: 400}});
        self.non_disposable_scos_factor = ko.observable()
            .extend({numeric: {min: 1.5, allowUndefined: false},
                     rateLimit: { method: "notifyWhenChangesStop", timeout: 400}});

        // Default data
        var vmData = $.extend({
            cluster_size: 4,
            dtl_transport: 'tcp',
            dtl_mode: 'no_sync',
            mds_config: {},
            sco_size: 64,
            write_buffer: 1024,
            scos_per_tlog: 8
        }, data);


        // Constants
        self.clusterSizes       = [4, 8, 16, 32, 64];
        self.dtlModes           = ['no_sync', 'a_sync', 'sync'];
        self.dtlTransportModes  = ['tcp', 'rdma'];
        self.scoSizes           = [4, 8, 16, 32, 64, 128];

        // Computed
        self.non_disposable_scos_factor = ko.computed( {
            deferEvaluation: true,
            read: function() {
                return storageDriverService.calculateNondispSCOFactor(self.sco_size(), self.write_buffer(), self.scos_per_tlog())
            }
        });
        // Bind the data into this
        ko.mapping.fromJS(vmData, configurationMapping, self);
    };
    var MDSConfigModel = function(data) {
        var self = this;
        // Observables (This will ensure that these observables are present even if the data is missing them)
        self.mds_maxload        = ko.observable();
        self.mds_tlogs          = ko.observable();
        self.mds_safety         = ko.observable().extend({ numeric: {min: 1, max: 5}});

        // Default data
        var vmData = $.extend({
            mds_safety: 2
        }, data);

        ko.mapping.fromJS(vmData, {}, self);
    };
    return ConfigurationViewModel;
});
