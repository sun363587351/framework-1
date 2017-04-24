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
    'jquery', 'knockout',
    'ovs/api', 'ovs/generic', 'ovs/shared',
    './data'
], function($, ko, api, generic, shared, data) {
    "use strict";
    return function() {
        var self = this;

        // Variables
        self.data   = data;
        self.shared = shared;

        // Handles
        self.fetchAlbaVPoolHandle = undefined;

        // Observables
        self.albaPresetMap          = ko.observable({});
        self.backendsAA             = ko.observableArray([]);
        self.fragmentCacheSettings  = ko.observableArray(['write', 'read', 'rw', 'none']);
        self.invalidAlbaInfo        = ko.observable(false);
        self.loadingBackends        = ko.observable(false);
        self.localBackendsAvailable = ko.observable(true);
        self.reUsedStorageRouter    = ko.observable();  // Connection info for this storagerouter will be used for accelerated ALBA

        // Computed
        self.isPresetAvailable = ko.computed(function() {
            var presetAvailable = true;
            if (self.data.backendAA() !== undefined && self.data.presetAA() !== undefined && self.data.useAA() === true) {
                var guid = self.data.backendAA().guid,
                    name = self.data.presetAA().name;
                if (self.albaPresetMap().hasOwnProperty(guid) && self.albaPresetMap()[guid].hasOwnProperty(name)) {
                    presetAvailable = self.albaPresetMap()[guid][name];
                }
            }
            return presetAvailable;
        });
        self.reUseableStorageRouters = ko.computed(function() {
            var temp = [];
            if (self.data.vPool() === undefined) {
                return temp;
            }
            $.each(self.data.storageRoutersUsed(), function(index, sr) {
                if (self.data.vPool().metadata().hasOwnProperty('backend_aa_' + sr.guid())) {
                    temp.push(sr);
                }
            });
            temp.unshift(undefined);  // Insert undefined as element 0
            return temp;
        });
        self.canContinue = ko.computed(function() {
            var showErrors = false, reasons = [], fields = [];
            if (self.data.useAA()) {
                if (self.loadingBackends() === true) {
                    reasons.push($.t('ovs:wizards.add_vpool.gather_backend.backends_loading'));
                } else {
                    if (self.data.backendAA() === undefined && self.invalidAlbaInfo() === false) {
                        reasons.push($.t('ovs:wizards.add_vpool.gather_backend.choose_backend'));
                        fields.push('backend');
                    } else if (self.data.presetAA() === undefined && self.invalidAlbaInfo() === false) {
                        reasons.push($.t('ovs:wizards.add_vpool.gather_backend.choose_preset'));
                        fields.push('preset');
                    }
                    if (!self.data.localHostAA()) {
                        if (!self.data.hostAA.valid()) {
                            fields.push('host');
                            reasons.push($.t('ovs:wizards.add_vpool.gather_backend.invalid_host'));
                        }
                        if (self.data.clientIDAA() === '' || self.data.clientSecretAA() === '') {
                            fields.push('clientid');
                            fields.push('clientsecret');
                            reasons.push($.t('ovs:wizards.add_vpool.gather_backend.no_credentials'));
                        }
                        if (self.invalidAlbaInfo()) {
                            reasons.push($.t('ovs:wizards.add_vpool.gather_backend.invalid_alba_info'));
                            fields.push('clientid');
                            fields.push('clientsecret');
                            fields.push('host');
                        }
                    }
                    var quota = self.data.cacheQuota();
                    if (quota !== undefined && quota !== '') {
                        if (isNaN(parseFloat(quota))) {
                            fields.push('quota');
                            reasons.push($.t('ovs:wizards.add_vpool.gather_backend.invalid_quota_nan'));
                        } else if (quota < 0.1 || quota > 1024) {
                            fields.push('quota');
                            reasons.push($.t('ovs:wizards.add_vpool.gather_backend.invalid_quota_boundaries_exceeded'));
                        } else if (self.data.backendAA() !== undefined && quota * Math.pow(1024, 3) * 10 > self.data.backendAA().usages.free) {
                            fields.push('quota');
                            reasons.push($.t('ovs:wizards.add_vpool.gather_backend.invalid_quota_too_much_requested'));
                        }
                    }
                }
            }
            return { value: reasons.length === 0, showErrors: showErrors, reasons: reasons, fields: fields };
        });
        self.fragmentCacheSetting = ko.computed({
            read: function() {
                if (self.data.fragmentCacheOnRead() && self.data.fragmentCacheOnWrite()) {
                    return 'rw';
                }
                if (self.data.fragmentCacheOnRead() || self.data.fragmentCacheOnWrite()) {
                    return self.data.fragmentCacheOnRead() ? 'read' : 'write';
                }
                return 'none';
            },
            write: function(cache) {
                self.data.fragmentCacheOnRead(['rw', 'read'].contains(cache));
                self.data.fragmentCacheOnWrite(['rw', 'write'].contains(cache));
                if (cache === 'none') {
                    self.data.useAA(false);
                }
            }
        });

        // Functions
        self.resetBackendsAA = function() {
            self.backendsAA([]);
            self.data.backendAA(undefined);
            self.data.presetAA(undefined);
        };
        self.shouldSkip = function() {
            return $.Deferred(function(deferred) {
                if (self.data.vPool() !== undefined && !self.data.fragmentCacheOnRead() && !self.data.fragmentCacheOnWrite()) {
                    deferred.resolve(true);
                } else {
                    deferred.resolve(false);
                }
            }).promise();
        };
        self.loadBackends = function() {
            return $.Deferred(function(albaDeferred) {
                generic.xhrAbort(self.fetchAlbaVPoolHandle);
                var relay = '', remoteInfo = {},
                    getData = {
                        contents: 'available'
                    };
                if (!self.data.localHostAA()) {
                    relay = 'relay/';
                    remoteInfo.ip = self.data.hostAA();
                    remoteInfo.port = self.data.portAA();
                    remoteInfo.client_id = self.data.clientIDAA().replace(/\s+/, "");
                    remoteInfo.client_secret = self.data.clientSecretAA().replace(/\s+/, "");
                }
                $.extend(getData, remoteInfo);
                self.loadingBackends(true);
                self.invalidAlbaInfo(false);
                self.fetchAlbaVPoolHandle = api.get(relay + 'alba/backends', { queryparams: getData })
                    .done(function(data) {
                        var available_backends = [], calls = [];
                        $.each(data.data, function (index, item) {
                            if (item.available === true) {
                                getData.contents = 'name,ns_statistics,presets,usages';
                                if (item.scaling === 'LOCAL') {
                                    getData.contents += ',asd_statistics';
                                }
                                calls.push(
                                    api.get(relay + 'alba/backends/' + item.guid + '/', { queryparams: getData })
                                        .then(function(data) {
                                            if (data.guid !== self.data.backend().guid) {
                                                if ((data.asd_statistics !== undefined && Object.keys(data.asd_statistics).length > 0) || data.scaling === 'GLOBAL') {
                                                    available_backends.push(data);
                                                    self.albaPresetMap()[data.guid] = {};
                                                    $.each(data.presets, function (_, preset) {
                                                        self.albaPresetMap()[data.guid][preset.name] = preset.is_available;
                                                    });
                                                }
                                            }
                                        })
                                );
                            }
                        });
                        $.when.apply($, calls)
                            .then(function() {
                                if (available_backends.length > 0) {
                                    available_backends.sort(function(backend1, backend2) {
                                        return backend1.name.toLowerCase() < backend2.name.toLowerCase() ? -1 : 1;
                                    });
                                    self.backendsAA(available_backends);
                                    if (self.data.backendAA() === undefined) {
                                        self.data.backendAA(available_backends[0]);
                                        self.data.presetAA(self.data.enhancedPresetsAA()[0]);
                                    }
                                } else {
                                    self.backendsAA([]);
                                    self.data.backendAA(undefined);
                                    self.data.presetAA(undefined);
                                }
                                self.loadingBackends(false);
                            })
                            .done(albaDeferred.resolve)
                            .fail(function() {
                                self.backendsAA([]);
                                self.data.backendAA(undefined);
                                self.data.presetAA(undefined);
                                self.loadingBackends(false);
                                self.invalidAlbaInfo(true);
                                albaDeferred.reject();
                            });
                    })
                    .fail(function() {
                        self.backendsAA([]);
                        self.data.backendAA(undefined);
                        self.data.presetAA(undefined);
                        self.loadingBackends(false);
                        self.invalidAlbaInfo(true);
                        albaDeferred.reject();
                    });
            }).promise();
        };

        // Durandal
        self.activate = function() {
            // Subscriptions
            self.useAASubscription = self.data.useAA.subscribe(function(accelerated) {
                if (accelerated === true && self.backendsAA().length === 0) {
                    self.loadBackends();
                }
            });
            self.reUsedStorageRouterSubscription = self.reUsedStorageRouter.subscribe(function(sr) {
                if (sr === undefined && !self.data.localHostAA() && self.data.storageRoutersUsed().length > 0) {
                    self.data.hostAA('');
                    self.data.portAA(80);
                    self.data.clientIDAA('');
                    self.data.clientSecretAA('');
                }
                if (sr !== undefined && self.data.vPool() !== undefined && self.data.vPool().metadata().hasOwnProperty('backend_aa_' + sr.guid())) {
                    var md = self.data.vPool().metadata()['backend_aa_' + sr.guid()];
                    if (md.hasOwnProperty('connection_info')) {
                        self.data.hostAA(md.connection_info.host);
                        self.data.portAA(md.connection_info.port);
                        self.data.clientIDAA(md.connection_info.client_id);
                        self.data.clientSecretAA(md.connection_info.client_secret);
                    }
                }
            });
            self.hostAASubscription = self.data.hostAA.subscribe(self.resetBackendsAA);
            self.portAASubscription = self.data.portAA.subscribe(self.resetBackendsAA);
            self.clientIDAASubscription = self.data.clientIDAA.subscribe(self.resetBackendsAA);
            self.clientSecretAASubscription = self.data.clientSecretAA.subscribe(self.resetBackendsAA);
            self.localHostAASubscription = self.data.localHostAA.subscribe(function(local) {
                self.data.hostAA('');
                self.data.portAA(80);
                self.data.clientIDAA('');
                self.data.clientSecretAA('');
                self.reUsedStorageRouter(undefined);
                if (local === true && self.data.useAA() === true && self.backendsAA().length === 0) {
                    self.loadBackends();
                }
            });

            var localBackendsRequiredAmount = self.data.localHost() === true ? 2 : 1;
            if (self.data.backends().length >= localBackendsRequiredAmount) {
                self.data.localHostAA(true);
                self.localBackendsAvailable(true);
            } else {
                self.data.localHostAA(false);
                self.localBackendsAvailable(false);
            }

            if (self.data.backend() !== undefined && self.data.backendAA() !== undefined && self.data.backend().guid === self.data.backendAA().guid) {
                self.backendsAA([]);
                $.each(self.data.backends(), function (_, backend) {
                    if (backend !== self.data.backend() && !self.backendsAA().contains(backend)) {
                        self.backendsAA().push(backend);
                    }
                });
                if (self.backendsAA().length === 0) {
                    self.data.backendAA(undefined);
                    self.data.presetAA(undefined);
                } else {
                    self.data.backendAA(self.backendsAA()[0]);
                    self.data.presetAA(self.data.enhancedPresetsAA()[0]);
                }
            }
            self.loadBackends();
        };
        self.deactivate = function() {
            self.useAASubscription.dispose();
            self.hostAASubscription.dispose();
            self.portAASubscription.dispose();
            self.clientIDAASubscription.dispose();
            self.localHostAASubscription.dispose();
            self.clientSecretAASubscription.dispose();
            self.reUsedStorageRouterSubscription.dispose();
        }
    };
});
