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
/*global define, window */
define([
    'knockout', 'jquery',
    'ovs/generic'
], function(ko, $, generic) {
    "use strict";
    return function() {
        var self = this;

        // Variables
        self.text          = undefined;
        self.unique        = generic.getTimestamp().toString();

        self.subscriptions = [];

        // Observables
        self.key            = ko.observable();
        self.keyIsFunction  = ko.observable(false);
        self.small          = ko.observable(false);
        self.multi          = ko.observable(false);
        self.free           = ko.observable(false);
        self.items          = ko.observableArray([]);
        self.target         = ko.observableArray([]);
        self._freeValue     = ko.observable();
        self.useFree        = ko.observable(false);
        self.emptyIsLoading = ko.observable(true);
        self.enabled        = ko.observable(true);

        // Computed
        self.selected  = ko.computed(function() {
            var items = [], i;
            for (i = 0; i < self.items().length; i += 1) {
                if (self.contains(self.items()[i])) {
                    items.push(self.items()[i]);
                }
            }
            return items;
        });
        self.freeValue = ko.computed({
            read: function() {
                return self._freeValue();
            },
            write: function(newValue) {
                self.target(newValue);
                self._freeValue(newValue);
            }
        });

        // Functions
        self.extract = generic.extract;
        self.set = function(item) {
            if (self.multi()) {
                if (self.contains(item)) {
                    self.remove(item);
                } else {
                    self.target.push(item);
                }
            } else {
                self.target(item);
                if (self.free() && $.inArray(self.target(), self.items()) === -1 && self.useFree()) {
                    self._freeValue(item);
                }
            }
        };
        self.remove = function(item) {
            if (self.key() === undefined) {
                return self.target.remove(item);
            }
            var itemIndex = -1;
            $.each(self.target(), function(index, targetItem) {
                if (self.keyIsFunction()) {
                    if (item[self.key()]() === targetItem[self.key()]()) {
                        itemIndex = index;
                        return false;
                    }
                } else if (item[self.key()] === targetItem[self.key()]) {
                    itemIndex = index;
                    return false;
                }
                return true;
            });
            self.target.splice(itemIndex, 1);
        };
        self.contains = function(item) {
            if (self.multi()) {
                if (self.key() === undefined) {
                    return self.target().contains(item);
                }
                var result = false, found;
                $.each(self.target(), function (index, targetItem) {
                    if (self.keyIsFunction()) {
                        found = item[self.key()]() === targetItem[self.key()]();
                        result |= found;
                        return !found;
                    } else {
                        found = item[self.key()] === targetItem[self.key()];
                        result |= found;
                        return !found;
                    }
                });
                return result;
            }
            return false;
        };
        self.translate = function() {
            window.setTimeout(function() { $('html').i18n(); }, 250);
        };

        // Durandal
        self.activate = function(settings) {
            if (!settings.hasOwnProperty('items') || !(ko.utils.unwrapObservable(settings.items) instanceof Array)) {
                throw 'Items should be a specified array';
            }
            if (!settings.hasOwnProperty('target')) {
                throw 'Target should be specified';
            }
            // Items can be dynamic if it is an observable array but this is not required
            if (settings.items.isObservableArray) {
                self.items = settings.items;
            } else if (ko.isObservable(settings.items)) {  // Handle the case where it would be an observable / computed so updates can be pushed
                self.items(settings.items());
                self.subscriptions.push(settings.items.subscribe(function(newValue){
                    self.items(newValue)
                }))
            } else {
                self.items(settings.items)  // Can be a normal array / computed / observable
            }
            self.target = settings.target;
            if ('enabled' in settings) {
                self.enabled = settings.enabled
            } else if ('disabled' in settings) {
                self.enabled = ko.pureComputed(function() {
                   return !settings.disabled()
                });
            } else {
                // Default to true
                self.enabled = ko.observable(true)
            }
            self.key(generic.tryGet(settings, 'key', undefined));
            self.small(generic.tryGet(settings, 'small', false));
            self.keyIsFunction(generic.tryGet(settings, 'keyisfunction', false));
            self.free(generic.tryGet(settings, 'free', false));  // Check if user can input items into the dropdown
            self.emptyIsLoading(generic.tryGet(settings, 'emptyisloading', true));
            if (self.free()) {
                if (!settings.hasOwnProperty('defaultfree')) {
                    throw 'If free values are allowed, a default should be provided';
                }
                if (self.target() !== undefined) {
                    self._freeValue(self.target());
                } else {
                    self._freeValue(settings.defaultfree);
                }
            }
            self.text = generic.tryGet(settings, 'text', function(item) { return item; });
            if (self.target.isObservableArray) {
                self.multi(true);
            } else if (self.target() === undefined && self.items().length > 0) {
                var foundDefault = false;
                var hasDefaultRegex = settings.hasOwnProperty('defaultRegex');
                $.each(self.items(), function(index, item) {
                    if (hasDefaultRegex) {
                        if (self.text(item).match(settings.defaultRegex) !== null) {
                            self.target(item);
                            foundDefault = true;
                            return false; // Break
                        }
                        return true;  // Continue
                    }
                    if (item === undefined) {
                        foundDefault = true;
                    }
                });
                if (!foundDefault) {
                    self.target(self.items()[0]);
                }
            }
            if (self.free() && self.multi()) {
                throw 'A dropdown cannot be a multiselect and allow free values at the same time.';
            }
            self.useFree(false);

            if (!ko.isComputed(self.target)) {
                self.target.valueHasMutated();
            }
            // Enforce that the items in the list must be available in options. Otherwise clear the current target
            self.subscriptions.push(self.items.subscribe(function(newValue){
                // Check if current target is still in the list of options. If not, reset
                var extractKey = function(item) {
                    var key = self.key();
                    var ret = item;
                    if (key && item) {
                        var valueOfKey = item[key];
                        if (self.keyIsFunction()) {
                            valueOfKey = valueOfKey() // Unpack
                        }
                        ret = valueOfKey
                    }
                    return ret;
                };
                var convertToKeyList = function(list) {
                    return list.map(function(item) {
                        return extractKey(item)
                    })
                };
                var options = convertToKeyList(newValue);
                var targetValue = self.target();
                if (self.multi()) {
                    self.target(targetValue.filter(function(value) {
                        return options.contains(extractKey(value))
                    }));
                } else {
                    if (!options.contains(extractKey(targetValue))) {
                        if (options.length > 0) {
                            self.target(self.items()[0])
                        } else {
                            self.target(undefined)
                        }
                    }
                }
            }));
        };
        self.deactivate = function() {
            // Dispose of the subscriptions
            $.each(self.subscriptions, function(index, subscription){
                subscription.dispose()
            })
        }
    };
});
